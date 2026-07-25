/*
 * GLIC Metal late-entropy glitch hook for the separately built x265 CLI.
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * This file is copied into the pinned x265 4.2 source tree by
 * build_x265_glitch_reference.py. It is not linked into libglic_metal.
 */

#ifndef X265_GLIC_ENTROPY_HOOK_H
#define X265_GLIC_ENTROPY_HOOK_H

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

namespace X265_NS {
namespace {

enum GlicHookEffect
{
    GLIC_HOOK_NONE = 0,
    GLIC_HOOK_MV_VORTEX,
    GLIC_HOOK_MV_MIRROR,
    GLIC_HOOK_MV_QUANTIZER,
    GLIC_HOOK_MV_FREEZE,
    GLIC_HOOK_COEFF_SIGN_FLIP,
    GLIC_HOOK_COEFF_BAND_GATE,
    GLIC_HOOK_COEFF_TRANSPLANT,
    GLIC_HOOK_COEFF_SCAN_FOLD,
};

struct GlicEntropyHook
{
    GlicHookEffect effect;
    const char* effectName;
    double amount;
    uint64_t seed;
    std::atomic<uint64_t> candidates;
    std::atomic<uint64_t> selected;
    std::atomic<uint64_t> changed;

    GlicEntropyHook()
        : effect(GLIC_HOOK_NONE)
        , effectName("")
        , amount(0.0)
        , seed(0x474C4943)
        , candidates(0)
        , selected(0)
        , changed(0)
    {
        const char* name = std::getenv("GLIC_X265_HOOK_EFFECT");
        if (!name)
            return;
        effectName = name;
        if (!std::strcmp(name, "compressed_motion_vector_vortex"))
            effect = GLIC_HOOK_MV_VORTEX;
        else if (!std::strcmp(name, "compressed_motion_vector_mirror"))
            effect = GLIC_HOOK_MV_MIRROR;
        else if (!std::strcmp(name, "compressed_motion_vector_quantizer"))
            effect = GLIC_HOOK_MV_QUANTIZER;
        else if (!std::strcmp(name, "compressed_motion_vector_freeze"))
            effect = GLIC_HOOK_MV_FREEZE;
        else if (!std::strcmp(name, "compressed_coefficient_sign_flip"))
            effect = GLIC_HOOK_COEFF_SIGN_FLIP;
        else if (!std::strcmp(name, "compressed_coefficient_band_gate"))
            effect = GLIC_HOOK_COEFF_BAND_GATE;
        else if (!std::strcmp(name, "compressed_coefficient_transplant"))
            effect = GLIC_HOOK_COEFF_TRANSPLANT;
        else if (!std::strcmp(name, "compressed_coefficient_scan_fold"))
            effect = GLIC_HOOK_COEFF_SCAN_FOLD;
        const char* amountText = std::getenv("GLIC_X265_HOOK_AMOUNT");
        if (amountText)
            amount = x265_clip3(0.0, 1.0, std::strtod(amountText, NULL));
        const char* seedText = std::getenv("GLIC_X265_HOOK_SEED");
        if (seedText)
            seed = std::strtoull(seedText, NULL, 0);
    }

    ~GlicEntropyHook()
    {
        if (effect != GLIC_HOOK_NONE)
            std::fprintf(
                stderr,
                "[glic-x265-hook] effect=%s candidates=%llu "
                "selected=%llu changed=%llu\n",
                effectName,
                (unsigned long long)candidates.load(),
                (unsigned long long)selected.load(),
                (unsigned long long)changed.load());
    }

    bool isMotion() const
    {
        return effect >= GLIC_HOOK_MV_VORTEX
            && effect <= GLIC_HOOK_MV_FREEZE;
    }

    bool isCoefficient() const
    {
        return effect >= GLIC_HOOK_COEFF_SIGN_FLIP
            && effect <= GLIC_HOOK_COEFF_SCAN_FOLD;
    }

    static uint64_t mix(uint64_t value)
    {
        value ^= value >> 30;
        value *= 0xBF58476D1CE4E5B9ULL;
        value ^= value >> 27;
        value *= 0x94D049BB133111EBULL;
        return value ^ (value >> 31);
    }

    bool choose(uint64_t key) const
    {
        if (amount <= 0.0)
            return false;
        if (amount >= 1.0)
            return true;
        return mix(seed ^ key)
            < (uint64_t)(amount * 18446744073709551615.0);
    }
};

GlicEntropyHook& glicEntropyHook()
{
    static GlicEntropyHook hook;
    return hook;
}

uint64_t glicHookKey(
    const CUData& cu, uint32_t absPartIdx, uint32_t lane)
{
    return ((uint64_t)(uint32_t)cu.m_slice->m_poc << 40)
        ^ ((uint64_t)cu.m_cuAddr << 16)
        ^ ((uint64_t)absPartIdx << 3)
        ^ lane;
}

bool glicMutateMvd(
    const CUData& cu,
    uint32_t absPartIdx,
    int list,
    int& horizontal,
    int& vertical)
{
    GlicEntropyHook& hook = glicEntropyHook();
    if (!hook.isMotion())
        return false;
    hook.candidates.fetch_add(1, std::memory_order_relaxed);
    if (!hook.choose(glicHookKey(cu, absPartIdx, (uint32_t)list)))
        return false;
    hook.selected.fetch_add(1, std::memory_order_relaxed);
    const int beforeHorizontal = horizontal;
    const int beforeVertical = vertical;
    if (hook.effect == GLIC_HOOK_MV_MIRROR)
    {
        const int gain = 1 + (int)std::round(hook.amount * 3.0);
        horizontal = x265_clip3(-2048, 2048, -horizontal * gain);
        vertical = x265_clip3(-2048, 2048, vertical * gain);
    }
    else if (hook.effect == GLIC_HOOK_MV_QUANTIZER)
    {
        const int step = 4 + (int)std::round(hook.amount * 60.0);
        horizontal = x265_clip3(
            -2048,
            2048,
            (int)std::round((double)horizontal / step) * step);
        vertical = x265_clip3(
            -2048,
            2048,
            (int)std::round((double)vertical / step) * step);
    }
    else if (hook.effect == GLIC_HOOK_MV_FREEZE)
    {
        horizontal = 0;
        vertical = 0;
    }
    else if (hook.effect == GLIC_HOOK_MV_VORTEX)
    {
        const int strength = 2 + (int)std::round(hook.amount * 10.0);
        const int dx = ((int)(cu.m_cuPelX >> 4) % 17) - 8;
        const int dy = ((int)(cu.m_cuPelY >> 4) % 17) - 8;
        horizontal = x265_clip3(
            -2048, 2048, horizontal - dy * strength);
        vertical = x265_clip3(
            -2048, 2048, vertical + dx * strength);
    }
    const uint64_t differences =
        (horizontal != beforeHorizontal) + (vertical != beforeVertical);
    hook.changed.fetch_add(differences, std::memory_order_relaxed);
    return differences != 0;
}

bool glicMutateCoefficients(
    const CUData& cu,
    const coeff_t* source,
    uint32_t absPartIdx,
    uint32_t transformSize,
    TextType textType,
    std::vector<coeff_t>& destination)
{
    GlicEntropyHook& hook = glicEntropyHook();
    if (!hook.isCoefficient())
        return false;
    const uint32_t count = transformSize * transformSize;
    hook.candidates.fetch_add(count, std::memory_order_relaxed);
    if (!hook.choose(
            glicHookKey(cu, absPartIdx, 4 + (uint32_t)textType)))
        return false;
    hook.selected.fetch_add(count, std::memory_order_relaxed);
    destination.assign(source, source + count);
    if (hook.effect == GLIC_HOOK_COEFF_SIGN_FLIP)
    {
        for (uint32_t index = 1; index < count; index++)
        {
            if (destination[index])
            {
                destination[index] = destination[index] == -32768
                    ? 32767
                    : (coeff_t)-destination[index];
            }
        }
    }
    else if (hook.effect == GLIC_HOOK_COEFF_BAND_GATE)
    {
        const uint32_t cutoff = x265_max(
            2U,
            (uint32_t)std::round(
                count * (1.0 - hook.amount * 0.85)));
        for (uint32_t index = cutoff; index < count; index++)
            destination[index] = 0;
    }
    else if (hook.effect == GLIC_HOOK_COEFF_SCAN_FOLD)
    {
        std::reverse(destination.begin() + 1, destination.end());
    }
    else if (hook.effect == GLIC_HOOK_COEFF_TRANSPLANT)
    {
        // The entropy runner forces one frame thread and disables WPP, making
        // this previous-block store deterministic and race-free.
        static std::vector<coeff_t> previous;
        if (previous.size() == count)
        {
            std::copy(
                previous.begin() + 1,
                previous.end(),
                destination.begin() + 1);
        }
        else if (count > 3)
        {
            std::rotate(
                destination.begin() + 1,
                destination.begin() + 1 + (count - 1) / 2,
                destination.end());
        }
        previous.assign(source, source + count);
    }

    uint32_t nonzero = 0;
    for (uint32_t index = 0; index < count; index++)
        nonzero += destination[index] != 0;
    if (!nonzero)
    {
        // The original coded-block flag requires at least one non-zero value.
        for (uint32_t index = 0; index < count; index++)
        {
            if (source[index])
            {
                destination[index] = source[index];
                break;
            }
        }
    }
    uint64_t differences = 0;
    for (uint32_t index = 0; index < count; index++)
        differences += destination[index] != source[index];
    hook.changed.fetch_add(differences, std::memory_order_relaxed);
    return differences != 0;
}

} // namespace
} // namespace X265_NS

#endif
