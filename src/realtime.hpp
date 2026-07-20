#pragma once

#include "config.hpp"

#include <cstdint>
#include <memory>
#include <span>
#include <string>

namespace glic {

enum class RealtimeBackendKind : uint8_t { AUTO = 0, CPU = 1, METAL = 2 };

struct RealtimePrepareOptions {
  int width = 0;
  int height = 0;
  CodecConfig config{};
  uint32_t seed = 0x474C4943u;
};

struct RealtimeFrameStats {
  double gpuMilliseconds = 0.0;
  uint64_t frameIndex = 0;
};

// Visual-only realtime processing backend. Unlike GlicCodec, this API does not
// serialize a .glic stream and immediately decode it again. It applies the
// preset's prediction, quantization and transform character directly to a
// frame. prepare() owns all resolution-dependent allocation; process() is
// allocation-free in the CPU backend after warm-up.
class RealtimeBackend {
public:
  virtual ~RealtimeBackend() = default;

  virtual bool prepare(const RealtimePrepareOptions &options,
                       std::string &error) = 0;
  virtual bool process(std::span<const Color> input, std::span<Color> output,
                       uint64_t frameIndex, std::string &error) = 0;

  // Opaque MTLTexture bridge for zero-copy integrations. The pointers must be
  // id<MTLTexture> objects bridged without transferring ownership. CPU
  // backends return false.
  virtual bool processTextures(void *inputTexture, void *outputTexture,
                               uint64_t frameIndex, std::string &error) {
    (void)inputTexture;
    (void)outputTexture;
    (void)frameIndex;
    error = "Texture interop is not supported by this backend";
    return false;
  }

  // Non-blocking Metal integration. commandBuffer must be an uncommitted
  // id<MTLCommandBuffer>. The backend appends its compute encoder but does not
  // commit or wait. Keep at most three frames in flight so the uniform ring is
  // not overwritten. CPU backends return false.
  virtual bool encodeTextures(void *commandBuffer, void *inputTexture,
                              void *outputTexture, uint64_t frameIndex,
                              std::string &error) {
    (void)commandBuffer;
    (void)inputTexture;
    (void)outputTexture;
    (void)frameIndex;
    error = "Asynchronous texture encoding is not supported by this backend";
    return false;
  }

  [[nodiscard]] virtual const char *name() const noexcept = 0;
  [[nodiscard]] virtual bool isHardwareAccelerated() const noexcept = 0;
  [[nodiscard]] virtual RealtimeFrameStats lastFrameStats() const noexcept = 0;
};

std::unique_ptr<RealtimeBackend>
createRealtimeBackend(RealtimeBackendKind requested, std::string &error);

RealtimeBackendKind realtimeBackendKindFromName(const std::string &name);
const char *realtimeBackendKindName(RealtimeBackendKind kind) noexcept;

} // namespace glic
