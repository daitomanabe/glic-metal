#include "preset_loader.hpp"

#include <algorithm>
#include <cmath>
#include <iostream>
#include <string>

#ifndef GLIC_TEST_PRESETS_DIR
#define GLIC_TEST_PRESETS_DIR "presets"
#endif

namespace {

bool expect(bool condition, const std::string& message) {
    if (!condition)
        std::cerr << "FAILED: " << message << '\n';
    return condition;
}

bool closeEnough(float actual, float expected) {
    return std::abs(actual - expected) <= 0.0001f;
}

bool hasReason(const glic::PresetMappingInfo& info,
               const std::string& reason) {
    return std::find(info.reasons.begin(), info.reasons.end(), reason) !=
           info.reasons.end();
}

bool testLegacyApiUnchanged() {
    glic::CodecConfig config;
    if (!glic::PresetLoader::loadPresetByName(GLIC_TEST_PRESETS_DIR,
                                               "default", config)) {
        return expect(false, "legacy default preset loads");
    }
    return expect(config.channels[0].minBlockSize == 2,
                  "legacy min remains raw controller value") &&
           expect(config.channels[0].maxBlockSize == 8,
                  "legacy max remains raw controller value") &&
           expect(config.channels[0].transformScale == 20,
                  "legacy scale remains raw controller value");
}

bool testDefaultGolden() {
    glic::OriginalPresetConfig config;
    if (!glic::PresetLoader::loadOriginalPresetByName(
            GLIC_TEST_PRESETS_DIR, "default", config)) {
        return expect(false, "original default preset loads");
    }

    bool passed = true;
    passed &= expect(!config.separateChannels,
                     "missing separate_channels means false");
    passed &= expect(config.colorSpace == glic::ColorSpace::RGB,
                     "default colorspace");
    for (size_t index = 0; index < config.channels.size(); ++index) {
        const auto& channel = config.channels[index];
        passed &= expect(channel.minBlockSize == 4,
                         "default min is 2^2");
        passed &= expect(channel.maxBlockSize == 256,
                         "default max is 2^8");
        passed &= expect(channel.predictionMethod ==
                             glic::PredictionMethod::AVG,
                         "default prediction list index 7");
        passed &= expect(channel.clampMethod == glic::ClampMethod::NONE,
                         "default clamp radio state");
        passed &= expect(channel.originalWaveletId == 0,
                         "default wavelet NONE");
        passed &= expect(channel.originalTransformType == 0,
                         "default transform FWT");
        passed &= expect(channel.transformScale == 1048576,
                         "default transform scale is 2^20");
        passed &= expect(channel.encodingMethod ==
                             glic::EncodingMethod::PACKED,
                         "default encoding PACKED");
    }

    glic::PresetMappingInfo mapping;
    const auto realtime =
        glic::PresetLoader::projectOriginalPresetToRealtime(config, &mapping);
    passed &= expect(mapping.fidelity ==
                         glic::PresetMappingFidelity::EXACT_COMPATIBLE,
                     "default field mapping is exact-compatible");
    passed &= expect(realtime.channels[0].minBlockSize == 4 &&
                         realtime.channels[0].transformScale == 1048576,
                     "default projected values retain GUI conversion");
    return passed;
}

bool testSharedChannelGolden() {
    glic::OriginalPresetConfig config;
    if (!glic::PresetLoader::loadOriginalPresetByName(
            GLIC_TEST_PRESETS_DIR, "bi0g4n1c", config)) {
        return expect(false, "bi0g4n1c preset loads");
    }

    bool passed = true;
    passed &= expect(!config.separateChannels,
                     "bi0g4n1c uses shared channel controls");
    for (const auto& channel : config.channels) {
        passed &= expect(channel.minBlockSize == 2 &&
                             channel.maxBlockSize == 256,
                         "shared ch0 block exponents copied to all channels");
        passed &= expect(closeEnough(channel.segmentationPrecision, 21.84375f),
                         "shared threshold copied to all channels");
        passed &= expect(channel.predictionMethod ==
                             glic::PredictionMethod::LDIAG,
                         "shared prediction copied to all channels");
        passed &= expect(channel.quantizationValue == 87,
                         "quantization uses Java int truncation");
        passed &= expect(closeEnough(channel.quantizationControllerValue,
                                     87.83333587646484f),
                         "raw quantization controller value retained");
        passed &= expect(closeEnough(channel.quantizationStep, 43.5f),
                         "codec quantization step applies v/2 once");
        passed &= expect(channel.clampMethod == glic::ClampMethod::MOD256,
                         "clamp radio active index decoded");
    }
    return passed;
}

bool testSpecialListAndWaveletGolden() {
    glic::OriginalPresetConfig config;
    if (!glic::PresetLoader::loadOriginalPresetByName(
            GLIC_TEST_PRESETS_DIR, "d1ffu510n", config)) {
        return expect(false, "d1ffu510n preset loads");
    }

    bool passed = true;
    for (const auto& channel : config.channels) {
        passed &= expect(channel.predictionListIndex == 17,
                         "stored special prediction index retained");
        passed &= expect(channel.predictionMethod ==
                             glic::PredictionMethod::BSAD,
                         "prediction index 17 maps to BSAD");
        passed &= expect(channel.originalWaveletId == 64,
                         "upstream CDF53 wavelet ID retained");
        passed &= expect(channel.transformScale == 172950,
                         "fractional scale controller applies 2^x");
    }

    glic::PresetMappingInfo mapping;
    (void)glic::PresetLoader::projectOriginalPresetToRealtime(config,
                                                               &mapping);
    passed &= expect(mapping.fidelity ==
                         glic::PresetMappingFidelity::APPROXIMATED,
                     "CDF53 projection is marked approximated");
    passed &= expect(hasReason(mapping, "special_wavelet_not_implemented"),
                     "CDF53 projection reason is explicit");
    return passed;
}

bool testSeparateChannelsGolden() {
    glic::OriginalPresetConfig config;
    if (!glic::PresetLoader::loadOriginalPresetByName(
            GLIC_TEST_PRESETS_DIR, "channels", config)) {
        return expect(false, "channels preset loads");
    }

    bool passed = true;
    passed &= expect(config.separateChannels,
                     "channels enables separate channel controls");
    passed &= expect(config.colorSpace == glic::ColorSpace::RGGBG,
                     "channels colorspace");
    passed &= expect(config.channels[0].minBlockSize == 64 &&
                         config.channels[0].maxBlockSize == 256 &&
                         config.channels[0].predictionMethod ==
                             glic::PredictionMethod::PAETH &&
                         config.channels[0].transformScale == 23,
                     "channels channel 0 golden");
    const float expectedCompression =
        50.0f * std::pow(79.33333587646484f / 255.0f, 2.0f);
    passed &= expect(closeEnough(
                         config.channels[0].transformCompressControllerValue,
                         79.33333587646484f) &&
                         closeEnough(config.channels[0]
                                         .transformCompressionThreshold,
                                     expectedCompression),
                     "compression retains controller and effective threshold");
    passed &= expect(config.channels[1].minBlockSize == 4 &&
                         config.channels[1].maxBlockSize == 16 &&
                         config.channels[1].predictionMethod ==
                             glic::PredictionMethod::H &&
                         config.channels[1].transformScale == 71,
                     "channels channel 1 golden");
    passed &= expect(config.channels[2].minBlockSize == 2 &&
                         config.channels[2].maxBlockSize == 512 &&
                         config.channels[2].predictionMethod ==
                             glic::PredictionMethod::HV &&
                         config.channels[2].transformScale == 14,
                     "channels channel 2 golden");
    return passed;
}

bool testUnsupportedRandomGolden() {
    glic::OriginalPresetConfig original;
    if (!glic::PresetLoader::loadOriginalPresetByName(
            GLIC_TEST_PRESETS_DIR, "sk0011rgb", original)) {
        return expect(false, "random-wavelet preset loads");
    }
    glic::PresetMappingInfo mapping;
    (void)glic::PresetLoader::projectOriginalPresetToRealtime(original,
                                                               &mapping);
    return expect(original.channels[0].originalWaveletId == -1,
                  "wavelet list index 68 maps to RANDOM") &&
           expect(mapping.fidelity ==
                      glic::PresetMappingFidelity::UNSUPPORTED,
                  "random wavelet is marked unsupported") &&
           expect(hasReason(mapping,
                            "random_wavelet_requires_per_encode_selection"),
                  "random wavelet reason is explicit");
}

bool testEveryUpstreamPresetDecodes() {
    const auto presets =
        glic::PresetLoader::listPresets(GLIC_TEST_PRESETS_DIR);
    bool passed = expect(presets.size() == 144,
                         "expected upstream 144-preset corpus");
    for (const auto& preset : presets) {
        glic::OriginalPresetConfig config;
        passed &= expect(glic::PresetLoader::loadOriginalPresetByName(
                             GLIC_TEST_PRESETS_DIR, preset, config),
                         "original decode: " + preset);
    }
    return passed;
}

} // namespace

int main() {
    bool passed = true;
    passed &= testLegacyApiUnchanged();
    passed &= testDefaultGolden();
    passed &= testSharedChannelGolden();
    passed &= testSpecialListAndWaveletGolden();
    passed &= testSeparateChannelsGolden();
    passed &= testUnsupportedRandomGolden();
    passed &= testEveryUpstreamPresetDecodes();
    if (passed)
        std::cout << "preset_loader_tests passed\n";
    return passed ? 0 : 1;
}
