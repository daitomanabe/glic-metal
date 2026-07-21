#include <glic_metal/glic_metal.h>

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#ifndef CONSUMER_PRESETS_DIR
#error CONSUMER_PRESETS_DIR must be provided by the package test
#endif

int main(void) {
  enum { width = 32, height = 24 };
  uint8_t input[width * height * 4];
  uint8_t output[width * height * 4];
  for (size_t index = 0; index < sizeof(input); index += 4) {
    input[index + 0] = (uint8_t)(index & 255);
    input[index + 1] = (uint8_t)((index * 3) & 255);
    input[index + 2] = (uint8_t)((index * 7) & 255);
    input[index + 3] = 255;
  }

  glic_metal_context *context = NULL;
  if (glic_metal_context_create(&context) != GLIC_METAL_OK)
    return 1;
  glic_metal_config config;
  glic_metal_config_init(&config);
  config.width = width;
  config.height = height;
  config.preset_directory = CONSUMER_PRESETS_DIR;
  config.preset_name = "default";
  config.backend = GLIC_METAL_BACKEND_CPU;
  config.mode = GLIC_METAL_MODE_COMPAT_REALTIME;
  config.effect_family = GLIC_METAL_EFFECT_POSTER_SOLAR;
  if (glic_metal_prepare(context, &config) != GLIC_METAL_OK) {
    fprintf(stderr, "%s\n", glic_metal_get_last_error(context));
    return 1;
  }
  if (glic_metal_process_frame(
          context, input, width * 4u, output, width * 4u,
          GLIC_METAL_PIXEL_FORMAT_BGRA8, 1) != GLIC_METAL_OK)
    return 1;
  if (memcmp(input, output, sizeof(input)) == 0)
    return 1;
  glic_metal_context_destroy(context);
  puts("PASS installed GlicMetal consumer");
  return 0;
}
