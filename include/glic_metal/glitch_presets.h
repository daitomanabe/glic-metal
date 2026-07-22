#ifndef GLIC_METAL_GLITCH_PRESETS_H
#define GLIC_METAL_GLITCH_PRESETS_H

#include <glic_metal/codec_glitch.h>
#include <glic_metal/glic_metal.h>

#include <stdint.h>

#if defined(__cplusplus)
extern "C" {
#endif

#define GLIC_GLITCH_PRESET_ABI_VERSION 1u

typedef int32_t glic_glitch_preset_status;
enum {
  GLIC_GLITCH_PRESET_OK = 0,
  GLIC_GLITCH_PRESET_INVALID_ARGUMENT = 1,
  GLIC_GLITCH_PRESET_NOT_FOUND = 2,
  GLIC_GLITCH_PRESET_WRONG_CATEGORY = 3
};

typedef int32_t glic_glitch_preset_category;
enum {
  GLIC_GLITCH_PRESET_ORIGINAL = 0,
  GLIC_GLITCH_PRESET_SPATIAL = 1,
  GLIC_GLITCH_PRESET_CODEC = 2
};

/*
 * A read-only view of one adopted preset. String pointers have process
 * lifetime and must not be freed. Initialize before glic_glitch_preset_get().
 * Fields that do not apply to the category use -1, NULL, or 0.
 */
typedef struct glic_glitch_preset_descriptor {
  uint32_t struct_size;
  uint32_t abi_version;
  const char *name;
  glic_glitch_preset_category category;
  const char *effect_name;
  const char *original_preset_name;
  glic_metal_effect_family spatial_effect;
  glic_codec_glitch_effect codec_effect;
  float amount;
  float scale;
  float rate;
  float feedback;
  uint64_t seed;
  uint32_t reserved[8];
} glic_glitch_preset_descriptor;

uint32_t glic_glitch_preset_get_abi_version(void);
const char *
glic_glitch_preset_status_string(glic_glitch_preset_status status);
const char *
glic_glitch_preset_category_name(glic_glitch_preset_category category);
void glic_glitch_preset_descriptor_init(
    glic_glitch_preset_descriptor *descriptor);

/* The returned catalog order matches resources/selected-presets.json. */
uint32_t glic_glitch_preset_count(void);
glic_glitch_preset_status
glic_glitch_preset_get(uint32_t index,
                       glic_glitch_preset_descriptor *descriptor);
glic_glitch_preset_status
glic_glitch_preset_find(const char *name,
                        glic_glitch_preset_descriptor *descriptor);

/*
 * Applies an adopted original or spatial preset without changing host-owned
 * width, height, preset_directory, metal_device, or metal_library_path.
 */
glic_glitch_preset_status
glic_glitch_preset_apply_metal(const char *name, glic_metal_config *config);

/* Initializes controls and applies an adopted codec preset. */
glic_glitch_preset_status glic_glitch_preset_apply_codec(
    const char *name, glic_codec_glitch_controls *controls);

#if defined(__cplusplus)
} /* extern "C" */
#endif

#endif /* GLIC_METAL_GLITCH_PRESETS_H */
