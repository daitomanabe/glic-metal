#include <glic_metal/codec_glitch.h>
#include <glic_metal/glic_metal.h>
#include <glic_metal/glitch_presets.h>

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#ifndef CONSUMER_PRESETS_DIR
#error CONSUMER_PRESETS_DIR must be provided by the package test
#endif

int main(void) {
  glic_glitch_preset_descriptor selected;
  glic_glitch_preset_descriptor_init(&selected);
  if (glic_glitch_preset_count() != 19 ||
      glic_glitch_preset_find("spatial__poster_solar", &selected) !=
          GLIC_GLITCH_PRESET_OK ||
      selected.category != GLIC_GLITCH_PRESET_SPATIAL)
    return 1;

  glic_codec_glitch_config codec_config;
  glic_codec_glitch_config_init(&codec_config);
  glic_codec_glitch_controls codec_controls;
  glic_codec_glitch_controls_init(&codec_controls);
  glic_codec_glitch_context *codec_context = NULL;
  if (glic_codec_glitch_get_abi_version() != GLIC_CODEC_GLITCH_ABI_VERSION ||
      codec_config.decoded_history_frames != 12 ||
      codec_controls.effect != GLIC_CODEC_GLITCH_BITRATE_CRUSH ||
      strcmp(glic_codec_glitch_effect_name(GLIC_CODEC_GLITCH_PAYLOAD_XOR),
             "payload_xor") != 0 ||
      glic_codec_glitch_context_create(&codec_context) !=
          GLIC_CODEC_GLITCH_OK ||
      codec_context == NULL)
    return 1;
  glic_codec_glitch_stats codec_stats;
  glic_codec_glitch_stats_init(&codec_stats);
  if (glic_codec_glitch_get_stats(codec_context, &codec_stats) !=
      GLIC_CODEC_GLITCH_NOT_PREPARED)
    return 1;
  glic_codec_glitch_context_destroy(codec_context);

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
  if (glic_metal_process_frame(context, input, width * 4u, output, width * 4u,
                               GLIC_METAL_PIXEL_FORMAT_BGRA8,
                               1) != GLIC_METAL_OK)
    return 1;
  if (memcmp(input, output, sizeof(input)) == 0)
    return 1;
  glic_metal_context_destroy(context);
  puts("PASS installed GlicMetal consumer");
  return 0;
}
