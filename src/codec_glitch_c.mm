#include <glic_metal/codec_glitch.h>

#include "codec_glitch.hpp"

#include <CoreFoundation/CoreFoundation.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <memory>
#include <mutex>
#include <new>
#include <string>

struct glic_codec_glitch_context {
  mutable std::mutex mutex;
  std::unique_ptr<glic::CodecGlitchEngine> engine;
  std::string lastError;
};

namespace {

bool validHeader(uint32_t structSize, uint32_t abiVersion,
                 std::size_t expectedSize) {
  return structSize >= expectedSize &&
         abiVersion == GLIC_CODEC_GLITCH_ABI_VERSION;
}

bool validEffect(glic_codec_glitch_effect effect) {
  return effect >= GLIC_CODEC_GLITCH_QP_PUMP &&
         effect <= GLIC_CODEC_GLITCH_CHROMA_CODEC_ECHO;
}

glic_codec_glitch_status fail(glic_codec_glitch_context *context,
                              glic_codec_glitch_status status,
                              std::string message) {
  if (context != nullptr) {
    std::lock_guard lock(context->mutex);
    context->lastError = std::move(message);
  }
  return status;
}

glic_codec_glitch_status
recordInternalFailure(glic_codec_glitch_context *context) noexcept {
  if (context != nullptr) {
    try {
      std::lock_guard lock(context->mutex);
      context->lastError = "unexpected exception at the C ABI boundary";
    } catch (...) {
      // Error reporting must not let a second exception cross the C ABI.
    }
  }
  return GLIC_CODEC_GLITCH_INTERNAL_ERROR;
}

template <typename Function>
glic_codec_glitch_status guardStatus(glic_codec_glitch_context *context,
                                     Function &&function) noexcept {
  try {
    return function();
  } catch (...) {
    return recordInternalFailure(context);
  }
}

glic::CodecGlitchControls
toCppControls(const glic_codec_glitch_controls &controls) {
  glic::CodecGlitchControls result;
  result.effect = static_cast<glic::CodecGlitchEffect>(controls.effect);
  result.amount = controls.amount;
  result.rate = controls.rate;
  result.feedback = controls.feedback;
  result.seed = controls.seed;
  result.minimumQp = controls.minimum_qp;
  result.maximumQp = controls.maximum_qp;
  result.crushedBitRate = controls.crushed_bit_rate;
  result.cascadeGenerations = controls.cascade_generations;
  result.reducedResolutionScale = controls.reduced_resolution_scale;
  return result;
}

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
  if (!validEffect(effect))
    return "unknown";
  return glic::codecGlitchEffectName(
      static_cast<glic::CodecGlitchEffect>(effect));
}

void glic_codec_glitch_config_init(glic_codec_glitch_config *config) {
  if (config == nullptr)
    return;
  *config = {};
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
}

void glic_codec_glitch_controls_init(glic_codec_glitch_controls *controls) {
  if (controls == nullptr)
    return;
  *controls = {};
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
  *frame = {};
  frame->struct_size = sizeof(*frame);
  frame->abi_version = GLIC_CODEC_GLITCH_ABI_VERSION;
}

void glic_codec_glitch_stats_init(glic_codec_glitch_stats *stats) {
  if (stats == nullptr)
    return;
  *stats = {};
  stats->struct_size = sizeof(*stats);
  stats->abi_version = GLIC_CODEC_GLITCH_ABI_VERSION;
}

glic_codec_glitch_status
glic_codec_glitch_context_create(glic_codec_glitch_context **context) {
  if (context == nullptr)
    return GLIC_CODEC_GLITCH_INVALID_ARGUMENT;
  *context = nullptr;
  try {
    *context = new glic_codec_glitch_context();
    return GLIC_CODEC_GLITCH_OK;
  } catch (...) {
    return GLIC_CODEC_GLITCH_INTERNAL_ERROR;
  }
}

void glic_codec_glitch_context_destroy(glic_codec_glitch_context *context) {
  if (context == nullptr)
    return;
  try {
    std::unique_ptr<glic::CodecGlitchEngine> engine;
    {
      std::lock_guard lock(context->mutex);
      engine = std::move(context->engine);
    }
    if (engine) {
      std::string ignored;
      engine->setOutputCallback({});
      engine->flush(std::chrono::milliseconds(2000), ignored);
    }
    delete context;
  } catch (...) {
    // The context still has to be retired if teardown diagnostics allocate or
    // a host-provided standard-library primitive reports an exception.
    try {
      delete context;
    } catch (...) {
    }
  }
}

glic_codec_glitch_status
glic_codec_glitch_prepare(glic_codec_glitch_context *context,
                          const glic_codec_glitch_config *config) {
  return guardStatus(context, [&]() -> glic_codec_glitch_status {
    if (context == nullptr || config == nullptr ||
        !validHeader(config->struct_size, config->abi_version, sizeof(*config)))
      return fail(context, GLIC_CODEC_GLITCH_INVALID_ARGUMENT,
                  "config is null, undersized, or uses an unsupported ABI");
    if (config->width <= 0 || config->height <= 0 || (config->width & 1) != 0 ||
        (config->height & 1) != 0 || config->frames_per_second <= 0 ||
        config->average_bit_rate < 16000 || config->key_frame_interval <= 0 ||
        config->maximum_slice_bytes < 0 ||
        config->decoded_history_frames <= 0 ||
        config->maximum_in_flight_frames <= 0 ||
        config->poll_queue_capacity <= 0)
      return fail(context, GLIC_CODEC_GLITCH_INVALID_ARGUMENT,
                  "codec configuration contains a non-positive value");

    glic::CodecGlitchConfiguration candidateConfig;
    candidateConfig.width = config->width;
    candidateConfig.height = config->height;
    candidateConfig.framesPerSecond = config->frames_per_second;
    candidateConfig.averageBitRate = config->average_bit_rate;
    candidateConfig.keyFrameInterval = config->key_frame_interval;
    candidateConfig.maximumSliceBytes = config->maximum_slice_bytes;
    candidateConfig.decodedHistoryFrames = config->decoded_history_frames;
    candidateConfig.maximumInFlightFrames = config->maximum_in_flight_frames;
    candidateConfig.pollQueueCapacity = config->poll_queue_capacity;
    candidateConfig.requireHardwareEncoder =
        config->require_hardware_encoder != 0;
    candidateConfig.requireHardwareDecoder =
        config->require_hardware_decoder != 0;
    candidateConfig.enableLowLatencyRateControl =
        config->enable_low_latency_rate_control != 0;

    std::string error;
    auto candidate = glic::createCodecGlitchEngine(candidateConfig, error);
    if (!candidate)
      return fail(context, GLIC_CODEC_GLITCH_BACKEND_UNAVAILABLE,
                  std::move(error));
    std::unique_ptr<glic::CodecGlitchEngine> previous;
    {
      std::lock_guard lock(context->mutex);
      previous = std::move(context->engine);
      context->engine = std::move(candidate);
      context->lastError.clear();
    }
    if (previous) {
      std::string ignored;
      previous->flush(std::chrono::milliseconds(1000), ignored);
    }
    return GLIC_CODEC_GLITCH_OK;
  });
}

glic_codec_glitch_status
glic_codec_glitch_set_controls(glic_codec_glitch_context *context,
                               const glic_codec_glitch_controls *controls) {
  return guardStatus(context, [&]() -> glic_codec_glitch_status {
    if (context == nullptr || controls == nullptr ||
        !validHeader(controls->struct_size, controls->abi_version,
                     sizeof(*controls)) ||
        !validEffect(controls->effect) || !std::isfinite(controls->amount) ||
        !std::isfinite(controls->rate) || !std::isfinite(controls->feedback) ||
        !std::isfinite(controls->reduced_resolution_scale))
      return fail(context, GLIC_CODEC_GLITCH_INVALID_ARGUMENT,
                  "controls contain an invalid header, enum, or number");
    std::lock_guard lock(context->mutex);
    if (!context->engine) {
      context->lastError = "codec engine is not prepared";
      return GLIC_CODEC_GLITCH_NOT_PREPARED;
    }
    context->engine->setControls(toCppControls(*controls));
    context->lastError.clear();
    return GLIC_CODEC_GLITCH_OK;
  });
}

glic_codec_glitch_status glic_codec_glitch_submit_pixel_buffer(
    glic_codec_glitch_context *context, void *pixelBuffer, uint64_t frameIndex,
    int64_t presentationValue, int32_t presentationTimescale) {
  return guardStatus(context, [&]() -> glic_codec_glitch_status {
    if (context == nullptr || pixelBuffer == nullptr ||
        presentationTimescale <= 0)
      return fail(context, GLIC_CODEC_GLITCH_INVALID_ARGUMENT,
                  "pixel buffer and positive PTS timescale are required");
    std::lock_guard lock(context->mutex);
    if (!context->engine) {
      context->lastError = "codec engine is not prepared";
      return GLIC_CODEC_GLITCH_NOT_PREPARED;
    }
    std::string error;
    const bool submitted = context->engine->submit(
        static_cast<CVPixelBufferRef>(pixelBuffer), frameIndex,
        CMTimeMake(presentationValue, presentationTimescale), error);
    if (!submitted) {
      context->lastError = std::move(error);
      if (context->lastError.find("backpressure") != std::string::npos ||
          context->lastError.find("in-flight") != std::string::npos ||
          context->lastError.find("queue is full") != std::string::npos)
        return GLIC_CODEC_GLITCH_BACKPRESSURE;
      return GLIC_CODEC_GLITCH_PROCESSING_FAILED;
    }
    context->lastError.clear();
    return GLIC_CODEC_GLITCH_OK;
  });
}

glic_codec_glitch_status
glic_codec_glitch_copy_latest_pixel_buffer(glic_codec_glitch_context *context,
                                           glic_codec_glitch_frame *frame) {
  return guardStatus(context, [&]() -> glic_codec_glitch_status {
    if (context == nullptr || frame == nullptr ||
        !validHeader(frame->struct_size, frame->abi_version, sizeof(*frame)) ||
        frame->pixel_buffer != nullptr)
      return fail(context, GLIC_CODEC_GLITCH_INVALID_ARGUMENT,
                  "frame must be initialized and must not own a pixel buffer");
    std::lock_guard lock(context->mutex);
    if (!context->engine) {
      context->lastError = "codec engine is not prepared";
      return GLIC_CODEC_GLITCH_NOT_PREPARED;
    }
    glic::CodecGlitchFrame result;
    if (!context->engine->poll(result))
      return GLIC_CODEC_GLITCH_NO_FRAME_AVAILABLE;
    CVPixelBufferRef pixelBuffer = result.pixelBuffer();
    if (pixelBuffer == nullptr) {
      context->lastError = "decoder emitted an empty pixel buffer";
      return GLIC_CODEC_GLITCH_PROCESSING_FAILED;
    }
    CFRetain(pixelBuffer);
    frame->pixel_buffer = pixelBuffer;
    frame->frame_index = result.frameIndex;
    frame->presentation_value = result.presentationTimeStamp.value;
    frame->presentation_timescale = result.presentationTimeStamp.timescale;
    frame->effect = static_cast<glic_codec_glitch_effect>(result.effect);
    frame->packet_was_modified = result.packetWasModified ? 1u : 0u;
    frame->repeated_previous_frame = result.repeatedPreviousFrame ? 1u : 0u;
    frame->intentional_repeat_frame = result.intentionalRepeat ? 1u : 0u;
    frame->non_intentional_fallback_frame =
        result.nonIntentionalFallback ? 1u : 0u;
    frame->codec_warmup_frame = result.codecWarmupFrame ? 1u : 0u;
    frame->watchdog_recovery_frame = result.watchdogRecoveryFrame ? 1u : 0u;
    frame->latency_milliseconds = result.latencyMilliseconds;
    context->lastError.clear();
    return GLIC_CODEC_GLITCH_OK;
  });
}

void glic_codec_glitch_pixel_buffer_release(void *pixelBuffer) {
  if (pixelBuffer != nullptr)
    CFRelease(pixelBuffer);
}

glic_codec_glitch_status
glic_codec_glitch_flush(glic_codec_glitch_context *context,
                        uint32_t timeoutMilliseconds) {
  return guardStatus(context, [&]() -> glic_codec_glitch_status {
    if (context == nullptr || timeoutMilliseconds == 0)
      return fail(context, GLIC_CODEC_GLITCH_INVALID_ARGUMENT,
                  "context and positive timeout are required");
    std::lock_guard lock(context->mutex);
    if (!context->engine) {
      context->lastError = "codec engine is not prepared";
      return GLIC_CODEC_GLITCH_NOT_PREPARED;
    }
    std::string error;
    if (!context->engine->flush(std::chrono::milliseconds(timeoutMilliseconds),
                                error)) {
      context->lastError = std::move(error);
      return (context->lastError.find("timed out") != std::string::npos ||
              context->lastError.find("Timed out") != std::string::npos)
                 ? GLIC_CODEC_GLITCH_TIMEOUT
                 : GLIC_CODEC_GLITCH_PROCESSING_FAILED;
    }
    context->lastError.clear();
    return GLIC_CODEC_GLITCH_OK;
  });
}

glic_codec_glitch_status
glic_codec_glitch_reset(glic_codec_glitch_context *context) {
  return guardStatus(context, [&]() -> glic_codec_glitch_status {
    if (context == nullptr)
      return GLIC_CODEC_GLITCH_INVALID_ARGUMENT;
    std::lock_guard lock(context->mutex);
    if (!context->engine) {
      context->lastError = "codec engine is not prepared";
      return GLIC_CODEC_GLITCH_NOT_PREPARED;
    }
    std::string error;
    if (!context->engine->reset(error)) {
      context->lastError = std::move(error);
      return GLIC_CODEC_GLITCH_PROCESSING_FAILED;
    }
    context->lastError.clear();
    return GLIC_CODEC_GLITCH_OK;
  });
}

glic_codec_glitch_status
glic_codec_glitch_get_stats(const glic_codec_glitch_context *context,
                            glic_codec_glitch_stats *stats) {
  auto *mutableContext = const_cast<glic_codec_glitch_context *>(context);
  return guardStatus(mutableContext, [&]() -> glic_codec_glitch_status {
    if (context == nullptr || stats == nullptr ||
        !validHeader(stats->struct_size, stats->abi_version, sizeof(*stats)))
      return GLIC_CODEC_GLITCH_INVALID_ARGUMENT;
    std::lock_guard lock(context->mutex);
    if (!context->engine)
      return GLIC_CODEC_GLITCH_NOT_PREPARED;
    const auto source = context->engine->stats();
    stats->submitted_frames = source.submittedFrames;
    stats->encoded_frames = source.encodedFrames;
    stats->decoded_frames = source.decodedFrames;
    stats->emitted_frames = source.emittedFrames;
    stats->backpressure_drops = source.backpressureDrops;
    stats->intentional_packet_drops = source.intentionalPacketDrops;
    stats->codec_errors = source.codecErrors;
    stats->watchdog_recoveries = source.watchdogRecoveries;
    stats->poll_queue_drops = source.pollQueueDrops;
    stats->last_latency_milliseconds = source.lastLatencyMilliseconds;
    stats->average_latency_milliseconds = source.averageLatencyMilliseconds;
    stats->hardware_encoder = source.hardwareEncoder ? 1u : 0u;
    stats->hardware_decoder = source.hardwareDecoder ? 1u : 0u;
    stats->base_frame_qp_supported = source.baseFrameQpSupported ? 1u : 0u;
    return GLIC_CODEC_GLITCH_OK;
  });
}

const char *
glic_codec_glitch_get_last_error(const glic_codec_glitch_context *context) {
  if (context == nullptr)
    return "context is null";
  try {
    thread_local std::string snapshot;
    std::lock_guard lock(context->mutex);
    snapshot = context->lastError;
    return snapshot.c_str();
  } catch (...) {
    return "unable to copy the codec glitch error";
  }
}

} // extern "C"
