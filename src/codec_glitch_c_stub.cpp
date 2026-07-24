#include <glic_metal/codec_glitch.h>

#if !defined(__APPLE__)

#include <cstring>

struct glic_codec_glitch_context {};

namespace {

constexpr const char *kEffectNames[] = {
    "qp_pump",        "bitrate_crush",
    "slice_dropout",  "slice_transplant",
    "pframe_loss",    "idr_starvation",
    "payload_xor",    "reference_timewarp",
    "codec_feedback", "generation_cascade",
    "resolution_hop", "chroma_codec_echo",
    "temporal_polyphony", "intra_cannibalism",
    "residual_rift", "codec_grain_synth",
    "recursive_codec_skin", "concealment_choreography",
    "dual_codec_crossbreed", "codec_pingpong",
    "gop_accordion", "bframe_braid",
    "plane_split_codec", "roi_quality_islands",
    "codec_phase_mosaic", "encoder_hot_swap",
    "pts_rubberband", "bitrate_raster",
    "plane_time_split", "reference_atlas",
    "flow_lattice", "scan_order_fold",
    "regional_gop_clock", "entropy_feedback",
    "rolling_time_shutter", "asymmetric_plane_codec",
};

} // namespace

extern "C" {

uint32_t glic_codec_glitch_get_abi_version(void) {
  return GLIC_CODEC_GLITCH_ABI_VERSION;
}

const char *glic_codec_glitch_status_string(glic_codec_glitch_status status) {
  switch (status) {
  case GLIC_CODEC_GLITCH_OK:
    return "ok";
  case GLIC_CODEC_GLITCH_INVALID_ARGUMENT:
    return "invalid argument";
  case GLIC_CODEC_GLITCH_NOT_PREPARED:
    return "not prepared";
  case GLIC_CODEC_GLITCH_BACKEND_UNAVAILABLE:
    return "backend unavailable";
  case GLIC_CODEC_GLITCH_BACKPRESSURE:
    return "backpressure";
  case GLIC_CODEC_GLITCH_TIMEOUT:
    return "timeout";
  case GLIC_CODEC_GLITCH_PROCESSING_FAILED:
    return "processing failed";
  case GLIC_CODEC_GLITCH_NO_FRAME_AVAILABLE:
    return "no frame available";
  default:
    return "internal error";
  }
}

const char *glic_codec_glitch_effect_name(glic_codec_glitch_effect effect) {
  if (effect < GLIC_CODEC_GLITCH_QP_PUMP ||
      effect > GLIC_CODEC_GLITCH_ASYMMETRIC_PLANE_CODEC)
    return "unknown";
  return kEffectNames[effect];
}

const char *
glic_codec_glitch_effect_implementation_level(glic_codec_glitch_effect effect) {
  if (effect < GLIC_CODEC_GLITCH_QP_PUMP ||
      effect > GLIC_CODEC_GLITCH_ASYMMETRIC_PLANE_CODEC)
    return "unknown";
  if (effect <= GLIC_CODEC_GLITCH_CONCEALMENT_CHOREOGRAPHY)
    return "videotoolbox_clean_encode_decode_plus_gpu_reconstruction";
  if (effect <= GLIC_CODEC_GLITCH_BITRATE_RASTER)
    return "videotoolbox_single_codec_crossbreed_plus_gpu_reconstruction";
  return "videotoolbox_decoded_history_plus_coreimage_metal_reconstruction";
}

const char *glic_codec_glitch_codec_name(glic_codec_glitch_codec codec) {
  switch (codec) {
  case GLIC_CODEC_GLITCH_CODEC_H264:
    return "h264";
  case GLIC_CODEC_GLITCH_CODEC_HEVC:
    return "hevc";
  case GLIC_CODEC_GLITCH_CODEC_PRORES_422:
    return "prores_422";
  default:
    return "unknown";
  }
}

void glic_codec_glitch_config_init(glic_codec_glitch_config *config) {
  if (config == nullptr)
    return;
  std::memset(config, 0, sizeof(*config));
  config->struct_size = sizeof(*config);
  config->abi_version = GLIC_CODEC_GLITCH_ABI_VERSION;
  config->width = 960;
  config->height = 540;
  config->frames_per_second = 30;
  config->average_bit_rate = 4000000;
  config->key_frame_interval = 60;
  config->maximum_slice_bytes = 4000;
  config->decoded_history_frames = 12;
  config->maximum_in_flight_frames = 24;
  config->poll_queue_capacity = 8;
  config->require_hardware_encoder = 1;
  config->require_hardware_decoder = 1;
  config->enable_low_latency_rate_control = 1;
  config->codec = GLIC_CODEC_GLITCH_CODEC_H264;
}

void glic_codec_glitch_controls_init(glic_codec_glitch_controls *controls) {
  if (controls == nullptr)
    return;
  std::memset(controls, 0, sizeof(*controls));
  controls->struct_size = sizeof(*controls);
  controls->abi_version = GLIC_CODEC_GLITCH_ABI_VERSION;
  controls->effect = GLIC_CODEC_GLITCH_BITRATE_CRUSH;
  controls->amount = 0.55f;
  controls->rate = 0.35f;
  controls->feedback = 0.60f;
  controls->seed = 0x474c4943434f4445ULL;
  controls->minimum_qp = 18;
  controls->maximum_qp = 51;
  controls->crushed_bit_rate = 120000;
  controls->cascade_generations = 3;
  controls->reduced_resolution_scale = 0.25f;
}

void glic_codec_glitch_frame_init(glic_codec_glitch_frame *frame) {
  if (frame == nullptr)
    return;
  std::memset(frame, 0, sizeof(*frame));
  frame->struct_size = sizeof(*frame);
  frame->abi_version = GLIC_CODEC_GLITCH_ABI_VERSION;
}

void glic_codec_glitch_stats_init(glic_codec_glitch_stats *stats) {
  if (stats == nullptr)
    return;
  std::memset(stats, 0, sizeof(*stats));
  stats->struct_size = sizeof(*stats);
  stats->abi_version = GLIC_CODEC_GLITCH_ABI_VERSION;
}

glic_codec_glitch_status
glic_codec_glitch_context_create(glic_codec_glitch_context **context) {
  if (context == nullptr)
    return GLIC_CODEC_GLITCH_INVALID_ARGUMENT;
  *context = nullptr;
  return GLIC_CODEC_GLITCH_BACKEND_UNAVAILABLE;
}

void glic_codec_glitch_context_destroy(glic_codec_glitch_context *context) {
  delete context;
}

glic_codec_glitch_status
glic_codec_glitch_prepare(glic_codec_glitch_context *context,
                          const glic_codec_glitch_config *config) {
  if (context == nullptr || config == nullptr)
    return GLIC_CODEC_GLITCH_INVALID_ARGUMENT;
  return GLIC_CODEC_GLITCH_BACKEND_UNAVAILABLE;
}

glic_codec_glitch_status
glic_codec_glitch_set_controls(glic_codec_glitch_context *context,
                               const glic_codec_glitch_controls *controls) {
  if (context == nullptr || controls == nullptr)
    return GLIC_CODEC_GLITCH_INVALID_ARGUMENT;
  return GLIC_CODEC_GLITCH_BACKEND_UNAVAILABLE;
}

glic_codec_glitch_status
glic_codec_glitch_submit_pixel_buffer(glic_codec_glitch_context *context,
                                      void *pixelBuffer, uint64_t, int64_t,
                                      int32_t presentationTimescale) {
  if (context == nullptr || pixelBuffer == nullptr ||
      presentationTimescale <= 0)
    return GLIC_CODEC_GLITCH_INVALID_ARGUMENT;
  return GLIC_CODEC_GLITCH_BACKEND_UNAVAILABLE;
}

glic_codec_glitch_status
glic_codec_glitch_copy_latest_pixel_buffer(glic_codec_glitch_context *context,
                                           glic_codec_glitch_frame *frame) {
  if (context == nullptr || frame == nullptr)
    return GLIC_CODEC_GLITCH_INVALID_ARGUMENT;
  return GLIC_CODEC_GLITCH_BACKEND_UNAVAILABLE;
}

void glic_codec_glitch_pixel_buffer_release(void *) {}

glic_codec_glitch_status
glic_codec_glitch_flush(glic_codec_glitch_context *context,
                        uint32_t timeoutMilliseconds) {
  if (context == nullptr || timeoutMilliseconds == 0)
    return GLIC_CODEC_GLITCH_INVALID_ARGUMENT;
  return GLIC_CODEC_GLITCH_BACKEND_UNAVAILABLE;
}

glic_codec_glitch_status
glic_codec_glitch_reset(glic_codec_glitch_context *context) {
  if (context == nullptr)
    return GLIC_CODEC_GLITCH_INVALID_ARGUMENT;
  return GLIC_CODEC_GLITCH_BACKEND_UNAVAILABLE;
}

glic_codec_glitch_status
glic_codec_glitch_get_stats(const glic_codec_glitch_context *context,
                            glic_codec_glitch_stats *stats) {
  if (context == nullptr || stats == nullptr)
    return GLIC_CODEC_GLITCH_INVALID_ARGUMENT;
  return GLIC_CODEC_GLITCH_BACKEND_UNAVAILABLE;
}

const char *
glic_codec_glitch_get_last_error(const glic_codec_glitch_context *) {
  return "Codec Glitch requires macOS VideoToolbox";
}

} // extern "C"

#endif
