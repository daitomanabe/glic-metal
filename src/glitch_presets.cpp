#include <glic_metal/glitch_presets.h>

#include <array>
#include <cstddef>
#include <cstring>

namespace {

struct PresetRecord {
  const char *name;
  glic_glitch_preset_category category;
  const char *effectName;
  const char *originalPresetName;
  glic_metal_effect_family spatialEffect;
  glic_codec_glitch_effect codecEffect;
  float amount;
  float scale;
  float rate;
  float feedback;
  uint64_t seed;
};

constexpr glic_metal_effect_family kNoSpatialEffect = -1;
constexpr glic_codec_glitch_effect kNoCodecEffect = -1;

constexpr PresetRecord original(const char *name, const char *preset) {
  return {name, GLIC_GLITCH_PRESET_ORIGINAL, preset, preset, kNoSpatialEffect,
          kNoCodecEffect, 0.0f, 0.0f, 0.0f, 0.0f, 0};
}

constexpr PresetRecord spatial(const char *name, const char *effectName,
                               glic_metal_effect_family effect, float amount,
                               float scale, float rate, uint64_t seed) {
  return {name, GLIC_GLITCH_PRESET_SPATIAL, effectName, nullptr, effect,
          kNoCodecEffect, amount, scale, rate, 0.0f, seed};
}

constexpr PresetRecord codec(const char *name, const char *effectName,
                             glic_codec_glitch_effect effect, float amount,
                             float rate, float feedback, uint64_t seed) {
  return {name, GLIC_GLITCH_PRESET_CODEC, effectName, nullptr,
          kNoSpatialEffect, effect, amount, 0.0f, rate, feedback, seed};
}

constexpr std::array<PresetRecord, 19> kPresets{{
    original("original__vv01", "vv01"),
    original("original__bl33dyl1n3z", "bl33dyl1n3z"),
    original("original__burn", "burn"),
    original("original__colour_waves_sharp", "colour_waves_sharp"),
    original("original__bl33dyl1n3z-2", "bl33dyl1n3z-2"),
    original("original__wtf", "wtf"),
    original("original__lightblur", "lightblur"),
    original("original__constrctivist_minimal", "constrctivist_minimal"),
    original("original__web_p_like", "web_p_like"),
    original("original__webp", "webp"),
    original("original__abstract_expressionism", "abstract_expressionism"),
    original("original__colour_glow", "colour_glow"),
    spatial("spatial__poster_solar", "poster_solar",
            GLIC_METAL_EFFECT_POSTER_SOLAR, 0.49f, 0.36f, 0.15f,
            UINT64_C(1296652297)),
    spatial("spatial__bitplane_dither", "bitplane_dither",
            GLIC_METAL_EFFECT_BITPLANE_DITHER, 0.79f, 0.28f, 0.92f,
            UINT64_C(1296652295)),
    spatial("spatial__scanline_weave", "scanline_weave",
            GLIC_METAL_EFFECT_SCANLINE_WEAVE, 0.62f, 0.38f, 0.72f,
            UINT64_C(1296652301)),
    codec("codec__bitrate_meltdown", "bitrate_crush",
          GLIC_CODEC_GLITCH_BITRATE_CRUSH, 0.96f, 0.78f, 0.55f,
          UINT64_C(1204376450)),
    original("original__beautifulwave", "beautifulwave"),
    spatial("spatial__diagonal_slip", "diagonal_slip",
            GLIC_METAL_EFFECT_DIAGONAL_SLIP, 0.70f, 0.72f, 0.58f,
            UINT64_C(1296652300)),
    original("original__bi0g4n1c", "bi0g4n1c"),
}};

const PresetRecord *findRecord(const char *name) {
  if (name == nullptr || name[0] == '\0')
    return nullptr;
  for (const auto &record : kPresets) {
    if (std::strcmp(record.name, name) == 0)
      return &record;
  }
  return nullptr;
}

bool descriptorIsValid(const glic_glitch_preset_descriptor *descriptor) {
  return descriptor != nullptr &&
         descriptor->struct_size >= sizeof(glic_glitch_preset_descriptor) &&
         descriptor->abi_version == GLIC_GLITCH_PRESET_ABI_VERSION;
}

void copyRecord(const PresetRecord &record,
                glic_glitch_preset_descriptor *descriptor) {
  descriptor->name = record.name;
  descriptor->category = record.category;
  descriptor->effect_name = record.effectName;
  descriptor->original_preset_name = record.originalPresetName;
  descriptor->spatial_effect = record.spatialEffect;
  descriptor->codec_effect = record.codecEffect;
  descriptor->amount = record.amount;
  descriptor->scale = record.scale;
  descriptor->rate = record.rate;
  descriptor->feedback = record.feedback;
  descriptor->seed = record.seed;
}

} // namespace

extern "C" {

uint32_t glic_glitch_preset_get_abi_version(void) {
  return GLIC_GLITCH_PRESET_ABI_VERSION;
}

const char *
glic_glitch_preset_status_string(glic_glitch_preset_status status) {
  switch (status) {
  case GLIC_GLITCH_PRESET_OK:
    return "ok";
  case GLIC_GLITCH_PRESET_INVALID_ARGUMENT:
    return "invalid argument";
  case GLIC_GLITCH_PRESET_NOT_FOUND:
    return "preset not found";
  case GLIC_GLITCH_PRESET_WRONG_CATEGORY:
    return "wrong preset category";
  default:
    return "unknown status";
  }
}

const char *
glic_glitch_preset_category_name(glic_glitch_preset_category category) {
  switch (category) {
  case GLIC_GLITCH_PRESET_ORIGINAL:
    return "original";
  case GLIC_GLITCH_PRESET_SPATIAL:
    return "spatial";
  case GLIC_GLITCH_PRESET_CODEC:
    return "codec";
  default:
    return "unknown";
  }
}

void glic_glitch_preset_descriptor_init(
    glic_glitch_preset_descriptor *descriptor) {
  if (descriptor == nullptr)
    return;
  std::memset(descriptor, 0, sizeof(*descriptor));
  descriptor->struct_size = sizeof(*descriptor);
  descriptor->abi_version = GLIC_GLITCH_PRESET_ABI_VERSION;
  descriptor->spatial_effect = kNoSpatialEffect;
  descriptor->codec_effect = kNoCodecEffect;
}

uint32_t glic_glitch_preset_count(void) {
  return static_cast<uint32_t>(kPresets.size());
}

glic_glitch_preset_status
glic_glitch_preset_get(uint32_t index,
                       glic_glitch_preset_descriptor *descriptor) {
  if (!descriptorIsValid(descriptor))
    return GLIC_GLITCH_PRESET_INVALID_ARGUMENT;
  if (index >= kPresets.size())
    return GLIC_GLITCH_PRESET_NOT_FOUND;
  copyRecord(kPresets[index], descriptor);
  return GLIC_GLITCH_PRESET_OK;
}

glic_glitch_preset_status
glic_glitch_preset_find(const char *name,
                        glic_glitch_preset_descriptor *descriptor) {
  if (!descriptorIsValid(descriptor) || name == nullptr)
    return GLIC_GLITCH_PRESET_INVALID_ARGUMENT;
  const PresetRecord *record = findRecord(name);
  if (record == nullptr)
    return GLIC_GLITCH_PRESET_NOT_FOUND;
  copyRecord(*record, descriptor);
  return GLIC_GLITCH_PRESET_OK;
}

glic_glitch_preset_status
glic_glitch_preset_apply_metal(const char *name, glic_metal_config *config) {
  if (config == nullptr ||
      config->struct_size < sizeof(glic_metal_config) ||
      config->abi_version != GLIC_METAL_ABI_VERSION)
    return GLIC_GLITCH_PRESET_INVALID_ARGUMENT;
  const PresetRecord *record = findRecord(name);
  if (record == nullptr)
    return GLIC_GLITCH_PRESET_NOT_FOUND;
  if (record->category == GLIC_GLITCH_PRESET_CODEC)
    return GLIC_GLITCH_PRESET_WRONG_CATEGORY;

  config->backend = GLIC_METAL_BACKEND_METAL;
  if (record->category == GLIC_GLITCH_PRESET_ORIGINAL) {
    config->mode = GLIC_METAL_MODE_ORIGINAL;
    config->preset_name = record->originalPresetName;
    return GLIC_GLITCH_PRESET_OK;
  }

  config->mode = GLIC_METAL_MODE_COMPAT_REALTIME;
  config->preset_name = "default";
  config->effect_family = record->spatialEffect;
  config->effect_amount = record->amount;
  config->effect_scale = record->scale;
  config->effect_rate = record->rate;
  config->seed = static_cast<uint32_t>(record->seed);
  config->effect_strength = 1.0f;
  return GLIC_GLITCH_PRESET_OK;
}

glic_glitch_preset_status glic_glitch_preset_apply_codec(
    const char *name, glic_codec_glitch_controls *controls) {
  if (controls == nullptr)
    return GLIC_GLITCH_PRESET_INVALID_ARGUMENT;
  const PresetRecord *record = findRecord(name);
  if (record == nullptr)
    return GLIC_GLITCH_PRESET_NOT_FOUND;
  if (record->category != GLIC_GLITCH_PRESET_CODEC)
    return GLIC_GLITCH_PRESET_WRONG_CATEGORY;

  glic_codec_glitch_controls_init(controls);
  controls->effect = record->codecEffect;
  controls->amount = record->amount;
  controls->rate = record->rate;
  controls->feedback = record->feedback;
  controls->seed = record->seed;
  return GLIC_GLITCH_PRESET_OK;
}

} // extern "C"
