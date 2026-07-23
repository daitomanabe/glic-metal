#ifndef GLIC_METAL_CODEC_GLITCH_H
#define GLIC_METAL_CODEC_GLITCH_H

#include <stddef.h>
#include <stdint.h>

#if defined(__cplusplus)
extern "C" {
#endif

#define GLIC_CODEC_GLITCH_ABI_VERSION 1u

typedef struct glic_codec_glitch_context glic_codec_glitch_context;

typedef int32_t glic_codec_glitch_status;
enum {
  GLIC_CODEC_GLITCH_OK = 0,
  GLIC_CODEC_GLITCH_INVALID_ARGUMENT = 1,
  GLIC_CODEC_GLITCH_NOT_PREPARED = 2,
  GLIC_CODEC_GLITCH_BACKEND_UNAVAILABLE = 3,
  GLIC_CODEC_GLITCH_BACKPRESSURE = 4,
  GLIC_CODEC_GLITCH_TIMEOUT = 5,
  GLIC_CODEC_GLITCH_PROCESSING_FAILED = 6,
  GLIC_CODEC_GLITCH_INTERNAL_ERROR = 7,
  GLIC_CODEC_GLITCH_NO_FRAME_AVAILABLE = 8
};

typedef int32_t glic_codec_glitch_effect;
enum {
  GLIC_CODEC_GLITCH_QP_PUMP = 0,
  GLIC_CODEC_GLITCH_BITRATE_CRUSH = 1,
  GLIC_CODEC_GLITCH_SLICE_DROPOUT = 2,
  GLIC_CODEC_GLITCH_SLICE_TRANSPLANT = 3,
  GLIC_CODEC_GLITCH_PFRAME_LOSS = 4,
  GLIC_CODEC_GLITCH_IDR_STARVATION = 5,
  GLIC_CODEC_GLITCH_PAYLOAD_XOR = 6,
  GLIC_CODEC_GLITCH_REFERENCE_TIMEWARP = 7,
  GLIC_CODEC_GLITCH_CODEC_FEEDBACK = 8,
  GLIC_CODEC_GLITCH_GENERATION_CASCADE = 9,
  GLIC_CODEC_GLITCH_RESOLUTION_HOP = 10,
  GLIC_CODEC_GLITCH_CHROMA_CODEC_ECHO = 11,
  GLIC_CODEC_GLITCH_TEMPORAL_POLYPHONY = 12,
  GLIC_CODEC_GLITCH_INTRA_CANNIBALISM = 13,
  GLIC_CODEC_GLITCH_RESIDUAL_RIFT = 14,
  GLIC_CODEC_GLITCH_CODEC_GRAIN_SYNTH = 15,
  GLIC_CODEC_GLITCH_RECURSIVE_CODEC_SKIN = 16,
  GLIC_CODEC_GLITCH_CONCEALMENT_CHOREOGRAPHY = 17
};

/*
 * Codec Glitch is macOS-only and operates asynchronously on opaque
 * CVPixelBufferRef values. Initialize this struct before changing fields.
 */
typedef struct glic_codec_glitch_config {
  uint32_t struct_size;
  uint32_t abi_version;
  int32_t width;
  int32_t height;
  int32_t frames_per_second;
  /* Defaults to 4,000,000 bps. Dynamic control never exceeds this value. */
  int32_t average_bit_rate;
  int32_t key_frame_interval;
  /* Encoder preference only; safe post-decode slice effects do not depend on
   * it. */
  int32_t maximum_slice_bytes;
  /* Decoded CVPixelBuffer history; clamped to [4, 12]. */
  int32_t decoded_history_frames;
  int32_t maximum_in_flight_frames;
  /* Bounds both polling and native callback delivery rings. */
  int32_t poll_queue_capacity;
  uint32_t require_hardware_encoder;
  uint32_t require_hardware_decoder;
  /* Enabled by default. RealTime remains enabled independently; dynamic
   * bitrate uses min(average_bit_rate, width * height * fps / 4) as its floor.
   */
  uint32_t enable_low_latency_rate_control;
  uint32_t reserved[8];
} glic_codec_glitch_config;

typedef struct glic_codec_glitch_controls {
  uint32_t struct_size;
  uint32_t abi_version;
  glic_codec_glitch_effect effect;
  float amount;
  float rate;
  float feedback;
  uint64_t seed;
  int32_t minimum_qp;
  int32_t maximum_qp;
  int32_t crushed_bit_rate;
  int32_t cascade_generations;
  float reduced_resolution_scale;
  uint32_t reserved[8];
} glic_codec_glitch_controls;

typedef struct glic_codec_glitch_frame {
  uint32_t struct_size;
  uint32_t abi_version;
  /* Retained CVPixelBufferRef. Release with the function below. */
  void *pixel_buffer;
  uint64_t frame_index;
  int64_t presentation_value;
  int32_t presentation_timescale;
  glic_codec_glitch_effect effect;
  uint32_t packet_was_modified;
  uint32_t repeated_previous_frame;
  uint32_t intentional_repeat_frame;
  /* True for any failure fallback, including retained input before first
   * decode. */
  uint32_t non_intentional_fallback_frame;
  /* True while a new encoder/decoder stage uses its longer startup deadline. */
  uint32_t codec_warmup_frame;
  uint32_t watchdog_recovery_frame;
  double latency_milliseconds;
  uint32_t reserved[5];
} glic_codec_glitch_frame;

typedef struct glic_codec_glitch_stats {
  uint32_t struct_size;
  uint32_t abi_version;
  uint64_t submitted_frames;
  uint64_t encoded_frames;
  uint64_t decoded_frames;
  uint64_t emitted_frames;
  uint64_t backpressure_drops;
  /* Legacy name: counts intentionally held encoded frames. */
  uint64_t intentional_packet_drops;
  /* All codec processing errors: encode, extraction, decode, and timeout. */
  uint64_t codec_errors;
  uint64_t watchdog_recoveries;
  /* Legacy name: combines bounded callback and polling delivery drops. */
  uint64_t poll_queue_drops;
  double last_latency_milliseconds;
  double average_latency_milliseconds;
  uint32_t hardware_encoder;
  uint32_t hardware_decoder;
  uint32_t base_frame_qp_supported;
  uint32_t reserved[8];
} glic_codec_glitch_stats;

uint32_t glic_codec_glitch_get_abi_version(void);
const char *glic_codec_glitch_status_string(glic_codec_glitch_status status);
const char *glic_codec_glitch_effect_name(glic_codec_glitch_effect effect);

void glic_codec_glitch_config_init(glic_codec_glitch_config *config);
void glic_codec_glitch_controls_init(glic_codec_glitch_controls *controls);
void glic_codec_glitch_frame_init(glic_codec_glitch_frame *frame);
void glic_codec_glitch_stats_init(glic_codec_glitch_stats *stats);

glic_codec_glitch_status
glic_codec_glitch_context_create(glic_codec_glitch_context **context);
void glic_codec_glitch_context_destroy(glic_codec_glitch_context *context);

/*
 * Prepares queues, pools, and the normal VideoToolbox encoder used to validate
 * the backend. Specialized encoders are lazy; a decoder is created from the
 * first encoded sample. Call outside a capture/render callback.
 */
glic_codec_glitch_status
glic_codec_glitch_prepare(glic_codec_glitch_context *context,
                          const glic_codec_glitch_config *config);

glic_codec_glitch_status
glic_codec_glitch_set_controls(glic_codec_glitch_context *context,
                               const glic_codec_glitch_controls *controls);

/*
 * Nonblocking submit. pixel_buffer is a CVPixelBufferRef bridged as void * and
 * only needs to remain valid for this call. PTS is expressed as value/scale.
 */
glic_codec_glitch_status
glic_codec_glitch_submit_pixel_buffer(glic_codec_glitch_context *context,
                                      void *pixel_buffer, uint64_t frame_index,
                                      int64_t presentation_value,
                                      int32_t presentation_timescale);

/* Returns NO_FRAME_AVAILABLE when the bounded poll ring has no decoded frame.
 */
glic_codec_glitch_status
glic_codec_glitch_copy_latest_pixel_buffer(glic_codec_glitch_context *context,
                                           glic_codec_glitch_frame *frame);

void glic_codec_glitch_pixel_buffer_release(void *pixel_buffer);

glic_codec_glitch_status
glic_codec_glitch_flush(glic_codec_glitch_context *context,
                        uint32_t timeout_milliseconds);
/* Explicitly drains and rebuilds codec sessions and clears decoded history.
 * Watchdog recovery does not call this; it forces the next IDR in-place. */
glic_codec_glitch_status
glic_codec_glitch_reset(glic_codec_glitch_context *context);
glic_codec_glitch_status
glic_codec_glitch_get_stats(const glic_codec_glitch_context *context,
                            glic_codec_glitch_stats *stats);
const char *
glic_codec_glitch_get_last_error(const glic_codec_glitch_context *context);

#if defined(__cplusplus)
} /* extern "C" */
#endif

#endif /* GLIC_METAL_CODEC_GLITCH_H */
