#include "original_realtime.hpp"
#include "original_realtime_metal.hpp"
#include "preset_loader.hpp"

#import <Accelerate/Accelerate.h>
#import <AppKit/AppKit.h>
#import <AVFoundation/AVFoundation.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <QuartzCore/QuartzCore.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstring>
#include <cstdio>
#include <cstdlib>
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
constexpr double kRealtimeBudgetMilliseconds =
    1000.0 / kMinimumFramesPerSecond;
constexpr double kGovernorHighWaterMilliseconds = 45.0;
constexpr double kGovernorLowWaterMilliseconds = 30.0;

enum class QualityMode : int {
  Strict = 0,
  FastMatch = 1,
  Auto20 = 2,
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
  uint64_t sequence = 0;
  FrameSlotState state = FrameSlotState::Empty;
};

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
       {std::filesystem::path(GLIC_SOURCE_PRESETS_DIR),
        std::filesystem::current_path() / "presets"}) {
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
      std::find_if(choices.begin(), choices.end(), [&](const auto &choice) {
        return choice.name == name;
      });
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
                       choices[static_cast<std::size_t>(index)].config, error)) {
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
  std::printf("PASS webcam preview Metal lane presets=%zu resolution=%dx%d\n",
              choices.size(), kProcessingWidth, kProcessingHeight);
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
    : NSObject <NSApplicationDelegate, AVCaptureVideoDataOutputSampleBufferDelegate> {
  NSWindow *_window;
  GLICPreviewView *_previewView;
  NSPopUpButton *_presetPopup;
  NSPopUpButton *_qualityPopup;
  NSTextField *_statusLabel;
  NSTextField *_metricsLabel;
  NSMenu *_presetMenu;
  NSMenu *_qualityMenu;

  AVCaptureSession *_captureSession;
  AVCaptureVideoDataOutput *_captureOutput;
  dispatch_queue_t _captureQueue;
  dispatch_queue_t _processingQueue;
  CVPixelBufferPoolRef _displayPixelBufferPool;
  CMVideoFormatDescriptionRef _displayFormatDescription;

  std::filesystem::path _presetDirectory;
  std::vector<PresetChoice> _presets;
  std::unordered_set<std::string> _fastMatchAllowlist;
  std::unique_ptr<glic::OriginalRealtimeMetalLane> _lane;
  std::array<FrameSlot, 3> _frameSlots;
  std::mutex _frameSlotMutex;
  uint64_t _captureSequence;
  std::atomic<int> _pendingPresetIndex;
  std::atomic<int> _pendingQualityMode;
  std::atomic<bool> _processingScheduled;
  std::atomic<bool> _stopping;
  std::atomic<bool> _displayEnqueuePending;
  std::atomic<uint64_t> _droppedCaptureFrames;
  int _activePresetIndex;
  QualityMode _activeQualityMode;
  bool _activeFastMatch;
  uint32_t _governorReuseFrames;
  uint64_t _frameIndex;
  uint64_t _rateFrameCount;
  std::chrono::steady_clock::time_point _rateStart;
  double _smoothedTotalMilliseconds;
  double _smoothedGpuMilliseconds;
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
    _pendingPresetIndex.store(-1);
    _pendingQualityMode.store(static_cast<int>(QualityMode::Auto20));
    _processingScheduled.store(false);
    _stopping.store(false);
    _displayEnqueuePending.store(false);
    _droppedCaptureFrames.store(0);
    _activePresetIndex = -1;
    _activeQualityMode = QualityMode::Auto20;
    _activeFastMatch = false;
    _governorReuseFrames = 2;
    _captureSequence = 0;
    _frameIndex = 0;
    _rateFrameCount = 0;
    _rateStart = std::chrono::steady_clock::now();
    _smoothedTotalMilliseconds = 0.0;
    _smoothedGpuMilliseconds = 0.0;
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
                      reason:@"Realtime webcam Metal processing"];
  [self buildWindow];
  [self loadPresetMenu];
  [self requestCameraAccessAndStart];
  [_window makeKeyAndOrderFront:nil];
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender {
  (void)sender;
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
    dispatch_sync(_processingQueue, ^{ self->_lane.reset(); });
  if (_displayFormatDescription != nullptr) {
    CFRelease(_displayFormatDescription);
    _displayFormatDescription = nullptr;
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
  _window.minSize = NSMakeSize(720, 460);
  [_window setFrameAutosaveName:@"GLICWebcamPreviewWindow"];

  NSView *content = [[NSView alloc] initWithFrame:_window.contentView.bounds];
  content.translatesAutoresizingMaskIntoConstraints = NO;
  _window.contentView = content;

  NSVisualEffectView *header = [[NSVisualEffectView alloc] initWithFrame:NSZeroRect];
  header.translatesAutoresizingMaskIntoConstraints = NO;
  header.material = NSVisualEffectMaterialHeaderView;
  header.blendingMode = NSVisualEffectBlendingModeWithinWindow;

  NSTextField *title = [NSTextField labelWithString:@"GLIC METAL · CAMERA"];
  title.translatesAutoresizingMaskIntoConstraints = NO;
  title.font = [NSFont systemFontOfSize:12 weight:NSFontWeightSemibold];
  title.textColor = NSColor.secondaryLabelColor;

  _presetPopup = [[NSPopUpButton alloc] initWithFrame:NSZeroRect pullsDown:NO];
  _presetPopup.translatesAutoresizingMaskIntoConstraints = NO;
  _presetPopup.target = self;
  _presetPopup.action = @selector(selectPreset:);
  _presetPopup.toolTip = @"Original GLIC preset";

  _qualityPopup = [[NSPopUpButton alloc] initWithFrame:NSZeroRect pullsDown:NO];
  _qualityPopup.translatesAutoresizingMaskIntoConstraints = NO;
  [_qualityPopup addItemsWithTitles:@[ @"Strict", @"Fast Match", @"Auto 20fps" ]];
  [_qualityPopup selectItemAtIndex:static_cast<NSInteger>(QualityMode::Auto20)];
  _qualityPopup.target = self;
  _qualityPopup.action = @selector(selectQuality:);
  _qualityPopup.toolTip = @"Strict fidelity or realtime Fast Match policy";

  _statusLabel = [NSTextField labelWithString:@"Starting…"];
  _statusLabel.translatesAutoresizingMaskIntoConstraints = NO;
  _statusLabel.font = [NSFont monospacedSystemFontOfSize:12
                                                 weight:NSFontWeightMedium];
  _statusLabel.alignment = NSTextAlignmentRight;

  _metricsLabel = [NSTextField labelWithString:@"960×540 · ≥20 fps"];
  _metricsLabel.translatesAutoresizingMaskIntoConstraints = NO;
  _metricsLabel.font = [NSFont monospacedDigitSystemFontOfSize:11
                                                     weight:NSFontWeightRegular];
  _metricsLabel.textColor = NSColor.secondaryLabelColor;
  _metricsLabel.alignment = NSTextAlignmentRight;

  NSStackView *rightStack = [[NSStackView alloc] initWithFrame:NSZeroRect];
  [rightStack addArrangedSubview:_statusLabel];
  [rightStack addArrangedSubview:_metricsLabel];
  rightStack.translatesAutoresizingMaskIntoConstraints = NO;
  rightStack.orientation = NSUserInterfaceLayoutOrientationVertical;
  rightStack.alignment = NSLayoutAttributeTrailing;
  rightStack.spacing = 2;

  [header addSubview:title];
  [header addSubview:_presetPopup];
  [header addSubview:_qualityPopup];
  [header addSubview:rightStack];

  _previewView = [[GLICPreviewView alloc] initWithFrame:NSZeroRect];
  _previewView.translatesAutoresizingMaskIntoConstraints = NO;

  [content addSubview:header];
  [content addSubview:_previewView];

  [NSLayoutConstraint activateConstraints:@[
    [header.leadingAnchor constraintEqualToAnchor:content.leadingAnchor],
    [header.trailingAnchor constraintEqualToAnchor:content.trailingAnchor],
    [header.topAnchor constraintEqualToAnchor:content.topAnchor],
    [header.heightAnchor constraintEqualToConstant:64],
    [title.leadingAnchor constraintEqualToAnchor:header.leadingAnchor constant:18],
    [title.centerYAnchor constraintEqualToAnchor:header.centerYAnchor],
    [_presetPopup.leadingAnchor constraintEqualToAnchor:title.trailingAnchor
                                                constant:18],
    [_presetPopup.centerYAnchor constraintEqualToAnchor:header.centerYAnchor],
    [_presetPopup.widthAnchor constraintEqualToConstant:250],
    [_qualityPopup.leadingAnchor constraintEqualToAnchor:_presetPopup.trailingAnchor
                                                 constant:10],
    [_qualityPopup.centerYAnchor constraintEqualToAnchor:header.centerYAnchor],
    [_qualityPopup.widthAnchor constraintEqualToConstant:128],
    [rightStack.trailingAnchor constraintEqualToAnchor:header.trailingAnchor
                                               constant:-18],
    [rightStack.centerYAnchor constraintEqualToAnchor:header.centerYAnchor],
    [rightStack.leadingAnchor constraintGreaterThanOrEqualToAnchor:
                                  _qualityPopup.trailingAnchor
                                                         constant:18],
    [_previewView.leadingAnchor constraintEqualToAnchor:content.leadingAnchor],
    [_previewView.trailingAnchor constraintEqualToAnchor:content.trailingAnchor],
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
  NSMenu *applicationMenu = [[NSMenu alloc] initWithTitle:@"GLIC Webcam Preview"];
  [applicationMenu addItemWithTitle:@"Quit GLIC Webcam Preview"
                              action:@selector(terminate:)
                       keyEquivalent:@"q"];
  applicationItem.submenu = applicationMenu;

  NSMenuItem *presetItem = [[NSMenuItem alloc] initWithTitle:@"Preset"
                                                      action:nil
                                               keyEquivalent:@""];
  [mainMenu addItem:presetItem];
  _presetMenu = [[NSMenu alloc] initWithTitle:@"Preset"];
  presetItem.submenu = _presetMenu;

  NSMenuItem *qualityItem = [[NSMenuItem alloc] initWithTitle:@"Quality"
                                                      action:nil
                                               keyEquivalent:@""];
  [mainMenu addItem:qualityItem];
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
  qualityItem.submenu = _qualityMenu;
  NSApp.mainMenu = mainMenu;
}

- (void)loadFastMatchAllowlist {
  _fastMatchAllowlist.clear();
  NSMutableArray<NSString *> *candidates = [NSMutableArray array];
  NSString *bundlePath = [NSBundle.mainBundle
      pathForResource:@"fast-match-allowlist"
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

  [_presetPopup removeAllItems];
  [_presetMenu removeAllItems];
  for (std::size_t index = 0; index < _presets.size(); ++index) {
    NSString *name = [NSString stringWithUTF8String:_presets[index].name.c_str()];
    [_presetPopup addItemWithTitle:name];
    NSMenuItem *item = [[NSMenuItem alloc] initWithTitle:name
                                                  action:@selector(selectPreset:)
                                           keyEquivalent:@""];
    item.target = self;
    item.tag = static_cast<NSInteger>(index);
    [_presetMenu addItem:item];
  }
  int initialIndex = findPresetIndex(_presets, "vv02");
  if (initialIndex < 0)
    initialIndex = 0;
  [_presetPopup selectItemAtIndex:initialIndex];
  _pendingPresetIndex.store(initialIndex, std::memory_order_release);
}

- (void)selectPreset:(id)sender {
  NSInteger index = -1;
  if ([sender isKindOfClass:NSPopUpButton.class])
    index = [(NSPopUpButton *)sender indexOfSelectedItem];
  else if ([sender isKindOfClass:NSMenuItem.class])
    index = [(NSMenuItem *)sender tag];
  if (index < 0 || static_cast<std::size_t>(index) >= _presets.size())
    return;
  [_presetPopup selectItemAtIndex:index];
  _pendingPresetIndex.store(static_cast<int>(index), std::memory_order_release);
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
  _pendingQualityMode.store(static_cast<int>(index),
                            std::memory_order_release);
  [self showStatus:@"Switching quality…" error:NO];
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
                                   [strongSelf showStatus:
                                                   @"Camera permission denied"
                                                        error:YES];
                               });
                             }];
    return;
  }
  [self showStatus:[NSString
                       stringWithFormat:@"Camera access %@",
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

- (bool)activatePendingPreset:(std::string &)error {
  const int pending = _pendingPresetIndex.load(std::memory_order_acquire);
  const int pendingQuality =
      _pendingQualityMode.load(std::memory_order_acquire);
  if (pending < 0)
    return false;
  const auto requestedQuality = static_cast<QualityMode>(pendingQuality);
  if (pending == _activePresetIndex && requestedQuality == _activeQualityMode)
    return _lane != nullptr;
  if (static_cast<std::size_t>(pending) >= _presets.size()) {
    error = "Preset index is out of range";
    return false;
  }

  const std::string &presetName =
      _presets[static_cast<std::size_t>(pending)].name;
  const bool useFastMatch =
      requestedQuality == QualityMode::FastMatch ||
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
  _lane = std::move(candidate);
  _activePresetIndex = pending;
  _activeQualityMode = requestedQuality;
  _activeFastMatch = useFastMatch;
  _governorReuseFrames =
      useFastMatch ? 2u : 1u;
  _frameIndex = 0;
  _droppedCaptureFrames.store(0, std::memory_order_release);
  _rateFrameCount = 0;
  _rateStart = std::chrono::steady_clock::now();
  _smoothedTotalMilliseconds = 0.0;
  _smoothedGpuMilliseconds = 0.0;
  return true;
}

- (bool)copyOrScaleInputPixelBuffer:(CVPixelBufferRef)pixelBuffer
                         destination:(std::vector<glic::Color> &)pixels {
  const int sourceWidth = static_cast<int>(CVPixelBufferGetWidth(pixelBuffer));
  const int sourceHeight = static_cast<int>(CVPixelBufferGetHeight(pixelBuffer));
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
        std::memcpy(destination + static_cast<std::size_t>(y) *
                                      destinationRowBytes,
                    source + static_cast<std::size_t>(y) * sourceRowBytes,
                    destinationRowBytes);
    }
    return true;
  }

  vImage_Buffer sourceBuffer = {
      const_cast<uint8_t *>(source), static_cast<vImagePixelCount>(sourceHeight),
      static_cast<vImagePixelCount>(sourceWidth), sourceRowBytes};
  vImage_Buffer destinationBuffer = {
      destination, static_cast<vImagePixelCount>(kProcessingHeight),
      static_cast<vImagePixelCount>(kProcessingWidth), destinationRowBytes};
  return vImageScale_ARGB8888(&sourceBuffer, &destinationBuffer, nullptr,
                              kvImageHighQualityResampling) == kvImageNoError;
}

- (void)presentOutputFrame:(const std::vector<glic::Color> &)pixels {
  if (_displayPixelBufferPool == nullptr ||
      _displayEnqueuePending.exchange(true, std::memory_order_acq_rel))
    return;

  CVPixelBufferRef displayPixelBuffer = nullptr;
  if (CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault,
                                         _displayPixelBufferPool,
                                         &displayPixelBuffer) !=
      kCVReturnSuccess) {
    _displayEnqueuePending.store(false, std::memory_order_release);
    return;
  }
  CVPixelBufferLockBaseAddress(displayPixelBuffer, 0);
  auto *destination =
      static_cast<uint8_t *>(CVPixelBufferGetBaseAddress(displayPixelBuffer));
  const std::size_t destinationRowBytes =
      CVPixelBufferGetBytesPerRow(displayPixelBuffer);
  constexpr std::size_t sourceRowBytes =
      static_cast<std::size_t>(kProcessingWidth) * sizeof(glic::Color);
  const auto *source =
      reinterpret_cast<const uint8_t *>(pixels.data());
  for (int y = 0; y < kProcessingHeight; ++y)
    std::memcpy(destination + static_cast<std::size_t>(y) *
                                  destinationRowBytes,
                source + static_cast<std::size_t>(y) * sourceRowBytes,
                sourceRowBytes);
  CVPixelBufferUnlockBaseAddress(displayPixelBuffer, 0);

  if (_displayFormatDescription == nullptr &&
      CMVideoFormatDescriptionCreateForImageBuffer(
          kCFAllocatorDefault, displayPixelBuffer,
          &_displayFormatDescription) != noErr) {
    CFRelease(displayPixelBuffer);
    _displayEnqueuePending.store(false, std::memory_order_release);
    return;
  }

  CMSampleTimingInfo timing = kCMTimingInfoInvalid;
  CMSampleBufferRef sampleBuffer = nullptr;
  const OSStatus sampleStatus = CMSampleBufferCreateForImageBuffer(
      kCFAllocatorDefault, displayPixelBuffer, true, nullptr, nullptr,
      _displayFormatDescription, &timing, &sampleBuffer);
  CFRelease(displayPixelBuffer);
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
    AVSampleBufferDisplayLayer *layer = self->_previewView.sampleBufferLayer;
    AVSampleBufferVideoRenderer *renderer = layer.sampleBufferRenderer;
    if (renderer.status == AVQueuedSampleBufferRenderingStatusFailed)
      [renderer flush];
    [renderer enqueueSampleBuffer:sampleBuffer];
    CFRelease(sampleBuffer);
    self->_displayEnqueuePending.store(false, std::memory_order_release);
  });
}

- (void)processLatestFrames {
  while (!_stopping.load(std::memory_order_acquire)) {
    FrameSlot *slot = nullptr;
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
          _frameSlots[index].state = FrameSlotState::Empty;
          _droppedCaptureFrames.fetch_add(1, std::memory_order_relaxed);
        }
      }
      slot = &_frameSlots[static_cast<std::size_t>(newest)];
      slot->state = FrameSlotState::Processing;
    }

    std::string error;
    if (![self activatePendingPreset:error]) {
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

    glic::OriginalRealtimeMetalFrameStats stats;
    const bool processed = _lane->process(slot->input, slot->output,
                                          _frameIndex++, &stats, error);
    if (processed)
      [self presentOutputFrame:slot->output];
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
      const double processedFps = static_cast<double>(_rateFrameCount) / elapsed;
      const uint64_t dropped = _droppedCaptureFrames.load();
      const std::string preset =
          _presets[static_cast<std::size_t>(_activePresetIndex)].name;
      std::string quality = qualityModeName(_activeQualityMode);
      if (_activeQualityMode == QualityMode::Auto20)
        quality += _activeFastMatch ? "/Fast" : "/Strict";
      const double totalMilliseconds = _smoothedTotalMilliseconds;
      const double gpuMilliseconds = _smoothedGpuMilliseconds;
      const uint32_t reuseFrames = _governorReuseFrames;
      _rateFrameCount = 0;
      _rateStart = now;
      dispatch_async(dispatch_get_main_queue(), ^{
        const bool realtime =
            processedFps >= kMinimumFramesPerSecond &&
            totalMilliseconds <= kRealtimeBudgetMilliseconds;
        NSString *status = [NSString
            stringWithFormat:@"%@ · %s", realtime ? @"LIVE" : @"SLOW",
                             preset.c_str()];
        NSString *metrics = [NSString
            stringWithFormat:
                @"960×540  %.1f fps  %.2f ms  GPU %.2f  %s x%u  drop %llu",
                processedFps, totalMilliseconds, gpuMilliseconds,
                quality.c_str(), reuseFrames,
                static_cast<unsigned long long>(dropped)];
        self->_statusLabel.stringValue = status;
        self->_statusLabel.textColor = realtime ? NSColor.systemGreenColor
                                                : NSColor.systemOrangeColor;
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
        return;
      }
      slot = &_frameSlots[static_cast<std::size_t>(selected)];
      if (slot->state == FrameSlotState::Ready)
        _droppedCaptureFrames.fetch_add(1, std::memory_order_relaxed);
      slot->state = FrameSlotState::Capturing;
      slot->sequence = ++_captureSequence;
    }

    CVPixelBufferRef imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer);
    bool copied = false;
    if (imageBuffer != nullptr) {
      CVPixelBufferLockBaseAddress(imageBuffer, kCVPixelBufferLock_ReadOnly);
      copied = [self copyOrScaleInputPixelBuffer:imageBuffer
                                     destination:slot->input];
      CVPixelBufferUnlockBaseAddress(imageBuffer,
                                     kCVPixelBufferLock_ReadOnly);
    }
    {
      std::lock_guard lock(_frameSlotMutex);
      slot->state = copied ? FrameSlotState::Ready : FrameSlotState::Empty;
    }
    if (!copied)
      return;

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
      std::printf("camera_authorization=%s camera_available=%s\n",
                  authorizationStatusName(status).UTF8String,
                  [AVCaptureDevice defaultDeviceWithMediaType:AVMediaTypeVideo]
                          != nil
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
