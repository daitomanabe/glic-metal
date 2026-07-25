/*
 * GLIC HEVC decoder-side syntax hook for the separately built FFmpeg CLI.
 *
 * This header is compiled only inside the pinned external FFmpeg checkout by
 * build_ffmpeg_hevc_glitch_reference.py. It is not linked into libglic_metal.
 */
#ifndef AVCODEC_HEVC_GLIC_DECODER_HOOK_H
#define AVCODEC_HEVC_GLIC_DECODER_HOOK_H

#include <stdint.h>

typedef struct HEVCLocalContext HEVCLocalContext;

void ff_glic_hevc_mutate_mvd(HEVCLocalContext *lc, int x0, int y0,
                             int list, int16_t *horizontal,
                             int16_t *vertical);
int64_t ff_glic_hevc_mutate_quantized_coefficient(
    HEVCLocalContext *lc, int x0, int y0, int component,
    int coefficient_index, int64_t value);

#endif
