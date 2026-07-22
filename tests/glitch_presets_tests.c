#include <glic_metal/glitch_presets.h>

#include <math.h>
#include <stdio.h>
#include <string.h>

static int failures = 0;

static void expect(int condition, const char *message) {
  if (!condition) {
    fprintf(stderr, "FAILED: %s\n", message);
    ++failures;
  }
}

static int nearly_equal(float left, float right) {
  return fabsf(left - right) < 0.00001f;
}

int main(void) {
  static const char *expected_names[] = {
      "original__vv01",
      "original__bl33dyl1n3z",
      "original__burn",
      "original__colour_waves_sharp",
      "original__bl33dyl1n3z-2",
      "original__wtf",
      "original__lightblur",
      "original__constrctivist_minimal",
      "original__web_p_like",
      "original__webp",
      "original__abstract_expressionism",
      "original__colour_glow",
      "spatial__poster_solar",
      "spatial__bitplane_dither",
      "spatial__scanline_weave",
      "codec__bitrate_meltdown",
      "original__beautifulwave",
      "spatial__diagonal_slip",
      "original__bi0g4n1c",
  };
  expect(glic_glitch_preset_get_abi_version() ==
             GLIC_GLITCH_PRESET_ABI_VERSION,
         "preset ABI version");
  expect(glic_glitch_preset_count() == 19, "selected preset count");

  unsigned original_count = 0;
  unsigned spatial_count = 0;
  unsigned codec_count = 0;
  for (uint32_t index = 0; index < glic_glitch_preset_count(); ++index) {
    glic_glitch_preset_descriptor descriptor;
    glic_glitch_preset_descriptor_init(&descriptor);
    expect(glic_glitch_preset_get(index, &descriptor) ==
               GLIC_GLITCH_PRESET_OK,
           "selected preset enumeration");
    expect(strcmp(descriptor.name, expected_names[index]) == 0,
           "selected preset order and name");
    original_count += descriptor.category == GLIC_GLITCH_PRESET_ORIGINAL;
    spatial_count += descriptor.category == GLIC_GLITCH_PRESET_SPATIAL;
    codec_count += descriptor.category == GLIC_GLITCH_PRESET_CODEC;
  }
  expect(original_count == 14, "14 original presets");
  expect(spatial_count == 4, "4 spatial presets");
  expect(codec_count == 1, "1 codec preset");

  glic_metal_config metal;
  glic_metal_config_init(&metal);
  metal.width = 960;
  metal.height = 540;
  metal.preset_directory = "host-owned";
  expect(glic_glitch_preset_apply_metal("original__vv01", &metal) ==
             GLIC_GLITCH_PRESET_OK,
         "apply original preset");
  expect(metal.mode == GLIC_METAL_MODE_ORIGINAL &&
             strcmp(metal.preset_name, "vv01") == 0 &&
             strcmp(metal.preset_directory, "host-owned") == 0,
         "original preset config and host resource path");

  expect(glic_glitch_preset_apply_metal("spatial__poster_solar", &metal) ==
             GLIC_GLITCH_PRESET_OK,
         "apply spatial preset");
  expect(metal.mode == GLIC_METAL_MODE_COMPAT_REALTIME &&
             metal.backend == GLIC_METAL_BACKEND_METAL &&
             metal.effect_family == GLIC_METAL_EFFECT_POSTER_SOLAR &&
             nearly_equal(metal.effect_amount, 0.49f) &&
             nearly_equal(metal.effect_scale, 0.36f) &&
             nearly_equal(metal.effect_rate, 0.15f) &&
             metal.seed == 1296652297u,
         "spatial preset exact controls");

  glic_codec_glitch_controls codec;
  expect(glic_glitch_preset_apply_codec("codec__bitrate_meltdown", &codec) ==
             GLIC_GLITCH_PRESET_OK,
         "apply codec preset");
  expect(codec.effect == GLIC_CODEC_GLITCH_BITRATE_CRUSH &&
             nearly_equal(codec.amount, 0.96f) &&
             nearly_equal(codec.rate, 0.78f) &&
             nearly_equal(codec.feedback, 0.55f) &&
             codec.seed == UINT64_C(1204376450),
         "codec preset exact controls");
  expect(glic_glitch_preset_apply_codec("original__vv01", &codec) ==
             GLIC_GLITCH_PRESET_WRONG_CATEGORY,
         "wrong category fails closed");

  glic_glitch_preset_descriptor found;
  glic_glitch_preset_descriptor_init(&found);
  expect(glic_glitch_preset_find("spatial__diagonal_slip", &found) ==
             GLIC_GLITCH_PRESET_OK &&
             found.spatial_effect == GLIC_METAL_EFFECT_DIAGONAL_SLIP &&
             nearly_equal(found.amount, 0.70f),
         "find selected preset by stable name");
  expect(glic_glitch_preset_find("missing", &found) ==
             GLIC_GLITCH_PRESET_NOT_FOUND,
         "missing preset lookup");

  if (failures != 0)
    return 1;
  puts("PASS selected glitch preset bank");
  return 0;
}
