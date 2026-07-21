#ifndef GLIC_METAL_GLIC_METAL_METAL_H
#define GLIC_METAL_GLIC_METAL_METAL_H

#include <glic_metal/glic_metal.h>

#if defined(__OBJC__) && defined(__APPLE__)
#import <Metal/Metal.h>

static inline glic_metal_status glic_metal_process_texture_objects(
    glic_metal_context *context, id<MTLTexture> input,
    id<MTLTexture> output, uint64_t frame_index) {
  return glic_metal_process_metal_textures(
      context, (__bridge void *)input, (__bridge void *)output, frame_index);
}

static inline glic_metal_status glic_metal_encode_texture_objects(
    glic_metal_context *context, id<MTLCommandBuffer> command_buffer,
    id<MTLTexture> input, id<MTLTexture> output, uint64_t frame_index) {
  return glic_metal_encode_metal_textures(
      context, (__bridge void *)command_buffer, (__bridge void *)input,
      (__bridge void *)output, frame_index);
}
#endif

#endif /* GLIC_METAL_GLIC_METAL_METAL_H */
