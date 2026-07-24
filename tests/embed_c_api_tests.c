#include <glic_metal/glic_metal.h>
#include <glic_metal/codec_glitch.h>

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef GLIC_TEST_PRESETS_DIR
#define GLIC_TEST_PRESETS_DIR "presets"
#endif

static int failures = 0;

static void expect(int condition, const char *message) {
  if (!condition) {
    fprintf(stderr, "FAILED: %s\n", message);
    ++failures;
  }
}

static void count_preset(const char *name, void *user_data) {
  size_t *count = (size_t *)user_data;
  expect(name != NULL && name[0] != '\0', "enumerated preset has a name");
  ++*count;
}

static void fill_bgra(uint8_t *pixels, int width, int height,
                      size_t row_bytes) {
  memset(pixels, 0x5a, row_bytes * (size_t)height);
  for (int y = 0; y < height; ++y) {
    for (int x = 0; x < width; ++x) {
      uint8_t *pixel = pixels + (size_t)y * row_bytes + (size_t)x * 4u;
      pixel[0] = (uint8_t)((x * 9 + y * 3) & 255);
      pixel[1] = (uint8_t)((x * 2 + y * 7) & 255);
      pixel[2] = (uint8_t)((x * 5 + y) & 255);
      pixel[3] = (uint8_t)(128 + ((x + y) & 127));
    }
  }
}

int main(void) {
  expect(glic_metal_get_abi_version() == GLIC_METAL_ABI_VERSION,
         "ABI version");
  expect(glic_metal_get_version_string()[0] != '\0', "version string");
  expect(strcmp(glic_codec_glitch_effect_implementation_level(
                    GLIC_CODEC_GLITCH_PLANE_TIME_SPLIT),
                "videotoolbox_decoded_history_plus_coreimage_metal_reconstruction") ==
             0,
         "codec glitch implementation level metadata");

  size_t preset_count = 0;
  expect(glic_metal_enumerate_presets(GLIC_TEST_PRESETS_DIR, count_preset,
                                      &preset_count) == GLIC_METAL_OK,
         "preset enumeration succeeds");
  expect(preset_count == 144, "all 144 presets are enumerated");

  glic_metal_context *context = NULL;
  expect(glic_metal_context_create(&context) == GLIC_METAL_OK &&
             context != NULL,
         "context creation");

  enum { width = 64, height = 48 };
  const size_t row_bytes = (size_t)width * 4u + 16u;
  const size_t byte_count = row_bytes * (size_t)height;
  uint8_t *input = (uint8_t *)malloc(byte_count);
  uint8_t *output = (uint8_t *)malloc(byte_count);
  expect(input != NULL && output != NULL, "test frame allocation");
  if (input == NULL || output == NULL)
    return 1;
  fill_bgra(input, width, height, row_bytes);
  memset(output, 0xa5, byte_count);

  glic_metal_config config;
  glic_metal_config_init(&config);
  config.width = width;
  config.height = height;
  config.preset_directory = GLIC_TEST_PRESETS_DIR;
  config.preset_name = "colour_glow";
  config.backend = GLIC_METAL_BACKEND_CPU;
  config.mode = GLIC_METAL_MODE_COMPAT_REALTIME;
  config.effect_family = GLIC_METAL_EFFECT_POSTER_SOLAR;
  expect(glic_metal_prepare(context, &config) == GLIC_METAL_OK,
         "compat CPU prepare");
  expect(strcmp(glic_metal_get_active_backend(context), "cpu-parallel") == 0,
         "compat CPU backend name");
  expect(glic_metal_process_frame(
             context, input, row_bytes, output, row_bytes,
             GLIC_METAL_PIXEL_FORMAT_BGRA8, 42) == GLIC_METAL_OK,
         "padded BGRA frame processing");
  expect(memcmp(input, output, byte_count) != 0,
         "compat effect changes the frame");
  expect(output[(size_t)width * 4u] == 0xa5,
         "output row padding is untouched");

  glic_metal_frame_stats stats;
  glic_metal_frame_stats_init(&stats);
  expect(glic_metal_get_last_stats(context, &stats) == GLIC_METAL_OK,
         "stats retrieval");
  expect(stats.frame_index == 42 && stats.total_milliseconds > 0.0,
         "stats identify processed frame");
  expect(stats.hardware_accelerated == 0,
         "CPU stats are not hardware accelerated");

  config.preset_name = "default";
  config.mode = GLIC_METAL_MODE_ORIGINAL;
  config.fidelity = GLIC_METAL_FIDELITY_STRICT;
  expect(glic_metal_prepare(context, &config) == GLIC_METAL_OK,
         "original CPU prepare");
  expect(glic_metal_process_frame(
             context, input, row_bytes, input, row_bytes,
             GLIC_METAL_PIXEL_FORMAT_BGRA8, 43) == GLIC_METAL_OK,
         "in-place original CPU processing");

  config.fidelity = GLIC_METAL_FIDELITY_FAST_MATCH;
  expect(glic_metal_prepare(context, &config) == GLIC_METAL_UNSUPPORTED,
         "Fast Match rejects CPU backend");
  expect(strcmp(glic_metal_get_active_backend(context), "cpu-original") == 0,
         "failed prepare preserves active engine");

  config.preset_name = "../default";
  expect(glic_metal_prepare(context, &config) == GLIC_METAL_INVALID_ARGUMENT,
         "preset path traversal is rejected");
  expect(glic_metal_get_last_error(context)[0] != '\0',
         "failed prepare has a diagnostic");

  glic_metal_context_destroy(context);
  free(input);
  free(output);

  if (failures != 0)
    return 1;
  printf("PASS embedded C API\n");
  return 0;
}
