#include <glic_metal/glic_metal.h>
#include <glic_metal/glitch_presets.h>

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv) {
  const char *preset_directory = argc > 1 ? argv[1] : "presets";
  const char *selected_preset = argc > 3 ? argv[3] : "original__vv01";
  const int width = 320;
  const int height = 180;
  const size_t row_bytes = (size_t)width * 4u;
  const size_t byte_count = row_bytes * (size_t)height;
  uint8_t *input = (uint8_t *)malloc(byte_count);
  uint8_t *output = (uint8_t *)malloc(byte_count);
  if (input == NULL || output == NULL) {
    fprintf(stderr, "frame allocation failed\n");
    free(input);
    free(output);
    return 1;
  }

  for (int y = 0; y < height; ++y) {
    for (int x = 0; x < width; ++x) {
      uint8_t *pixel = input + (size_t)y * row_bytes + (size_t)x * 4u;
      pixel[0] = (uint8_t)((x * 3 + y) & 255);     /* B */
      pixel[1] = (uint8_t)((x + y * 5) & 255);     /* G */
      pixel[2] = (uint8_t)((x * 7 + y * 2) & 255); /* R */
      pixel[3] = 255;                               /* A */
    }
  }

  glic_metal_context *context = NULL;
  glic_metal_status status = glic_metal_context_create(&context);
  if (status != GLIC_METAL_OK) {
    fprintf(stderr, "context creation failed: %s\n",
            glic_metal_status_string(status));
    free(input);
    free(output);
    return 1;
  }

  glic_metal_config config;
  glic_metal_config_init(&config);
  config.width = width;
  config.height = height;
  config.preset_directory = preset_directory;
  if (argc > 2)
    config.metal_library_path = argv[2];

  glic_glitch_preset_status preset_status =
      glic_glitch_preset_apply_metal(selected_preset, &config);
  if (preset_status != GLIC_GLITCH_PRESET_OK) {
    fprintf(stderr, "selected preset failed: %s (%s)\n", selected_preset,
            glic_glitch_preset_status_string(preset_status));
    glic_metal_context_destroy(context);
    free(input);
    free(output);
    return 1;
  }

  status = glic_metal_prepare(context, &config);
  if (status == GLIC_METAL_OK) {
    status = glic_metal_process_frame(
        context, input, row_bytes, output, row_bytes,
        GLIC_METAL_PIXEL_FORMAT_BGRA8, 0);
  }
  if (status != GLIC_METAL_OK) {
    fprintf(stderr, "GLIC Metal failed: %s (%s)\n",
            glic_metal_status_string(status),
            glic_metal_get_last_error(context));
    glic_metal_context_destroy(context);
    free(input);
    free(output);
    return 1;
  }

  glic_metal_frame_stats stats;
  glic_metal_frame_stats_init(&stats);
  (void)glic_metal_get_last_stats(context, &stats);
  printf("preset=%s backend=%s frame_ms=%.3f gpu_ms=%.3f segments=%llu\n",
         selected_preset, glic_metal_get_active_backend(context),
         stats.total_milliseconds, stats.gpu_milliseconds,
         (unsigned long long)stats.total_segments);

  glic_metal_context_destroy(context);
  free(input);
  free(output);
  return 0;
}
