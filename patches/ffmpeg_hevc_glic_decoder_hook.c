/*
 * Decoder-side HEVC MVD and quantized-coefficient mutation for pinned FFmpeg.
 *
 * The host process must use one decoder thread. The hook changes syntax
 * values after CABAC parsing and before motion compensation or inverse
 * quantization. It never claims to emit a mutated HEVC bitstream.
 */
#include "ffmpeg_hevc_glic_decoder_hook.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum glic_hevc_effect {
    GLIC_HEVC_NONE = 0,
    GLIC_HEVC_MV_VORTEX,
    GLIC_HEVC_MV_MIRROR,
    GLIC_HEVC_MV_QUANTIZER,
    GLIC_HEVC_MV_FREEZE,
    GLIC_HEVC_COEFF_SIGN_FLIP,
    GLIC_HEVC_COEFF_BAND_GATE,
    GLIC_HEVC_COEFF_TRANSPLANT,
    GLIC_HEVC_COEFF_SCAN_FOLD,
};

typedef struct GlicHEVCHookState {
    int initialized;
    enum glic_hevc_effect effect;
    const char *effect_name;
    double amount;
    uint64_t seed;
    uint64_t candidates;
    uint64_t selected;
    uint64_t changed;
    int16_t previous_mvd[2];
    int64_t coefficient_history[64];
    unsigned coefficient_history_count;
    unsigned coefficient_history_cursor;
} GlicHEVCHookState;

static GlicHEVCHookState glic_hook;

static uint64_t glic_mix(uint64_t value)
{
    value ^= value >> 30;
    value *= UINT64_C(0xbf58476d1ce4e5b9);
    value ^= value >> 27;
    value *= UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}

static int glic_clip(int value, int minimum, int maximum)
{
    return value < minimum ? minimum : value > maximum ? maximum : value;
}

static int glic_round_step(int value, int step)
{
    if (!value)
        return 0;
    return value < 0 ? -((-value + step / 2) / step) * step
                     : ((value + step / 2) / step) * step;
}

static void glic_report(void)
{
    if (glic_hook.effect == GLIC_HEVC_NONE)
        return;
    fprintf(stderr,
            "[glic-ffmpeg-hevc-hook] effect=%s candidates=%" PRIu64
            " selected=%" PRIu64 " changed=%" PRIu64 "\n",
            glic_hook.effect_name, glic_hook.candidates,
            glic_hook.selected, glic_hook.changed);
}

static void glic_initialize(void)
{
    const char *name;
    const char *amount;
    const char *seed;
    if (glic_hook.initialized)
        return;
    glic_hook.initialized = 1;
    glic_hook.effect_name = "";
    glic_hook.seed = UINT64_C(0x474c4943);
    name = getenv("GLIC_FFMPEG_HEVC_HOOK_EFFECT");
    if (!name)
        return;
    glic_hook.effect_name = name;
    if (!strcmp(name, "compressed_motion_vector_vortex"))
        glic_hook.effect = GLIC_HEVC_MV_VORTEX;
    else if (!strcmp(name, "compressed_motion_vector_mirror"))
        glic_hook.effect = GLIC_HEVC_MV_MIRROR;
    else if (!strcmp(name, "compressed_motion_vector_quantizer"))
        glic_hook.effect = GLIC_HEVC_MV_QUANTIZER;
    else if (!strcmp(name, "compressed_motion_vector_freeze"))
        glic_hook.effect = GLIC_HEVC_MV_FREEZE;
    else if (!strcmp(name, "compressed_coefficient_sign_flip"))
        glic_hook.effect = GLIC_HEVC_COEFF_SIGN_FLIP;
    else if (!strcmp(name, "compressed_coefficient_band_gate"))
        glic_hook.effect = GLIC_HEVC_COEFF_BAND_GATE;
    else if (!strcmp(name, "compressed_coefficient_transplant"))
        glic_hook.effect = GLIC_HEVC_COEFF_TRANSPLANT;
    else if (!strcmp(name, "compressed_coefficient_scan_fold"))
        glic_hook.effect = GLIC_HEVC_COEFF_SCAN_FOLD;
    amount = getenv("GLIC_FFMPEG_HEVC_HOOK_AMOUNT");
    if (amount)
        glic_hook.amount = strtod(amount, NULL);
    if (glic_hook.amount < 0.0)
        glic_hook.amount = 0.0;
    if (glic_hook.amount > 1.0)
        glic_hook.amount = 1.0;
    seed = getenv("GLIC_FFMPEG_HEVC_HOOK_SEED");
    if (seed)
        glic_hook.seed = strtoull(seed, NULL, 0);
    if (glic_hook.effect != GLIC_HEVC_NONE)
        atexit(glic_report);
}

static int glic_choose(uint64_t key)
{
    uint64_t threshold;
    if (glic_hook.amount <= 0.0)
        return 0;
    if (glic_hook.amount >= 1.0)
        return 1;
    threshold = (uint64_t)(
        glic_hook.amount * 18446744073709551615.0);
    return glic_mix(glic_hook.seed ^ key) < threshold;
}

static uint64_t glic_key(int x0, int y0, int lane, uint64_t ordinal)
{
    return glic_mix(((uint64_t)(uint32_t)x0 << 32) ^
                    ((uint64_t)(uint32_t)y0 << 8) ^
                    ((uint64_t)(unsigned)lane << 56) ^ ordinal);
}

void ff_glic_hevc_mutate_mvd(HEVCLocalContext *lc, int x0, int y0,
                             int list, int16_t *horizontal,
                             int16_t *vertical)
{
    int before_x;
    int before_y;
    uint64_t ordinal;
    (void)lc;
    glic_initialize();
    if (glic_hook.effect < GLIC_HEVC_MV_VORTEX ||
        glic_hook.effect > GLIC_HEVC_MV_FREEZE)
        return;
    ordinal = glic_hook.candidates++;
    if (!glic_choose(glic_key(x0, y0, list, ordinal)))
        return;
    glic_hook.selected++;
    before_x = *horizontal;
    before_y = *vertical;
    if (glic_hook.effect == GLIC_HEVC_MV_MIRROR) {
        int gain = 1 + (int)(glic_hook.amount * 3.0 + 0.5);
        *horizontal = glic_clip(-*horizontal * gain, -8192, 8191);
        *vertical = glic_clip(*vertical * gain, -8192, 8191);
    } else if (glic_hook.effect == GLIC_HEVC_MV_QUANTIZER) {
        int step = 4 + (int)(glic_hook.amount * 60.0 + 0.5);
        *horizontal = glic_clip(
            glic_round_step(*horizontal, step), -8192, 8191);
        *vertical = glic_clip(
            glic_round_step(*vertical, step), -8192, 8191);
    } else if (glic_hook.effect == GLIC_HEVC_MV_FREEZE) {
        *horizontal = glic_hook.previous_mvd[0];
        *vertical = glic_hook.previous_mvd[1];
    } else {
        int strength = 2 + (int)(glic_hook.amount * 10.0 + 0.5);
        int rotated_x = -*vertical * strength;
        int rotated_y = *horizontal * strength;
        *horizontal = glic_clip(rotated_x, -8192, 8191);
        *vertical = glic_clip(rotated_y, -8192, 8191);
    }
    glic_hook.previous_mvd[0] = before_x;
    glic_hook.previous_mvd[1] = before_y;
    glic_hook.changed += (*horizontal != before_x) + (*vertical != before_y);
}

int64_t ff_glic_hevc_mutate_quantized_coefficient(
    HEVCLocalContext *lc, int x0, int y0, int component,
    int coefficient_index, int64_t value)
{
    int64_t changed = value;
    int64_t magnitude;
    uint64_t ordinal;
    unsigned history_index;
    (void)lc;
    glic_initialize();
    if (glic_hook.effect < GLIC_HEVC_COEFF_SIGN_FLIP ||
        glic_hook.effect > GLIC_HEVC_COEFF_SCAN_FOLD || !value)
        return value;
    ordinal = glic_hook.candidates++;
    if (!glic_choose(glic_key(
            x0 + coefficient_index, y0, component + 4, ordinal)))
        return value;
    glic_hook.selected++;
    magnitude = value < 0 ? -value : value;
    if (glic_hook.effect == GLIC_HEVC_COEFF_SIGN_FLIP) {
        changed = -value;
    } else if (glic_hook.effect == GLIC_HEVC_COEFF_BAND_GATE) {
        int64_t cap = 1 + (int64_t)((1.0 - glic_hook.amount) * 7.0);
        if (magnitude > cap)
            magnitude = cap;
        changed = value < 0 ? -magnitude : magnitude;
    } else if (glic_hook.coefficient_history_count) {
        if (glic_hook.effect == GLIC_HEVC_COEFF_TRANSPLANT) {
            history_index = (unsigned)(
                glic_mix(ordinal ^ glic_hook.seed) %
                glic_hook.coefficient_history_count);
        } else {
            history_index = (
                glic_hook.coefficient_history_cursor +
                glic_hook.coefficient_history_count - 1 -
                (unsigned)(coefficient_index & 7)) %
                glic_hook.coefficient_history_count;
        }
        magnitude = glic_hook.coefficient_history[history_index];
        if (magnitude < 0)
            magnitude = -magnitude;
        if (!magnitude)
            magnitude = 1;
        changed = value < 0 ? -magnitude : magnitude;
    }
    glic_hook.coefficient_history[
        glic_hook.coefficient_history_cursor] = value;
    glic_hook.coefficient_history_cursor =
        (glic_hook.coefficient_history_cursor + 1) % 64;
    if (glic_hook.coefficient_history_count < 64)
        glic_hook.coefficient_history_count++;
    glic_hook.changed += changed != value;
    return changed;
}
