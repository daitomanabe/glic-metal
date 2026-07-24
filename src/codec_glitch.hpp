#pragma once

#if !defined(__APPLE__)
#error "CodecGlitchEngine requires macOS VideoToolbox"
#endif

#include <CoreMedia/CoreMedia.h>
#include <CoreVideo/CoreVideo.h>

#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <string_view>

namespace glic {

enum class CodecGlitchCodec : uint32_t {
  H264 = 0,
  HEVC,
  ProRes422,
  Count,
};

const char *codecGlitchCodecName(CodecGlitchCodec codec) noexcept;
bool codecGlitchCodecFromName(std::string_view name,
                              CodecGlitchCodec &codec) noexcept;

// Stateful effects backed by a valid VideoToolbox encode -> decode loop,
// intentional
// encoded-frame holds, and Metal-backed post-decode transforms. These are
// separate from one-input/one-output image effects because codec effects keep
// reference frames and are necessarily asynchronous.
enum class CodecGlitchEffect : uint32_t {
  QpPump = 0,
  BitrateCrush,
  SliceDropout,
  SliceTransplant,
  PFrameLoss,
  IdrStarvation,
  PayloadXor,
  ReferenceTimewarp,
  CodecFeedback,
  GenerationCascade,
  ResolutionHop,
  ChromaCodecEcho,
  TemporalPolyphony,
  IntraCannibalism,
  ResidualRift,
  CodecGrainSynth,
  RecursiveCodecSkin,
  ConcealmentChoreography,
  DualCodecCrossbreed,
  CodecPingpong,
  GopAccordion,
  BframeBraid,
  PlaneSplitCodec,
  RoiQualityIslands,
  CodecPhaseMosaic,
  EncoderHotSwap,
  PtsRubberband,
  BitrateRaster,
  PlaneTimeSplit,
  ReferenceAtlas,
  FlowLattice,
  ScanOrderFold,
  RegionalGopClock,
  EntropyFeedback,
  RollingTimeShutter,
  AsymmetricPlaneCodec,
  Count,
};

const char *codecGlitchEffectName(CodecGlitchEffect effect) noexcept;
const char *
codecGlitchEffectImplementationLevel(CodecGlitchEffect effect) noexcept;
bool codecGlitchEffectFromName(std::string_view name,
                               CodecGlitchEffect &effect) noexcept;

struct CodecGlitchControls {
  CodecGlitchEffect effect = CodecGlitchEffect::BitrateCrush;

  // Generic normalized controls.  amount controls damage/mix, rate controls
  // temporal frequency, and feedback controls history contribution.
  float amount = 0.55f;
  float rate = 0.35f;
  float feedback = 0.60f;
  uint64_t seed = 0x474c4943434f4445ULL;

  // Effect-specific bounds.  Values outside documented ranges are clamped by
  // setControls/submit so live UI automation cannot destabilize VideoToolbox.
  int minimumQp = 18;
  int maximumQp = 51;
  int crushedBitRate = 120000;
  int cascadeGenerations = 3;           // [2, 3]
  float reducedResolutionScale = 0.25f; // [0.25, 0.5]
};

struct CodecGlitchConfiguration {
  CodecGlitchCodec codec = CodecGlitchCodec::H264;
  int width = 960;
  int height = 540;
  int framesPerSecond = 30;
  int averageBitRate = 4000000;
  int keyFrameInterval = 60;
  // Optional H.264 packet-size preference. Other codecs ignore it; correctness
  // and the slice effects never depend on physical compressed-frame splitting.
  int maximumSliceBytes = 4000;
  int decodedHistoryFrames = 12;
  int maximumInFlightFrames = 24;
  int pollQueueCapacity = 8;
  bool requireHardwareEncoder = true;
  bool requireHardwareDecoder = true;
  bool enableLowLatencyRateControl = true;
};

struct CodecGlitchStatistics {
  uint64_t submittedFrames = 0;
  uint64_t encodedFrames = 0;
  uint64_t decodedFrames = 0;
  uint64_t emittedFrames = 0;
  uint64_t backpressureDrops = 0;
  uint64_t intentionalPacketDrops = 0;
  uint64_t codecErrors = 0;
  uint64_t watchdogRecoveries = 0;
  uint64_t pollQueueDrops = 0;
  double lastLatencyMilliseconds = 0.0;
  double averageLatencyMilliseconds = 0.0;
  bool hardwareEncoder = false;
  bool hardwareDecoder = false;
  bool baseFrameQpSupported = false;
};

// Owns one retain on its pixel buffer.  Copying a frame retains the buffer;
// moving transfers it.  This makes both callback and polling use safe without
// requiring Objective-C ownership rules in the host application.
class CodecGlitchFrame {
public:
  CodecGlitchFrame() noexcept = default;
  // Retains pixelBuffer.  Hosts normally receive frames from the engine, but
  // this constructor is public so adapter layers can preserve the same RAII
  // ownership contract without Objective-C bridging helpers.
  explicit CodecGlitchFrame(CVPixelBufferRef pixelBuffer) noexcept;
  CodecGlitchFrame(const CodecGlitchFrame &other) noexcept;
  CodecGlitchFrame &operator=(const CodecGlitchFrame &other) noexcept;
  CodecGlitchFrame(CodecGlitchFrame &&other) noexcept;
  CodecGlitchFrame &operator=(CodecGlitchFrame &&other) noexcept;
  ~CodecGlitchFrame();

  CVPixelBufferRef pixelBuffer() const noexcept { return pixelBuffer_; }
  explicit operator bool() const noexcept { return pixelBuffer_ != nullptr; }

  uint64_t frameIndex = 0;
  CMTime presentationTimeStamp = kCMTimeInvalid;
  CodecGlitchEffect effect = CodecGlitchEffect::BitrateCrush;
  bool packetWasModified = false;
  bool repeatedPreviousFrame = false;
  bool intentionalRepeat = false;
  bool nonIntentionalFallback = false;
  bool codecWarmupFrame = false;
  bool watchdogRecoveryFrame = false;
  double latencyMilliseconds = 0.0;

private:
  CVPixelBufferRef pixelBuffer_ = nullptr;
};

using CodecGlitchOutputCallback =
    std::function<void(const CodecGlitchFrame &frame)>;

class CodecGlitchEngine {
public:
  virtual ~CodecGlitchEngine() = default;

  // Thread-safe and non-blocking apart from a short dispatch/pool operation.
  // The caller only needs to keep input alive for this call; the engine takes
  // a retain before handing it to its serial encode queue.
  virtual bool submit(CVPixelBufferRef input, uint64_t frameIndex,
                      CMTime presentationTimeStamp, std::string &error) = 0;

  virtual void setControls(const CodecGlitchControls &controls) = 0;
  virtual CodecGlitchControls controls() const = 0;

  // If a callback is installed, decoded frames are delivered on a private
  // serial callback queue.  With no callback they enter the bounded poll ring.
  virtual void setOutputCallback(CodecGlitchOutputCallback callback) = 0;
  virtual bool poll(CodecGlitchFrame &frame) = 0;

  virtual bool flush(std::chrono::milliseconds timeout, std::string &error) = 0;
  virtual bool reset(std::string &error) = 0;
  virtual CodecGlitchStatistics stats() const noexcept = 0;
};

std::unique_ptr<CodecGlitchEngine>
createCodecGlitchEngine(const CodecGlitchConfiguration &configuration,
                        std::string &error);

} // namespace glic
