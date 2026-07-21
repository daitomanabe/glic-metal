#ifndef GLIC_METAL_GLIC_METAL_H
#define GLIC_METAL_GLIC_METAL_H

#include <stddef.h>
#include <stdint.h>

#if defined(__cplusplus)
extern "C" {
#endif

#define GLIC_METAL_ABI_VERSION 1u

typedef struct glic_metal_context glic_metal_context;

typedef int32_t glic_metal_status;
enum {
  GLIC_METAL_OK = 0,
  GLIC_METAL_INVALID_ARGUMENT = 1,
  GLIC_METAL_NOT_PREPARED = 2,
  GLIC_METAL_PRESET_NOT_FOUND = 3,
  GLIC_METAL_UNSUPPORTED = 4,
  GLIC_METAL_BACKEND_UNAVAILABLE = 5,
  GLIC_METAL_PROCESSING_FAILED = 6,
  GLIC_METAL_INTERNAL_ERROR = 7
};

typedef int32_t glic_metal_backend;
enum {
  GLIC_METAL_BACKEND_AUTO = 0,
  GLIC_METAL_BACKEND_CPU = 1,
  GLIC_METAL_BACKEND_METAL = 2
};

typedef int32_t glic_metal_mode;
enum {
  /* Fast visual approximation available for all upstream presets. */
  GLIC_METAL_MODE_COMPAT_REALTIME = 0,
  /* Higher-fidelity original-style lane; unsupported presets fail closed. */
  GLIC_METAL_MODE_ORIGINAL = 1
};

typedef int32_t glic_metal_fidelity;
enum {
  GLIC_METAL_FIDELITY_STRICT = 0,
  GLIC_METAL_FIDELITY_FAST_MATCH = 1
};

typedef int32_t glic_metal_pixel_format;
enum {
  /* Byte order in memory: B, G, R, A. Matches kCVPixelFormatType_32BGRA. */
  GLIC_METAL_PIXEL_FORMAT_BGRA8 = 0,
  /* Byte order in memory: R, G, B, A. */
  GLIC_METAL_PIXEL_FORMAT_RGBA8 = 1
};

typedef int32_t glic_metal_effect_family;
enum {
  GLIC_METAL_EFFECT_LEGACY_BLOCK = 0,
  GLIC_METAL_EFFECT_LINE_TEAR = 1,
  GLIC_METAL_EFFECT_CHANNEL_SHEAR = 2,
  GLIC_METAL_EFFECT_ANALOG_SYNC = 3,
  GLIC_METAL_EFFECT_MIRROR_FOLD = 4,
  GLIC_METAL_EFFECT_EDGE_ECHO = 5,
  GLIC_METAL_EFFECT_BITPLANE_DITHER = 6,
  GLIC_METAL_EFFECT_WAVE_WARP = 7,
  GLIC_METAL_EFFECT_POSTER_SOLAR = 8
};

/*
 * Initialize with glic_metal_config_init() before changing fields. String
 * pointers and metal_device only need to remain valid for the duration of
 * glic_metal_prepare(); the library copies the values it needs.
 */
typedef struct glic_metal_config {
  uint32_t struct_size;
  uint32_t abi_version;
  int32_t width;
  int32_t height;
  const char *preset_directory;
  const char *preset_name;
  glic_metal_backend backend;
  glic_metal_mode mode;
  glic_metal_fidelity fidelity;
  uint32_t segmentation_reuse_frames;
  uint32_t seed;
  float effect_strength;
  glic_metal_effect_family effect_family;
  float effect_amount;
  float effect_scale;
  float effect_rate;
  /* Optional id<MTLDevice>, bridged without ownership transfer on macOS. */
  void *metal_device;
  /* Optional explicit path to glic_realtime.metallib. */
  const char *metal_library_path;
  uint32_t reserved[8];
} glic_metal_config;

typedef struct glic_metal_frame_stats {
  uint32_t struct_size;
  uint32_t abi_version;
  uint64_t frame_index;
  uint64_t total_segments;
  double total_milliseconds;
  double gpu_milliseconds;
  double cpu_prepare_milliseconds;
  double cpu_output_milliseconds;
  uint32_t hardware_accelerated;
  uint32_t segmentation_reused;
  uint32_t reserved[8];
} glic_metal_frame_stats;

typedef void (*glic_metal_preset_callback)(const char *preset_name,
                                           void *user_data);

uint32_t glic_metal_get_abi_version(void);
const char *glic_metal_get_version_string(void);
const char *glic_metal_status_string(glic_metal_status status);

void glic_metal_config_init(glic_metal_config *config);
void glic_metal_frame_stats_init(glic_metal_frame_stats *stats);

glic_metal_status glic_metal_context_create(glic_metal_context **context);
void glic_metal_context_destroy(glic_metal_context *context);

/*
 * Prepare or switch a preset. Call this outside the realtime/render callback.
 * A failed prepare leaves the previously prepared engine active.
 */
glic_metal_status glic_metal_prepare(glic_metal_context *context,
                                     const glic_metal_config *config);

/*
 * Process a CPU-addressable frame. Input and output may be the same buffer.
 * bytes_per_row must be at least width * 4. No allocation occurs after a
 * successful prepare unless an error message grows.
 */
glic_metal_status glic_metal_process_frame(
    glic_metal_context *context, const void *input, size_t input_bytes_per_row,
    void *output, size_t output_bytes_per_row,
    glic_metal_pixel_format pixel_format, uint64_t frame_index);

/*
 * Synchronous zero-copy compatibility-mode processing for BGRA8Unorm
 * id<MTLTexture> objects. The pointers are bridged without ownership transfer.
 */
glic_metal_status glic_metal_process_metal_textures(
    glic_metal_context *context, void *input_texture, void *output_texture,
    uint64_t frame_index);

/*
 * Append compatibility-mode work to an uncommitted id<MTLCommandBuffer>.
 * The caller owns commit/wait and must keep at most three frames in flight.
 */
glic_metal_status glic_metal_encode_metal_textures(
    glic_metal_context *context, void *command_buffer, void *input_texture,
    void *output_texture, uint64_t frame_index);

glic_metal_status glic_metal_get_last_stats(
    const glic_metal_context *context, glic_metal_frame_stats *stats);
const char *glic_metal_get_last_error(const glic_metal_context *context);
const char *glic_metal_get_active_backend(const glic_metal_context *context);
int32_t glic_metal_is_hardware_accelerated(
    const glic_metal_context *context);

glic_metal_status glic_metal_enumerate_presets(
    const char *preset_directory, glic_metal_preset_callback callback,
    void *user_data);

#if defined(__cplusplus)
} /* extern "C" */
#endif

#endif /* GLIC_METAL_GLIC_METAL_H */
