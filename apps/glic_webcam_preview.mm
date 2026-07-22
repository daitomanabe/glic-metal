#include "codec_glitch.hpp"
#include "original_realtime.hpp"
#include "original_realtime_metal.hpp"
#include "preset_loader.hpp"

#import <AVFoundation/AVFoundation.h>
#import <Accelerate/Accelerate.h>
#import <AppKit/AppKit.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <QuartzCore/QuartzCore.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <memory>
#include <mutex>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <unordered_set>
#include <vector>

#ifndef GLIC_SOURCE_PRESETS_DIR
#define GLIC_SOURCE_PRESETS_DIR "presets"
#endif

namespace {

constexpr int kProcessingWidth = 960;
constexpr int kProcessingHeight = 540;
constexpr double kMinimumFramesPerSecond = 20.0;
constexpr double kRealtimeBudgetMilliseconds = 1000.0 / kMinimumFramesPerSecond;
constexpr double kGovernorHighWaterMilliseconds = 45.0;
constexpr double kGovernorLowWaterMilliseconds = 30.0;

enum class QualityMode : int {
  Strict = 0,
  FastMatch = 1,
  Auto20 = 2,
};

enum class ProcessingLane : int {
  OriginalVisual = 0,
  CodecGlitch = 1,
};

enum class FrameSlotState {
  Empty,
  Capturing,
  Ready,
  Processing,
};

struct FrameSlot {
  std::vector<glic::Color> input;
  std::vector<glic::Color> output;
  CVPixelBufferRef pixelBuffer = nullptr;
  CMTime presentationTimeStamp = kCMTimeInvalid;
  uint64_t sequence = 0;
  FrameSlotState state = FrameSlotState::Empty;
};

struct CodecPresetChoice {
  glic::CodecGlitchEffect effect;
  const char *title;
  glic::CodecGlitchControls controls;
};

std::vector<CodecPresetChoice> makeCodecPresetChoices() {
  const auto make = [](glic::CodecGlitchEffect effect, const char *title,
                       float amount, float rate, float feedback) {
    glic::CodecGlitchControls controls;
    controls.effect = effect;
    controls.amount = amount;
    controls.rate = rate;
    controls.feedback = feedback;
    return CodecPresetChoice{effect, title, controls};
  };

  std::vector<CodecPresetChoice> choices{
      make(glic::CodecGlitchEffect::QpPump, "QP Pump", 0.72f, 0.42f, 0.35f),
      make(glic::CodecGlitchEffect::BitrateCrush, "Bitrate Crush", 0.70f, 0.32f,
           0.45f),
      make(glic::CodecGlitchEffect::SliceDropout, "Slice Dropout", 0.48f, 0.35f,
           0.45f),
      make(glic::CodecGlitchEffect::SliceTransplant, "Slice Transplant", 0.58f,
           0.30f, 0.70f),
      make(glic::CodecGlitchEffect::PFrameLoss, "P-Frame Loss", 0.40f, 0.25f,
           0.55f),
      make(glic::CodecGlitchEffect::IdrStarvation, "IDR Starvation", 0.62f,
           0.18f, 0.65f),
      make(glic::CodecGlitchEffect::PayloadXor, "Payload XOR", 0.18f, 0.28f,
           0.45f),
      make(glic::CodecGlitchEffect::ReferenceTimewarp, "Reference Timewarp",
           0.58f, 0.26f, 0.72f),
      make(glic::CodecGlitchEffect::CodecFeedback, "Codec Feedback", 0.60f,
           0.30f, 0.78f),
      make(glic::CodecGlitchEffect::GenerationCascade, "Generation Cascade",
           0.55f, 0.22f, 0.60f),
      make(glic::CodecGlitchEffect::ResolutionHop, "Resolution Hop", 0.75f,
           0.24f, 0.50f),
      make(glic::CodecGlitchEffect::ChromaCodecEcho, "Chroma Codec Echo", 0.68f,
           0.28f, 0.72f),
  };
  choices[1].controls.crushedBitRate = 100000;
  choices[9].controls.cascadeGenerations = 3;
  choices[10].controls.reducedResolutionScale = 0.25f;
  return choices;
}

const char *qualityModeName(QualityMode mode) {
  switch (mode) {
  case QualityMode::Strict:
    return "Strict";
  case QualityMode::FastMatch:
    return "Fast Match";
  case QualityMode::Auto20:
    return "Auto 20fps";
  }
  return "Unknown";
}

glic::OriginalRealtimeMetalOptions laneOptions(QualityMode mode) {
  glic::OriginalRealtimeMetalOptions options;
  if (mode != QualityMode::Strict) {
    options.fidelity = glic::OriginalRealtimeMetalFidelity::FastMatch;
    options.segmentationReuseFrames = 2;
  }
  return options;
}

struct PresetChoice {
  std::string name;
  glic::OriginalPresetConfig config;
};

std::optional<std::filesystem::path> findPresetDirectory() {
  if (const char *environment = std::getenv("GLIC_PRESETS_DIR");
      environment != nullptr && environment[0] != '\0') {
    std::filesystem::path candidate(environment);
    if (std::filesystem::is_directory(candidate))
      return candidate;
  }

  @autoreleasepool {
    NSString *bundlePath = [NSBundle.mainBundle pathForResource:@"Presets"
                                                         ofType:nil];
    if (bundlePath != nil) {
      std::filesystem::path candidate(bundlePath.fileSystemRepresentation);
      if (std::filesystem::is_directory(candidate))
        return candidate;
    }
  }

  for (const std::filesystem::path &candidate :
       {std::filesystem::current_path() / "presets"}) {
    if (std::filesystem::is_directory(candidate))
      return candidate;
  }
  return std::nullopt;
}

std::vector<PresetChoice>
loadSupportedPresets(const std::filesystem::path &directory) {
  std::vector<PresetChoice> choices;
  for (const std::string &name :
       glic::PresetLoader::listPresets(directory.string())) {
    glic::OriginalPresetConfig config;
    if (!glic::PresetLoader::loadOriginalPresetByName(directory.string(), name,
                                                      config))
      continue;
    if (!glic::evaluateOriginalRealtimeSupport(config).supported)
      continue;
    choices.push_back({name, config});
  }
  std::sort(choices.begin(), choices.end(),
            [](const PresetChoice &left, const PresetChoice &right) {
              return left.name < right.name;
            });
  return choices;
}

int findPresetIndex(const std::vector<PresetChoice> &choices,
                    std::string_view name) {
  const auto match =
      std::find_if(choices.begin(), choices.end(),
                   [&](const auto &choice) { return choice.name == name; });
  return match == choices.end()
             ? -1
             : static_cast<int>(std::distance(choices.begin(), match));
}

int runSelfTest() {
  const auto directory = findPresetDirectory();
  if (!directory) {
    std::fprintf(stderr, "FAIL preset directory was not found\n");
    return 2;
  }
  const auto choices = loadSupportedPresets(*directory);
  if (choices.size() != 37u) {
    std::fprintf(stderr, "FAIL expected 37 supported presets, got %zu\n",
                 choices.size());
    return 3;
  }

  std::string error;
  auto lane = glic::createOriginalRealtimeMetalLane(error);
  if (!lane) {
    std::fprintf(stderr, "FAIL Metal initialization: %s\n", error.c_str());
    return 4;
  }
  const std::size_t pixelCount =
      static_cast<std::size_t>(kProcessingWidth) * kProcessingHeight;
  std::vector<glic::Color> input(pixelCount);
  std::vector<glic::Color> output(pixelCount);
  for (int y = 0; y < kProcessingHeight; ++y) {
    for (int x = 0; x < kProcessingWidth; ++x) {
      input[static_cast<std::size_t>(y) * kProcessingWidth + x] =
          glic::makeColor(static_cast<uint8_t>((x * 255) / kProcessingWidth),
                          static_cast<uint8_t>((y * 255) / kProcessingHeight),
                          static_cast<uint8_t>((x + y) & 255));
    }
  }

  for (const std::string_view name :
       {std::string_view("vv02"), std::string_view("beautifulwave"),
        std::string_view("colour_mess2")}) {
    const int index = findPresetIndex(choices, name);
    if (index < 0 ||
        !lane->prepare(kProcessingWidth, kProcessingHeight,
                       choices[static_cast<std::size_t>(index)].config,
                       error)) {
      std::fprintf(stderr, "FAIL prepare %.*s: %s\n",
                   static_cast<int>(name.size()), name.data(), error.c_str());
      return 5;
    }
    glic::OriginalRealtimeMetalFrameStats stats;
    if (!lane->process(input, output, 0, &stats, error) ||
        !stats.pipelineAccountingPassed || stats.totalMilliseconds <= 0.0) {
      std::fprintf(stderr, "FAIL process %.*s: %s\n",
                   static_cast<int>(name.size()), name.data(), error.c_str());
      return 6;
    }
    std::printf("preset=%.*s total_ms=%.3f gpu_ms=%.3f\n",
                static_cast<int>(name.size()), name.data(),
                stats.totalMilliseconds, stats.gpuMilliseconds);
  }
  glic::OriginalRealtimeMetalOptions fastOptions;
  fastOptions.fidelity = glic::OriginalRealtimeMetalFidelity::FastMatch;
  fastOptions.segmentationReuseFrames = 2;
  auto fastLane = glic::createOriginalRealtimeMetalLane(fastOptions, error);
  const int adaptiveIndex = findPresetIndex(choices, "colour_mess2");
  glic::OriginalRealtimeMetalFrameStats firstFastStats;
  glic::OriginalRealtimeMetalFrameStats secondFastStats;
  if (!fastLane || adaptiveIndex < 0 ||
      !fastLane->prepare(
          kProcessingWidth, kProcessingHeight,
          choices[static_cast<std::size_t>(adaptiveIndex)].config, error) ||
      !fastLane->process(input, output, 0, &firstFastStats, error) ||
      !fastLane->process(input, output, 1, &secondFastStats, error) ||
      !firstFastStats.fastCdf97 || !secondFastStats.fastCdf97 ||
      !secondFastStats.adaptiveScheduleReused ||
      secondFastStats.adaptiveScheduleAge != 1u) {
    std::fprintf(stderr, "FAIL Fast Match lane: %s\n", error.c_str());
    return 7;
  }
  std::printf("fast_match=reuse%d first_ms=%.3f second_ms=%.3f\n",
              secondFastStats.adaptiveScheduleReused ? 1 : 0,
              firstFastStats.totalMilliseconds,
              secondFastStats.totalMilliseconds);

  const auto codecPresets = makeCodecPresetChoices();
  if (codecPresets.size() !=
      static_cast<std::size_t>(glic::CodecGlitchEffect::Count)) {
    std::fprintf(stderr, "FAIL expected 12 codec presets, got %zu\n",
                 codecPresets.size());
    return 8;
  }
  std::unordered_set<std::string> codecNames;
  for (const auto &preset : codecPresets) {
    const char *name = glic::codecGlitchEffectName(preset.effect);
    glic::CodecGlitchEffect roundTrip{};
    if (name == nullptr || name[0] == '\0' ||
        !codecNames.emplace(name).second ||
        !glic::codecGlitchEffectFromName(name, roundTrip) ||
        roundTrip != preset.effect) {
      std::fprintf(stderr, "FAIL invalid codec preset catalog entry: %s\n",
                   name == nullptr ? "(null)" : name);
      return 9;
    }
  }

  NSDictionary *pixelAttributes = @{
    (id)kCVPixelBufferPixelFormatTypeKey : @(kCVPixelFormatType_32BGRA),
    (id)kCVPixelBufferWidthKey : @(kProcessingWidth),
    (id)kCVPixelBufferHeightKey : @(kProcessingHeight),
    (id)kCVPixelBufferMetalCompatibilityKey : @YES,
    (id)kCVPixelBufferIOSurfacePropertiesKey : @{},
  };
  CVPixelBufferRef codecInput = nullptr;
  if (CVPixelBufferCreate(kCFAllocatorDefault, kProcessingWidth,
                          kProcessingHeight, kCVPixelFormatType_32BGRA,
                          (__bridge CFDictionaryRef)pixelAttributes,
                          &codecInput) != kCVReturnSuccess ||
      codecInput == nullptr) {
    std::fprintf(stderr, "FAIL codec self-test input allocation\n");
    return 10;
  }
  CVPixelBufferLockBaseAddress(codecInput, 0);
  auto *codecBytes =
      static_cast<uint8_t *>(CVPixelBufferGetBaseAddress(codecInput));
  const std::size_t codecRowBytes = CVPixelBufferGetBytesPerRow(codecInput);
  for (int y = 0; y < kProcessingHeight; ++y) {
    for (int x = 0; x < kProcessingWidth; ++x) {
      uint8_t *pixel = codecBytes +
                       static_cast<std::size_t>(y) * codecRowBytes +
                       static_cast<std::size_t>(x) * 4;
      pixel[0] = static_cast<uint8_t>((x + y) & 255);
      pixel[1] = static_cast<uint8_t>((y * 255) / kProcessingHeight);
      pixel[2] = static_cast<uint8_t>((x * 255) / kProcessingWidth);
      pixel[3] = 255;
    }
  }
  CVPixelBufferUnlockBaseAddress(codecInput, 0);

  glic::CodecGlitchConfiguration codecConfiguration;
  codecConfiguration.width = kProcessingWidth;
  codecConfiguration.height = kProcessingHeight;
  codecConfiguration.framesPerSecond = 30;
  codecConfiguration.maximumInFlightFrames = 24;
  std::atomic<uint64_t> codecOutputCount{0};
  std::atomic<bool> codecOutputInvalid{false};
  auto codecLane = glic::createCodecGlitchEngine(codecConfiguration, error);
  if (!codecLane) {
    std::fprintf(stderr, "FAIL codec engine initialization: %s\n",
                 error.c_str());
    CFRelease(codecInput);
    return 11;
  }

  glic::CodecGlitchOutputCallback selfTestCallback =
      [&](const glic::CodecGlitchFrame &frame) {
        CVPixelBufferRef pixelBuffer = frame.pixelBuffer();
        if (pixelBuffer == nullptr ||
            CVPixelBufferGetWidth(pixelBuffer) != kProcessingWidth ||
            CVPixelBufferGetHeight(pixelBuffer) != kProcessingHeight ||
            CVPixelBufferGetPixelFormatType(pixelBuffer) !=
                kCVPixelFormatType_32BGRA) {
          codecOutputInvalid.store(true, std::memory_order_release);
        }
        codecOutputCount.fetch_add(1, std::memory_order_relaxed);
      };

  uint64_t codecFrameIndex = 0;
  for (const auto &preset : codecPresets) {
    if (!codecLane->reset(error)) {
      std::fprintf(stderr, "FAIL codec reset %s: %s\n", preset.title,
                   error.c_str());
      CFRelease(codecInput);
      return 12;
    }
    codecLane->setOutputCallback(selfTestCallback);
    codecLane->setControls(preset.controls);
    const uint64_t outputBefore = codecOutputCount.load();
    for (int frame = 0; frame < 18; ++frame) {
      const CMTime timestamp =
          CMTimeMake(static_cast<int64_t>(codecFrameIndex), 30);
      if (!codecLane->submit(codecInput, codecFrameIndex++, timestamp, error)) {
        std::fprintf(stderr, "FAIL codec submit %s: %s\n", preset.title,
                     error.c_str());
        CFRelease(codecInput);
        return 13;
      }
    }
    if (!codecLane->flush(std::chrono::seconds(5), error)) {
      std::fprintf(stderr, "FAIL codec flush %s: %s\n", preset.title,
                   error.c_str());
      CFRelease(codecInput);
      return 14;
    }
    const uint64_t emitted = codecOutputCount.load() - outputBefore;
    if (emitted == 0 || codecOutputInvalid.load(std::memory_order_acquire)) {
      std::fprintf(stderr, "FAIL codec output %s emitted=%llu valid=%s\n",
                   preset.title, static_cast<unsigned long long>(emitted),
                   codecOutputInvalid.load() ? "false" : "true");
      CFRelease(codecInput);
      return 15;
    }
    std::printf("codec_preset=%s emitted=%llu\n",
                glic::codecGlitchEffectName(preset.effect),
                static_cast<unsigned long long>(emitted));
  }
  const auto codecStats = codecLane->stats();
  codecLane->setOutputCallback({});
  if (!codecLane->flush(std::chrono::seconds(5), error) ||
      !codecStats.hardwareEncoder || !codecStats.hardwareDecoder) {
    std::fprintf(stderr,
                 "FAIL codec hardware/drain encoder=%s decoder=%s: %s\n",
                 codecStats.hardwareEncoder ? "true" : "false",
                 codecStats.hardwareDecoder ? "true" : "false", error.c_str());
    CFRelease(codecInput);
    return 16;
  }
  CFRelease(codecInput);
  std::printf(
      "PASS webcam preview lanes original_presets=%zu codec_presets=%zu "
      "resolution=%dx%d codec_hw=true\n",
      choices.size(), codecPresets.size(), kProcessingWidth, kProcessingHeight);
  return 0;
}

NSString *authorizationStatusName(AVAuthorizationStatus status) {
  switch (status) {
  case AVAuthorizationStatusAuthorized:
    return @"authorized";
  case AVAuthorizationStatusDenied:
    return @"denied";
  case AVAuthorizationStatusRestricted:
    return @"restricted";
  case AVAuthorizationStatusNotDetermined:
    return @"not-determined";
  }
}

} // namespace

@interface GLICPreviewView : NSView
@property(nonatomic, readonly) AVSampleBufferDisplayLayer *sampleBufferLayer;
@end

@implementation GLICPreviewView

- (instancetype)initWithFrame:(NSRect)frame {
  self = [super initWithFrame:frame];
  if (self != nil) {
    self.wantsLayer = YES;
    self.layer.backgroundColor = NSColor.blackColor.CGColor;
  }
  return self;
}

- (CALayer *)makeBackingLayer {
  AVSampleBufferDisplayLayer *layer = [AVSampleBufferDisplayLayer layer];
  layer.videoGravity = AVLayerVideoGravityResizeAspect;
  layer.backgroundColor = NSColor.blackColor.CGColor;
  return layer;
}

- (AVSampleBufferDisplayLayer *)sampleBufferLayer {
  return (AVSampleBufferDisplayLayer *)self.layer;
}

@end

@interface GLICAppController
    : NSObject <NSApplicationDelegate,
                AVCaptureVideoDataOutputSampleBufferDelegate> {
  NSWindow *_window;
  GLICPreviewView *_previewView;
  NSPopUpButton *_lanePopup;
  NSPopUpButton *_presetPopup;
  NSPopUpButton *_qualityPopup;
  NSStackView *_codecControlsStack;
  NSSlider *_codecAmountSlider;
  NSTextField *_codecAmountLabel;
  NSButton *_codecResetButton;
  NSTextField *_statusLabel;
  NSTextField *_metricsLabel;
  NSMenu *_laneMenu;
  NSMenu *_presetMenu;
  NSMenu *_qualityMenu;
  NSMenuItem *_qualityRootMenuItem;

  AVCaptureSession *_captureSession;
  AVCaptureVideoDataOutput *_captureOutput;
  dispatch_queue_t _captureQueue;
  dispatch_queue_t _processingQueue;
  CVPixelBufferPoolRef _displayPixelBufferPool;
  CMVideoFormatDescriptionRef _displayFormatDescription;
  std::mutex _displayFormatMutex;

  std::filesystem::path _presetDirectory;
  std::vector<PresetChoice> _presets;
  std::vector<CodecPresetChoice> _codecPresets;
  std::unordered_set<std::string> _fastMatchAllowlist;
  std::unique_ptr<glic::OriginalRealtimeMetalLane> _lane;
  std::unique_ptr<glic::CodecGlitchEngine> _codecLane;
  std::array<FrameSlot, 3> _frameSlots;
  std::mutex _frameSlotMutex;
  uint64_t _captureSequence;
  std::atomic<int> _pendingLane;
  std::atomic<int> _pendingPresetIndex;
  std::atomic<int> _pendingCodecPresetIndex;
  std::atomic<int> _pendingQualityMode;
  std::atomic<float> _pendingCodecAmount;
  std::atomic<uint64_t> _requestedGeneration;
  std::atomic<bool> _processingScheduled;
  std::atomic<bool> _stopping;
  std::atomic<bool> _displayEnqueuePending;
  std::atomic<uint64_t> _droppedCaptureFrames;
  std::atomic<uint64_t> _droppedCodecSubmissions;
  std::atomic<uint64_t> _codecRecoveryFrames;
  ProcessingLane _activeLane;
  uint64_t _activeGeneration;
  int _activePresetIndex;
  int _activeCodecPresetIndex;
  QualityMode _activeQualityMode;
  bool _activeFastMatch;
  uint32_t _governorReuseFrames;
  uint64_t _frameIndex;
  uint64_t _rateFrameCount;
  std::chrono::steady_clock::time_point _rateStart;
  double _smoothedTotalMilliseconds;
  double _smoothedGpuMilliseconds;
  std::mutex _codecMetricsMutex;
  uint64_t _codecRateFrameCount;
  std::chrono::steady_clock::time_point _codecRateStart;
  double _smoothedCodecLatencyMilliseconds;
  id _activityToken;
}
@end

@implementation GLICAppController

- (instancetype)init {
  self = [super init];
  if (self != nil) {
    _captureQueue = dispatch_queue_create("ws.daito.glic.webcam.capture",
                                          DISPATCH_QUEUE_SERIAL);
    _processingQueue = dispatch_queue_create("ws.daito.glic.webcam.processing",
                                             DISPATCH_QUEUE_SERIAL);
    _codecPresets = makeCodecPresetChoices();
    _pendingLane.store(static_cast<int>(ProcessingLane::OriginalVisual));
    _pendingPresetIndex.store(-1);
    _pendingCodecPresetIndex.store(0);
    _pendingQualityMode.store(static_cast<int>(QualityMode::Auto20));
    _pendingCodecAmount.store(_codecPresets.front().controls.amount);
    _requestedGeneration.store(1);
    _processingScheduled.store(false);
    _stopping.store(false);
    _displayEnqueuePending.store(false);
    _droppedCaptureFrames.store(0);
    _droppedCodecSubmissions.store(0);
    _codecRecoveryFrames.store(0);
    _activeLane = ProcessingLane::OriginalVisual;
    _activeGeneration = 0;
    _activePresetIndex = -1;
    _activeCodecPresetIndex = -1;
    _activeQualityMode = QualityMode::Auto20;
    _activeFastMatch = false;
    _governorReuseFrames = 2;
    _captureSequence = 0;
    _frameIndex = 0;
    _rateFrameCount = 0;
    _rateStart = std::chrono::steady_clock::now();
    _smoothedTotalMilliseconds = 0.0;
    _smoothedGpuMilliseconds = 0.0;
    _codecRateFrameCount = 0;
    _codecRateStart = std::chrono::steady_clock::now();
    _smoothedCodecLatencyMilliseconds = 0.0;
    _displayPixelBufferPool = nullptr;
    _displayFormatDescription = nullptr;
  }
  return self;
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
  (void)notification;
  _activityToken = [NSProcessInfo.processInfo
      beginActivityWithOptions:(NSActivityUserInitiatedAllowingIdleSystemSleep |
                                NSActivityLatencyCritical)
                        reason:@"Realtime webcam Metal and video codec "
                               @"processing"];
  [self buildWindow];
  [self loadPresetMenu];
  [self requestCameraAccessAndStart];
  if (NSApp.isActive)
    [_window makeKeyAndOrderFront:nil];
  else
    [_window orderFront:nil];
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:
    (NSApplication *)sender {
  (void)sender;
  return YES;
}

- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender
                    hasVisibleWindows:(BOOL)hasVisibleWindows {
  (void)sender;
  if (!hasVisibleWindows)
    [_window makeKeyAndOrderFront:nil];
  return YES;
}

- (void)applicationWillTerminate:(NSNotification *)notification {
  (void)notification;
  _stopping.store(true, std::memory_order_release);
  if (_captureQueue != nil) {
    dispatch_sync(_captureQueue, ^{
      if (self->_captureSession.running)
        [self->_captureSession stopRunning];
      [self->_captureOutput setSampleBufferDelegate:nil queue:nullptr];
    });
  }
  if (_processingQueue != nil)
    dispatch_sync(_processingQueue, ^{
      if (self->_codecLane) {
        self->_codecLane->setOutputCallback({});
        std::string flushError;
        self->_codecLane->flush(std::chrono::milliseconds(750), flushError);
        self->_codecLane.reset();
      }
      self->_lane.reset();
      std::lock_guard lock(self->_frameSlotMutex);
      for (auto &slot : self->_frameSlots) {
        if (slot.pixelBuffer != nullptr) {
          CFRelease(slot.pixelBuffer);
          slot.pixelBuffer = nullptr;
        }
        slot.state = FrameSlotState::Empty;
      }
    });
  {
    std::lock_guard lock(_displayFormatMutex);
    if (_displayFormatDescription != nullptr) {
      CFRelease(_displayFormatDescription);
      _displayFormatDescription = nullptr;
    }
  }
  if (_displayPixelBufferPool != nullptr) {
    CFRelease(_displayPixelBufferPool);
    _displayPixelBufferPool = nullptr;
  }
  if (_activityToken != nil) {
    [NSProcessInfo.processInfo endActivity:_activityToken];
    _activityToken = nil;
  }
}

- (void)buildWindow {
  _window = [[NSWindow alloc]
      initWithContentRect:NSMakeRect(80, 80, 1100, 700)
                styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                           NSWindowStyleMaskMiniaturizable |
                           NSWindowStyleMaskResizable)
                  backing:NSBackingStoreBuffered
                    defer:NO];
  _window.title = @"GLIC Webcam Preview";
  _window.minSize = NSMakeSize(720, 480);
  [_window setFrameAutosaveName:@"GLICWebcamPreviewWindow"];

  NSView *content = [[NSView alloc] initWithFrame:_window.contentView.bounds];
  content.translatesAutoresizingMaskIntoConstraints = NO;
  _window.contentView = content;

  NSVisualEffectView *header =
      [[NSVisualEffectView alloc] initWithFrame:NSZeroRect];
  header.translatesAutoresizingMaskIntoConstraints = NO;
  header.material = NSVisualEffectMaterialHeaderView;
  header.blendingMode = NSVisualEffectBlendingModeWithinWindow;

  NSTextField *title = [NSTextField labelWithString:@"GLIC METAL · CAMERA"];
  title.translatesAutoresizingMaskIntoConstraints = NO;
  title.font = [NSFont systemFontOfSize:12 weight:NSFontWeightSemibold];
  title.textColor = NSColor.secondaryLabelColor;

  _lanePopup = [[NSPopUpButton alloc] initWithFrame:NSZeroRect pullsDown:NO];
  _lanePopup.translatesAutoresizingMaskIntoConstraints = NO;
  [_lanePopup addItemsWithTitles:@[ @"Original Visual", @"Codec Glitch" ]];
  [_lanePopup selectItemAtIndex:0];
  _lanePopup.target = self;
  _lanePopup.action = @selector(selectLane:);
  _lanePopup.toolTip =
      @"Choose the original GLIC image lane or H.264 codec glitch lane";

  _presetPopup = [[NSPopUpButton alloc] initWithFrame:NSZeroRect pullsDown:NO];
  _presetPopup.translatesAutoresizingMaskIntoConstraints = NO;
  _presetPopup.target = self;
  _presetPopup.action = @selector(selectPreset:);
  _presetPopup.toolTip = @"Original GLIC preset";

  _qualityPopup = [[NSPopUpButton alloc] initWithFrame:NSZeroRect pullsDown:NO];
  _qualityPopup.translatesAutoresizingMaskIntoConstraints = NO;
  [_qualityPopup
      addItemsWithTitles:@[ @"Strict", @"Fast Match", @"Auto 20fps" ]];
  [_qualityPopup selectItemAtIndex:static_cast<NSInteger>(QualityMode::Auto20)];
  _qualityPopup.target = self;
  _qualityPopup.action = @selector(selectQuality:);
  _qualityPopup.toolTip = @"Strict fidelity or realtime Fast Match policy";

  _codecAmountLabel = [NSTextField labelWithString:@"Amount 70%"];
  _codecAmountLabel.translatesAutoresizingMaskIntoConstraints = NO;
  _codecAmountLabel.font =
      [NSFont monospacedDigitSystemFontOfSize:11 weight:NSFontWeightRegular];
  _codecAmountSlider = [NSSlider sliderWithValue:0.70
                                        minValue:0.0
                                        maxValue:1.0
                                          target:self
                                          action:@selector(changeCodecAmount:)];
  _codecAmountSlider.translatesAutoresizingMaskIntoConstraints = NO;
  _codecAmountSlider.continuous = YES;
  _codecAmountSlider.toolTip =
      @"Codec quality, post-effect, and feedback intensity";
  _codecResetButton = [NSButton buttonWithTitle:@"Reset"
                                         target:self
                                         action:@selector(resetCodecStream:)];
  _codecResetButton.translatesAutoresizingMaskIntoConstraints = NO;
  _codecResetButton.bezelStyle = NSBezelStyleRounded;
  _codecResetButton.toolTip =
      @"Clear codec history and request a clean recovery frame";
  _codecControlsStack = [[NSStackView alloc] initWithFrame:NSZeroRect];
  [_codecControlsStack addArrangedSubview:_codecAmountLabel];
  [_codecControlsStack addArrangedSubview:_codecAmountSlider];
  [_codecControlsStack addArrangedSubview:_codecResetButton];
  _codecControlsStack.translatesAutoresizingMaskIntoConstraints = NO;
  _codecControlsStack.orientation = NSUserInterfaceLayoutOrientationHorizontal;
  _codecControlsStack.alignment = NSLayoutAttributeCenterY;
  _codecControlsStack.spacing = 8;
  _codecControlsStack.hidden = YES;

  _statusLabel = [NSTextField labelWithString:@"Starting…"];
  _statusLabel.translatesAutoresizingMaskIntoConstraints = NO;
  _statusLabel.font = [NSFont monospacedSystemFontOfSize:12
                                                  weight:NSFontWeightMedium];
  _statusLabel.alignment = NSTextAlignmentRight;
  _statusLabel.lineBreakMode = NSLineBreakByTruncatingMiddle;
  [_statusLabel
      setContentCompressionResistancePriority:NSLayoutPriorityDefaultLow
                               forOrientation:
                                   NSLayoutConstraintOrientationHorizontal];

  _metricsLabel = [NSTextField labelWithString:@"960×540 · ≥20 fps"];
  _metricsLabel.translatesAutoresizingMaskIntoConstraints = NO;
  _metricsLabel.font =
      [NSFont monospacedDigitSystemFontOfSize:11 weight:NSFontWeightRegular];
  _metricsLabel.textColor = NSColor.secondaryLabelColor;
  _metricsLabel.alignment = NSTextAlignmentRight;
  _metricsLabel.lineBreakMode = NSLineBreakByTruncatingHead;
  [_metricsLabel
      setContentCompressionResistancePriority:NSLayoutPriorityDefaultLow
                               forOrientation:
                                   NSLayoutConstraintOrientationHorizontal];

  NSStackView *rightStack = [[NSStackView alloc] initWithFrame:NSZeroRect];
  [rightStack addArrangedSubview:_statusLabel];
  [rightStack addArrangedSubview:_metricsLabel];
  rightStack.translatesAutoresizingMaskIntoConstraints = NO;
  rightStack.orientation = NSUserInterfaceLayoutOrientationVertical;
  rightStack.alignment = NSLayoutAttributeTrailing;
  rightStack.spacing = 2;

  [header addSubview:title];
  [header addSubview:_lanePopup];
  [header addSubview:_presetPopup];
  [header addSubview:_qualityPopup];
  [header addSubview:_codecControlsStack];
  [header addSubview:rightStack];

  _previewView = [[GLICPreviewView alloc] initWithFrame:NSZeroRect];
  _previewView.translatesAutoresizingMaskIntoConstraints = NO;

  [content addSubview:header];
  [content addSubview:_previewView];

  [NSLayoutConstraint activateConstraints:@[
    [header.leadingAnchor constraintEqualToAnchor:content.leadingAnchor],
    [header.trailingAnchor constraintEqualToAnchor:content.trailingAnchor],
    [header.topAnchor constraintEqualToAnchor:content.topAnchor],
    [header.heightAnchor constraintEqualToConstant:92],
    [title.leadingAnchor constraintEqualToAnchor:header.leadingAnchor
                                        constant:18],
    [title.topAnchor constraintEqualToAnchor:header.topAnchor constant:14],
    [_lanePopup.leadingAnchor constraintEqualToAnchor:header.leadingAnchor
                                             constant:18],
    [_lanePopup.bottomAnchor constraintEqualToAnchor:header.bottomAnchor
                                            constant:-10],
    [_lanePopup.widthAnchor constraintEqualToConstant:150],
    [_presetPopup.leadingAnchor
        constraintEqualToAnchor:_lanePopup.trailingAnchor
                       constant:8],
    [_presetPopup.centerYAnchor
        constraintEqualToAnchor:_lanePopup.centerYAnchor],
    [_presetPopup.widthAnchor constraintEqualToConstant:220],
    [_qualityPopup.leadingAnchor
        constraintEqualToAnchor:_presetPopup.trailingAnchor
                       constant:8],
    [_qualityPopup.centerYAnchor
        constraintEqualToAnchor:_lanePopup.centerYAnchor],
    [_qualityPopup.widthAnchor constraintEqualToConstant:128],
    [_codecControlsStack.leadingAnchor
        constraintEqualToAnchor:_presetPopup.trailingAnchor
                       constant:8],
    [_codecControlsStack.centerYAnchor
        constraintEqualToAnchor:_lanePopup.centerYAnchor],
    [_codecAmountSlider.widthAnchor constraintEqualToConstant:96],
    [rightStack.trailingAnchor constraintEqualToAnchor:header.trailingAnchor
                                              constant:-18],
    [rightStack.topAnchor constraintEqualToAnchor:header.topAnchor constant:8],
    [rightStack.leadingAnchor
        constraintGreaterThanOrEqualToAnchor:title.trailingAnchor
                                    constant:18],
    [_previewView.leadingAnchor constraintEqualToAnchor:content.leadingAnchor],
    [_previewView.trailingAnchor
        constraintEqualToAnchor:content.trailingAnchor],
    [_previewView.topAnchor constraintEqualToAnchor:header.bottomAnchor],
    [_previewView.bottomAnchor constraintEqualToAnchor:content.bottomAnchor],
  ]];

  [self installApplicationMenu];
}

- (void)installApplicationMenu {
  NSMenu *mainMenu = [[NSMenu alloc] initWithTitle:@""];
  NSMenuItem *applicationItem = [[NSMenuItem alloc] initWithTitle:@""
                                                           action:nil
                                                    keyEquivalent:@""];
  [mainMenu addItem:applicationItem];
  NSMenu *applicationMenu =
      [[NSMenu alloc] initWithTitle:@"GLIC Webcam Preview"];
  [applicationMenu addItemWithTitle:@"Quit GLIC Webcam Preview"
                             action:@selector(terminate:)
                      keyEquivalent:@"q"];
  applicationItem.submenu = applicationMenu;

  NSMenuItem *laneItem = [[NSMenuItem alloc] initWithTitle:@"Lane"
                                                    action:nil
                                             keyEquivalent:@""];
  [mainMenu addItem:laneItem];
  _laneMenu = [[NSMenu alloc] initWithTitle:@"Lane"];
  for (NSInteger index = 0; index < 2; ++index) {
    NSMenuItem *item = [[NSMenuItem alloc]
        initWithTitle:@[ @"Original Visual", @"Codec Glitch" ][index]
               action:@selector(selectLane:)
        keyEquivalent:@""];
    item.target = self;
    item.tag = index;
    [_laneMenu addItem:item];
  }
  laneItem.submenu = _laneMenu;

  NSMenuItem *presetItem = [[NSMenuItem alloc] initWithTitle:@"Preset"
                                                      action:nil
                                               keyEquivalent:@""];
  [mainMenu addItem:presetItem];
  _presetMenu = [[NSMenu alloc] initWithTitle:@"Preset"];
  presetItem.submenu = _presetMenu;

  _qualityRootMenuItem = [[NSMenuItem alloc] initWithTitle:@"Quality"
                                                    action:nil
                                             keyEquivalent:@""];
  [mainMenu addItem:_qualityRootMenuItem];
  _qualityMenu = [[NSMenu alloc] initWithTitle:@"Quality"];
  for (NSInteger index = 0; index < 3; ++index) {
    NSMenuItem *item = [[NSMenuItem alloc]
        initWithTitle:@[ @"Strict", @"Fast Match", @"Auto 20fps" ][index]
               action:@selector(selectQuality:)
        keyEquivalent:@""];
    item.target = self;
    item.tag = index;
    [_qualityMenu addItem:item];
  }
  _qualityRootMenuItem.submenu = _qualityMenu;
  NSApp.mainMenu = mainMenu;
}

- (void)loadFastMatchAllowlist {
  _fastMatchAllowlist.clear();
  NSMutableArray<NSString *> *candidates = [NSMutableArray array];
  NSString *bundlePath =
      [NSBundle.mainBundle pathForResource:@"fast-match-allowlist"
                                    ofType:@"json"];
  if (bundlePath != nil)
    [candidates addObject:bundlePath];
  [candidates addObject:@"config/fast-match-allowlist.json"];
  for (NSString *path in candidates) {
    NSData *data = [NSData dataWithContentsOfFile:path];
    if (data == nil)
      continue;
    NSError *jsonError = nil;
    id root = [NSJSONSerialization JSONObjectWithData:data
                                              options:0
                                                error:&jsonError];
    if (![root isKindOfClass:NSDictionary.class] || jsonError != nil)
      continue;
    NSDictionary *dictionary = (NSDictionary *)root;
    if (![dictionary[@"schema"]
            isEqualToString:@"glic-fast-match-allowlist-v1"] ||
        ![dictionary[@"allowlist"] isKindOfClass:NSArray.class])
      continue;
    for (id value in (NSArray *)dictionary[@"allowlist"]) {
      if ([value isKindOfClass:NSString.class])
        _fastMatchAllowlist.emplace([(NSString *)value UTF8String]);
    }
    return;
  }
}

- (void)rebuildPresetControls {
  const auto lane =
      static_cast<ProcessingLane>(_pendingLane.load(std::memory_order_acquire));
  [_presetPopup removeAllItems];
  [_presetMenu removeAllItems];

  if (lane == ProcessingLane::OriginalVisual) {
    for (std::size_t index = 0; index < _presets.size(); ++index) {
      NSString *name =
          [NSString stringWithUTF8String:_presets[index].name.c_str()];
      [_presetPopup addItemWithTitle:name];
      NSMenuItem *item =
          [[NSMenuItem alloc] initWithTitle:name
                                     action:@selector(selectPreset:)
                              keyEquivalent:@""];
      item.target = self;
      item.tag = static_cast<NSInteger>(index);
      [_presetMenu addItem:item];
    }
    int index = _pendingPresetIndex.load(std::memory_order_acquire);
    if (index < 0 || static_cast<std::size_t>(index) >= _presets.size())
      index = 0;
    [_presetPopup selectItemAtIndex:index];
    _presetPopup.toolTip = @"Original GLIC preset";
    _qualityPopup.hidden = NO;
    _codecControlsStack.hidden = YES;
    _qualityRootMenuItem.enabled = YES;
  } else {
    for (std::size_t index = 0; index < _codecPresets.size(); ++index) {
      NSString *title =
          [NSString stringWithUTF8String:_codecPresets[index].title];
      [_presetPopup addItemWithTitle:title];
      NSMenuItem *item =
          [[NSMenuItem alloc] initWithTitle:title
                                     action:@selector(selectPreset:)
                              keyEquivalent:@""];
      item.target = self;
      item.tag = static_cast<NSInteger>(index);
      [_presetMenu addItem:item];
    }
    int index = _pendingCodecPresetIndex.load(std::memory_order_acquire);
    if (index < 0 || static_cast<std::size_t>(index) >= _codecPresets.size())
      index = 0;
    [_presetPopup selectItemAtIndex:index];
    const float amount = _pendingCodecAmount.load(std::memory_order_acquire);
    _codecAmountSlider.doubleValue = amount;
    _codecAmountLabel.stringValue =
        [NSString stringWithFormat:@"Amount %.0f%%", amount * 100.0f];
    _presetPopup.toolTip = @"Stateful H.264 codec glitch preset";
    _qualityPopup.hidden = YES;
    _codecControlsStack.hidden = NO;
    _qualityRootMenuItem.enabled = NO;
  }

  [_lanePopup selectItemAtIndex:static_cast<NSInteger>(lane)];
  for (NSMenuItem *item in _laneMenu.itemArray)
    item.state = item.tag == static_cast<NSInteger>(lane)
                     ? NSControlStateValueOn
                     : NSControlStateValueOff;
  const NSInteger quality = _qualityPopup.indexOfSelectedItem;
  for (NSMenuItem *item in _qualityMenu.itemArray)
    item.state =
        item.tag == quality ? NSControlStateValueOn : NSControlStateValueOff;
  const NSInteger selected = _presetPopup.indexOfSelectedItem;
  for (NSMenuItem *item in _presetMenu.itemArray)
    item.state =
        item.tag == selected ? NSControlStateValueOn : NSControlStateValueOff;
}

- (void)loadPresetMenu {
  const auto directory = findPresetDirectory();
  if (!directory) {
    [self showStatus:@"Preset directory not found" error:YES];
    _presetPopup.enabled = NO;
    return;
  }
  _presetDirectory = *directory;
  _presets = loadSupportedPresets(_presetDirectory);
  [self loadFastMatchAllowlist];
  if (_presets.empty()) {
    [self showStatus:@"No supported presets" error:YES];
    _presetPopup.enabled = NO;
    return;
  }

  int initialIndex = findPresetIndex(_presets, "vv02");
  if (initialIndex < 0)
    initialIndex = 0;
  _pendingPresetIndex.store(initialIndex, std::memory_order_release);
  [self rebuildPresetControls];
}

- (void)selectLane:(id)sender {
  NSInteger index = -1;
  if ([sender isKindOfClass:NSPopUpButton.class])
    index = [(NSPopUpButton *)sender indexOfSelectedItem];
  else if ([sender isKindOfClass:NSMenuItem.class])
    index = [(NSMenuItem *)sender tag];
  if (index < 0 || index > static_cast<NSInteger>(ProcessingLane::CodecGlitch))
    return;
  const int previous = _pendingLane.exchange(static_cast<int>(index));
  if (previous != static_cast<int>(index))
    _requestedGeneration.fetch_add(1, std::memory_order_acq_rel);
  [self rebuildPresetControls];
  [self flushPreviewRenderer];
  [self showStatus:@"Switching processing lane…" error:NO];
}

- (void)selectPreset:(id)sender {
  NSInteger index = -1;
  if ([sender isKindOfClass:NSPopUpButton.class])
    index = [(NSPopUpButton *)sender indexOfSelectedItem];
  else if ([sender isKindOfClass:NSMenuItem.class])
    index = [(NSMenuItem *)sender tag];
  const auto lane =
      static_cast<ProcessingLane>(_pendingLane.load(std::memory_order_acquire));
  const std::size_t count = lane == ProcessingLane::OriginalVisual
                                ? _presets.size()
                                : _codecPresets.size();
  if (index < 0 || static_cast<std::size_t>(index) >= count)
    return;
  [_presetPopup selectItemAtIndex:index];
  if (lane == ProcessingLane::OriginalVisual) {
    const int previous = _pendingPresetIndex.exchange(static_cast<int>(index));
    if (previous != static_cast<int>(index))
      _requestedGeneration.fetch_add(1, std::memory_order_acq_rel);
  } else {
    const int previous =
        _pendingCodecPresetIndex.exchange(static_cast<int>(index));
    const float amount =
        _codecPresets[static_cast<std::size_t>(index)].controls.amount;
    _pendingCodecAmount.store(amount, std::memory_order_release);
    _codecAmountSlider.doubleValue = amount;
    _codecAmountLabel.stringValue =
        [NSString stringWithFormat:@"Amount %.0f%%", amount * 100.0f];
    if (previous != static_cast<int>(index))
      _requestedGeneration.fetch_add(1, std::memory_order_acq_rel);
  }
  for (NSMenuItem *item in _presetMenu.itemArray)
    item.state =
        item.tag == index ? NSControlStateValueOn : NSControlStateValueOff;
  [self flushPreviewRenderer];
  [self showStatus:@"Switching preset…" error:NO];
}

- (void)selectQuality:(id)sender {
  NSInteger index = -1;
  if ([sender isKindOfClass:NSPopUpButton.class])
    index = [(NSPopUpButton *)sender indexOfSelectedItem];
  else if ([sender isKindOfClass:NSMenuItem.class])
    index = [(NSMenuItem *)sender tag];
  if (index < 0 || index > static_cast<NSInteger>(QualityMode::Auto20))
    return;
  [_qualityPopup selectItemAtIndex:index];
  const int previous = _pendingQualityMode.exchange(static_cast<int>(index));
  if (previous != static_cast<int>(index))
    _requestedGeneration.fetch_add(1, std::memory_order_acq_rel);
  for (NSMenuItem *item in _qualityMenu.itemArray)
    item.state =
        item.tag == index ? NSControlStateValueOn : NSControlStateValueOff;
  [self showStatus:@"Switching quality…" error:NO];
}

- (void)changeCodecAmount:(NSSlider *)sender {
  const float amount = static_cast<float>(sender.doubleValue);
  _pendingCodecAmount.store(amount, std::memory_order_release);
  _codecAmountLabel.stringValue =
      [NSString stringWithFormat:@"Amount %.0f%%", amount * 100.0f];
}

- (void)resetCodecStream:(id)sender {
  (void)sender;
  if (static_cast<ProcessingLane>(_pendingLane.load()) !=
      ProcessingLane::CodecGlitch)
    return;
  _requestedGeneration.fetch_add(1, std::memory_order_acq_rel);
  [self flushPreviewRenderer];
  [self showStatus:@"Resetting codec stream…" error:NO];
}

- (void)requestCameraAccessAndStart {
  const AVAuthorizationStatus status =
      [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo];
  if (status == AVAuthorizationStatusAuthorized) {
    [self startCapture];
    return;
  }
  if (status == AVAuthorizationStatusNotDetermined) {
    [self showStatus:@"Waiting for camera permission…" error:NO];
    __weak GLICAppController *weakSelf = self;
    [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo
                             completionHandler:^(BOOL granted) {
                               dispatch_async(dispatch_get_main_queue(), ^{
                                 GLICAppController *strongSelf = weakSelf;
                                 if (strongSelf == nil)
                                   return;
                                 if (granted)
                                   [strongSelf startCapture];
                                 else
                                   [strongSelf
                                       showStatus:@"Camera permission denied"
                                            error:YES];
                               });
                             }];
    return;
  }
  [self showStatus:[NSString stringWithFormat:@"Camera access %@",
                                              authorizationStatusName(status)]
             error:YES];
}

- (void)startCapture {
  if (_presets.empty())
    return;
  [self showStatus:@"Opening camera…" error:NO];
  dispatch_async(_captureQueue, ^{
    @autoreleasepool {
      [self configureAndStartCapture];
    }
  });
}

- (void)configureAndStartCapture {
  AVCaptureDevice *camera =
      [AVCaptureDevice defaultDeviceWithMediaType:AVMediaTypeVideo];
  if (camera == nil) {
    dispatch_async(dispatch_get_main_queue(), ^{
      [self showStatus:@"No camera was found" error:YES];
    });
    return;
  }

  NSError *inputError = nil;
  AVCaptureDeviceInput *input =
      [AVCaptureDeviceInput deviceInputWithDevice:camera error:&inputError];
  if (input == nil) {
    NSString *message = [NSString
        stringWithFormat:@"Camera input failed: %@",
                         inputError.localizedDescription ?: @"unknown error"];
    dispatch_async(dispatch_get_main_queue(), ^{
      [self showStatus:message error:YES];
    });
    return;
  }

  AVCaptureSession *session = [[AVCaptureSession alloc] init];
  [session beginConfiguration];
  if ([session canSetSessionPreset:AVCaptureSessionPreset960x540])
    session.sessionPreset = AVCaptureSessionPreset960x540;
  else
    session.sessionPreset = AVCaptureSessionPresetHigh;
  if (![session canAddInput:input]) {
    [session commitConfiguration];
    dispatch_async(dispatch_get_main_queue(), ^{
      [self showStatus:@"Camera input is unavailable" error:YES];
    });
    return;
  }
  [session addInput:input];

  AVCaptureVideoDataOutput *output = [[AVCaptureVideoDataOutput alloc] init];
  output.alwaysDiscardsLateVideoFrames = YES;
  output.videoSettings = @{
    (id)kCVPixelBufferPixelFormatTypeKey : @(kCVPixelFormatType_32BGRA),
    (id)kCVPixelBufferWidthKey : @(kProcessingWidth),
    (id)kCVPixelBufferHeightKey : @(kProcessingHeight),
    (id)kCVPixelBufferMetalCompatibilityKey : @YES,
  };
  [output setSampleBufferDelegate:self queue:_captureQueue];
  if (![session canAddOutput:output]) {
    [session commitConfiguration];
    dispatch_async(dispatch_get_main_queue(), ^{
      [self showStatus:@"Camera video output is unavailable" error:YES];
    });
    return;
  }
  [session addOutput:output];
  AVCaptureConnection *connection =
      [output connectionWithMediaType:AVMediaTypeVideo];
  if (connection.isVideoMirroringSupported) {
    connection.automaticallyAdjustsVideoMirroring = NO;
    connection.videoMirrored = YES;
  }
  if (connection.isVideoMinFrameDurationSupported)
    connection.videoMinFrameDuration = CMTimeMake(1, 30);
  if (connection.isVideoMaxFrameDurationSupported)
    connection.videoMaxFrameDuration = CMTimeMake(1, 30);
  [session commitConfiguration];

  NSDictionary *poolAttributes = @{
    (id)kCVPixelBufferPixelFormatTypeKey : @(kCVPixelFormatType_32BGRA),
    (id)kCVPixelBufferWidthKey : @(kProcessingWidth),
    (id)kCVPixelBufferHeightKey : @(kProcessingHeight),
    (id)kCVPixelBufferBytesPerRowAlignmentKey : @64,
    (id)kCVPixelBufferIOSurfacePropertiesKey : @{},
  };
  const CVReturn poolStatus = CVPixelBufferPoolCreate(
      kCFAllocatorDefault, nullptr, (__bridge CFDictionaryRef)poolAttributes,
      &_displayPixelBufferPool);
  if (poolStatus != kCVReturnSuccess) {
    dispatch_async(dispatch_get_main_queue(), ^{
      [self showStatus:@"Display buffer pool creation failed" error:YES];
    });
    return;
  }

  const std::size_t pixelCount =
      static_cast<std::size_t>(kProcessingWidth) * kProcessingHeight;
  for (auto &slot : _frameSlots) {
    slot.input.resize(pixelCount);
    slot.output.resize(pixelCount);
    slot.state = FrameSlotState::Empty;
  }
  _captureSession = session;
  _captureOutput = output;
  [session startRunning];
  dispatch_async(dispatch_get_main_queue(), ^{
    [self showStatus:@"LIVE · Metal" error:NO];
  });
}

- (void)deactivateCodecLane {
  if (!_codecLane)
    return;
  _codecLane->setOutputCallback({});
  std::string flushError;
  _codecLane->flush(std::chrono::milliseconds(750), flushError);
  _codecLane.reset();
  _activeCodecPresetIndex = -1;
}

- (bool)activatePendingOriginalPreset:(std::string &)error {
  const int pending = _pendingPresetIndex.load(std::memory_order_acquire);
  const int pendingQuality =
      _pendingQualityMode.load(std::memory_order_acquire);
  if (pending < 0)
    return false;
  const auto requestedQuality = static_cast<QualityMode>(pendingQuality);
  if (_activeLane == ProcessingLane::OriginalVisual &&
      pending == _activePresetIndex && requestedQuality == _activeQualityMode &&
      _lane != nullptr) {
    _activeGeneration = _requestedGeneration.load(std::memory_order_acquire);
    return _lane != nullptr;
  }
  if (static_cast<std::size_t>(pending) >= _presets.size()) {
    error = "Preset index is out of range";
    return false;
  }

  const std::string &presetName =
      _presets[static_cast<std::size_t>(pending)].name;
  const bool useFastMatch = requestedQuality == QualityMode::FastMatch ||
                            (requestedQuality == QualityMode::Auto20 &&
                             _fastMatchAllowlist.contains(presetName));
  auto candidate = glic::createOriginalRealtimeMetalLane(
      laneOptions(useFastMatch ? QualityMode::FastMatch : QualityMode::Strict),
      error);
  if (!candidate ||
      !candidate->prepare(kProcessingWidth, kProcessingHeight,
                          _presets[static_cast<std::size_t>(pending)].config,
                          error))
    return false;
  [self deactivateCodecLane];
  _lane = std::move(candidate);
  _activeLane = ProcessingLane::OriginalVisual;
  _activeGeneration = _requestedGeneration.load(std::memory_order_acquire);
  _activePresetIndex = pending;
  _activeQualityMode = requestedQuality;
  _activeFastMatch = useFastMatch;
  _governorReuseFrames = useFastMatch ? 2u : 1u;
  _frameIndex = 0;
  _droppedCaptureFrames.store(0, std::memory_order_release);
  _rateFrameCount = 0;
  _rateStart = std::chrono::steady_clock::now();
  _smoothedTotalMilliseconds = 0.0;
  _smoothedGpuMilliseconds = 0.0;
  return true;
}

- (bool)activatePendingCodecPreset:(std::string &)error {
  const int pending = _pendingCodecPresetIndex.load(std::memory_order_acquire);
  if (pending < 0 ||
      static_cast<std::size_t>(pending) >= _codecPresets.size()) {
    error = "Codec preset index is out of range";
    return false;
  }

  const uint64_t requestedGeneration =
      _requestedGeneration.load(std::memory_order_acquire);
  const float requestedAmount =
      _pendingCodecAmount.load(std::memory_order_acquire);
  if (_activeLane == ProcessingLane::CodecGlitch && _codecLane != nullptr &&
      _activeCodecPresetIndex == pending &&
      _activeGeneration == requestedGeneration) {
    auto controls = _codecLane->controls();
    if (std::abs(controls.amount - requestedAmount) > 0.0001f) {
      controls.amount = requestedAmount;
      _codecLane->setControls(controls);
    }
    return true;
  }

  _lane.reset();
  _activePresetIndex = -1;
  if (_codecLane) {
    _codecLane->setOutputCallback({});
    std::string drainError;
    const bool drained =
        _codecLane->flush(std::chrono::milliseconds(750), drainError);
    if (!drained || !_codecLane->reset(error))
      _codecLane.reset();
  }
  if (!_codecLane) {
    glic::CodecGlitchConfiguration configuration;
    configuration.width = kProcessingWidth;
    configuration.height = kProcessingHeight;
    configuration.framesPerSecond = 30;
    configuration.maximumInFlightFrames = 4;
    _codecLane = glic::createCodecGlitchEngine(configuration, error);
    if (!_codecLane)
      return false;
  }

  auto controls = _codecPresets[static_cast<std::size_t>(pending)].controls;
  controls.amount = requestedAmount;
  _codecLane->setControls(controls);
  _activeLane = ProcessingLane::CodecGlitch;
  _activeGeneration = requestedGeneration;
  _activeCodecPresetIndex = pending;
  _frameIndex = 0;
  _droppedCaptureFrames.store(0, std::memory_order_release);
  _droppedCodecSubmissions.store(0, std::memory_order_release);
  _codecRecoveryFrames.store(0, std::memory_order_release);
  {
    std::lock_guard lock(_codecMetricsMutex);
    _codecRateFrameCount = 0;
    _codecRateStart = std::chrono::steady_clock::now();
    _smoothedCodecLatencyMilliseconds = 0.0;
  }

  __weak GLICAppController *weakSelf = self;
  const uint64_t callbackGeneration = _activeGeneration;
  _codecLane->setOutputCallback([weakSelf, callbackGeneration](
                                    const glic::CodecGlitchFrame &frame) {
    @autoreleasepool {
      GLICAppController *strongSelf = weakSelf;
      if (strongSelf == nil ||
          strongSelf->_stopping.load(std::memory_order_acquire) ||
          strongSelf->_requestedGeneration.load(std::memory_order_acquire) !=
              callbackGeneration)
        return;
      if (frame.watchdogRecoveryFrame)
        strongSelf->_codecRecoveryFrames.fetch_add(1,
                                                   std::memory_order_relaxed);
      [strongSelf recordCodecOutput:frame generation:callbackGeneration];
      [strongSelf presentOutputPixelBuffer:frame.pixelBuffer()
                                generation:callbackGeneration];
    }
  });
  return true;
}

- (bool)activatePendingProcessingLane:(std::string &)error {
  const auto lane =
      static_cast<ProcessingLane>(_pendingLane.load(std::memory_order_acquire));
  if (lane == ProcessingLane::CodecGlitch)
    return [self activatePendingCodecPreset:error];
  return [self activatePendingOriginalPreset:error];
}

- (bool)copyOrScaleInputPixelBuffer:(CVPixelBufferRef)pixelBuffer
                        destination:(std::vector<glic::Color> &)pixels {
  const int sourceWidth = static_cast<int>(CVPixelBufferGetWidth(pixelBuffer));
  const int sourceHeight =
      static_cast<int>(CVPixelBufferGetHeight(pixelBuffer));
  const std::size_t sourceRowBytes = CVPixelBufferGetBytesPerRow(pixelBuffer);
  const auto *source =
      static_cast<const uint8_t *>(CVPixelBufferGetBaseAddress(pixelBuffer));
  if (source == nullptr)
    return false;

  auto *destination = reinterpret_cast<uint8_t *>(pixels.data());
  constexpr std::size_t destinationRowBytes =
      static_cast<std::size_t>(kProcessingWidth) * sizeof(glic::Color);
  if (sourceWidth == kProcessingWidth && sourceHeight == kProcessingHeight) {
    if (sourceRowBytes == destinationRowBytes) {
      std::memcpy(destination, source, destinationRowBytes * kProcessingHeight);
    } else {
      for (int y = 0; y < kProcessingHeight; ++y)
        std::memcpy(destination +
                        static_cast<std::size_t>(y) * destinationRowBytes,
                    source + static_cast<std::size_t>(y) * sourceRowBytes,
                    destinationRowBytes);
    }
    return true;
  }

  vImage_Buffer sourceBuffer = {const_cast<uint8_t *>(source),
                                static_cast<vImagePixelCount>(sourceHeight),
                                static_cast<vImagePixelCount>(sourceWidth),
                                sourceRowBytes};
  vImage_Buffer destinationBuffer = {
      destination, static_cast<vImagePixelCount>(kProcessingHeight),
      static_cast<vImagePixelCount>(kProcessingWidth), destinationRowBytes};
  return vImageScale_ARGB8888(&sourceBuffer, &destinationBuffer, nullptr,
                              kvImageHighQualityResampling) == kvImageNoError;
}

- (void)flushPreviewRenderer {
  if (!NSThread.isMainThread) {
    dispatch_async(dispatch_get_main_queue(), ^{
      [self flushPreviewRenderer];
    });
    return;
  }
  [self->_previewView.sampleBufferLayer.sampleBufferRenderer flush];
}

- (void)presentOutputPixelBuffer:(CVPixelBufferRef)pixelBuffer
                      generation:(uint64_t)generation {
  if (pixelBuffer == nullptr ||
      generation != _requestedGeneration.load(std::memory_order_acquire) ||
      _displayEnqueuePending.exchange(true, std::memory_order_acq_rel))
    return;

  CMVideoFormatDescriptionRef formatDescription = nullptr;
  {
    std::lock_guard lock(_displayFormatMutex);
    if (_displayFormatDescription == nullptr ||
        !CMVideoFormatDescriptionMatchesImageBuffer(_displayFormatDescription,
                                                    pixelBuffer)) {
      CMVideoFormatDescriptionRef candidate = nullptr;
      if (CMVideoFormatDescriptionCreateForImageBuffer(
              kCFAllocatorDefault, pixelBuffer, &candidate) != noErr) {
        _displayEnqueuePending.store(false, std::memory_order_release);
        return;
      }
      if (_displayFormatDescription != nullptr)
        CFRelease(_displayFormatDescription);
      _displayFormatDescription = candidate;
    }
    formatDescription = _displayFormatDescription;
    CFRetain(formatDescription);
  }

  CMSampleTimingInfo timing = kCMTimingInfoInvalid;
  CMSampleBufferRef sampleBuffer = nullptr;
  const OSStatus sampleStatus = CMSampleBufferCreateForImageBuffer(
      kCFAllocatorDefault, pixelBuffer, true, nullptr, nullptr,
      formatDescription, &timing, &sampleBuffer);
  CFRelease(formatDescription);
  if (sampleStatus != noErr || sampleBuffer == nullptr) {
    _displayEnqueuePending.store(false, std::memory_order_release);
    return;
  }
  CFArrayRef attachments =
      CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, true);
  if (attachments != nullptr && CFArrayGetCount(attachments) > 0) {
    auto dictionary = static_cast<CFMutableDictionaryRef>(
        const_cast<void *>(CFArrayGetValueAtIndex(attachments, 0)));
    CFDictionarySetValue(dictionary, kCMSampleAttachmentKey_DisplayImmediately,
                         kCFBooleanTrue);
  }

  dispatch_async(dispatch_get_main_queue(), ^{
    if (generation ==
        self->_requestedGeneration.load(std::memory_order_acquire)) {
      AVSampleBufferVideoRenderer *renderer =
          self->_previewView.sampleBufferLayer.sampleBufferRenderer;
      if (renderer.status == AVQueuedSampleBufferRenderingStatusFailed)
        [renderer flush];
      if (renderer.readyForMoreMediaData)
        [renderer enqueueSampleBuffer:sampleBuffer];
    }
    CFRelease(sampleBuffer);
    self->_displayEnqueuePending.store(false, std::memory_order_release);
  });
}

- (void)presentOutputFrame:(const std::vector<glic::Color> &)pixels
                generation:(uint64_t)generation {
  if (_displayPixelBufferPool == nullptr ||
      _displayEnqueuePending.load(std::memory_order_acquire))
    return;

  CVPixelBufferRef displayPixelBuffer = nullptr;
  if (CVPixelBufferPoolCreatePixelBuffer(
          kCFAllocatorDefault, _displayPixelBufferPool, &displayPixelBuffer) !=
      kCVReturnSuccess)
    return;
  CVPixelBufferLockBaseAddress(displayPixelBuffer, 0);
  auto *destination =
      static_cast<uint8_t *>(CVPixelBufferGetBaseAddress(displayPixelBuffer));
  const std::size_t destinationRowBytes =
      CVPixelBufferGetBytesPerRow(displayPixelBuffer);
  constexpr std::size_t sourceRowBytes =
      static_cast<std::size_t>(kProcessingWidth) * sizeof(glic::Color);
  const auto *source = reinterpret_cast<const uint8_t *>(pixels.data());
  for (int y = 0; y < kProcessingHeight; ++y)
    std::memcpy(destination + static_cast<std::size_t>(y) * destinationRowBytes,
                source + static_cast<std::size_t>(y) * sourceRowBytes,
                sourceRowBytes);
  CVPixelBufferUnlockBaseAddress(displayPixelBuffer, 0);
  [self presentOutputPixelBuffer:displayPixelBuffer generation:generation];
  CFRelease(displayPixelBuffer);
}

- (void)recordCodecOutput:(const glic::CodecGlitchFrame &)frame
               generation:(uint64_t)generation {
  double processedFps = 0.0;
  double latencyMilliseconds = 0.0;
  bool publish = false;
  {
    std::lock_guard lock(_codecMetricsMutex);
    constexpr double smoothing = 0.12;
    if (_smoothedCodecLatencyMilliseconds == 0.0)
      _smoothedCodecLatencyMilliseconds = frame.latencyMilliseconds;
    else
      _smoothedCodecLatencyMilliseconds +=
          smoothing *
          (frame.latencyMilliseconds - _smoothedCodecLatencyMilliseconds);
    ++_codecRateFrameCount;
    const auto now = std::chrono::steady_clock::now();
    const double elapsed =
        std::chrono::duration<double>(now - _codecRateStart).count();
    if (elapsed >= 0.5) {
      processedFps = static_cast<double>(_codecRateFrameCount) / elapsed;
      latencyMilliseconds = _smoothedCodecLatencyMilliseconds;
      _codecRateFrameCount = 0;
      _codecRateStart = now;
      publish = true;
    }
  }
  if (!publish)
    return;

  const uint64_t captureDrops = _droppedCaptureFrames.load();
  const uint64_t submitDrops = _droppedCodecSubmissions.load();
  const uint64_t recoveries = _codecRecoveryFrames.load();
  const std::string effect = glic::codecGlitchEffectName(frame.effect);
  dispatch_async(dispatch_get_main_queue(), ^{
    if (generation !=
        self->_requestedGeneration.load(std::memory_order_acquire))
      return;
    const bool realtime = processedFps >= kMinimumFramesPerSecond &&
                          latencyMilliseconds <= kRealtimeBudgetMilliseconds;
    self->_statusLabel.stringValue =
        [NSString stringWithFormat:@"%@ · %s", realtime ? @"LIVE" : @"SLOW",
                                   effect.c_str()];
    self->_statusLabel.textColor =
        realtime ? NSColor.systemGreenColor : NSColor.systemOrangeColor;
    self->_metricsLabel.stringValue = [NSString
        stringWithFormat:@"960×540  %.1f fps  Codec %.2f ms  H.264 HW  drop "
                         @"%llu/%llu  rec %llu",
                         processedFps, latencyMilliseconds,
                         static_cast<unsigned long long>(captureDrops),
                         static_cast<unsigned long long>(submitDrops),
                         static_cast<unsigned long long>(recoveries)];
  });
}

- (void)processLatestFrames {
  while (!_stopping.load(std::memory_order_acquire)) {
    FrameSlot *slot = nullptr;
    CVPixelBufferRef inputPixelBuffer = nullptr;
    CMTime presentationTimeStamp = kCMTimeInvalid;
    uint64_t captureSequence = 0;
    {
      std::lock_guard lock(_frameSlotMutex);
      int newest = -1;
      uint64_t newestSequence = 0;
      for (std::size_t index = 0; index < _frameSlots.size(); ++index) {
        if (_frameSlots[index].state == FrameSlotState::Ready &&
            (newest < 0 || _frameSlots[index].sequence > newestSequence)) {
          newest = static_cast<int>(index);
          newestSequence = _frameSlots[index].sequence;
        }
      }
      if (newest < 0) {
        _processingScheduled.store(false, std::memory_order_release);
        return;
      }
      for (std::size_t index = 0; index < _frameSlots.size(); ++index) {
        if (static_cast<int>(index) != newest &&
            _frameSlots[index].state == FrameSlotState::Ready) {
          if (_frameSlots[index].pixelBuffer != nullptr) {
            CFRelease(_frameSlots[index].pixelBuffer);
            _frameSlots[index].pixelBuffer = nullptr;
          }
          _frameSlots[index].state = FrameSlotState::Empty;
          _droppedCaptureFrames.fetch_add(1, std::memory_order_relaxed);
        }
      }
      slot = &_frameSlots[static_cast<std::size_t>(newest)];
      slot->state = FrameSlotState::Processing;
      inputPixelBuffer = slot->pixelBuffer;
      slot->pixelBuffer = nullptr;
      presentationTimeStamp = slot->presentationTimeStamp;
      captureSequence = slot->sequence;
    }

    std::string error;
    if (![self activatePendingProcessingLane:error]) {
      if (inputPixelBuffer != nullptr)
        CFRelease(inputPixelBuffer);
      {
        std::lock_guard lock(_frameSlotMutex);
        slot->state = FrameSlotState::Empty;
        _processingScheduled.store(false, std::memory_order_release);
      }
      NSString *message =
          [NSString stringWithFormat:@"Preset failed: %s", error.c_str()];
      dispatch_async(dispatch_get_main_queue(), ^{
        [self showStatus:message error:YES];
      });
      return;
    }

    if (_activeLane == ProcessingLane::CodecGlitch) {
      const bool submitted =
          inputPixelBuffer != nullptr &&
          _codecLane->submit(inputPixelBuffer, captureSequence,
                             presentationTimeStamp, error);
      if (inputPixelBuffer != nullptr)
        CFRelease(inputPixelBuffer);
      {
        std::lock_guard lock(_frameSlotMutex);
        slot->state = FrameSlotState::Empty;
      }
      if (!submitted)
        _droppedCodecSubmissions.fetch_add(1, std::memory_order_relaxed);
      continue;
    }

    bool copied = false;
    if (inputPixelBuffer != nullptr) {
      CVPixelBufferLockBaseAddress(inputPixelBuffer,
                                   kCVPixelBufferLock_ReadOnly);
      copied = [self copyOrScaleInputPixelBuffer:inputPixelBuffer
                                     destination:slot->input];
      CVPixelBufferUnlockBaseAddress(inputPixelBuffer,
                                     kCVPixelBufferLock_ReadOnly);
      CFRelease(inputPixelBuffer);
    }
    if (!copied) {
      std::lock_guard lock(_frameSlotMutex);
      slot->state = FrameSlotState::Empty;
      _droppedCaptureFrames.fetch_add(1, std::memory_order_relaxed);
      continue;
    }

    glic::OriginalRealtimeMetalFrameStats stats;
    const bool processed =
        _lane->process(slot->input, slot->output, _frameIndex++, &stats, error);
    if (processed)
      [self presentOutputFrame:slot->output generation:_activeGeneration];
    {
      std::lock_guard lock(_frameSlotMutex);
      slot->state = FrameSlotState::Empty;
    }
    if (!processed) {
      NSString *message = [NSString
          stringWithFormat:@"Metal processing failed: %s", error.c_str()];
      dispatch_async(dispatch_get_main_queue(), ^{
        [self showStatus:message error:YES];
      });
      continue;
    }

    constexpr double smoothing = 0.12;
    if (_smoothedTotalMilliseconds == 0.0) {
      _smoothedTotalMilliseconds = stats.totalMilliseconds;
      _smoothedGpuMilliseconds = stats.gpuMilliseconds;
    } else {
      _smoothedTotalMilliseconds +=
          smoothing * (stats.totalMilliseconds - _smoothedTotalMilliseconds);
      _smoothedGpuMilliseconds +=
          smoothing * (stats.gpuMilliseconds - _smoothedGpuMilliseconds);
    }

    if (_activeQualityMode == QualityMode::Auto20 && _activeFastMatch) {
      uint32_t desiredReuse = _governorReuseFrames;
      if (_smoothedTotalMilliseconds > kGovernorHighWaterMilliseconds)
        desiredReuse = 4;
      else if (_smoothedTotalMilliseconds < kGovernorLowWaterMilliseconds)
        desiredReuse = 2;
      if (desiredReuse != _governorReuseFrames) {
        _governorReuseFrames = desiredReuse;
        _lane->setSegmentationReuseFrames(desiredReuse);
      }
    }

    ++_rateFrameCount;
    const auto now = std::chrono::steady_clock::now();
    const double elapsed =
        std::chrono::duration<double>(now - _rateStart).count();
    if (elapsed >= 0.5) {
      const double processedFps =
          static_cast<double>(_rateFrameCount) / elapsed;
      const uint64_t dropped = _droppedCaptureFrames.load();
      const std::string preset =
          _presets[static_cast<std::size_t>(_activePresetIndex)].name;
      std::string quality = qualityModeName(_activeQualityMode);
      if (_activeQualityMode == QualityMode::Auto20)
        quality += _activeFastMatch ? "/Fast" : "/Strict";
      const double totalMilliseconds = _smoothedTotalMilliseconds;
      const double gpuMilliseconds = _smoothedGpuMilliseconds;
      const uint32_t reuseFrames = _governorReuseFrames;
      const uint64_t generation = _activeGeneration;
      _rateFrameCount = 0;
      _rateStart = now;
      dispatch_async(dispatch_get_main_queue(), ^{
        if (generation != self->_requestedGeneration.load())
          return;
        const bool realtime = processedFps >= kMinimumFramesPerSecond &&
                              totalMilliseconds <= kRealtimeBudgetMilliseconds;
        NSString *status =
            [NSString stringWithFormat:@"%@ · %s", realtime ? @"LIVE" : @"SLOW",
                                       preset.c_str()];
        NSString *metrics = [NSString
            stringWithFormat:
                @"960×540  %.1f fps  %.2f ms  GPU %.2f  %s x%u  drop %llu",
                processedFps, totalMilliseconds, gpuMilliseconds,
                quality.c_str(), reuseFrames,
                static_cast<unsigned long long>(dropped)];
        self->_statusLabel.stringValue = status;
        self->_statusLabel.textColor =
            realtime ? NSColor.systemGreenColor : NSColor.systemOrangeColor;
        self->_metricsLabel.stringValue = metrics;
      });
    }
  }
  _processingScheduled.store(false, std::memory_order_release);
}

- (void)captureOutput:(AVCaptureOutput *)output
    didOutputSampleBuffer:(CMSampleBufferRef)sampleBuffer
           fromConnection:(AVCaptureConnection *)connection {
  (void)output;
  (void)connection;
  if (_stopping.load(std::memory_order_acquire))
    return;
  @autoreleasepool {
    CVPixelBufferRef imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer);
    if (imageBuffer == nullptr)
      return;
    CFRetain(imageBuffer);
    const CMTime presentationTimeStamp =
        CMSampleBufferGetPresentationTimeStamp(sampleBuffer);
    FrameSlot *slot = nullptr;
    {
      std::lock_guard lock(_frameSlotMutex);
      int selected = -1;
      uint64_t oldestReadySequence = UINT64_MAX;
      for (std::size_t index = 0; index < _frameSlots.size(); ++index) {
        if (_frameSlots[index].state == FrameSlotState::Empty) {
          selected = static_cast<int>(index);
          break;
        }
        if (_frameSlots[index].state == FrameSlotState::Ready &&
            _frameSlots[index].sequence < oldestReadySequence) {
          selected = static_cast<int>(index);
          oldestReadySequence = _frameSlots[index].sequence;
        }
      }
      if (selected < 0) {
        _droppedCaptureFrames.fetch_add(1, std::memory_order_relaxed);
        CFRelease(imageBuffer);
        return;
      }
      slot = &_frameSlots[static_cast<std::size_t>(selected)];
      if (slot->state == FrameSlotState::Ready) {
        _droppedCaptureFrames.fetch_add(1, std::memory_order_relaxed);
        if (slot->pixelBuffer != nullptr)
          CFRelease(slot->pixelBuffer);
      }
      slot->pixelBuffer = imageBuffer;
      slot->presentationTimeStamp = presentationTimeStamp;
      slot->state = FrameSlotState::Ready;
      slot->sequence = ++_captureSequence;
    }

    bool expected = false;
    if (_processingScheduled.compare_exchange_strong(
            expected, true, std::memory_order_acq_rel)) {
      dispatch_async(_processingQueue, ^{
        @autoreleasepool {
          [self processLatestFrames];
        }
      });
    }
  }
}

- (void)captureOutput:(AVCaptureOutput *)output
    didDropSampleBuffer:(CMSampleBufferRef)sampleBuffer
         fromConnection:(AVCaptureConnection *)connection {
  (void)output;
  (void)sampleBuffer;
  (void)connection;
  _droppedCaptureFrames.fetch_add(1, std::memory_order_relaxed);
}

- (void)showStatus:(NSString *)status error:(BOOL)isError {
  _statusLabel.stringValue = status;
  _statusLabel.textColor =
      isError ? NSColor.systemRedColor : NSColor.secondaryLabelColor;
}

@end

int main(int argc, const char *argv[]) {
  @autoreleasepool {
    if (argc == 2 && std::string_view(argv[1]) == "--self-test")
      return runSelfTest();
    if (argc == 2 && std::string_view(argv[1]) == "--camera-status") {
      const AVAuthorizationStatus status =
          [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo];
      std::printf(
          "camera_authorization=%s camera_available=%s\n",
          authorizationStatusName(status).UTF8String,
          [AVCaptureDevice defaultDeviceWithMediaType:AVMediaTypeVideo] != nil
              ? "true"
              : "false");
      return 0;
    }

    NSApplication *application = NSApplication.sharedApplication;
    application.activationPolicy = NSApplicationActivationPolicyRegular;
    GLICAppController *controller = [[GLICAppController alloc] init];
    application.delegate = controller;
    [application run];
  }
  return 0;
}
