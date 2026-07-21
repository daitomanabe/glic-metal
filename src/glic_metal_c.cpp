#include <glic_metal/glic_metal.h>

#include "original_realtime.hpp"
#include "original_realtime_metal.hpp"
#include "preset_loader.hpp"
#include "realtime.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <limits>
#include <memory>
#include <new>
#include <sstream>
#include <span>
#include <string>
#include <utility>
#include <vector>

#ifndef GLIC_METAL_VERSION_STRING
#define GLIC_METAL_VERSION_STRING "development"
#endif

namespace {

using Clock = std::chrono::steady_clock;

enum class ActiveMode { None, Compat, OriginalCpu, OriginalMetal };

struct EngineState {
  std::unique_ptr<glic::RealtimeBackend> compat;
  std::unique_ptr<glic::OriginalRealtimeCpuLane> originalCpu;
  std::unique_ptr<glic::OriginalRealtimeMetalLane> originalMetal;
  std::vector<glic::Color> inputPixels;
  std::vector<glic::Color> outputPixels;
  std::string backendName;
  ActiveMode mode = ActiveMode::None;
  int width = 0;
  int height = 0;
  bool hardwareAccelerated = false;
};

double elapsedMilliseconds(Clock::time_point start, Clock::time_point stop) {
  return std::chrono::duration<double, std::milli>(stop - start).count();
}

glic::RealtimeBackendKind backendKind(glic_metal_backend backend) {
  switch (backend) {
  case GLIC_METAL_BACKEND_CPU:
    return glic::RealtimeBackendKind::CPU;
  case GLIC_METAL_BACKEND_METAL:
    return glic::RealtimeBackendKind::METAL;
  default:
    return glic::RealtimeBackendKind::AUTO;
  }
}

bool isValidPresetName(const char *name) {
  if (name == nullptr || name[0] == '\0')
    return false;
  const std::filesystem::path path(name);
  return path == path.filename() && path != "." && path != "..";
}

std::string supportDiagnostic(const glic::OriginalRealtimeSupport &support) {
  std::ostringstream stream;
  stream << "preset is unavailable in original mode";
  for (const auto &reason : support.reasons)
    stream << (stream.tellp() > 0 ? ": " : "") << reason;
  return stream.str();
}

bool validConfigHeader(const glic_metal_config *config) {
  return config != nullptr && config->struct_size >= sizeof(glic_metal_config) &&
         config->abi_version == GLIC_METAL_ABI_VERSION;
}

bool validStatsHeader(const glic_metal_frame_stats *stats) {
  return stats != nullptr &&
         stats->struct_size >= sizeof(glic_metal_frame_stats) &&
         stats->abi_version == GLIC_METAL_ABI_VERSION;
}

} // namespace

struct glic_metal_context {
  EngineState engine;
  glic_metal_frame_stats lastStats{};
  std::string lastError;
};

namespace {

glic_metal_status fail(glic_metal_context *context, glic_metal_status status,
                       std::string message) {
  if (context != nullptr)
    context->lastError = std::move(message);
  return status;
}

void clearError(glic_metal_context *context) {
  if (context != nullptr)
    context->lastError.clear();
}

void resetStats(glic_metal_context *context, uint64_t frameIndex) {
  glic_metal_frame_stats_init(&context->lastStats);
  context->lastStats.frame_index = frameIndex;
  context->lastStats.hardware_accelerated =
      context->engine.hardwareAccelerated ? 1u : 0u;
}

void readFrame(const EngineState &engine, const void *input,
               size_t inputBytesPerRow, glic_metal_pixel_format pixelFormat,
               std::vector<glic::Color> &pixels) {
  const auto *source = static_cast<const uint8_t *>(input);
  for (int y = 0; y < engine.height; ++y) {
    const uint8_t *row = source + static_cast<size_t>(y) * inputBytesPerRow;
    for (int x = 0; x < engine.width; ++x) {
      const uint8_t *pixel = row + static_cast<size_t>(x) * 4u;
      const size_t index = static_cast<size_t>(y) * engine.width + x;
      if (pixelFormat == GLIC_METAL_PIXEL_FORMAT_BGRA8) {
        pixels[index] = glic::makeColor(pixel[2], pixel[1], pixel[0], pixel[3]);
      } else {
        pixels[index] = glic::makeColor(pixel[0], pixel[1], pixel[2], pixel[3]);
      }
    }
  }
}

void writeFrame(const EngineState &engine,
                const std::vector<glic::Color> &pixels, void *output,
                size_t outputBytesPerRow,
                glic_metal_pixel_format pixelFormat) {
  auto *destination = static_cast<uint8_t *>(output);
  for (int y = 0; y < engine.height; ++y) {
    uint8_t *row = destination + static_cast<size_t>(y) * outputBytesPerRow;
    for (int x = 0; x < engine.width; ++x) {
      const size_t index = static_cast<size_t>(y) * engine.width + x;
      const glic::Color color = pixels[index];
      uint8_t *pixel = row + static_cast<size_t>(x) * 4u;
      if (pixelFormat == GLIC_METAL_PIXEL_FORMAT_BGRA8) {
        pixel[0] = glic::getB(color);
        pixel[1] = glic::getG(color);
        pixel[2] = glic::getR(color);
      } else {
        pixel[0] = glic::getR(color);
        pixel[1] = glic::getG(color);
        pixel[2] = glic::getB(color);
      }
      pixel[3] = glic::getA(color);
    }
  }
}

glic_metal_status prepareCompat(glic_metal_context *context,
                                const glic_metal_config &config,
                                const glic::OriginalPresetConfig &original,
                                EngineState &next) {
  glic::PresetMappingInfo mapping;
  const glic::CodecConfig projected =
      glic::PresetLoader::projectOriginalPresetToRealtime(original, &mapping);
  (void)mapping;

  glic::RealtimeBackendCreateOptions createOptions;
  createOptions.metalDevice = config.metal_device;
  if (config.metal_library_path != nullptr)
    createOptions.metalLibraryPath = config.metal_library_path;

  next.compat = glic::createRealtimeBackend(
      backendKind(config.backend), createOptions, context->lastError);
  if (!next.compat)
    return GLIC_METAL_BACKEND_UNAVAILABLE;

  glic::RealtimePrepareOptions options;
  options.width = config.width;
  options.height = config.height;
  options.config = projected;
  options.seed = config.seed;
  options.effectStrength = std::clamp(config.effect_strength, 0.0f, 2.0f);
  options.effect.family =
      static_cast<glic::RealtimeEffectFamily>(config.effect_family);
  options.effect.amount = std::clamp(config.effect_amount, 0.0f, 1.0f);
  options.effect.scale = std::clamp(config.effect_scale, 0.0f, 1.0f);
  options.effect.rate = std::clamp(config.effect_rate, 0.0f, 1.0f);
  if (!next.compat->prepare(options, context->lastError))
    return GLIC_METAL_PROCESSING_FAILED;

  next.mode = ActiveMode::Compat;
  next.backendName = next.compat->name();
  next.hardwareAccelerated = next.compat->isHardwareAccelerated();
  return GLIC_METAL_OK;
}

glic_metal_status prepareOriginal(glic_metal_context *context,
                                  const glic_metal_config &config,
                                  const glic::OriginalPresetConfig &original,
                                  EngineState &next) {
  const auto support = glic::evaluateOriginalRealtimeSupport(original);
  if (!support.supported)
    return fail(context, GLIC_METAL_UNSUPPORTED,
                supportDiagnostic(support));

  const bool fastMatch = config.fidelity == GLIC_METAL_FIDELITY_FAST_MATCH;
  if (config.backend != GLIC_METAL_BACKEND_CPU) {
    glic::OriginalRealtimeMetalOptions options;
    options.fidelity = fastMatch
                           ? glic::OriginalRealtimeMetalFidelity::FastMatch
                           : glic::OriginalRealtimeMetalFidelity::Strict;
    options.segmentationReuseFrames =
        std::max<uint32_t>(1u, config.segmentation_reuse_frames);
    options.metalDevice = config.metal_device;
    if (config.metal_library_path != nullptr)
      options.metalLibraryPath = config.metal_library_path;
    next.originalMetal =
        glic::createOriginalRealtimeMetalLane(options, context->lastError);
    if (next.originalMetal &&
        next.originalMetal->prepare(config.width, config.height, original,
                                    context->lastError)) {
      next.mode = ActiveMode::OriginalMetal;
      next.backendName = next.originalMetal->name();
      next.hardwareAccelerated = true;
      return GLIC_METAL_OK;
    }
    next.originalMetal.reset();
    if (config.backend == GLIC_METAL_BACKEND_METAL || fastMatch)
      return GLIC_METAL_BACKEND_UNAVAILABLE;
  }

  if (fastMatch)
    return fail(context, GLIC_METAL_UNSUPPORTED,
                "Fast Match requires the Metal backend");
  next.originalCpu = std::make_unique<glic::OriginalRealtimeCpuLane>();
  if (!next.originalCpu->prepare(config.width, config.height, original,
                                 context->lastError))
    return GLIC_METAL_PROCESSING_FAILED;
  next.mode = ActiveMode::OriginalCpu;
  next.backendName = "cpu-original";
  next.hardwareAccelerated = false;
  return GLIC_METAL_OK;
}

} // namespace

extern "C" {

uint32_t glic_metal_get_abi_version(void) { return GLIC_METAL_ABI_VERSION; }

const char *glic_metal_get_version_string(void) {
  return GLIC_METAL_VERSION_STRING;
}

const char *glic_metal_status_string(glic_metal_status status) {
  switch (status) {
  case GLIC_METAL_OK:
    return "ok";
  case GLIC_METAL_INVALID_ARGUMENT:
    return "invalid argument";
  case GLIC_METAL_NOT_PREPARED:
    return "not prepared";
  case GLIC_METAL_PRESET_NOT_FOUND:
    return "preset not found";
  case GLIC_METAL_UNSUPPORTED:
    return "unsupported";
  case GLIC_METAL_BACKEND_UNAVAILABLE:
    return "backend unavailable";
  case GLIC_METAL_PROCESSING_FAILED:
    return "processing failed";
  default:
    return "internal error";
  }
}

void glic_metal_config_init(glic_metal_config *config) {
  if (config == nullptr)
    return;
  *config = {};
  config->struct_size = sizeof(*config);
  config->abi_version = GLIC_METAL_ABI_VERSION;
  config->width = 960;
  config->height = 540;
  config->preset_directory = "presets";
  config->preset_name = "vv02";
  config->backend = GLIC_METAL_BACKEND_AUTO;
  config->mode = GLIC_METAL_MODE_ORIGINAL;
  config->fidelity = GLIC_METAL_FIDELITY_STRICT;
  config->segmentation_reuse_frames = 1;
  config->seed = 0x474C4943u;
  config->effect_strength = 1.0f;
  config->effect_family = GLIC_METAL_EFFECT_LEGACY_BLOCK;
  config->effect_amount = 0.7f;
  config->effect_scale = 0.5f;
  config->effect_rate = 0.5f;
}

void glic_metal_frame_stats_init(glic_metal_frame_stats *stats) {
  if (stats == nullptr)
    return;
  *stats = {};
  stats->struct_size = sizeof(*stats);
  stats->abi_version = GLIC_METAL_ABI_VERSION;
}

glic_metal_status glic_metal_context_create(glic_metal_context **context) {
  if (context == nullptr)
    return GLIC_METAL_INVALID_ARGUMENT;
  *context = nullptr;
  try {
    auto candidate = std::make_unique<glic_metal_context>();
    glic_metal_frame_stats_init(&candidate->lastStats);
    *context = candidate.release();
    return GLIC_METAL_OK;
  } catch (const std::bad_alloc &) {
    return GLIC_METAL_INTERNAL_ERROR;
  } catch (...) {
    return GLIC_METAL_INTERNAL_ERROR;
  }
}

void glic_metal_context_destroy(glic_metal_context *context) { delete context; }

glic_metal_status glic_metal_prepare(glic_metal_context *context,
                                     const glic_metal_config *config) {
  if (context == nullptr || !validConfigHeader(config))
    return fail(context, GLIC_METAL_INVALID_ARGUMENT,
                "config is null, undersized, or uses an unsupported ABI");
  if (config->width <= 0 || config->height <= 0 ||
      config->preset_directory == nullptr ||
      config->preset_directory[0] == '\0' ||
      !isValidPresetName(config->preset_name))
    return fail(context, GLIC_METAL_INVALID_ARGUMENT,
                "dimensions, preset directory, or preset name are invalid");
  if (config->backend < GLIC_METAL_BACKEND_AUTO ||
      config->backend > GLIC_METAL_BACKEND_METAL ||
      config->mode < GLIC_METAL_MODE_COMPAT_REALTIME ||
      config->mode > GLIC_METAL_MODE_ORIGINAL ||
      config->fidelity < GLIC_METAL_FIDELITY_STRICT ||
      config->fidelity > GLIC_METAL_FIDELITY_FAST_MATCH ||
      config->effect_family < GLIC_METAL_EFFECT_LEGACY_BLOCK ||
      config->effect_family > GLIC_METAL_EFFECT_POSTER_SOLAR ||
      !std::isfinite(config->effect_strength) ||
      !std::isfinite(config->effect_amount) ||
      !std::isfinite(config->effect_scale) ||
      !std::isfinite(config->effect_rate))
    return fail(context, GLIC_METAL_INVALID_ARGUMENT,
                "config contains an invalid enum or non-finite value");

  const uint64_t pixelCount64 = static_cast<uint64_t>(config->width) *
                                static_cast<uint64_t>(config->height);
  if (pixelCount64 >
      std::numeric_limits<size_t>::max() / sizeof(glic::Color))
    return fail(context, GLIC_METAL_INVALID_ARGUMENT,
                "frame dimensions are too large");

  try {
    glic::OriginalPresetConfig original;
    if (!glic::PresetLoader::loadOriginalPresetByName(
            config->preset_directory, config->preset_name, original))
      return fail(context, GLIC_METAL_PRESET_NOT_FOUND,
                  std::string("could not load preset: ") +
                      config->preset_name);

    EngineState next;
    next.width = config->width;
    next.height = config->height;
    const size_t pixelCount = static_cast<size_t>(pixelCount64);
    next.inputPixels.resize(pixelCount);
    next.outputPixels.resize(pixelCount);

    glic_metal_status status = GLIC_METAL_INTERNAL_ERROR;
    if (config->mode == GLIC_METAL_MODE_COMPAT_REALTIME)
      status = prepareCompat(context, *config, original, next);
    else
      status = prepareOriginal(context, *config, original, next);
    if (status != GLIC_METAL_OK)
      return status;

    context->engine = std::move(next);
    resetStats(context, 0);
    clearError(context);
    return GLIC_METAL_OK;
  } catch (const std::exception &exception) {
    return fail(context, GLIC_METAL_INTERNAL_ERROR, exception.what());
  } catch (...) {
    return fail(context, GLIC_METAL_INTERNAL_ERROR,
                "unknown exception while preparing the engine");
  }
}

glic_metal_status glic_metal_process_frame(
    glic_metal_context *context, const void *input, size_t inputBytesPerRow,
    void *output, size_t outputBytesPerRow,
    glic_metal_pixel_format pixelFormat, uint64_t frameIndex) try {
  if (context == nullptr)
    return GLIC_METAL_INVALID_ARGUMENT;
  if (context->engine.mode == ActiveMode::None)
    return fail(context, GLIC_METAL_NOT_PREPARED,
                "context has not been prepared");
  const size_t minimumRowBytes =
      static_cast<size_t>(context->engine.width) * 4u;
  if (input == nullptr || output == nullptr ||
      inputBytesPerRow < minimumRowBytes ||
      outputBytesPerRow < minimumRowBytes ||
      (pixelFormat != GLIC_METAL_PIXEL_FORMAT_BGRA8 &&
       pixelFormat != GLIC_METAL_PIXEL_FORMAT_RGBA8))
    return fail(context, GLIC_METAL_INVALID_ARGUMENT,
                "frame pointers, row bytes, or pixel format are invalid");

  const auto totalStart = Clock::now();
  readFrame(context->engine, input, inputBytesPerRow, pixelFormat,
            context->engine.inputPixels);
  resetStats(context, frameIndex);

  bool processed = false;
  if (context->engine.mode == ActiveMode::Compat) {
    processed = context->engine.compat->process(
        context->engine.inputPixels, context->engine.outputPixels, frameIndex,
        context->lastError);
    if (processed) {
      const auto stats = context->engine.compat->lastFrameStats();
      context->lastStats.gpu_milliseconds = stats.gpuMilliseconds;
    }
  } else if (context->engine.mode == ActiveMode::OriginalMetal) {
    glic::OriginalRealtimeMetalFrameStats stats;
    processed = context->engine.originalMetal->process(
        context->engine.inputPixels, context->engine.outputPixels, frameIndex,
        &stats, context->lastError);
    if (processed) {
      context->lastStats.total_segments = stats.totalSegments;
      context->lastStats.gpu_milliseconds = stats.gpuMilliseconds;
      context->lastStats.cpu_prepare_milliseconds =
          stats.cpuPrepareMilliseconds;
      context->lastStats.cpu_output_milliseconds = stats.cpuOutputMilliseconds;
      context->lastStats.segmentation_reused =
          (stats.staticScheduleReused || stats.adaptiveScheduleReused) ? 1u
                                                                      : 0u;
    }
  } else {
    glic::OriginalRealtimeFrameStats stats;
    processed = context->engine.originalCpu->process(
        context->engine.inputPixels, context->engine.outputPixels, &stats,
        context->lastError);
    if (processed) {
      context->lastStats.total_segments = stats.segmentCounts[0] +
                                          stats.segmentCounts[1] +
                                          stats.segmentCounts[2];
    }
  }
  if (!processed)
    return GLIC_METAL_PROCESSING_FAILED;

  const auto outputStart = Clock::now();
  writeFrame(context->engine, context->engine.outputPixels, output,
             outputBytesPerRow, pixelFormat);
  const auto stop = Clock::now();
  context->lastStats.cpu_output_milliseconds +=
      elapsedMilliseconds(outputStart, stop);
  context->lastStats.total_milliseconds =
      elapsedMilliseconds(totalStart, stop);
  clearError(context);
  return GLIC_METAL_OK;
} catch (const std::exception &exception) {
  return fail(context, GLIC_METAL_INTERNAL_ERROR, exception.what());
} catch (...) {
  return fail(context, GLIC_METAL_INTERNAL_ERROR,
              "unknown exception while processing a frame");
}

glic_metal_status glic_metal_process_metal_textures(
    glic_metal_context *context, void *inputTexture, void *outputTexture,
    uint64_t frameIndex) try {
  if (context == nullptr)
    return GLIC_METAL_INVALID_ARGUMENT;
  if (context->engine.mode == ActiveMode::None)
    return fail(context, GLIC_METAL_NOT_PREPARED,
                "context has not been prepared");
  if (context->engine.mode != ActiveMode::Compat ||
      !context->engine.hardwareAccelerated)
    return fail(context, GLIC_METAL_UNSUPPORTED,
                "texture interop requires compatibility mode with Metal");
  const auto start = Clock::now();
  if (!context->engine.compat->processTextures(
          inputTexture, outputTexture, frameIndex, context->lastError))
    return GLIC_METAL_PROCESSING_FAILED;
  resetStats(context, frameIndex);
  const auto stats = context->engine.compat->lastFrameStats();
  context->lastStats.gpu_milliseconds = stats.gpuMilliseconds;
  context->lastStats.total_milliseconds =
      elapsedMilliseconds(start, Clock::now());
  clearError(context);
  return GLIC_METAL_OK;
} catch (const std::exception &exception) {
  return fail(context, GLIC_METAL_INTERNAL_ERROR, exception.what());
} catch (...) {
  return fail(context, GLIC_METAL_INTERNAL_ERROR,
              "unknown exception while processing Metal textures");
}

glic_metal_status glic_metal_encode_metal_textures(
    glic_metal_context *context, void *commandBuffer, void *inputTexture,
    void *outputTexture, uint64_t frameIndex) try {
  if (context == nullptr)
    return GLIC_METAL_INVALID_ARGUMENT;
  if (context->engine.mode == ActiveMode::None)
    return fail(context, GLIC_METAL_NOT_PREPARED,
                "context has not been prepared");
  if (context->engine.mode != ActiveMode::Compat ||
      !context->engine.hardwareAccelerated)
    return fail(context, GLIC_METAL_UNSUPPORTED,
                "texture encoding requires compatibility mode with Metal");
  const auto start = Clock::now();
  if (!context->engine.compat->encodeTextures(
          commandBuffer, inputTexture, outputTexture, frameIndex,
          context->lastError))
    return GLIC_METAL_PROCESSING_FAILED;
  resetStats(context, frameIndex);
  context->lastStats.total_milliseconds =
      elapsedMilliseconds(start, Clock::now());
  clearError(context);
  return GLIC_METAL_OK;
} catch (const std::exception &exception) {
  return fail(context, GLIC_METAL_INTERNAL_ERROR, exception.what());
} catch (...) {
  return fail(context, GLIC_METAL_INTERNAL_ERROR,
              "unknown exception while encoding Metal textures");
}

glic_metal_status glic_metal_get_last_stats(
    const glic_metal_context *context, glic_metal_frame_stats *stats) {
  if (context == nullptr || !validStatsHeader(stats))
    return GLIC_METAL_INVALID_ARGUMENT;
  *stats = context->lastStats;
  return GLIC_METAL_OK;
}

const char *glic_metal_get_last_error(const glic_metal_context *context) {
  return context == nullptr ? "context is null" : context->lastError.c_str();
}

const char *glic_metal_get_active_backend(const glic_metal_context *context) {
  if (context == nullptr || context->engine.backendName.empty())
    return "unprepared";
  return context->engine.backendName.c_str();
}

int32_t
glic_metal_is_hardware_accelerated(const glic_metal_context *context) {
  return context != nullptr && context->engine.hardwareAccelerated ? 1 : 0;
}

glic_metal_status glic_metal_enumerate_presets(
    const char *presetDirectory, glic_metal_preset_callback callback,
    void *userData) {
  if (presetDirectory == nullptr || presetDirectory[0] == '\0' ||
      callback == nullptr)
    return GLIC_METAL_INVALID_ARGUMENT;
  try {
    const auto presets = glic::PresetLoader::listPresets(presetDirectory);
    if (presets.empty())
      return GLIC_METAL_PRESET_NOT_FOUND;
    for (const auto &preset : presets)
      callback(preset.c_str(), userData);
    return GLIC_METAL_OK;
  } catch (...) {
    return GLIC_METAL_INTERNAL_ERROR;
  }
}

} // extern "C"
