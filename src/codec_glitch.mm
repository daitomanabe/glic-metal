#include "codec_glitch.hpp"

#import <CoreImage/CoreImage.h>
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <VideoToolbox/VideoToolbox.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <condition_variable>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <limits>
#include <mutex>
#include <optional>
#include <span>
#include <utility>
#include <vector>

namespace glic {
namespace {

constexpr int kStageNormal = 0;
constexpr int kStageQp = 1;
constexpr int kStageCascadeSecond = 2;
constexpr int kStageCascadeThird = 3;
constexpr int kStageLowQuarter = 4;
constexpr int kStageLowHalf = 5;
constexpr int kStageCount = 6;
constexpr uint64_t kHashMultiplier = 0x9e3779b97f4a7c15ULL;
constexpr int kWatchdogRecoveryThreshold = 6;
constexpr auto kDecodeDeadline = std::chrono::milliseconds(45);
constexpr auto kDecodeWarmupDeadline = std::chrono::milliseconds(300);
// Hardware session warm-up can exceed one 20 fps frame even though sustained
// throughput is far higher. Keep hang recovery bounded without misclassifying
// a one-time encoder start as a dropped frame; the realtime gate separately
// enforces p95 <= 50 ms and zero fallback.
constexpr auto kEncodeDeadline = std::chrono::milliseconds(100);
constexpr auto kEncodeWarmupDeadline = std::chrono::milliseconds(500);
char kCodecEncodeQueueKey;
char kCodecCallbackQueueKey;

template <typename T> T clampValue(T value, T lower, T upper) {
  return std::min(upper, std::max(lower, value));
}

uint64_t mixHash(uint64_t value) noexcept {
  value += kHashMultiplier;
  value = (value ^ (value >> 30U)) * 0xbf58476d1ce4e5b9ULL;
  value = (value ^ (value >> 27U)) * 0x94d049bb133111ebULL;
  return value ^ (value >> 31U);
}

double hashUnit(uint64_t value) noexcept {
  return static_cast<double>(mixHash(value) >> 11U) *
         (1.0 / 9007199254740992.0);
}

double temporalWave(uint64_t frameIndex, float rate) noexcept {
  constexpr double kTwoPi = 6.28318530717958647692;
  const double cyclesPerFrame = 0.0025 + 0.08 * clampValue(rate, 0.0f, 1.0f);
  return 0.5 + 0.5 * std::sin(static_cast<double>(frameIndex) * cyclesPerFrame *
                              kTwoPi);
}

CodecGlitchControls sanitizedControls(CodecGlitchControls controls) {
  if (static_cast<uint32_t>(controls.effect) >=
      static_cast<uint32_t>(CodecGlitchEffect::Count))
    controls.effect = CodecGlitchEffect::BitrateCrush;
  controls.amount = clampValue(controls.amount, 0.0f, 1.0f);
  controls.rate = clampValue(controls.rate, 0.0f, 1.0f);
  controls.feedback = clampValue(controls.feedback, 0.0f, 0.98f);
  controls.minimumQp = clampValue(controls.minimumQp, 0, 51);
  controls.maximumQp = clampValue(controls.maximumQp, 0, 51);
  if (controls.minimumQp > controls.maximumQp)
    std::swap(controls.minimumQp, controls.maximumQp);
  controls.crushedBitRate =
      clampValue(controls.crushedBitRate, 16000, 100000000);
  controls.cascadeGenerations = clampValue(controls.cascadeGenerations, 2, 3);
  controls.reducedResolutionScale =
      controls.reducedResolutionScale < 0.375f ? 0.25f : 0.5f;
  return controls;
}

std::string statusError(const char *operation, OSStatus status) {
  return std::string(operation) + " failed with OSStatus " +
         std::to_string(static_cast<long long>(status));
}

void setSessionProperty(VTSessionRef session, CFStringRef key,
                        CFTypeRef value) {
  if (session != nullptr && key != nullptr && value != nullptr)
    (void)VTSessionSetProperty(session, key, value);
}

void setSessionInt(VTSessionRef session, CFStringRef key, int value) {
  int32_t narrowed = static_cast<int32_t>(value);
  CFNumberRef number =
      CFNumberCreate(kCFAllocatorDefault, kCFNumberSInt32Type, &narrowed);
  setSessionProperty(session, key, number);
  CFRelease(number);
}

bool copySessionBool(VTSessionRef session, CFStringRef key) {
  CFTypeRef value = nullptr;
  const OSStatus status =
      VTSessionCopyProperty(session, key, kCFAllocatorDefault, &value);
  const bool result = status == noErr && value == kCFBooleanTrue;
  if (value != nullptr)
    CFRelease(value);
  return result;
}

int evenDimension(double value) {
  const int rounded = std::max(2, static_cast<int>(std::lround(value)));
  return rounded & ~1;
}

int64_t steadyNanoseconds() noexcept {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

struct NalSpan {
  size_t lengthOffset = 0;
  size_t payloadOffset = 0;
  size_t payloadSize = 0;
  uint8_t type = 0;

  bool isVideoSlice() const noexcept { return type == 1 || type == 5; }
};

uint32_t readNalLength(const uint8_t *bytes, int lengthBytes) noexcept {
  uint32_t value = 0;
  for (int index = 0; index < lengthBytes; ++index)
    value = (value << 8U) | bytes[index];
  return value;
}

bool parseNals(std::span<const uint8_t> bytes, int lengthBytes,
               std::vector<NalSpan> &nals) {
  nals.clear();
  if (lengthBytes < 1 || lengthBytes > 4)
    return false;
  size_t offset = 0;
  while (offset < bytes.size()) {
    if (bytes.size() - offset < static_cast<size_t>(lengthBytes))
      return false;
    const size_t lengthOffset = offset;
    const uint32_t payloadLength =
        readNalLength(bytes.data() + offset, lengthBytes);
    offset += static_cast<size_t>(lengthBytes);
    if (payloadLength == 0 || payloadLength > bytes.size() - offset)
      return false;
    nals.push_back({lengthOffset, offset, payloadLength,
                    static_cast<uint8_t>(bytes[offset] & 0x1fU)});
    offset += payloadLength;
  }
  return offset == bytes.size() && !nals.empty();
}

struct FrameContext {
  std::atomic<bool> inUse{false};
  // Compression uses a separate opaque token and deadline. VideoToolbox must
  // never receive a raw FrameContext pointer because a late callback can
  // arrive after a timed-out slot has been reused.
  // A nonzero token is the operation's single claim. Callback and watchdog
  // both CAS that exact value to zero, preventing a late callback from
  // claiming a newer operation after this slot has been reused.
  std::atomic<uint64_t> encodeToken{0};
  std::atomic<int64_t> encodeDeadlineNanoseconds{0};
  dispatch_source_t encodeDeadlineTimer = nullptr;
  // Decoder callbacks receive decodeToken as an opaque integer, never this
  // pointer.  A timed-out context can therefore be reused without a late
  // callback observing the new frame through stale pointer state.
  std::atomic<uint64_t> decodeToken{0};
  std::atomic<int64_t> decodeDeadlineNanoseconds{0};
  std::atomic<int> decodeStageIndex{-1};
  dispatch_source_t decodeDeadlineTimer = nullptr;
  CVPixelBufferRef fallbackInput = nullptr;
  uint64_t frameIndex = 0;
  CMTime presentationTimeStamp = kCMTimeInvalid;
  CodecGlitchControls controls;
  std::chrono::steady_clock::time_point submittedAt;
  int generation = 0;
  int targetGenerations = 1;
  bool lowResolution = false;
  bool packetWasModified = false;
  bool watchdogRecovery = false;
  bool codecWarmup = false;
};

} // namespace

const char *codecGlitchEffectName(CodecGlitchEffect effect) noexcept {
  switch (effect) {
  case CodecGlitchEffect::QpPump:
    return "qp_pump";
  case CodecGlitchEffect::BitrateCrush:
    return "bitrate_crush";
  case CodecGlitchEffect::SliceDropout:
    return "slice_dropout";
  case CodecGlitchEffect::SliceTransplant:
    return "slice_transplant";
  case CodecGlitchEffect::PFrameLoss:
    return "pframe_loss";
  case CodecGlitchEffect::IdrStarvation:
    return "idr_starvation";
  case CodecGlitchEffect::PayloadXor:
    return "payload_xor";
  case CodecGlitchEffect::ReferenceTimewarp:
    return "reference_timewarp";
  case CodecGlitchEffect::CodecFeedback:
    return "codec_feedback";
  case CodecGlitchEffect::GenerationCascade:
    return "generation_cascade";
  case CodecGlitchEffect::ResolutionHop:
    return "resolution_hop";
  case CodecGlitchEffect::ChromaCodecEcho:
    return "chroma_codec_echo";
  case CodecGlitchEffect::Count:
    break;
  }
  return "unknown";
}

bool codecGlitchEffectFromName(std::string_view name,
                               CodecGlitchEffect &effect) noexcept {
  for (uint32_t index = 0;
       index < static_cast<uint32_t>(CodecGlitchEffect::Count); ++index) {
    const auto candidate = static_cast<CodecGlitchEffect>(index);
    if (name == codecGlitchEffectName(candidate)) {
      effect = candidate;
      return true;
    }
  }
  return false;
}

CodecGlitchFrame::CodecGlitchFrame(CVPixelBufferRef pixelBuffer) noexcept
    : pixelBuffer_(pixelBuffer) {
  if (pixelBuffer_ != nullptr)
    CFRetain(pixelBuffer_);
}

CodecGlitchFrame::CodecGlitchFrame(const CodecGlitchFrame &other) noexcept
    : frameIndex(other.frameIndex),
      presentationTimeStamp(other.presentationTimeStamp), effect(other.effect),
      packetWasModified(other.packetWasModified),
      repeatedPreviousFrame(other.repeatedPreviousFrame),
      intentionalRepeat(other.intentionalRepeat),
      nonIntentionalFallback(other.nonIntentionalFallback),
      codecWarmupFrame(other.codecWarmupFrame),
      watchdogRecoveryFrame(other.watchdogRecoveryFrame),
      latencyMilliseconds(other.latencyMilliseconds),
      pixelBuffer_(other.pixelBuffer_) {
  if (pixelBuffer_ != nullptr)
    CFRetain(pixelBuffer_);
}

CodecGlitchFrame &
CodecGlitchFrame::operator=(const CodecGlitchFrame &other) noexcept {
  if (this == &other)
    return *this;
  CVPixelBufferRef replacement = other.pixelBuffer_;
  if (replacement != nullptr)
    CFRetain(replacement);
  if (pixelBuffer_ != nullptr)
    CFRelease(pixelBuffer_);
  pixelBuffer_ = replacement;
  frameIndex = other.frameIndex;
  presentationTimeStamp = other.presentationTimeStamp;
  effect = other.effect;
  packetWasModified = other.packetWasModified;
  repeatedPreviousFrame = other.repeatedPreviousFrame;
  intentionalRepeat = other.intentionalRepeat;
  nonIntentionalFallback = other.nonIntentionalFallback;
  codecWarmupFrame = other.codecWarmupFrame;
  watchdogRecoveryFrame = other.watchdogRecoveryFrame;
  latencyMilliseconds = other.latencyMilliseconds;
  return *this;
}

CodecGlitchFrame::CodecGlitchFrame(CodecGlitchFrame &&other) noexcept
    : frameIndex(other.frameIndex),
      presentationTimeStamp(other.presentationTimeStamp), effect(other.effect),
      packetWasModified(other.packetWasModified),
      repeatedPreviousFrame(other.repeatedPreviousFrame),
      intentionalRepeat(other.intentionalRepeat),
      nonIntentionalFallback(other.nonIntentionalFallback),
      codecWarmupFrame(other.codecWarmupFrame),
      watchdogRecoveryFrame(other.watchdogRecoveryFrame),
      latencyMilliseconds(other.latencyMilliseconds),
      pixelBuffer_(std::exchange(other.pixelBuffer_, nullptr)) {}

CodecGlitchFrame &
CodecGlitchFrame::operator=(CodecGlitchFrame &&other) noexcept {
  if (this == &other)
    return *this;
  if (pixelBuffer_ != nullptr)
    CFRelease(pixelBuffer_);
  pixelBuffer_ = std::exchange(other.pixelBuffer_, nullptr);
  frameIndex = other.frameIndex;
  presentationTimeStamp = other.presentationTimeStamp;
  effect = other.effect;
  packetWasModified = other.packetWasModified;
  repeatedPreviousFrame = other.repeatedPreviousFrame;
  intentionalRepeat = other.intentionalRepeat;
  nonIntentionalFallback = other.nonIntentionalFallback;
  codecWarmupFrame = other.codecWarmupFrame;
  watchdogRecoveryFrame = other.watchdogRecoveryFrame;
  latencyMilliseconds = other.latencyMilliseconds;
  return *this;
}

CodecGlitchFrame::~CodecGlitchFrame() {
  if (pixelBuffer_ != nullptr)
    CFRelease(pixelBuffer_);
}

namespace {

class CodecGlitchEngineImpl;

struct CodecStage {
  CodecGlitchEngineImpl *owner = nullptr;
  int index = 0;
  int width = 0;
  int height = 0;
  bool qpMode = false;
  bool lowResolution = false;
  VTCompressionSessionRef encoder = nullptr;
  VTDecompressionSessionRef decoder = nullptr;
  CMVideoFormatDescriptionRef decoderFormat = nullptr;
  bool hardwareEncoder = false;
  bool hardwareDecoder = false;
  bool baseQpSupported = false;
  std::atomic<bool> decoderHasOutput{false};
  int currentBitRate = 0;
  std::mutex packetMutex;
  uint64_t packetCount = 0;
  std::vector<uint8_t> packetScratch;
  std::vector<NalSpan> nals;
};

struct AtomicStatistics {
  std::atomic<uint64_t> submitted{0};
  std::atomic<uint64_t> encoded{0};
  std::atomic<uint64_t> decoded{0};
  std::atomic<uint64_t> emitted{0};
  std::atomic<uint64_t> backpressureDrops{0};
  std::atomic<uint64_t> intentionalDrops{0};
  std::atomic<uint64_t> codecErrors{0};
  std::atomic<uint64_t> recoveries{0};
  std::atomic<uint64_t> pollDrops{0};
  std::atomic<uint64_t> totalLatencyMicroseconds{0};
  std::atomic<double> lastLatencyMilliseconds{0.0};
  std::atomic<bool> hardwareEncoder{false};
  std::atomic<bool> hardwareDecoder{false};
  std::atomic<bool> baseQpSupported{false};
};

struct CallbackDeliveryState {
  std::mutex mutex;
  CodecGlitchOutputCallback callback;
  std::vector<CodecGlitchFrame> ring;
  size_t read = 0;
  size_t write = 0;
  size_t count = 0;
  bool drainScheduled = false;
  std::atomic<uint64_t> drops{0};
  dispatch_group_t group = dispatch_group_create();
};

void drainCallbackState(
    const std::shared_ptr<CallbackDeliveryState> &state) noexcept {
  for (;;) {
    CodecGlitchFrame frame;
    CodecGlitchOutputCallback callback;
    try {
      std::lock_guard lock(state->mutex);
      if (!state->callback || state->count == 0 || state->ring.empty()) {
        state->drainScheduled = false;
        return;
      }
      frame = std::move(state->ring[state->read]);
      state->ring[state->read] = CodecGlitchFrame{};
      state->read = (state->read + 1) % state->ring.size();
      --state->count;
      callback = state->callback;
    } catch (...) {
      state->drops.fetch_add(1, std::memory_order_relaxed);
      continue;
    }
    if (callback) {
      try {
        @autoreleasepool {
          callback(frame);
        }
      } catch (...) {
        // Host exceptions must never unwind through a libdispatch block.
      }
    }
  }
}

struct PacketDecision {
  bool drop = false;
};

static void compressionOutputCallback(void *outputCallbackRefCon,
                                      void *sourceFrameRefCon, OSStatus status,
                                      VTEncodeInfoFlags infoFlags,
                                      CMSampleBufferRef sampleBuffer);

static void decompressionOutputCallback(
    void *decompressionOutputRefCon, void *sourceFrameRefCon, OSStatus status,
    VTDecodeInfoFlags infoFlags, CVImageBufferRef imageBuffer,
    CMTime presentationTimeStamp, CMTime presentationDuration);

class CodecGlitchEngineImpl final : public CodecGlitchEngine {
public:
  explicit CodecGlitchEngineImpl(CodecGlitchConfiguration configuration)
      : configuration_(configuration),
        controls_(sanitizedControls(CodecGlitchControls{})) {}

  ~CodecGlitchEngineImpl() override;

  bool initialize(std::string &error);
  bool submit(CVPixelBufferRef input, uint64_t frameIndex,
              CMTime presentationTimeStamp, std::string &error) override;
  void setControls(const CodecGlitchControls &controls) override;
  CodecGlitchControls controls() const override;
  void setOutputCallback(CodecGlitchOutputCallback callback) override;
  bool poll(CodecGlitchFrame &frame) override;
  bool flush(std::chrono::milliseconds timeout, std::string &error) override;
  bool reset(std::string &error) override;
  CodecGlitchStatistics stats() const noexcept override;

  void handleCompressed(CodecStage &stage, uint64_t encodeToken,
                        OSStatus status, VTEncodeInfoFlags infoFlags,
                        CMSampleBufferRef sampleBuffer);
  void handleDecoded(CodecStage &stage, uint64_t decodeToken, OSStatus status,
                     VTDecodeInfoFlags infoFlags, CVImageBufferRef imageBuffer,
                     CMTime presentationTimeStamp);

private:
  bool createResources(std::string &error);
  void destroyResources();
  bool createStage(CodecStage &stage, int index, int width, int height,
                   bool qpMode, bool lowResolution, std::string &error);
  bool ensureStageEncoder(CodecStage &stage, std::string &error);
  void destroyStage(CodecStage &stage);
  bool createDecoder(CodecStage &stage, CMVideoFormatDescriptionRef format,
                     std::string &error);
  FrameContext *acquireContext();
  void releaseContext(FrameContext &context);
  FrameContext *findEncodeContext(uint64_t encodeToken) noexcept;
  uint64_t armEncodeDeadline(FrameContext &context,
                             std::chrono::milliseconds timeout);
  void disarmEncodeDeadline(FrameContext &context);
  void handleEncodeDeadline(FrameContext &context);
  FrameContext *findDecodeContext(uint64_t decodeToken) noexcept;
  uint64_t armDecodeDeadline(FrameContext &context,
                             std::chrono::milliseconds timeout);
  void disarmDecodeDeadline(FrameContext &context);
  void handleDecodeDeadline(FrameContext &context);
  void encodeInitial(FrameContext &context, CVPixelBufferRef input);
  void encodeOnStage(CodecStage &stage, FrameContext &context,
                     CVPixelBufferRef input);
  bool configureFrameOptions(CodecStage &stage, FrameContext &context,
                             CFMutableDictionaryRef options,
                             std::string &error);
  bool extractPacket(CodecStage &stage, CMSampleBufferRef sampleBuffer,
                     bool &keyFrame, int &nalLengthBytes,
                     CMVideoFormatDescriptionRef &format,
                     CMSampleTimingInfo &timing, std::string &error);
  PacketDecision decidePacketDrop(CodecStage &stage, FrameContext &context,
                                  bool keyFrame);
  bool decodeBytes(CodecStage &stage, FrameContext &context,
                   CMVideoFormatDescriptionRef format,
                   const CMSampleTimingInfo &timing, bool keyFrame,
                   std::span<const uint8_t> bytes, std::string &error);
  CVPixelBufferRef renderScaled(CVPixelBufferRef input,
                                CVPixelBufferPoolRef pool, int width,
                                int height, float pixelScale = 0.0f);
  CVPixelBufferRef renderFeedback(CVPixelBufferRef input,
                                  CVPixelBufferRef history, float mix);
  CVPixelBufferRef renderSliceDropout(CVPixelBufferRef input,
                                      CVPixelBufferRef history,
                                      const FrameContext &context);
  CVPixelBufferRef renderSliceTransplant(CVPixelBufferRef input,
                                         CVPixelBufferRef history,
                                         const FrameContext &context);
  CVPixelBufferRef renderPayloadXor(CVPixelBufferRef input,
                                    const FrameContext &context);
  CVPixelBufferRef renderCompressionArtifacts(CVPixelBufferRef input,
                                              const FrameContext &context,
                                              bool generationCascade);
  CVPixelBufferRef renderChromaEcho(CVPixelBufferRef input,
                                    CVPixelBufferRef history, float mix);
  void finishDecodedFrame(CodecStage &stage, FrameContext &context,
                          CVPixelBufferRef imageBuffer);
  void emit(FrameContext &context, CVPixelBufferRef imageBuffer,
            bool repeatedPreviousFrame, bool intentionalRepeat = false,
            bool nonIntentionalFallback = false);
  void repeatOrDrop(FrameContext &context, bool intentional);
  void markDecodeFailure(FrameContext &context);
  void failClaimedContext(FrameContext &context) noexcept;
  void replaceLastOutput(CVPixelBufferRef imageBuffer);
  CVPixelBufferRef copyLastOutput();
  CVPixelBufferRef copyHistoricalOutput(size_t age);
  void clearOutputHistory();
  void clearPendingCallbacks(bool clearCallback);

  CodecGlitchConfiguration configuration_;
  mutable std::mutex controlsMutex_;
  CodecGlitchControls controls_;
  std::array<CodecStage, kStageCount> stages_;
  std::unique_ptr<FrameContext[]> contexts_;
  size_t contextCount_ = 0;
  std::atomic<uint64_t> inFlight_{0};
  std::atomic<uint64_t> nextEncodeToken_{1};
  std::atomic<uint64_t> nextDecodeToken_{1};
  std::mutex inFlightMutex_;
  std::condition_variable inFlightCondition_;
  std::mutex lifecycleMutex_;
  std::atomic<bool> shuttingDown_{false};
  std::atomic<bool> acceptingSubmissions_{true};
  std::atomic<bool> resetting_{false};
  std::atomic<bool> forceRecovery_{false};
  std::atomic<int> consecutiveDecodeErrors_{0};

  dispatch_queue_t encodeQueue_ = nullptr;
  dispatch_queue_t callbackQueue_ = nullptr;
  dispatch_queue_t watchdogQueue_ = nullptr;
  std::shared_ptr<CallbackDeliveryState> callbackState_;

  mutable std::mutex pollMutex_;
  std::vector<CodecGlitchFrame> pollRing_;
  size_t pollRead_ = 0;
  size_t pollWrite_ = 0;
  size_t pollCount_ = 0;

  mutable std::mutex historyMutex_;
  CVPixelBufferRef lastOutput_ = nullptr;
  std::vector<CVPixelBufferRef> outputHistory_;
  size_t outputHistoryNext_ = 0;
  size_t outputHistoryCount_ = 0;

  id<MTLDevice> metalDevice_ = nil;
  CIContext *ciContext_ = nil;
  CVPixelBufferPoolRef fullSizePool_ = nullptr;
  CVPixelBufferPoolRef quarterSizePool_ = nullptr;
  CVPixelBufferPoolRef halfSizePool_ = nullptr;
  AtomicStatistics statistics_;
};

} // namespace

namespace {

bool createPixelBufferPool(int width, int height, int minimumBuffers,
                           CVPixelBufferPoolRef &pool, std::string &error) {
  NSDictionary *poolAttributes = @{
    (__bridge NSString *)
    kCVPixelBufferPoolMinimumBufferCountKey : @(minimumBuffers)
  };
  NSDictionary *pixelAttributes = @{
    (__bridge NSString *)
    kCVPixelBufferPixelFormatTypeKey : @(kCVPixelFormatType_32BGRA),
    (__bridge NSString *)kCVPixelBufferWidthKey : @(width),
    (__bridge NSString *)kCVPixelBufferHeightKey : @(height),
    (__bridge NSString *)kCVPixelBufferMetalCompatibilityKey : @YES,
    (__bridge NSString *)kCVPixelBufferIOSurfacePropertiesKey : @{}
  };
  const CVReturn status = CVPixelBufferPoolCreate(
      kCFAllocatorDefault, (__bridge CFDictionaryRef)poolAttributes,
      (__bridge CFDictionaryRef)pixelAttributes, &pool);
  if (status != kCVReturnSuccess || pool == nullptr) {
    error =
        "CVPixelBufferPoolCreate failed with status " + std::to_string(status);
    return false;
  }
  return true;
}

CodecGlitchEngineImpl::~CodecGlitchEngineImpl() {
  try {
    std::lock_guard lock(lifecycleMutex_);
    acceptingSubmissions_.store(false, std::memory_order_release);
  } catch (...) {
    acceptingSubmissions_.store(false, std::memory_order_release);
  }
  try {
    std::string ignored;
    (void)flush(std::chrono::milliseconds(2000), ignored);
  } catch (...) {
    // Destructors are a hard C++/C ABI boundary and must remain noexcept.
  }
  shuttingDown_.store(true, std::memory_order_release);
  try {
    clearPendingCallbacks(true);
  } catch (...) {
  }
  try {
    if (encodeQueue_ != nullptr &&
        dispatch_get_specific(&kCodecEncodeQueueKey) != this)
      dispatch_sync(encodeQueue_, ^{
        destroyResources();
      });
    else
      destroyResources();
  } catch (...) {
  }
  try {
    clearOutputHistory();
  } catch (...) {
  }
}

bool CodecGlitchEngineImpl::initialize(std::string &error) {
  if (configuration_.width <= 0 || configuration_.height <= 0 ||
      configuration_.framesPerSecond <= 0 ||
      configuration_.averageBitRate < 16000 ||
      configuration_.keyFrameInterval < 1) {
    error = "Invalid codec glitch dimensions, frame rate, bitrate, or GOP";
    return false;
  }
  if ((configuration_.width & 1) != 0 || (configuration_.height & 1) != 0) {
    error = "H.264 codec glitch dimensions must be even";
    return false;
  }
  configuration_.decodedHistoryFrames =
      clampValue(configuration_.decodedHistoryFrames, 4, 12);
  configuration_.maximumInFlightFrames =
      clampValue(configuration_.maximumInFlightFrames, 4, 64);
  configuration_.pollQueueCapacity =
      clampValue(configuration_.pollQueueCapacity, 1, 120);
  callbackState_ = std::make_shared<CallbackDeliveryState>();
  callbackState_->ring.resize(
      static_cast<size_t>(configuration_.pollQueueCapacity));
  configuration_.maximumSliceBytes =
      clampValue(configuration_.maximumSliceBytes, 1024, 1048576);
  outputHistory_.assign(
      static_cast<size_t>(configuration_.decodedHistoryFrames), nullptr);
  outputHistoryNext_ = 0;
  outputHistoryCount_ = 0;

  encodeQueue_ = dispatch_queue_create("ws.daito.glic.codec-encode",
                                       DISPATCH_QUEUE_SERIAL);
  callbackQueue_ = dispatch_queue_create("ws.daito.glic.codec-callback",
                                         DISPATCH_QUEUE_SERIAL);
  watchdogQueue_ = dispatch_queue_create("ws.daito.glic.codec-watchdog",
                                         DISPATCH_QUEUE_SERIAL);
  if (encodeQueue_ == nullptr || callbackQueue_ == nullptr ||
      watchdogQueue_ == nullptr) {
    error = "Failed to create codec glitch dispatch queues";
    return false;
  }
  dispatch_queue_set_specific(encodeQueue_, &kCodecEncodeQueueKey, this,
                              nullptr);
  dispatch_queue_set_specific(callbackQueue_, &kCodecCallbackQueueKey, this,
                              nullptr);
  return createResources(error);
}

bool CodecGlitchEngineImpl::createResources(std::string &error) {
  try {
    @autoreleasepool {
      // The UsingHardware... session properties can remain false until the
      // first asynchronous output on some macOS releases. A decoder capability
      // probe plus successful creation with RequireHardware=true is
      // authoritative.
      if (configuration_.requireHardwareDecoder &&
          VTIsHardwareDecodeSupported(kCMVideoCodecType_H264)) {
        statistics_.hardwareDecoder.store(true, std::memory_order_relaxed);
      }
      metalDevice_ = MTLCreateSystemDefaultDevice();
      if (metalDevice_ == nil) {
        error = "No Metal device is available for codec preprocessing";
        return false;
      }
      ciContext_ =
          [CIContext contextWithMTLDevice:metalDevice_
                                  options:@{
                                    kCIContextWorkingColorSpace : NSNull.null,
                                    kCIContextOutputColorSpace : NSNull.null,
                                    kCIContextCacheIntermediates : @NO
                                  }];
      if (ciContext_ == nil) {
        error = "Failed to create the Metal-backed Core Image context";
        return false;
      }

      const int poolSize = configuration_.maximumInFlightFrames + 4;
      if (!createPixelBufferPool(configuration_.width, configuration_.height,
                                 poolSize, fullSizePool_, error) ||
          !createPixelBufferPool(evenDimension(configuration_.width * 0.25),
                                 evenDimension(configuration_.height * 0.25),
                                 poolSize, quarterSizePool_, error) ||
          !createPixelBufferPool(evenDimension(configuration_.width * 0.5),
                                 evenDimension(configuration_.height * 0.5),
                                 poolSize, halfSizePool_, error)) {
        destroyResources();
        return false;
      }

      contextCount_ = static_cast<size_t>(configuration_.maximumInFlightFrames);
      contexts_ = std::make_unique<FrameContext[]>(contextCount_);
      for (size_t index = 0; index < contextCount_; ++index) {
        FrameContext *context = &contexts_[index];
        context->encodeDeadlineTimer = dispatch_source_create(
            DISPATCH_SOURCE_TYPE_TIMER, 0, 0, watchdogQueue_);
        context->decodeDeadlineTimer = dispatch_source_create(
            DISPATCH_SOURCE_TYPE_TIMER, 0, 0, watchdogQueue_);
        if (context->encodeDeadlineTimer == nullptr ||
            context->decodeDeadlineTimer == nullptr) {
          error = "Failed to create a codec operation deadline timer";
          destroyResources();
          return false;
        }
        dispatch_source_set_timer(context->encodeDeadlineTimer,
                                  DISPATCH_TIME_FOREVER, DISPATCH_TIME_FOREVER,
                                  0);
        dispatch_source_set_event_handler(context->encodeDeadlineTimer, ^{
          handleEncodeDeadline(*context);
        });
        dispatch_resume(context->encodeDeadlineTimer);
        dispatch_source_set_timer(context->decodeDeadlineTimer,
                                  DISPATCH_TIME_FOREVER, DISPATCH_TIME_FOREVER,
                                  0);
        dispatch_source_set_event_handler(context->decodeDeadlineTimer, ^{
          handleDecodeDeadline(*context);
        });
        dispatch_resume(context->decodeDeadlineTimer);
      }
      pollRing_.clear();
      pollRing_.resize(static_cast<size_t>(configuration_.pollQueueCapacity));
      pollRead_ = pollWrite_ = pollCount_ = 0;

      const int quarterWidth = evenDimension(configuration_.width * 0.25);
      const int quarterHeight = evenDimension(configuration_.height * 0.25);
      if (!createStage(stages_[kStageNormal], kStageNormal,
                       configuration_.width, configuration_.height, false,
                       false, error) ||
          !createStage(stages_[kStageQp], kStageQp, configuration_.width,
                       configuration_.height, true, false, error) ||
          !createStage(stages_[kStageCascadeSecond], kStageCascadeSecond,
                       configuration_.width, configuration_.height, false,
                       false, error) ||
          !createStage(stages_[kStageCascadeThird], kStageCascadeThird,
                       configuration_.width, configuration_.height, false,
                       false, error) ||
          !createStage(stages_[kStageLowQuarter], kStageLowQuarter,
                       quarterWidth, quarterHeight, false, true, error)) {
        destroyResources();
        return false;
      }
      const int halfWidth = evenDimension(configuration_.width * 0.5);
      const int halfHeight = evenDimension(configuration_.height * 0.5);
      if (!createStage(stages_[kStageLowHalf], kStageLowHalf, halfWidth,
                       halfHeight, false, true, error)) {
        destroyResources();
        return false;
      }
      // Validate the requested hardware backend with one primary session.
      // Specialized QP/cascade/downscale sessions are created on first use so a
      // one-effect host does not reserve six hardware encoders at startup.
      if (!ensureStageEncoder(stages_[kStageNormal], error)) {
        destroyResources();
        return false;
      }
      error.clear();
      return true;
    }
  } catch (...) {
    destroyResources();
    error = "Unexpected exception while creating codec glitch resources";
    return false;
  }
}

void CodecGlitchEngineImpl::destroyResources() {
  if (contexts_ != nullptr) {
    for (size_t index = 0; index < contextCount_; ++index) {
      FrameContext &context = contexts_[index];
      context.encodeToken.store(0, std::memory_order_release);
      context.decodeToken.store(0, std::memory_order_release);
      if (context.fallbackInput != nullptr) {
        CFRelease(context.fallbackInput);
        context.fallbackInput = nullptr;
      }
      if (context.encodeDeadlineTimer != nullptr) {
        dispatch_source_cancel(context.encodeDeadlineTimer);
        context.encodeDeadlineTimer = nullptr;
      }
      if (context.decodeDeadlineTimer != nullptr) {
        dispatch_source_cancel(context.decodeDeadlineTimer);
        context.decodeDeadlineTimer = nullptr;
      }
    }
    if (watchdogQueue_ != nullptr)
      dispatch_sync(watchdogQueue_, ^{
                    });
  }
  for (CodecStage &stage : stages_)
    destroyStage(stage);
  if (fullSizePool_ != nullptr) {
    CVPixelBufferPoolRelease(fullSizePool_);
    fullSizePool_ = nullptr;
  }
  if (quarterSizePool_ != nullptr) {
    CVPixelBufferPoolRelease(quarterSizePool_);
    quarterSizePool_ = nullptr;
  }
  if (halfSizePool_ != nullptr) {
    CVPixelBufferPoolRelease(halfSizePool_);
    halfSizePool_ = nullptr;
  }
  contexts_.reset();
  contextCount_ = 0;
  ciContext_ = nil;
  metalDevice_ = nil;
}

bool CodecGlitchEngineImpl::createStage(CodecStage &stage, int index, int width,
                                        int height, bool qpMode,
                                        bool lowResolution,
                                        std::string &error) {
  stage.owner = this;
  stage.index = index;
  stage.width = width;
  stage.height = height;
  stage.qpMode = qpMode;
  stage.lowResolution = lowResolution;
  stage.currentBitRate = configuration_.averageBitRate;
  stage.packetCount = 0;
  stage.decoderHasOutput.store(false, std::memory_order_relaxed);
  const size_t packetReserve = std::max<size_t>(
      65536, static_cast<size_t>(width) * static_cast<size_t>(height) / 3);
  stage.packetScratch.clear();
  stage.packetScratch.reserve(packetReserve * 2);
  stage.nals.clear();
  stage.nals.reserve(96);
  error.clear();
  return true;
}

bool CodecGlitchEngineImpl::ensureStageEncoder(CodecStage &stage,
                                               std::string &error) {
  if (stage.encoder != nullptr) {
    error.clear();
    return true;
  }
  const int width = stage.width;
  const int height = stage.height;
  const bool qpMode = stage.qpMode;

  NSMutableDictionary *encoderSpecification = [NSMutableDictionary dictionary];
  if (configuration_.requireHardwareEncoder) {
    encoderSpecification
        [(__bridge NSString *)
             kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder] =
            @YES;
  } else {
    encoderSpecification
        [(__bridge NSString *)
             kVTVideoEncoderSpecification_EnableHardwareAcceleratedVideoEncoder] =
            @YES;
  }
  if (@available(macOS 11.3, *)) {
    if (configuration_.enableLowLatencyRateControl) {
      encoderSpecification[(
          __bridge NSString
              *)kVTVideoEncoderSpecification_EnableLowLatencyRateControl] =
          @YES;
    }
  }

  NSDictionary *sourceAttributes = @{
    (__bridge NSString *)
    kCVPixelBufferPixelFormatTypeKey : @(kCVPixelFormatType_32BGRA),
    (__bridge NSString *)kCVPixelBufferWidthKey : @(width),
    (__bridge NSString *)kCVPixelBufferHeightKey : @(height),
    (__bridge NSString *)kCVPixelBufferMetalCompatibilityKey : @YES,
    (__bridge NSString *)kCVPixelBufferIOSurfacePropertiesKey : @{}
  };
  const OSStatus status = VTCompressionSessionCreate(
      kCFAllocatorDefault, width, height, kCMVideoCodecType_H264,
      (__bridge CFDictionaryRef)encoderSpecification,
      (__bridge CFDictionaryRef)sourceAttributes, kCFAllocatorDefault,
      compressionOutputCallback, &stage, &stage.encoder);
  if (status != noErr || stage.encoder == nullptr) {
    error = statusError("VTCompressionSessionCreate", status);
    return false;
  }

  setSessionProperty(stage.encoder, kVTCompressionPropertyKey_RealTime,
                     kCFBooleanTrue);
  setSessionProperty(stage.encoder,
                     kVTCompressionPropertyKey_AllowFrameReordering,
                     kCFBooleanFalse);
  setSessionProperty(stage.encoder,
                     kVTCompressionPropertyKey_AllowTemporalCompression,
                     kCFBooleanTrue);
  setSessionProperty(stage.encoder, kVTCompressionPropertyKey_ProfileLevel,
                     kVTProfileLevel_H264_Main_AutoLevel);
  setSessionInt(stage.encoder, kVTCompressionPropertyKey_ExpectedFrameRate,
                configuration_.framesPerSecond);
  setSessionInt(stage.encoder, kVTCompressionPropertyKey_MaxKeyFrameInterval,
                configuration_.keyFrameInterval);
  setSessionInt(stage.encoder,
                kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration,
                std::max(1, configuration_.keyFrameInterval /
                                configuration_.framesPerSecond));
  setSessionInt(stage.encoder, kVTCompressionPropertyKey_AverageBitRate,
                configuration_.averageBitRate);
  setSessionInt(stage.encoder, kVTCompressionPropertyKey_MaxH264SliceBytes,
                configuration_.maximumSliceBytes);

  const OSStatus prepareStatus =
      VTCompressionSessionPrepareToEncodeFrames(stage.encoder);
  if (prepareStatus != noErr) {
    error =
        statusError("VTCompressionSessionPrepareToEncodeFrames", prepareStatus);
    VTCompressionSessionInvalidate(stage.encoder);
    CFRelease(stage.encoder);
    stage.encoder = nullptr;
    return false;
  }
  stage.hardwareEncoder =
      configuration_.requireHardwareEncoder ||
      copySessionBool(
          stage.encoder,
          kVTCompressionPropertyKey_UsingHardwareAcceleratedVideoEncoder);
  if (@available(macOS 12.0, *)) {
    stage.baseQpSupported = copySessionBool(
        stage.encoder, kVTCompressionPropertyKey_SupportsBaseFrameQP);
  }
  statistics_.hardwareEncoder.store(
      statistics_.hardwareEncoder.load(std::memory_order_relaxed) ||
          stage.hardwareEncoder,
      std::memory_order_relaxed);
  if (qpMode)
    statistics_.baseQpSupported.store(stage.baseQpSupported,
                                      std::memory_order_relaxed);
  error.clear();
  return true;
}

void CodecGlitchEngineImpl::destroyStage(CodecStage &stage) {
  if (stage.encoder != nullptr) {
    // CompleteFrames has no timeout. The bounded operation deadlines handle
    // realtime completion; invalidate cancels work during reset/destruction.
    VTCompressionSessionInvalidate(stage.encoder);
    CFRelease(stage.encoder);
    stage.encoder = nullptr;
  }
  if (stage.decoder != nullptr) {
    // Waiting for asynchronous frames has no timeout and can deadlock teardown.
    // Invalidate cancels work; tokens make any late callback harmless.
    VTDecompressionSessionInvalidate(stage.decoder);
    CFRelease(stage.decoder);
    stage.decoder = nullptr;
  }
  if (stage.decoderFormat != nullptr) {
    CFRelease(stage.decoderFormat);
    stage.decoderFormat = nullptr;
  }
  stage.packetCount = 0;
  stage.decoderHasOutput.store(false, std::memory_order_release);
}

bool CodecGlitchEngineImpl::createDecoder(CodecStage &stage,
                                          CMVideoFormatDescriptionRef format,
                                          std::string &error) {
  if (stage.decoder != nullptr && stage.decoderFormat != nullptr &&
      CMFormatDescriptionEqual(stage.decoderFormat, format))
    return true;

  if (stage.decoder != nullptr) {
    VTDecompressionSessionInvalidate(stage.decoder);
    CFRelease(stage.decoder);
    stage.decoder = nullptr;
  }
  if (stage.decoderFormat != nullptr) {
    CFRelease(stage.decoderFormat);
    stage.decoderFormat = nullptr;
  }
  stage.decoderHasOutput.store(false, std::memory_order_release);

  NSMutableDictionary *decoderSpecification = [NSMutableDictionary dictionary];
  if (configuration_.requireHardwareDecoder) {
    decoderSpecification
        [(__bridge NSString *)
             kVTVideoDecoderSpecification_RequireHardwareAcceleratedVideoDecoder] =
            @YES;
  } else {
    decoderSpecification
        [(__bridge NSString *)
             kVTVideoDecoderSpecification_EnableHardwareAcceleratedVideoDecoder] =
            @YES;
  }
  NSDictionary *destinationAttributes = @{
    (__bridge NSString *)
    kCVPixelBufferPixelFormatTypeKey : @(kCVPixelFormatType_32BGRA),
    (__bridge NSString *)kCVPixelBufferWidthKey : @(stage.width),
    (__bridge NSString *)kCVPixelBufferHeightKey : @(stage.height),
    (__bridge NSString *)kCVPixelBufferMetalCompatibilityKey : @YES,
    (__bridge NSString *)kCVPixelBufferIOSurfacePropertiesKey : @{}
  };
  VTDecompressionOutputCallbackRecord callbackRecord = {
      decompressionOutputCallback, &stage};
  const OSStatus status = VTDecompressionSessionCreate(
      kCFAllocatorDefault, format,
      (__bridge CFDictionaryRef)decoderSpecification,
      (__bridge CFDictionaryRef)destinationAttributes, &callbackRecord,
      &stage.decoder);
  if (status != noErr || stage.decoder == nullptr) {
    error = statusError("VTDecompressionSessionCreate", status);
    return false;
  }
  stage.decoderFormat =
      static_cast<CMVideoFormatDescriptionRef>(CFRetain(format));
  stage.hardwareDecoder =
      configuration_.requireHardwareDecoder ||
      copySessionBool(
          stage.decoder,
          kVTDecompressionPropertyKey_UsingHardwareAcceleratedVideoDecoder);
  statistics_.hardwareDecoder.store(
      statistics_.hardwareDecoder.load(std::memory_order_relaxed) ||
          stage.hardwareDecoder,
      std::memory_order_relaxed);
  return true;
}

FrameContext *CodecGlitchEngineImpl::acquireContext() {
  if (contexts_ == nullptr)
    return nullptr;
  for (size_t index = 0; index < contextCount_; ++index) {
    bool expected = false;
    if (contexts_[index].inUse.compare_exchange_strong(
            expected, true, std::memory_order_acq_rel,
            std::memory_order_relaxed)) {
      inFlight_.fetch_add(1, std::memory_order_relaxed);
      contexts_[index].decodeToken.store(0, std::memory_order_relaxed);
      contexts_[index].encodeToken.store(0, std::memory_order_relaxed);
      return &contexts_[index];
    }
  }
  return nullptr;
}

void CodecGlitchEngineImpl::releaseContext(FrameContext &context) {
  disarmEncodeDeadline(context);
  disarmDecodeDeadline(context);
  context.encodeToken.store(0, std::memory_order_release);
  context.decodeToken.store(0, std::memory_order_release);
  CVPixelBufferRef fallbackInput = context.fallbackInput;
  context.fallbackInput = nullptr;
  if (fallbackInput != nullptr)
    CFRelease(fallbackInput);
  context.inUse.store(false, std::memory_order_release);
  const uint64_t remaining =
      inFlight_.fetch_sub(1, std::memory_order_acq_rel) - 1;
  if (remaining == 0) {
    std::lock_guard lock(inFlightMutex_);
    inFlightCondition_.notify_all();
  }
}

FrameContext *
CodecGlitchEngineImpl::findEncodeContext(uint64_t encodeToken) noexcept {
  if (encodeToken == 0 || contexts_ == nullptr)
    return nullptr;
  for (size_t index = 0; index < contextCount_; ++index) {
    FrameContext &context = contexts_[index];
    if (context.inUse.load(std::memory_order_acquire) &&
        context.encodeToken.load(std::memory_order_acquire) == encodeToken)
      return &context;
  }
  return nullptr;
}

uint64_t
CodecGlitchEngineImpl::armEncodeDeadline(FrameContext &context,
                                         std::chrono::milliseconds timeout) {
  uint64_t token = nextEncodeToken_.fetch_add(1, std::memory_order_relaxed);
  if (token == 0)
    token = nextEncodeToken_.fetch_add(1, std::memory_order_relaxed);
  const int64_t deadline =
      steadyNanoseconds() +
      std::chrono::duration_cast<std::chrono::nanoseconds>(timeout).count();
  context.encodeDeadlineNanoseconds.store(deadline, std::memory_order_release);
  context.encodeToken.store(token, std::memory_order_release);
  if (context.encodeDeadlineTimer != nullptr) {
    dispatch_source_set_timer(
        context.encodeDeadlineTimer,
        dispatch_time(
            DISPATCH_TIME_NOW,
            static_cast<int64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(timeout)
                    .count())),
        DISPATCH_TIME_FOREVER, NSEC_PER_MSEC);
  }
  return token;
}

void CodecGlitchEngineImpl::disarmEncodeDeadline(FrameContext &context) {
  if (context.encodeDeadlineTimer != nullptr) {
    dispatch_source_set_timer(context.encodeDeadlineTimer,
                              DISPATCH_TIME_FOREVER, DISPATCH_TIME_FOREVER, 0);
  }
}

void CodecGlitchEngineImpl::handleEncodeDeadline(FrameContext &context) {
  if (shuttingDown_.load(std::memory_order_acquire) ||
      !context.inUse.load(std::memory_order_acquire))
    return;
  const int64_t deadline =
      context.encodeDeadlineNanoseconds.load(std::memory_order_acquire);
  const int64_t now = steadyNanoseconds();
  if (now < deadline) {
    if (context.encodeDeadlineTimer != nullptr) {
      dispatch_source_set_timer(
          context.encodeDeadlineTimer,
          dispatch_time(DISPATCH_TIME_NOW, deadline - now),
          DISPATCH_TIME_FOREVER, NSEC_PER_MSEC);
    }
    return;
  }
  uint64_t token = context.encodeToken.load(std::memory_order_acquire);
  if (token == 0 ||
      !context.encodeToken.compare_exchange_strong(
          token, 0, std::memory_order_acq_rel, std::memory_order_acquire))
    return;
  disarmEncodeDeadline(context);
  if (std::getenv("GLIC_CODEC_DEADLINE_DEBUG") != nullptr)
    std::fprintf(stderr, "codec-encode-timeout frame=%llu token=%llu\n",
                 static_cast<unsigned long long>(context.frameIndex),
                 static_cast<unsigned long long>(token));
  forceRecovery_.store(true, std::memory_order_release);
  markDecodeFailure(context);
}

FrameContext *
CodecGlitchEngineImpl::findDecodeContext(uint64_t decodeToken) noexcept {
  if (decodeToken == 0 || contexts_ == nullptr)
    return nullptr;
  for (size_t index = 0; index < contextCount_; ++index) {
    FrameContext &context = contexts_[index];
    if (context.inUse.load(std::memory_order_acquire) &&
        context.decodeToken.load(std::memory_order_acquire) == decodeToken)
      return &context;
  }
  return nullptr;
}

uint64_t
CodecGlitchEngineImpl::armDecodeDeadline(FrameContext &context,
                                         std::chrono::milliseconds timeout) {
  uint64_t token = nextDecodeToken_.fetch_add(1, std::memory_order_relaxed);
  if (token == 0)
    token = nextDecodeToken_.fetch_add(1, std::memory_order_relaxed);
  const int64_t deadline =
      steadyNanoseconds() +
      std::chrono::duration_cast<std::chrono::nanoseconds>(timeout).count();
  context.decodeDeadlineNanoseconds.store(deadline, std::memory_order_release);
  context.decodeToken.store(token, std::memory_order_release);
  if (context.decodeDeadlineTimer != nullptr) {
    dispatch_source_set_timer(
        context.decodeDeadlineTimer,
        dispatch_time(
            DISPATCH_TIME_NOW,
            static_cast<int64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(timeout)
                    .count())),
        DISPATCH_TIME_FOREVER, NSEC_PER_MSEC);
  }
  return token;
}

void CodecGlitchEngineImpl::disarmDecodeDeadline(FrameContext &context) {
  if (context.decodeDeadlineTimer != nullptr) {
    dispatch_source_set_timer(context.decodeDeadlineTimer,
                              DISPATCH_TIME_FOREVER, DISPATCH_TIME_FOREVER, 0);
  }
}

void CodecGlitchEngineImpl::handleDecodeDeadline(FrameContext &context) {
  if (shuttingDown_.load(std::memory_order_acquire) ||
      !context.inUse.load(std::memory_order_acquire))
    return;
  const int64_t deadline =
      context.decodeDeadlineNanoseconds.load(std::memory_order_acquire);
  const int64_t now = steadyNanoseconds();
  // A canceled timer event may already have been queued when a later cascade
  // generation re-armed the same source.  Never let that stale event expire
  // the new decode early; schedule only its remaining interval.
  if (now < deadline) {
    if (context.decodeDeadlineTimer != nullptr) {
      dispatch_source_set_timer(
          context.decodeDeadlineTimer,
          dispatch_time(DISPATCH_TIME_NOW, deadline - now),
          DISPATCH_TIME_FOREVER, NSEC_PER_MSEC);
    }
    return;
  }
  uint64_t token = context.decodeToken.load(std::memory_order_acquire);
  if (token == 0 ||
      !context.decodeToken.compare_exchange_strong(
          token, 0, std::memory_order_acq_rel, std::memory_order_acquire))
    return;
  if (std::getenv("GLIC_CODEC_DEADLINE_DEBUG") != nullptr)
    std::fprintf(stderr, "codec-timeout frame=%llu token=%llu\n",
                 static_cast<unsigned long long>(context.frameIndex),
                 static_cast<unsigned long long>(token));
  disarmDecodeDeadline(context);
  // Never invalidate a VideoToolbox session from the watchdog queue. Session
  // invalidation and the packet mutex are both potentially blocking, which
  // would defeat the deadline and could stall every later context. Effects
  // use valid H.264 samples; a timeout repeats the last good output and forces
  // an IDR through the existing session. Teardown remains on reset/destructor.
  forceRecovery_.store(true, std::memory_order_release);
  markDecodeFailure(context);
}

bool CodecGlitchEngineImpl::submit(CVPixelBufferRef input, uint64_t frameIndex,
                                   CMTime presentationTimeStamp,
                                   std::string &error) {
  if (input == nullptr) {
    error = "Codec glitch input pixel buffer is null";
    return false;
  }
  std::lock_guard lifecycleLock(lifecycleMutex_);
  if (!acceptingSubmissions_.load(std::memory_order_acquire) ||
      shuttingDown_.load(std::memory_order_acquire) ||
      resetting_.load(std::memory_order_acquire) || encodeQueue_ == nullptr) {
    error = "Codec glitch engine is shutting down";
    return false;
  }
  if (CVPixelBufferGetWidth(input) !=
          static_cast<size_t>(configuration_.width) ||
      CVPixelBufferGetHeight(input) !=
          static_cast<size_t>(configuration_.height)) {
    error = "Codec glitch input dimensions do not match the configuration";
    return false;
  }

  FrameContext *context = acquireContext();
  if (context == nullptr) {
    statistics_.backpressureDrops.fetch_add(1, std::memory_order_relaxed);
    error = "Codec glitch input queue is full";
    return false;
  }

  CodecGlitchControls controlsSnapshot;
  {
    std::lock_guard lock(controlsMutex_);
    controlsSnapshot = controls_;
  }
  context->frameIndex = frameIndex;
  context->presentationTimeStamp =
      CMTIME_IS_VALID(presentationTimeStamp)
          ? presentationTimeStamp
          : CMTimeMake(static_cast<int64_t>(frameIndex),
                       configuration_.framesPerSecond);
  context->controls = controlsSnapshot;
  context->submittedAt = std::chrono::steady_clock::now();
  context->generation = 0;
  context->targetGenerations =
      controlsSnapshot.effect == CodecGlitchEffect::GenerationCascade
          ? controlsSnapshot.cascadeGenerations
          : 1;
  context->lowResolution = false;
  context->packetWasModified = false;
  context->codecWarmup = false;
  CFRetain(input);
  context->fallbackInput = input;

  context->watchdogRecovery =
      forceRecovery_.exchange(false, std::memory_order_acq_rel);
  if (context->watchdogRecovery)
    statistics_.recoveries.fetch_add(1, std::memory_order_relaxed);

  statistics_.submitted.fetch_add(1, std::memory_order_relaxed);
  CFRetain(input);
  dispatch_async(encodeQueue_, ^{
    @autoreleasepool {
      if (shuttingDown_.load(std::memory_order_acquire)) {
        CFRelease(input);
        releaseContext(*context);
        return;
      }
      encodeInitial(*context, input);
      CFRelease(input);
    }
  });
  error.clear();
  return true;
}

void CodecGlitchEngineImpl::setControls(const CodecGlitchControls &controls) {
  std::lock_guard lock(controlsMutex_);
  controls_ = sanitizedControls(controls);
}

CodecGlitchControls CodecGlitchEngineImpl::controls() const {
  std::lock_guard lock(controlsMutex_);
  return controls_;
}

void CodecGlitchEngineImpl::setOutputCallback(
    CodecGlitchOutputCallback callback) {
  const auto state = callbackState_;
  if (!state)
    return;
  std::lock_guard lock(state->mutex);
  state->callback = std::move(callback);
  if (!state->callback) {
    for (CodecGlitchFrame &frame : state->ring)
      frame = CodecGlitchFrame{};
    state->read = state->write = state->count = 0;
  }
}

bool CodecGlitchEngineImpl::poll(CodecGlitchFrame &frame) {
  std::lock_guard lock(pollMutex_);
  if (pollCount_ == 0 || pollRing_.empty())
    return false;
  frame = std::move(pollRing_[pollRead_]);
  pollRing_[pollRead_] = CodecGlitchFrame{};
  pollRead_ = (pollRead_ + 1) % pollRing_.size();
  --pollCount_;
  return true;
}

CodecGlitchStatistics CodecGlitchEngineImpl::stats() const noexcept {
  CodecGlitchStatistics result;
  result.submittedFrames =
      statistics_.submitted.load(std::memory_order_relaxed);
  result.encodedFrames = statistics_.encoded.load(std::memory_order_relaxed);
  result.decodedFrames = statistics_.decoded.load(std::memory_order_relaxed);
  result.emittedFrames = statistics_.emitted.load(std::memory_order_relaxed);
  result.backpressureDrops =
      statistics_.backpressureDrops.load(std::memory_order_relaxed);
  result.intentionalPacketDrops =
      statistics_.intentionalDrops.load(std::memory_order_relaxed);
  result.codecErrors = statistics_.codecErrors.load(std::memory_order_relaxed);
  result.watchdogRecoveries =
      statistics_.recoveries.load(std::memory_order_relaxed);
  result.pollQueueDrops = statistics_.pollDrops.load(std::memory_order_relaxed);
  if (callbackState_)
    result.pollQueueDrops +=
        callbackState_->drops.load(std::memory_order_relaxed);
  result.lastLatencyMilliseconds =
      statistics_.lastLatencyMilliseconds.load(std::memory_order_relaxed);
  const uint64_t emitted = result.emittedFrames;
  result.averageLatencyMilliseconds =
      emitted == 0
          ? 0.0
          : static_cast<double>(statistics_.totalLatencyMicroseconds.load(
                std::memory_order_relaxed)) /
                (1000.0 * static_cast<double>(emitted));
  result.hardwareEncoder =
      statistics_.hardwareEncoder.load(std::memory_order_relaxed);
  result.hardwareDecoder =
      statistics_.hardwareDecoder.load(std::memory_order_relaxed);
  result.baseFrameQpSupported =
      statistics_.baseQpSupported.load(std::memory_order_relaxed);
  return result;
}

void CodecGlitchEngineImpl::encodeInitial(FrameContext &context,
                                          CVPixelBufferRef input) {
  CVPixelBufferRef prepared = nullptr;
  CodecStage *stage = &stages_[kStageNormal];

  if (context.controls.effect == CodecGlitchEffect::QpPump)
    stage = &stages_[kStageQp];

  if (context.controls.effect == CodecGlitchEffect::ResolutionHop) {
    const double gate = temporalWave(context.frameIndex, context.controls.rate);
    const bool useReducedResolution =
        gate <= std::max(0.08f, context.controls.amount);
    if (useReducedResolution) {
      const bool quarter = context.controls.reducedResolutionScale < 0.375f;
      stage = &stages_[quarter ? kStageLowQuarter : kStageLowHalf];
      CVPixelBufferPoolRef pool = quarter ? quarterSizePool_ : halfSizePool_;
      prepared = renderScaled(input, pool, stage->width, stage->height);
      context.lowResolution = true;
    }
  }

  if (context.controls.effect == CodecGlitchEffect::CodecFeedback) {
    CVPixelBufferRef history = copyLastOutput();
    if (history != nullptr) {
      prepared = renderFeedback(
          input, history, context.controls.feedback * context.controls.amount);
      CFRelease(history);
    }
  }

  if (prepared == nullptr &&
      CVPixelBufferGetPixelFormatType(input) != kCVPixelFormatType_32BGRA) {
    prepared = renderScaled(input, fullSizePool_, configuration_.width,
                            configuration_.height);
  }

  CVPixelBufferRef encodeInput = prepared != nullptr ? prepared : input;
  if (encodeInput == nullptr) {
    markDecodeFailure(context);
    return;
  }
  encodeOnStage(*stage, context, encodeInput);
  if (prepared != nullptr)
    CFRelease(prepared);
}

bool CodecGlitchEngineImpl::configureFrameOptions(
    CodecStage &stage, FrameContext &context, CFMutableDictionaryRef options,
    std::string &error) {
  int desiredBitRate = configuration_.averageBitRate;
  const int stableHardwareBitRateFloor = std::min(
      configuration_.averageBitRate,
      std::max(16000, static_cast<int>((static_cast<int64_t>(stage.width) *
                                        static_cast<int64_t>(stage.height) *
                                        configuration_.framesPerSecond) /
                                       4)));
  if (context.controls.effect == CodecGlitchEffect::BitrateCrush &&
      stage.index == kStageNormal) {
    const double wave = temporalWave(context.frameIndex, context.controls.rate);
    const double damage = clampValue(
        std::pow(static_cast<double>(context.controls.amount), 0.75) *
            (0.70 + 0.45 * wave),
        0.0, 1.0);
    desiredBitRate = static_cast<int>(
        std::lround(configuration_.averageBitRate * (1.0 - damage) +
                    context.controls.crushedBitRate * damage));
    desiredBitRate =
        std::max(stableHardwareBitRateFloor, (desiredBitRate / 8000) * 8000);
  } else if (context.controls.effect == CodecGlitchEffect::GenerationCascade &&
             (stage.index == kStageCascadeSecond ||
              stage.index == kStageCascadeThird)) {
    const double severity = stage.index == kStageCascadeSecond ? 0.68 : 0.92;
    const double damage = clampValue(
        static_cast<double>(context.controls.amount) * severity, 0.0, 0.96);
    desiredBitRate = static_cast<int>(
        std::lround(configuration_.averageBitRate * (1.0 - damage) +
                    context.controls.crushedBitRate * damage));
    desiredBitRate =
        std::max(stableHardwareBitRateFloor, (desiredBitRate / 8000) * 8000);
  }

  if (stage.qpMode && stage.baseQpSupported) {
    if (@available(macOS 12.0, *)) {
      const double wave =
          temporalWave(context.frameIndex, context.controls.rate);
      const double exponent = 0.35 + (1.0 - context.controls.amount) * 0.65;
      const double shaped = std::pow(wave, exponent);
      const double damage = clampValue(
          context.controls.amount * (0.12 + 0.98 * shaped), 0.0, 1.0);
      const int qp =
          clampValue(static_cast<int>(std::lround(context.controls.minimumQp +
                                                  (context.controls.maximumQp -
                                                   context.controls.minimumQp) *
                                                      damage)),
                     0, 51);
      int32_t narrowed = qp;
      CFNumberRef qpNumber =
          CFNumberCreate(kCFAllocatorDefault, kCFNumberSInt32Type, &narrowed);
      CFDictionarySetValue(options, kVTEncodeFrameOptionKey_BaseFrameQP,
                           qpNumber);
      CFRelease(qpNumber);
    }
  } else if (stage.qpMode) {
    const double wave = temporalWave(context.frameIndex, context.controls.rate);
    const double damage = context.controls.amount * wave;
    desiredBitRate = static_cast<int>(
        std::lround(configuration_.averageBitRate * (1.0 - damage) +
                    context.controls.crushedBitRate * damage));
  }

  if (!stage.qpMode || !stage.baseQpSupported)
    desiredBitRate = std::max(stableHardwareBitRateFloor, desiredBitRate);
  if ((!stage.qpMode || !stage.baseQpSupported) &&
      desiredBitRate < stage.currentBitRate) {
    // Abrupt multi-megabit rate-control jumps can make the hardware encoder
    // intentionally drop its next frame. Slew downward over a few frames so
    // bitrate crush remains visible without becoming an accidental fallback.
    desiredBitRate =
        std::max(desiredBitRate,
                 static_cast<int>(std::lround(stage.currentBitRate * 0.84)));
    desiredBitRate =
        std::max(stableHardwareBitRateFloor, (desiredBitRate / 8000) * 8000);
  }
  if ((!stage.qpMode || !stage.baseQpSupported) &&
      desiredBitRate != stage.currentBitRate) {
    setSessionInt(stage.encoder, kVTCompressionPropertyKey_AverageBitRate,
                  desiredBitRate);
    stage.currentBitRate = desiredBitRate;
  }
  if (context.watchdogRecovery) {
    CFDictionarySetValue(options, kVTEncodeFrameOptionKey_ForceKeyFrame,
                         kCFBooleanTrue);
  }
  error.clear();
  return true;
}

void CodecGlitchEngineImpl::encodeOnStage(CodecStage &stage,
                                          FrameContext &context,
                                          CVPixelBufferRef input) {
  std::string error;
  if (!ensureStageEncoder(stage, error)) {
    markDecodeFailure(context);
    return;
  }
  CFMutableDictionaryRef frameOptions = CFDictionaryCreateMutable(
      kCFAllocatorDefault, 3, &kCFTypeDictionaryKeyCallBacks,
      &kCFTypeDictionaryValueCallBacks);
  if (frameOptions == nullptr) {
    markDecodeFailure(context);
    return;
  }
  if (!configureFrameOptions(stage, context, frameOptions, error)) {
    CFRelease(frameOptions);
    markDecodeFailure(context);
    return;
  }
  const CMTime duration = CMTimeMake(1, configuration_.framesPerSecond);
  bool stageWarmup = false;
  {
    std::lock_guard lock(stage.packetMutex);
    stageWarmup = stage.packetCount == 0;
  }
  if (stageWarmup)
    context.codecWarmup = true;
  const uint64_t encodeToken = armEncodeDeadline(
      context, stageWarmup ? kEncodeWarmupDeadline : kEncodeDeadline);
  static_assert(sizeof(uintptr_t) >= sizeof(uint64_t),
                "Codec glitch operation tokens require a 64-bit macOS process");
  void *const callbackToken =
      reinterpret_cast<void *>(static_cast<uintptr_t>(encodeToken));
  const OSStatus status = VTCompressionSessionEncodeFrame(
      stage.encoder, input, context.presentationTimeStamp, duration,
      frameOptions, callbackToken, nullptr);
  CFRelease(frameOptions);
  if (status != noErr) {
    uint64_t expectedToken = encodeToken;
    if (!context.encodeToken.compare_exchange_strong(expectedToken, 0,
                                                     std::memory_order_acq_rel,
                                                     std::memory_order_acquire))
      return;
    disarmEncodeDeadline(context);
    if (std::getenv("GLIC_CODEC_DEADLINE_DEBUG") != nullptr)
      std::fprintf(stderr, "codec-encode-submit-error frame=%llu status=%d\n",
                   static_cast<unsigned long long>(context.frameIndex),
                   static_cast<int>(status));
    markDecodeFailure(context);
  }
}

bool CodecGlitchEngineImpl::extractPacket(CodecStage &stage,
                                          CMSampleBufferRef sampleBuffer,
                                          bool &keyFrame, int &nalLengthBytes,
                                          CMVideoFormatDescriptionRef &format,
                                          CMSampleTimingInfo &timing,
                                          std::string &error) {
  if (sampleBuffer == nullptr || !CMSampleBufferDataIsReady(sampleBuffer)) {
    error = "VideoToolbox returned an encoded sample with no ready data";
    return false;
  }
  CMBlockBufferRef block = CMSampleBufferGetDataBuffer(sampleBuffer);
  format = static_cast<CMVideoFormatDescriptionRef>(
      CMSampleBufferGetFormatDescription(sampleBuffer));
  if (block == nullptr || format == nullptr) {
    error = "Encoded H.264 sample is missing data or format description";
    return false;
  }

  const size_t dataLength = CMBlockBufferGetDataLength(block);
  if (dataLength == 0 ||
      dataLength > static_cast<size_t>(configuration_.width) *
                       static_cast<size_t>(configuration_.height) * 8) {
    error = "Encoded H.264 sample has an invalid data length";
    return false;
  }
  stage.packetScratch.resize(dataLength);
  const OSStatus copyStatus = CMBlockBufferCopyDataBytes(
      block, 0, dataLength, stage.packetScratch.data());
  if (copyStatus != kCMBlockBufferNoErr) {
    error = statusError("CMBlockBufferCopyDataBytes", copyStatus);
    return false;
  }

  const uint8_t *parameterSet = nullptr;
  size_t parameterSetSize = 0;
  size_t parameterSetCount = 0;
  int lengthHeader = 0;
  const OSStatus formatStatus =
      CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
          format, 0, &parameterSet, &parameterSetSize, &parameterSetCount,
          &lengthHeader);
  if (formatStatus != noErr || lengthHeader < 1 || lengthHeader > 4) {
    error = statusError("CMVideoFormatDescriptionGetH264ParameterSetAtIndex",
                        formatStatus);
    return false;
  }
  nalLengthBytes = lengthHeader;
  if (!parseNals(stage.packetScratch, nalLengthBytes, stage.nals)) {
    error = "Encoded H.264 sample has malformed AVCC NAL lengths";
    return false;
  }

  keyFrame = true;
  CFArrayRef attachments =
      CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, false);
  if (attachments != nullptr && CFArrayGetCount(attachments) > 0) {
    CFDictionaryRef attachment =
        static_cast<CFDictionaryRef>(CFArrayGetValueAtIndex(attachments, 0));
    if (attachment != nullptr) {
      CFTypeRef notSync =
          CFDictionaryGetValue(attachment, kCMSampleAttachmentKey_NotSync);
      keyFrame = notSync != kCFBooleanTrue;
    }
  }
  if (CMSampleBufferGetSampleTimingInfo(sampleBuffer, 0, &timing) != noErr) {
    timing.duration = CMTimeMake(1, configuration_.framesPerSecond);
    timing.presentationTimeStamp =
        CMSampleBufferGetPresentationTimeStamp(sampleBuffer);
    timing.decodeTimeStamp = kCMTimeInvalid;
  }
  CFRetain(format);
  error.clear();
  return true;
}

PacketDecision CodecGlitchEngineImpl::decidePacketDrop(CodecStage &stage,
                                                       FrameContext &context,
                                                       bool keyFrame) {
  PacketDecision result;
  // Always establish one clean decoder reference before intentionally holding
  // a keyframe so the temporal effects have a valid last-good output.
  if (context.watchdogRecovery || (keyFrame && stage.packetCount == 0))
    return result;

  const CodecGlitchEffect effect = context.controls.effect;
  const double randomGate =
      hashUnit(context.controls.seed ^ (context.frameIndex * kHashMultiplier) ^
               static_cast<uint64_t>(stage.index));
  const double amount = context.controls.amount;

  if (effect == CodecGlitchEffect::PFrameLoss && !keyFrame &&
      randomGate < amount * (0.15 + 0.55 * context.controls.rate)) {
    result.drop = true;
    return result;
  }
  if (effect == CodecGlitchEffect::IdrStarvation && keyFrame &&
      stage.packetCount > 1 && randomGate < std::max(0.05, amount)) {
    result.drop = true;
    return result;
  }
  return result;
}

bool CodecGlitchEngineImpl::decodeBytes(
    CodecStage &stage, FrameContext &context,
    CMVideoFormatDescriptionRef format, const CMSampleTimingInfo &timing,
    bool keyFrame, std::span<const uint8_t> bytes, std::string &error) {
  const bool decoderWarmup =
      stage.decoder == nullptr || stage.decoderFormat == nullptr ||
      !CMFormatDescriptionEqual(stage.decoderFormat, format) ||
      !stage.decoderHasOutput.load(std::memory_order_acquire);
  if (!createDecoder(stage, format, error))
    return false;
  if (decoderWarmup)
    context.codecWarmup = true;

  CMBlockBufferRef block = nullptr;
  OSStatus status = CMBlockBufferCreateWithMemoryBlock(
      kCFAllocatorDefault, nullptr, bytes.size(), kCFAllocatorDefault, nullptr,
      0, bytes.size(), 0, &block);
  if (status != kCMBlockBufferNoErr || block == nullptr) {
    error = statusError("CMBlockBufferCreateWithMemoryBlock", status);
    return false;
  }
  status = CMBlockBufferReplaceDataBytes(bytes.data(), block, 0, bytes.size());
  if (status != kCMBlockBufferNoErr) {
    CFRelease(block);
    error = statusError("CMBlockBufferReplaceDataBytes", status);
    return false;
  }

  CMSampleBufferRef sample = nullptr;
  const size_t sampleSize = bytes.size();
  CMSampleTimingInfo adjustedTiming = timing;
  adjustedTiming.presentationTimeStamp = context.presentationTimeStamp;
  adjustedTiming.decodeTimeStamp = kCMTimeInvalid;
  status = CMSampleBufferCreateReady(kCFAllocatorDefault, block, format, 1, 1,
                                     &adjustedTiming, 1, &sampleSize, &sample);
  CFRelease(block);
  if (status != noErr || sample == nullptr) {
    error = statusError("CMSampleBufferCreateReady", status);
    return false;
  }
  CFArrayRef attachments =
      CMSampleBufferGetSampleAttachmentsArray(sample, true);
  if (!keyFrame && attachments != nullptr && CFArrayGetCount(attachments) > 0) {
    CFMutableDictionaryRef attachment = static_cast<CFMutableDictionaryRef>(
        const_cast<void *>(CFArrayGetValueAtIndex(attachments, 0)));
    CFDictionarySetValue(attachment, kCMSampleAttachmentKey_NotSync,
                         kCFBooleanTrue);
  }

  const VTDecodeFrameFlags flags =
      kVTDecodeFrame_EnableAsynchronousDecompression |
      kVTDecodeFrame_1xRealTimePlayback;
  context.decodeStageIndex.store(stage.index, std::memory_order_release);
  const uint64_t decodeToken = armDecodeDeadline(
      context, decoderWarmup ? kDecodeWarmupDeadline : kDecodeDeadline);
  static_assert(sizeof(uintptr_t) >= sizeof(uint64_t),
                "Codec glitch decode tokens require a 64-bit macOS process");
  void *const callbackToken =
      reinterpret_cast<void *>(static_cast<uintptr_t>(decodeToken));
  status = VTDecompressionSessionDecodeFrame(stage.decoder, sample, flags,
                                             callbackToken, nullptr);
  CFRelease(sample);
  if (status != noErr) {
    uint64_t expectedToken = decodeToken;
    if (!context.decodeToken.compare_exchange_strong(
            expectedToken, 0, std::memory_order_acq_rel,
            std::memory_order_acquire)) {
      // A synchronous callback already claimed and completed this decode.
      error.clear();
      return true;
    }
    disarmDecodeDeadline(context);
    error = statusError("VTDecompressionSessionDecodeFrame", status);
    return false;
  }
  error.clear();
  return true;
}

void CodecGlitchEngineImpl::handleCompressed(CodecStage &stage,
                                             uint64_t encodeToken,
                                             OSStatus status,
                                             VTEncodeInfoFlags infoFlags,
                                             CMSampleBufferRef sampleBuffer) {
  FrameContext *contextPointer = findEncodeContext(encodeToken);
  if (contextPointer == nullptr)
    return;
  uint64_t expectedToken = encodeToken;
  if (!contextPointer->encodeToken.compare_exchange_strong(
          expectedToken, 0, std::memory_order_acq_rel,
          std::memory_order_acquire))
    return;
  disarmEncodeDeadline(*contextPointer);
  FrameContext &context = *contextPointer;
  try {
    @autoreleasepool {
      if (status != noErr || (infoFlags & kVTEncodeInfo_FrameDropped) != 0 ||
          sampleBuffer == nullptr) {
        if (std::getenv("GLIC_CODEC_DEADLINE_DEBUG") != nullptr)
          std::fprintf(stderr,
                       "codec-encode-callback-error frame=%llu status=%d "
                       "flags=%u sample=%d\n",
                       static_cast<unsigned long long>(context.frameIndex),
                       static_cast<int>(status),
                       static_cast<unsigned>(infoFlags),
                       sampleBuffer != nullptr ? 1 : 0);
        markDecodeFailure(context);
        return;
      }
      statistics_.encoded.fetch_add(1, std::memory_order_relaxed);

      bool keyFrame = false;
      int nalLengthBytes = 4;
      CMVideoFormatDescriptionRef format = nullptr;
      CMSampleTimingInfo timing{};
      std::string error;

      std::unique_lock lock(stage.packetMutex);
      if (!extractPacket(stage, sampleBuffer, keyFrame, nalLengthBytes, format,
                         timing, error)) {
        lock.unlock();
        markDecodeFailure(context);
        return;
      }

      const PacketDecision decision =
          decidePacketDrop(stage, context, keyFrame);
      ++stage.packetCount;

      if (decision.drop) {
        CFRelease(format);
        lock.unlock();
        statistics_.intentionalDrops.fetch_add(1, std::memory_order_relaxed);
        repeatOrDrop(context, true);
        return;
      }

      const std::span<const uint8_t> bytes(stage.packetScratch);
      const bool decoded =
          decodeBytes(stage, context, format, timing, keyFrame, bytes, error);
      CFRelease(format);
      lock.unlock();
      if (!decoded)
        markDecodeFailure(context);
    }
  } catch (...) {
    failClaimedContext(context);
  }
}

static void compressionOutputCallback(void *outputCallbackRefCon,
                                      void *sourceFrameRefCon, OSStatus status,
                                      VTEncodeInfoFlags infoFlags,
                                      CMSampleBufferRef sampleBuffer) {
  auto *stage = static_cast<CodecStage *>(outputCallbackRefCon);
  const uint64_t encodeToken =
      static_cast<uint64_t>(reinterpret_cast<uintptr_t>(sourceFrameRefCon));
  if (stage == nullptr || stage->owner == nullptr || encodeToken == 0)
    return;
  try {
    stage->owner->handleCompressed(*stage, encodeToken, status, infoFlags,
                                   sampleBuffer);
  } catch (...) {
    // No C++ exception may cross the VideoToolbox C callback boundary.
  }
}

CVPixelBufferRef CodecGlitchEngineImpl::renderScaled(CVPixelBufferRef input,
                                                     CVPixelBufferPoolRef pool,
                                                     int width, int height,
                                                     float pixelScale) {
  if (input == nullptr || pool == nullptr || ciContext_ == nil)
    return nullptr;
  CVPixelBufferRef output = nullptr;
  if (CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, pool, &output) !=
          kCVReturnSuccess ||
      output == nullptr)
    return nullptr;

  @autoreleasepool {
    CIImage *source = [CIImage imageWithCVPixelBuffer:input];
    const CGRect extent = source.extent;
    if (CGRectIsEmpty(extent)) {
      CFRelease(output);
      return nullptr;
    }
    const CGAffineTransform normalize =
        CGAffineTransformMakeTranslation(-extent.origin.x, -extent.origin.y);
    CIImage *normalized = [source imageByApplyingTransform:normalize];
    const CGFloat scaleX = static_cast<CGFloat>(width) / extent.size.width;
    const CGFloat scaleY = static_cast<CGFloat>(height) / extent.size.height;
    CIImage *scaled = [normalized
        imageByApplyingTransform:CGAffineTransformMakeScale(scaleX, scaleY)];
    scaled = [scaled imageByCroppingToRect:CGRectMake(0, 0, width, height)];
    if (pixelScale > 1.0f) {
      CIFilter *pixelate = [CIFilter filterWithName:@"CIPixellate"];
      [pixelate setValue:scaled forKey:kCIInputImageKey];
      [pixelate setValue:@(pixelScale) forKey:kCIInputScaleKey];
      if (pixelate.outputImage != nil)
        scaled = pixelate.outputImage;
    }
    [ciContext_ render:scaled
        toCVPixelBuffer:output
                 bounds:CGRectMake(0, 0, width, height)
             colorSpace:nil];
  }
  return output;
}

CVPixelBufferRef CodecGlitchEngineImpl::renderFeedback(CVPixelBufferRef input,
                                                       CVPixelBufferRef history,
                                                       float mix) {
  if (input == nullptr || history == nullptr || fullSizePool_ == nullptr ||
      ciContext_ == nil)
    return nullptr;
  CVPixelBufferRef output = nullptr;
  if (CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, fullSizePool_,
                                         &output) != kCVReturnSuccess ||
      output == nullptr)
    return nullptr;

  @autoreleasepool {
    CIImage *current = [CIImage imageWithCVPixelBuffer:input];
    CIImage *previous = [CIImage imageWithCVPixelBuffer:history];
    CIFilter *dissolve = [CIFilter filterWithName:@"CIDissolveTransition"];
    [dissolve setValue:current forKey:kCIInputImageKey];
    [dissolve setValue:previous forKey:kCIInputTargetImageKey];
    [dissolve setValue:@(clampValue(mix, 0.0f, 0.98f)) forKey:kCIInputTimeKey];
    CIImage *result = dissolve.outputImage;
    if (result == nil) {
      CFRelease(output);
      return nullptr;
    }
    result =
        [result imageByCroppingToRect:CGRectMake(0, 0, configuration_.width,
                                                 configuration_.height)];
    [ciContext_ render:result
        toCVPixelBuffer:output
                 bounds:CGRectMake(0, 0, configuration_.width,
                                   configuration_.height)
             colorSpace:nil];
  }
  return output;
}

CVPixelBufferRef
CodecGlitchEngineImpl::renderSliceDropout(CVPixelBufferRef input,
                                          CVPixelBufferRef history,
                                          const FrameContext &context) {
  if (input == nullptr || history == nullptr || fullSizePool_ == nullptr ||
      ciContext_ == nil)
    return nullptr;
  CVPixelBufferRef output = nullptr;
  if (CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, fullSizePool_,
                                         &output) != kCVReturnSuccess ||
      output == nullptr)
    return nullptr;

  @autoreleasepool {
    CIImage *result = [CIImage imageWithCVPixelBuffer:input];
    CIImage *previous = [CIImage imageWithCVPixelBuffer:history];
    CIFilter *pixelate = [CIFilter filterWithName:@"CIPixellate"];
    [pixelate setValue:previous forKey:kCIInputImageKey];
    [pixelate setValue:@(5.0 + context.controls.amount * 25.0)
                forKey:kCIInputScaleKey];
    CIImage *heldRows = pixelate.outputImage ?: previous;

    const int bandCount = 1 + static_cast<int>(context.controls.amount * 3.0f);
    const CGFloat baseHeight =
        configuration_.height * (0.07 + context.controls.amount * 0.18);
    for (int band = 0; band < bandCount; ++band) {
      const uint64_t hash = mixHash(
          context.controls.seed ^ (context.frameIndex * kHashMultiplier) ^
          (static_cast<uint64_t>(band + 1) * 0x9e3779b97f4a7c15ULL));
      if (hashUnit(hash) > 0.58 + context.controls.amount *
                                      (0.24 + 0.16 * context.controls.rate))
        continue;
      const CGFloat bandHeight = std::min<CGFloat>(
          configuration_.height,
          baseHeight * (0.55 + 1.25 * hashUnit(hash ^ 0xa0761d6478bd642fULL)));
      const CGFloat availableY =
          std::max<CGFloat>(0.0, configuration_.height - bandHeight);
      const CGFloat y = hashUnit(hash ^ 0xe7037ed1a0b428dbULL) * availableY;
      CIImage *strip =
          [heldRows imageByCroppingToRect:CGRectMake(0, y, configuration_.width,
                                                     bandHeight)];
      result =
          [strip imageByApplyingFilter:@"CISourceOverCompositing"
                   withInputParameters:@{kCIInputBackgroundImageKey : result}];
    }
    result =
        [result imageByCroppingToRect:CGRectMake(0, 0, configuration_.width,
                                                 configuration_.height)];
    [ciContext_ render:result
        toCVPixelBuffer:output
                 bounds:CGRectMake(0, 0, configuration_.width,
                                   configuration_.height)
             colorSpace:nil];
  }
  return output;
}

CVPixelBufferRef
CodecGlitchEngineImpl::renderSliceTransplant(CVPixelBufferRef input,
                                             CVPixelBufferRef history,
                                             const FrameContext &context) {
  if (input == nullptr || history == nullptr || fullSizePool_ == nullptr ||
      ciContext_ == nil)
    return nullptr;
  CVPixelBufferRef output = nullptr;
  if (CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, fullSizePool_,
                                         &output) != kCVReturnSuccess ||
      output == nullptr)
    return nullptr;

  @autoreleasepool {
    CIImage *result = [CIImage imageWithCVPixelBuffer:input];
    CIImage *previous = [CIImage imageWithCVPixelBuffer:history];
    const int bandCount = 2 + static_cast<int>(context.controls.amount * 10.0f);
    const CGFloat baseHeight =
        5.0 + context.controls.amount * configuration_.height * 0.055;
    for (int band = 0; band < bandCount; ++band) {
      const uint64_t hash =
          mixHash(context.controls.seed ^ context.frameIndex ^
                  (static_cast<uint64_t>(band + 1) * 0x632be59bd9b4e019ULL));
      if (hashUnit(hash) > 0.30 + context.controls.amount * 0.65)
        continue;
      const CGFloat bandHeight = std::min<CGFloat>(
          configuration_.height,
          baseHeight * (0.65 + 0.7 * hashUnit(hash ^ 0x91e10da5c79e7b1dULL)));
      const CGFloat availableY =
          std::max<CGFloat>(0.0, configuration_.height - bandHeight);
      const CGFloat y = hashUnit(hash ^ 0xd1b54a32d192ed03ULL) * availableY;
      const CGFloat horizontalShift =
          (hashUnit(hash ^ 0x94d049bb133111ebULL) - 0.5) *
          configuration_.width * context.controls.feedback * 0.22;
      CIImage *strip =
          [previous imageByCroppingToRect:CGRectMake(0, y, configuration_.width,
                                                     bandHeight)];
      strip = [strip imageByApplyingTransform:CGAffineTransformMakeTranslation(
                                                  horizontalShift, 0)];
      result =
          [strip imageByApplyingFilter:@"CISourceOverCompositing"
                   withInputParameters:@{kCIInputBackgroundImageKey : result}];
    }
    result =
        [result imageByCroppingToRect:CGRectMake(0, 0, configuration_.width,
                                                 configuration_.height)];
    [ciContext_ render:result
        toCVPixelBuffer:output
                 bounds:CGRectMake(0, 0, configuration_.width,
                                   configuration_.height)
             colorSpace:nil];
  }
  return output;
}

CVPixelBufferRef
CodecGlitchEngineImpl::renderPayloadXor(CVPixelBufferRef input,
                                        const FrameContext &context) {
  if (input == nullptr || fullSizePool_ == nullptr || ciContext_ == nil)
    return nullptr;
  CVPixelBufferRef output = nullptr;
  if (CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, fullSizePool_,
                                         &output) != kCVReturnSuccess ||
      output == nullptr)
    return nullptr;

  @autoreleasepool {
    CIImage *current = [CIImage imageWithCVPixelBuffer:input];
    CIFilter *posterize = [CIFilter filterWithName:@"CIColorPosterize"];
    [posterize setValue:current forKey:kCIInputImageKey];
    [posterize setValue:@(3.0 + (1.0 - context.controls.amount) * 8.0)
                 forKey:@"inputLevels"];
    CIImage *digital = posterize.outputImage ?: current;

    const uint64_t frameHash = mixHash(
        context.controls.seed ^ (context.frameIndex * 0xd6e8feb86659fd93ULL));
    CIFilter *matrix = [CIFilter filterWithName:@"CIColorMatrix"];
    [matrix setValue:digital forKey:kCIInputImageKey];
    if ((frameHash & 1U) == 0) {
      [matrix setValue:[CIVector vectorWithX:0 Y:1 Z:0 W:0]
                forKey:@"inputRVector"];
      [matrix setValue:[CIVector vectorWithX:0 Y:0 Z:1 W:0]
                forKey:@"inputGVector"];
      [matrix setValue:[CIVector vectorWithX:1 Y:0 Z:0 W:0]
                forKey:@"inputBVector"];
    } else {
      [matrix setValue:[CIVector vectorWithX:0 Y:0 Z:1 W:0]
                forKey:@"inputRVector"];
      [matrix setValue:[CIVector vectorWithX:1 Y:0 Z:0 W:0]
                forKey:@"inputGVector"];
      [matrix setValue:[CIVector vectorWithX:0 Y:1 Z:0 W:0]
                forKey:@"inputBVector"];
    }
    digital = matrix.outputImage ?: digital;

    CIImage *result = current;
    // Keep the distortion spatially varied without making the Core Image
    // compositor the dominant cost under a busy host application.
    const int tileCount = 3 + static_cast<int>(context.controls.amount * 7.0f);
    const CGFloat grid = 8.0 + std::floor(context.controls.amount * 3.0) * 8.0;
    for (int tile = 0; tile < tileCount; ++tile) {
      const uint64_t hash =
          mixHash(frameHash ^
                  (static_cast<uint64_t>(tile + 1) * 0xa0761d6478bd642fULL));
      if (hashUnit(hash) > 0.35 + context.controls.amount *
                                      (0.40 + 0.20 * context.controls.rate))
        continue;
      const CGFloat tileWidth = std::min<CGFloat>(
          configuration_.width,
          grid * (2.0 + std::floor(hashUnit(hash ^ 0xe7037ed1a0b428dbULL) *
                                   (4.0 + context.controls.amount * 9.0))));
      const CGFloat tileHeight = std::min<CGFloat>(
          configuration_.height,
          grid * (1.0 + std::floor(hashUnit(hash ^ 0x8ebc6af09c88c6e3ULL) *
                                   (2.0 + context.controls.amount * 4.0))));
      const CGFloat x =
          grid *
          std::floor(hashUnit(hash ^ 0x589965cc75374cc3ULL) *
                     std::max<CGFloat>(1.0, (configuration_.width - tileWidth) /
                                                grid));
      const CGFloat y =
          grid *
          std::floor(hashUnit(hash ^ 0x1d8e4e27c47d124fULL) *
                     std::max<CGFloat>(
                         1.0, (configuration_.height - tileHeight) / grid));
      const CGFloat shift =
          grid * std::floor((hashUnit(hash ^ 0xeb44accab455d165ULL) - 0.5) *
                            (3.0 + context.controls.amount * 12.0));
      CIImage *block = [digital
          imageByCroppingToRect:CGRectMake(x, y, tileWidth, tileHeight)];
      block = [block
          imageByApplyingTransform:CGAffineTransformMakeTranslation(shift, 0)];
      result =
          [block imageByApplyingFilter:@"CISourceOverCompositing"
                   withInputParameters:@{kCIInputBackgroundImageKey : result}];
    }
    result =
        [result imageByCroppingToRect:CGRectMake(0, 0, configuration_.width,
                                                 configuration_.height)];
    [ciContext_ render:result
        toCVPixelBuffer:output
                 bounds:CGRectMake(0, 0, configuration_.width,
                                   configuration_.height)
             colorSpace:nil];
  }
  return output;
}

CVPixelBufferRef
CodecGlitchEngineImpl::renderCompressionArtifacts(CVPixelBufferRef input,
                                                  const FrameContext &context,
                                                  bool generationCascade) {
  if (input == nullptr || fullSizePool_ == nullptr || ciContext_ == nil)
    return nullptr;
  CVPixelBufferRef output = nullptr;
  if (CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, fullSizePool_,
                                         &output) != kCVReturnSuccess ||
      output == nullptr)
    return nullptr;

  @autoreleasepool {
    CIImage *source = [CIImage imageWithCVPixelBuffer:input];
    CIImage *degraded = source;
    const double wave = temporalWave(context.frameIndex, context.controls.rate);
    if (generationCascade) {
      CIFilter *posterize = [CIFilter filterWithName:@"CIColorPosterize"];
      [posterize setValue:degraded forKey:kCIInputImageKey];
      [posterize setValue:@(5.0 + (1.0 - context.controls.amount) * 17.0 +
                            (1.0 - wave) * 3.0)
                   forKey:@"inputLevels"];
      degraded = posterize.outputImage ?: degraded;

      CIFilter *sharpen = [CIFilter filterWithName:@"CISharpenLuminance"];
      [sharpen setValue:degraded forKey:kCIInputImageKey];
      [sharpen setValue:@(0.25 + context.controls.amount * 1.35)
                 forKey:kCIInputSharpnessKey];
      degraded = sharpen.outputImage ?: degraded;

      CIFilter *color = [CIFilter filterWithName:@"CIColorControls"];
      [color setValue:degraded forKey:kCIInputImageKey];
      [color setValue:@(1.0 - context.controls.amount * 0.28)
               forKey:kCIInputSaturationKey];
      [color setValue:@(1.0 + context.controls.amount * 0.16)
               forKey:kCIInputContrastKey];
      degraded = color.outputImage ?: degraded;
    } else {
      CIFilter *pixelate = [CIFilter filterWithName:@"CIPixellate"];
      [pixelate setValue:degraded forKey:kCIInputImageKey];
      [pixelate setValue:@(4.0 + context.controls.amount * (6.0 + wave * 10.0))
                  forKey:kCIInputScaleKey];
      degraded = pixelate.outputImage ?: degraded;

      CIFilter *posterize = [CIFilter filterWithName:@"CIColorPosterize"];
      [posterize setValue:degraded forKey:kCIInputImageKey];
      [posterize setValue:@(4.0 + (1.0 - context.controls.amount) * 14.0)
                   forKey:@"inputLevels"];
      degraded = posterize.outputImage ?: degraded;
    }

    CIFilter *dissolve = [CIFilter filterWithName:@"CIDissolveTransition"];
    [dissolve setValue:source forKey:kCIInputImageKey];
    [dissolve setValue:degraded forKey:kCIInputTargetImageKey];
    const double baseMix = generationCascade ? 0.62 : 0.78;
    const double modulation = generationCascade ? 0.18 : 0.22 * wave;
    [dissolve
        setValue:@(clampValue(static_cast<double>(context.controls.amount) *
                                  (baseMix + modulation),
                              0.0, 0.94))
          forKey:kCIInputTimeKey];
    CIImage *result = dissolve.outputImage ?: degraded;
    result =
        [result imageByCroppingToRect:CGRectMake(0, 0, configuration_.width,
                                                 configuration_.height)];
    [ciContext_ render:result
        toCVPixelBuffer:output
                 bounds:CGRectMake(0, 0, configuration_.width,
                                   configuration_.height)
             colorSpace:nil];
  }
  return output;
}

CVPixelBufferRef
CodecGlitchEngineImpl::renderChromaEcho(CVPixelBufferRef input,
                                        CVPixelBufferRef history, float mix) {
  if (input == nullptr || history == nullptr || fullSizePool_ == nullptr ||
      ciContext_ == nil)
    return nullptr;
  CVPixelBufferRef output = nullptr;
  if (CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, fullSizePool_,
                                         &output) != kCVReturnSuccess ||
      output == nullptr)
    return nullptr;

  @autoreleasepool {
    const CGFloat echo = clampValue<CGFloat>(mix, 0.0, 0.98);
    const CGFloat retainCurrentChroma = 1.0 - echo;
    CIImage *current = [CIImage imageWithCVPixelBuffer:input];
    CIImage *previous = [CIImage imageWithCVPixelBuffer:history];

    // currentBase = luma(current) + (1-echo) * chroma(current)
    const CGFloat yR = 0.2126;
    const CGFloat yG = 0.7152;
    const CGFloat yB = 0.0722;
    NSDictionary *currentMatrix = @{
      @"inputRVector" : [CIVector vectorWithX:retainCurrentChroma + echo * yR
                                            Y:echo * yG
                                            Z:echo * yB
                                            W:0],
      @"inputGVector" : [CIVector vectorWithX:echo * yR
                                            Y:retainCurrentChroma + echo * yG
                                            Z:echo * yB
                                            W:0],
      @"inputBVector" : [CIVector vectorWithX:echo * yR
                                            Y:echo * yG
                                            Z:retainCurrentChroma + echo * yB
                                            W:0],
      @"inputAVector" : [CIVector vectorWithX:0 Y:0 Z:0 W:1]
    };
    // oldChroma = echo * (oldRGB - luma(old)).
    NSDictionary *oldMatrix = @{
      @"inputRVector" : [CIVector vectorWithX:echo * (1.0 - yR)
                                            Y:-echo * yG
                                            Z:-echo * yB
                                            W:0],
      @"inputGVector" : [CIVector vectorWithX:-echo * yR
                                            Y:echo * (1.0 - yG)
                                            Z:-echo * yB
                                            W:0],
      @"inputBVector" : [CIVector vectorWithX:-echo * yR
                                            Y:-echo * yG
                                            Z:echo * (1.0 - yB)
                                            W:0],
      @"inputAVector" : [CIVector vectorWithX:0 Y:0 Z:0 W:0]
    };
    CIImage *currentBase = [current imageByApplyingFilter:@"CIColorMatrix"
                                      withInputParameters:currentMatrix];
    CIImage *oldChroma = [previous imageByApplyingFilter:@"CIColorMatrix"
                                     withInputParameters:oldMatrix];
    CIImage *result = [oldChroma
        imageByApplyingFilter:@"CIAdditionCompositing"
          withInputParameters:@{kCIInputBackgroundImageKey : currentBase}];
    if (result == nil) {
      CFRelease(output);
      return nullptr;
    }
    result =
        [result imageByCroppingToRect:CGRectMake(0, 0, configuration_.width,
                                                 configuration_.height)];
    [ciContext_ render:result
        toCVPixelBuffer:output
                 bounds:CGRectMake(0, 0, configuration_.width,
                                   configuration_.height)
             colorSpace:nil];
  }
  return output;
}

void CodecGlitchEngineImpl::handleDecoded(CodecStage &stage,
                                          uint64_t decodeToken, OSStatus status,
                                          VTDecodeInfoFlags infoFlags,
                                          CVImageBufferRef imageBuffer,
                                          CMTime presentationTimeStamp) {
  (void)presentationTimeStamp;
  FrameContext *context = findDecodeContext(decodeToken);
  if (context == nullptr)
    return;
  uint64_t expectedToken = decodeToken;
  if (!context->decodeToken.compare_exchange_strong(
          expectedToken, 0, std::memory_order_acq_rel,
          std::memory_order_acquire)) {
    return;
  }
  try {
    disarmDecodeDeadline(*context);
    if (status != noErr || imageBuffer == nullptr ||
        (infoFlags & kVTDecodeInfo_FrameDropped) != 0) {
      markDecodeFailure(*context);
      return;
    }
    statistics_.decoded.fetch_add(1, std::memory_order_relaxed);
    stage.decoderHasOutput.store(true, std::memory_order_release);
    consecutiveDecodeErrors_.store(0, std::memory_order_release);
    finishDecodedFrame(stage, *context,
                       static_cast<CVPixelBufferRef>(imageBuffer));
  } catch (...) {
    failClaimedContext(*context);
  }
}

void CodecGlitchEngineImpl::finishDecodedFrame(CodecStage &stage,
                                               FrameContext &context,
                                               CVPixelBufferRef imageBuffer) {
  if (context.controls.effect == CodecGlitchEffect::GenerationCascade &&
      context.generation + 1 < context.targetGenerations) {
    const int nextStage =
        context.generation == 0 ? kStageCascadeSecond : kStageCascadeThird;
    ++context.generation;
    CFRetain(imageBuffer);
    dispatch_async(encodeQueue_, ^{
      @autoreleasepool {
        encodeOnStage(stages_[nextStage], context, imageBuffer);
        CFRelease(imageBuffer);
      }
    });
    return;
  }

  CVPixelBufferRef processed = nullptr;
  bool requiredPostProcessing = false;
  if (stage.lowResolution ||
      CVPixelBufferGetWidth(imageBuffer) !=
          static_cast<size_t>(configuration_.width) ||
      CVPixelBufferGetHeight(imageBuffer) !=
          static_cast<size_t>(configuration_.height)) {
    const float pixelScale =
        context.controls.effect == CodecGlitchEffect::ResolutionHop
            ? 3.0f + context.controls.amount * 15.0f
            : 0.0f;
    processed = renderScaled(imageBuffer, fullSizePool_, configuration_.width,
                             configuration_.height, pixelScale);
    requiredPostProcessing = true;
  } else if (context.controls.effect == CodecGlitchEffect::SliceDropout) {
    CVPixelBufferRef history = copyLastOutput();
    if (history != nullptr) {
      processed = renderSliceDropout(imageBuffer, history, context);
      requiredPostProcessing = true;
      CFRelease(history);
    }
  } else if (context.controls.effect == CodecGlitchEffect::SliceTransplant) {
    CVPixelBufferRef history = copyLastOutput();
    if (history != nullptr) {
      processed = renderSliceTransplant(imageBuffer, history, context);
      requiredPostProcessing = true;
      CFRelease(history);
    }
  } else if (context.controls.effect == CodecGlitchEffect::PayloadXor) {
    processed = renderPayloadXor(imageBuffer, context);
    requiredPostProcessing = true;
  } else if (context.controls.effect == CodecGlitchEffect::BitrateCrush) {
    processed = renderCompressionArtifacts(imageBuffer, context, false);
    requiredPostProcessing = true;
  } else if (context.controls.effect == CodecGlitchEffect::GenerationCascade) {
    processed = renderCompressionArtifacts(imageBuffer, context, true);
    requiredPostProcessing = true;
  } else if (context.controls.effect == CodecGlitchEffect::ReferenceTimewarp) {
    const uint64_t hash = mixHash(context.controls.seed ^
                                  (context.frameIndex * 0x9e3779b97f4a7c15ULL));
    const double gate =
        0.16 + context.controls.amount * (0.54 + 0.24 * context.controls.rate);
    if (hashUnit(hash) < gate) {
      const size_t maximumAge =
          2 + static_cast<size_t>(context.controls.feedback * 10.0f);
      const size_t age =
          2 + static_cast<size_t>(hashUnit(hash ^ 0xd1b54a32d192ed03ULL) *
                                  static_cast<double>(maximumAge));
      processed = copyHistoricalOutput(age);
    }
  } else if (context.controls.effect == CodecGlitchEffect::ChromaCodecEcho) {
    CVPixelBufferRef history = copyLastOutput();
    if (history != nullptr) {
      processed =
          renderChromaEcho(imageBuffer, history,
                           context.controls.feedback * context.controls.amount);
      requiredPostProcessing = true;
      CFRelease(history);
    }
  }

  if (requiredPostProcessing && processed == nullptr) {
    markDecodeFailure(context);
    return;
  }

  CVPixelBufferRef output = processed != nullptr ? processed : imageBuffer;
  emit(context, output, false);
  if (processed != nullptr)
    CFRelease(processed);
}

static void decompressionOutputCallback(
    void *decompressionOutputRefCon, void *sourceFrameRefCon, OSStatus status,
    VTDecodeInfoFlags infoFlags, CVImageBufferRef imageBuffer,
    CMTime presentationTimeStamp, CMTime presentationDuration) {
  (void)presentationDuration;
  auto *stage = static_cast<CodecStage *>(decompressionOutputRefCon);
  const uint64_t decodeToken =
      static_cast<uint64_t>(reinterpret_cast<uintptr_t>(sourceFrameRefCon));
  if (stage == nullptr || stage->owner == nullptr || decodeToken == 0)
    return;
  try {
    stage->owner->handleDecoded(*stage, decodeToken, status, infoFlags,
                                imageBuffer, presentationTimeStamp);
  } catch (...) {
    // No C++ exception may cross the VideoToolbox C callback boundary.
  }
}

void CodecGlitchEngineImpl::replaceLastOutput(CVPixelBufferRef imageBuffer) {
  std::lock_guard lock(historyMutex_);
  if (imageBuffer != nullptr)
    CFRetain(imageBuffer);
  if (lastOutput_ != nullptr)
    CFRelease(lastOutput_);
  lastOutput_ = imageBuffer;

  if (!outputHistory_.empty() && imageBuffer != nullptr) {
    CFRetain(imageBuffer);
    CVPixelBufferRef &slot = outputHistory_[outputHistoryNext_];
    if (slot != nullptr)
      CFRelease(slot);
    slot = imageBuffer;
    outputHistoryNext_ = (outputHistoryNext_ + 1) % outputHistory_.size();
    outputHistoryCount_ =
        std::min(outputHistoryCount_ + 1, outputHistory_.size());
  }
}

CVPixelBufferRef CodecGlitchEngineImpl::copyLastOutput() {
  std::lock_guard lock(historyMutex_);
  if (lastOutput_ != nullptr)
    CFRetain(lastOutput_);
  return lastOutput_;
}

CVPixelBufferRef CodecGlitchEngineImpl::copyHistoricalOutput(size_t age) {
  std::lock_guard lock(historyMutex_);
  if (outputHistoryCount_ == 0 || outputHistory_.empty())
    return nullptr;
  age = clampValue<size_t>(age, 1, outputHistoryCount_);
  const size_t index = (outputHistoryNext_ + outputHistory_.size() - age) %
                       outputHistory_.size();
  CVPixelBufferRef result = outputHistory_[index];
  if (result != nullptr)
    CFRetain(result);
  return result;
}

void CodecGlitchEngineImpl::clearOutputHistory() {
  std::lock_guard lock(historyMutex_);
  if (lastOutput_ != nullptr) {
    CFRelease(lastOutput_);
    lastOutput_ = nullptr;
  }
  for (CVPixelBufferRef &frame : outputHistory_) {
    if (frame != nullptr) {
      CFRelease(frame);
      frame = nullptr;
    }
  }
  outputHistoryNext_ = 0;
  outputHistoryCount_ = 0;
}

void CodecGlitchEngineImpl::clearPendingCallbacks(bool clearCallback) {
  const auto state = callbackState_;
  if (!state)
    return;
  std::lock_guard lock(state->mutex);
  if (clearCallback)
    state->callback = {};
  for (CodecGlitchFrame &frame : state->ring)
    frame = CodecGlitchFrame{};
  state->read = state->write = state->count = 0;
}

void CodecGlitchEngineImpl::emit(FrameContext &context,
                                 CVPixelBufferRef imageBuffer,
                                 bool repeatedPreviousFrame,
                                 bool intentionalRepeat,
                                 bool nonIntentionalFallback) {
  if (imageBuffer == nullptr) {
    releaseContext(context);
    return;
  }
  replaceLastOutput(imageBuffer);

  const auto elapsed = std::chrono::steady_clock::now() - context.submittedAt;
  const auto microseconds =
      std::chrono::duration_cast<std::chrono::microseconds>(elapsed).count();
  const double milliseconds = static_cast<double>(microseconds) / 1000.0;

  CodecGlitchFrame frame(imageBuffer);
  frame.frameIndex = context.frameIndex;
  frame.presentationTimeStamp = context.presentationTimeStamp;
  frame.effect = context.controls.effect;
  frame.packetWasModified = context.packetWasModified;
  frame.repeatedPreviousFrame = repeatedPreviousFrame;
  frame.intentionalRepeat = intentionalRepeat;
  frame.nonIntentionalFallback = nonIntentionalFallback;
  frame.codecWarmupFrame = context.codecWarmup;
  frame.watchdogRecoveryFrame = context.watchdogRecovery;
  frame.latencyMilliseconds = milliseconds;

  statistics_.emitted.fetch_add(1, std::memory_order_relaxed);
  statistics_.totalLatencyMicroseconds.fetch_add(
      static_cast<uint64_t>(std::max<int64_t>(0, microseconds)),
      std::memory_order_relaxed);
  statistics_.lastLatencyMilliseconds.store(milliseconds,
                                            std::memory_order_relaxed);
  bool deliverCallback = false;
  bool scheduleCallbackDrain = false;
  const auto callbackState = callbackState_;
  if (callbackState) {
    std::lock_guard lock(callbackState->mutex);
    if (callbackState->callback && !callbackState->ring.empty()) {
      if (callbackState->count == callbackState->ring.size()) {
        callbackState->ring[callbackState->read] = CodecGlitchFrame{};
        callbackState->read =
            (callbackState->read + 1) % callbackState->ring.size();
        --callbackState->count;
        callbackState->drops.fetch_add(1, std::memory_order_relaxed);
      }
      callbackState->ring[callbackState->write] = std::move(frame);
      callbackState->write =
          (callbackState->write + 1) % callbackState->ring.size();
      ++callbackState->count;
      deliverCallback = true;
      if (!callbackState->drainScheduled) {
        callbackState->drainScheduled = true;
        scheduleCallbackDrain = true;
      }
    }
  }
  if (deliverCallback) {
    if (scheduleCallbackDrain) {
      dispatch_group_async(callbackState->group, callbackQueue_, ^{
        drainCallbackState(callbackState);
      });
    }
    releaseContext(context);
    return;
  }

  {
    std::lock_guard lock(pollMutex_);
    if (!pollRing_.empty()) {
      if (pollCount_ == pollRing_.size()) {
        pollRing_[pollRead_] = CodecGlitchFrame{};
        pollRead_ = (pollRead_ + 1) % pollRing_.size();
        --pollCount_;
        statistics_.pollDrops.fetch_add(1, std::memory_order_relaxed);
      }
      pollRing_[pollWrite_] = std::move(frame);
      pollWrite_ = (pollWrite_ + 1) % pollRing_.size();
      ++pollCount_;
    }
  }
  releaseContext(context);
}

void CodecGlitchEngineImpl::repeatOrDrop(FrameContext &context,
                                         bool intentional) {
  (void)intentional;
  CVPixelBufferRef previous = copyLastOutput();
  if (previous != nullptr) {
    emit(context, previous, true, intentional, !intentional);
    CFRelease(previous);
  } else if (context.fallbackInput != nullptr) {
    emit(context, context.fallbackInput, false, false, true);
  } else {
    releaseContext(context);
  }
}

void CodecGlitchEngineImpl::markDecodeFailure(FrameContext &context) {
  if (std::getenv("GLIC_CODEC_DEADLINE_DEBUG") != nullptr)
    std::fprintf(stderr, "codec-failure frame=%llu\n",
                 static_cast<unsigned long long>(context.frameIndex));
  statistics_.codecErrors.fetch_add(1, std::memory_order_relaxed);
  const int errors =
      consecutiveDecodeErrors_.fetch_add(1, std::memory_order_acq_rel) + 1;
  if (errors >= kWatchdogRecoveryThreshold)
    forceRecovery_.store(true, std::memory_order_release);
  repeatOrDrop(context, false);
}

void CodecGlitchEngineImpl::failClaimedContext(FrameContext &context) noexcept {
  try {
    markDecodeFailure(context);
  } catch (...) {
    try {
      releaseContext(context);
    } catch (...) {
      // The context token has already been claimed. Last-resort cleanup must
      // remain noexcept because this path can run inside a C callback.
    }
  }
}

bool CodecGlitchEngineImpl::flush(std::chrono::milliseconds timeout,
                                  std::string &error) {
  if (encodeQueue_ == nullptr) {
    error.clear();
    return true;
  }
  const auto deadline = std::chrono::steady_clock::now() + timeout;

  // A decode callback can enqueue generation two or three while we wait.
  // VideoToolbox CompleteFrames/Wait APIs have no timeout, so fixed
  // FrameContext deadlines are the only blocking boundary used here.
  do {
    if (dispatch_get_specific(&kCodecEncodeQueueKey) != this)
      dispatch_sync(encodeQueue_, ^{
                    });
    if (inFlight_.load(std::memory_order_acquire) == 0)
      break;
    std::unique_lock lock(inFlightMutex_);
    const auto shortDeadline =
        std::min(deadline, std::chrono::steady_clock::now() +
                               std::chrono::milliseconds(20));
    inFlightCondition_.wait_until(lock, shortDeadline, [&] {
      return inFlight_.load(std::memory_order_acquire) == 0;
    });
  } while (std::chrono::steady_clock::now() < deadline);

  if (inFlight_.load(std::memory_order_acquire) != 0) {
    forceRecovery_.store(true, std::memory_order_release);
    error = "Timed out while flushing codec glitch frames";
    return false;
  }
  const auto callbackState = callbackState_;
  if (callbackState && dispatch_get_specific(&kCodecCallbackQueueKey) != this) {
    const auto remaining = deadline - std::chrono::steady_clock::now();
    const int64_t remainingNanoseconds = std::max<int64_t>(
        0, std::chrono::duration_cast<std::chrono::nanoseconds>(remaining)
               .count());
    const dispatch_time_t callbackDeadline =
        dispatch_time(DISPATCH_TIME_NOW, remainingNanoseconds);
    if (dispatch_group_wait(callbackState->group, callbackDeadline) != 0) {
      error = "Timed out while flushing codec glitch output callbacks";
      return false;
    }
  }
  error.clear();
  return true;
}

bool CodecGlitchEngineImpl::reset(std::string &error) {
  {
    std::lock_guard lock(lifecycleMutex_);
    bool expected = false;
    if (!resetting_.compare_exchange_strong(expected, true,
                                            std::memory_order_acq_rel)) {
      error = "Codec glitch reset is already in progress";
      return false;
    }
  }
  if (!flush(std::chrono::milliseconds(2000), error)) {
    resetting_.store(false, std::memory_order_release);
    return false;
  }

  clearOutputHistory();
  clearPendingCallbacks(false);
  {
    std::lock_guard lock(pollMutex_);
    for (CodecGlitchFrame &frame : pollRing_)
      frame = CodecGlitchFrame{};
    pollRead_ = pollWrite_ = pollCount_ = 0;
  }

  __block bool success = false;
  __block std::string resetError;
  const auto rebuild = ^{
    destroyResources();
    success = createResources(resetError);
  };
  if (dispatch_get_specific(&kCodecEncodeQueueKey) == this)
    rebuild();
  else
    dispatch_sync(encodeQueue_, rebuild);
  if (!success) {
    {
      std::lock_guard lock(lifecycleMutex_);
      acceptingSubmissions_.store(false, std::memory_order_release);
    }
    resetting_.store(false, std::memory_order_release);
    error = resetError;
    return false;
  }

  // Fresh VideoToolbox sessions naturally start with a keyframe. A reset is
  // not a watchdog recovery and must not increment the recovery statistic.
  forceRecovery_.store(false, std::memory_order_release);
  consecutiveDecodeErrors_.store(0, std::memory_order_release);
  acceptingSubmissions_.store(true, std::memory_order_release);
  resetting_.store(false, std::memory_order_release);
  error.clear();
  return true;
}

} // namespace

std::unique_ptr<CodecGlitchEngine>
createCodecGlitchEngine(const CodecGlitchConfiguration &configuration,
                        std::string &error) {
  auto engine = std::make_unique<CodecGlitchEngineImpl>(configuration);
  if (!engine->initialize(error))
    return nullptr;
  error.clear();
  return engine;
}

} // namespace glic
