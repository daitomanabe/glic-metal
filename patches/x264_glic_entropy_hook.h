/*
 * GLIC Metal late-entropy glitch hook for the separately built x264 CLI.
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * This file is copied into the pinned x264 source tree by
 * build_x264_glitch_reference.py. It is not linked into libglic_metal.
 */

#ifndef X264_GLIC_ENTROPY_HOOK_H
#define X264_GLIC_ENTROPY_HOOK_H

int x264_glic_mutate_mvd(
    x264_t *h,
    int list,
    int block,
    int *horizontal,
    int *vertical);

dctcoef *x264_glic_mutate_coefficients(
    x264_t *h,
    int category,
    dctcoef *source,
    int count,
    dctcoef scratch[64]);

#endif
