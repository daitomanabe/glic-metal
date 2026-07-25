/*
 * GLIC Metal late-entropy glitch hook for the separately built x264 CLI.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "common/common.h"
#include "x264_glic_entropy_hook.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum glic_hook_effect
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

typedef struct
{
    int initialized;
    enum glic_hook_effect effect;
    const char *effect_name;
    double amount;
    uint64_t seed;
    uint64_t candidates;
    uint64_t selected;
    uint64_t changed;
    dctcoef previous[64];
    int previous_count;
} glic_hook_state;

static glic_hook_state glic_hook;

static uint64_t glic_mix( uint64_t value )
{
    value ^= value >> 30;
    value *= UINT64_C(0xBF58476D1CE4E5B9);
    value ^= value >> 27;
    value *= UINT64_C(0x94D049BB133111EB);
    return value ^ (value >> 31);
}

static int glic_clip( int value, int minimum, int maximum )
{
    return value < minimum ? minimum : value > maximum ? maximum : value;
}

static int glic_round_step( int value, int step )
{
    if( value < 0 )
        return -((-value + step / 2) / step) * step;
    return ((value + step / 2) / step) * step;
}

static void glic_report( void )
{
    if( glic_hook.effect == GLIC_HOOK_NONE )
        return;
    fprintf(
        stderr,
        "[glic-x264-hook] effect=%s candidates=%llu selected=%llu "
        "changed=%llu\n",
        glic_hook.effect_name,
        (unsigned long long)glic_hook.candidates,
        (unsigned long long)glic_hook.selected,
        (unsigned long long)glic_hook.changed );
}

static void glic_initialize( void )
{
    const char *name;
    const char *amount;
    const char *seed;
    if( glic_hook.initialized )
        return;
    glic_hook.initialized = 1;
    glic_hook.effect_name = "";
    glic_hook.amount = 0.0;
    glic_hook.seed = UINT64_C(0x474C4943);
    name = getenv( "GLIC_X264_HOOK_EFFECT" );
    if( !name )
        return;
    glic_hook.effect_name = name;
    if( !strcmp( name, "compressed_motion_vector_vortex" ) )
        glic_hook.effect = GLIC_HOOK_MV_VORTEX;
    else if( !strcmp( name, "compressed_motion_vector_mirror" ) )
        glic_hook.effect = GLIC_HOOK_MV_MIRROR;
    else if( !strcmp( name, "compressed_motion_vector_quantizer" ) )
        glic_hook.effect = GLIC_HOOK_MV_QUANTIZER;
    else if( !strcmp( name, "compressed_motion_vector_freeze" ) )
        glic_hook.effect = GLIC_HOOK_MV_FREEZE;
    else if( !strcmp( name, "compressed_coefficient_sign_flip" ) )
        glic_hook.effect = GLIC_HOOK_COEFF_SIGN_FLIP;
    else if( !strcmp( name, "compressed_coefficient_band_gate" ) )
        glic_hook.effect = GLIC_HOOK_COEFF_BAND_GATE;
    else if( !strcmp( name, "compressed_coefficient_transplant" ) )
        glic_hook.effect = GLIC_HOOK_COEFF_TRANSPLANT;
    else if( !strcmp( name, "compressed_coefficient_scan_fold" ) )
        glic_hook.effect = GLIC_HOOK_COEFF_SCAN_FOLD;
    amount = getenv( "GLIC_X264_HOOK_AMOUNT" );
    if( amount )
        glic_hook.amount = strtod( amount, NULL );
    if( glic_hook.amount < 0.0 )
        glic_hook.amount = 0.0;
    if( glic_hook.amount > 1.0 )
        glic_hook.amount = 1.0;
    seed = getenv( "GLIC_X264_HOOK_SEED" );
    if( seed )
        glic_hook.seed = strtoull( seed, NULL, 0 );
    if( glic_hook.effect != GLIC_HOOK_NONE )
        atexit( glic_report );
}

static int glic_choose( uint64_t key )
{
    uint64_t threshold;
    if( glic_hook.amount <= 0.0 )
        return 0;
    if( glic_hook.amount >= 1.0 )
        return 1;
    threshold = (uint64_t)(
        glic_hook.amount * 18446744073709551615.0 );
    return glic_mix( glic_hook.seed ^ key ) < threshold;
}

static uint64_t glic_key( x264_t *h, int block, int lane )
{
    return ((uint64_t)(uint32_t)h->i_frame << 40)
         ^ ((uint64_t)(uint32_t)h->mb.i_mb_y << 24)
         ^ ((uint64_t)(uint32_t)h->mb.i_mb_x << 12)
         ^ ((uint64_t)(uint32_t)block << 3)
         ^ (uint32_t)lane;
}

int x264_glic_mutate_mvd(
    x264_t *h,
    int list,
    int block,
    int *horizontal,
    int *vertical)
{
    int before_horizontal;
    int before_vertical;
    int differences;
    glic_initialize();
    if( glic_hook.effect < GLIC_HOOK_MV_VORTEX
        || glic_hook.effect > GLIC_HOOK_MV_FREEZE )
        return 0;
    glic_hook.candidates++;
    if( !glic_choose( glic_key( h, block, list ) ) )
        return 0;
    glic_hook.selected++;
    before_horizontal = *horizontal;
    before_vertical = *vertical;
    if( glic_hook.effect == GLIC_HOOK_MV_MIRROR )
    {
        int gain = 1 + (int)(glic_hook.amount * 3.0 + 0.5);
        *horizontal = glic_clip( -*horizontal * gain, -2048, 2048 );
        *vertical = glic_clip( *vertical * gain, -2048, 2048 );
    }
    else if( glic_hook.effect == GLIC_HOOK_MV_QUANTIZER )
    {
        int step = 4 + (int)(glic_hook.amount * 60.0 + 0.5);
        *horizontal = glic_clip(
            glic_round_step( *horizontal, step ), -2048, 2048 );
        *vertical = glic_clip(
            glic_round_step( *vertical, step ), -2048, 2048 );
    }
    else if( glic_hook.effect == GLIC_HOOK_MV_FREEZE )
    {
        *horizontal = 0;
        *vertical = 0;
    }
    else
    {
        int strength = 2 + (int)(glic_hook.amount * 10.0 + 0.5);
        int dx = (h->mb.i_mb_x % 17) - 8;
        int dy = (h->mb.i_mb_y % 17) - 8;
        *horizontal = glic_clip(
            *horizontal - dy * strength, -2048, 2048 );
        *vertical = glic_clip(
            *vertical + dx * strength, -2048, 2048 );
    }
    differences = (*horizontal != before_horizontal)
                + (*vertical != before_vertical);
    glic_hook.changed += differences;
    return differences != 0;
}

dctcoef *x264_glic_mutate_coefficients(
    x264_t *h,
    int category,
    dctcoef *source,
    int count,
    dctcoef scratch[64])
{
    int nonzero_indices[64];
    int nonzero_count = 0;
    int differences = 0;
    int index;
    glic_initialize();
    if( glic_hook.effect < GLIC_HOOK_COEFF_SIGN_FLIP
        || glic_hook.effect > GLIC_HOOK_COEFF_SCAN_FOLD )
        return source;
    glic_hook.candidates += count;
    if( !glic_choose( glic_key( h, category, 4 ) ) )
        return source;
    glic_hook.selected += count;
    memcpy( scratch, source, count * sizeof(*source) );
    for( index = 0; index < count; index++ )
        if( scratch[index] )
            nonzero_indices[nonzero_count++] = index;
    if( !nonzero_count )
        return source;

    if( glic_hook.effect == GLIC_HOOK_COEFF_SIGN_FLIP )
    {
        for( index = 1; index < nonzero_count; index++ )
        {
            int position = nonzero_indices[index];
            scratch[position] = scratch[position] == INT16_MIN
                ? INT16_MAX : -scratch[position];
        }
    }
    else if( glic_hook.effect == GLIC_HOOK_COEFF_BAND_GATE )
    {
        int start = (int)(
            nonzero_count * (1.0 - glic_hook.amount * 0.85) );
        if( start < 1 )
            start = 1;
        for( index = start; index < nonzero_count; index++ )
        {
            int position = nonzero_indices[index];
            scratch[position] = scratch[position] < 0 ? -1 : 1;
        }
    }
    else if( glic_hook.effect == GLIC_HOOK_COEFF_TRANSPLANT )
    {
        if( glic_hook.previous_count )
        {
            for( index = 1; index < nonzero_count; index++ )
            {
                int position = nonzero_indices[index];
                int previous = glic_hook.previous[
                    index % glic_hook.previous_count];
                if( !previous )
                    previous = scratch[position];
                scratch[position] = previous;
            }
        }
        else
        {
            for( index = 1; index < nonzero_count; index++ )
            {
                int source_index = nonzero_indices[
                    nonzero_count - index];
                scratch[nonzero_indices[index]] = source[source_index];
            }
        }
    }
    else
    {
        for( index = 1; index < (nonzero_count + 1) / 2; index++ )
        {
            int left = nonzero_indices[index];
            int right = nonzero_indices[nonzero_count - index];
            dctcoef temporary = scratch[left];
            scratch[left] = scratch[right];
            scratch[right] = temporary;
        }
    }

    glic_hook.previous_count = nonzero_count;
    for( index = 0; index < nonzero_count; index++ )
        glic_hook.previous[index] = source[nonzero_indices[index]];
    for( index = 0; index < count; index++ )
        differences += scratch[index] != source[index];
    glic_hook.changed += differences;
    return differences ? scratch : source;
}
