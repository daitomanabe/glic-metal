#include "preset_loader.hpp"
#include <fstream>
#include <algorithm>
#include <ranges>
#include <iostream>
#include <filesystem>
#include <cmath>
#include <bit>
#include <limits>
#include <span>
#include <array>

namespace glic {

// Known preset keys
static const std::array<std::string, 31> FLOAT_KEYS = {{
    "ch0trans", "ch1trans", "ch2trans",
    "ch0pred", "ch1pred", "ch2pred",
    "ch0min", "ch1min", "ch2min",
    "ch0max", "ch1max", "ch2max",
    "ch0quant", "ch1quant", "ch2quant",
    "ch0scale", "ch1scale", "ch2scale",
    "ch0compress", "ch1compress", "ch2compress",
    "ch0encoding", "ch1encoding", "ch2encoding",
    "ch0thr", "ch1thr", "ch2thr",
    "colorspace",
    "color_outside_r", "color_outside_g", "color_outside_b"
}};

static const std::array<std::string, 6> FLOAT_ARRAY_KEYS = {{
    "ch0clamp", "ch1clamp", "ch2clamp",
    "ch0transtype", "ch1transtype", "ch2transtype"
}};

namespace {

constexpr std::array<std::string_view, 31> ORIGINAL_FLOAT_KEYS = {{
    "ch0trans", "ch1trans", "ch2trans",
    "ch0pred", "ch1pred", "ch2pred",
    "ch0min", "ch1min", "ch2min",
    "ch0max", "ch1max", "ch2max",
    "ch0quant", "ch1quant", "ch2quant",
    "ch0scale", "ch1scale", "ch2scale",
    "ch0compress", "ch1compress", "ch2compress",
    "ch0encoding", "ch1encoding", "ch2encoding",
    "ch0thr", "ch1thr", "ch2thr",
    "colorspace",
    "color_outside_r", "color_outside_g", "color_outside_b"
}};

constexpr std::array<std::string_view, 7> ORIGINAL_FLOAT_ARRAY_KEYS = {{
    "ch0clamp", "ch1clamp", "ch2clamp",
    "ch0transtype", "ch1transtype", "ch2transtype",
    "separate_channels"
}};

float readBigEndianFloat(const uint8_t* data) noexcept {
    const uint32_t bits = (static_cast<uint32_t>(data[0]) << 24) |
                          (static_cast<uint32_t>(data[1]) << 16) |
                          (static_cast<uint32_t>(data[2]) << 8) |
                          static_cast<uint32_t>(data[3]);
    return std::bit_cast<float>(bits);
}

uint32_t readBigEndianU32(const uint8_t* data) noexcept {
    return (static_cast<uint32_t>(data[0]) << 24) |
           (static_cast<uint32_t>(data[1]) << 16) |
           (static_cast<uint32_t>(data[2]) << 8) |
           static_cast<uint32_t>(data[3]);
}

std::optional<size_t> findSerializedString(
    const std::vector<uint8_t>& data, std::string_view value) {
    if (value.size() > std::numeric_limits<uint16_t>::max())
        return std::nullopt;
    for (size_t token = 0; token + 3 + value.size() <= data.size(); ++token) {
        if (data[token] != 0x74)
            continue;
        const uint16_t length = static_cast<uint16_t>(
            (static_cast<uint16_t>(data[token + 1]) << 8) |
            static_cast<uint16_t>(data[token + 2]));
        if (length != value.size())
            continue;
        const size_t content = token + 3;
        if (std::equal(value.begin(), value.end(), data.begin() +
                                                   static_cast<ptrdiff_t>(content))) {
            return content;
        }
    }
    return std::nullopt;
}

std::optional<size_t> serializedPayloadOffset(
    const std::vector<uint8_t>& data, size_t objectOffset,
    uint8_t expectedObjectToken) {
    if (objectOffset + 2 > data.size() ||
        data[objectOffset] != expectedObjectToken) {
        return std::nullopt;
    }

    // A repeated Float/[F object references an already described class:
    // TC_OBJECT/TC_ARRAY, TC_REFERENCE, four-byte wire handle, payload.
    if (data[objectOffset + 1] == 0x71) {
        const size_t payload = objectOffset + 6;
        return payload <= data.size() ? std::optional<size_t>(payload)
                                      : std::nullopt;
    }

    // The first object of each class contains a class descriptor. Both
    // java.lang.Float and float[] finish it with TC_ENDBLOCKDATA, TC_NULL
    // immediately before class data.
    if (data[objectOffset + 1] == 0x72) {
        const size_t searchEnd = std::min(data.size(), objectOffset + 256);
        for (size_t cursor = objectOffset + 2; cursor + 1 < searchEnd;
             ++cursor) {
            if (data[cursor] == 0x78 && data[cursor + 1] == 0x70) {
                return cursor + 2;
            }
        }
    }
    return std::nullopt;
}

std::optional<float> serializedFloatAfterKey(
    const std::vector<uint8_t>& data, size_t keyEnd) {
    const auto payload = serializedPayloadOffset(data, keyEnd, 0x73);
    if (!payload || *payload + 4 > data.size())
        return std::nullopt;
    const float value = readBigEndianFloat(&data[*payload]);
    if (!std::isfinite(value))
        return std::nullopt;
    return value;
}

std::optional<std::vector<float>> serializedFloatArrayAfterKey(
    const std::vector<uint8_t>& data, size_t keyEnd) {
    const auto payload = serializedPayloadOffset(data, keyEnd, 0x75);
    if (!payload || *payload + 4 > data.size())
        return std::nullopt;
    const uint32_t length = readBigEndianU32(&data[*payload]);
    if (length == 0 || length > 10 ||
        *payload + 4 + static_cast<size_t>(length) * 4 > data.size()) {
        return std::nullopt;
    }
    std::vector<float> values;
    values.reserve(length);
    size_t cursor = *payload + 4;
    for (uint32_t index = 0; index < length; ++index, cursor += 4) {
        const float value = readBigEndianFloat(&data[cursor]);
        if (!std::isfinite(value))
            return std::nullopt;
        values.push_back(value);
    }
    return values;
}

int activeRadioIndex(const std::vector<float>& values, int fallback) noexcept {
    for (size_t index = 0; index < values.size(); ++index) {
        if (values[index] > 0.5f)
            return static_cast<int>(index);
    }
    return fallback;
}

std::optional<int> powerOfTwoControllerValue(float exponent) {
    const int integralExponent = static_cast<int>(exponent);
    if (integralExponent < 0 || integralExponent > 30)
        return std::nullopt;
    return 1 << integralExponent;
}

std::optional<int> transformScaleControllerValue(float exponent) {
    // Processing's pow(float, float) returns float before the Java int cast.
    const float value = std::pow(2.0f, exponent);
    if (!std::isfinite(value) || value < 0.0f ||
        static_cast<double>(value) >
            static_cast<double>(std::numeric_limits<int>::max())) {
        return std::nullopt;
    }
    return static_cast<int>(value);
}

std::optional<PredictionMethod> originalPredictionMethod(int listIndex) {
    if (listIndex >= 0 && listIndex <= 15)
        return static_cast<PredictionMethod>(listIndex);
    switch (listIndex) {
    case 16:
        return PredictionMethod::SAD;
    case 17:
        return PredictionMethod::BSAD;
    case 18:
        return PredictionMethod::RANDOM;
    default:
        return std::nullopt;
    }
}

void raiseMappingFidelity(PresetMappingInfo& info,
                          PresetMappingFidelity fidelity,
                          std::string reason) {
    if (static_cast<int>(fidelity) > static_cast<int>(info.fidelity))
        info.fidelity = fidelity;
    if (std::find(info.reasons.begin(), info.reasons.end(), reason) ==
        info.reasons.end()) {
        info.reasons.push_back(std::move(reason));
    }
}

WaveletType projectOriginalWavelet(int originalId,
                                   PresetMappingInfo& info) {
    if (originalId == -1) {
        raiseMappingFidelity(info, PresetMappingFidelity::UNSUPPORTED,
                             "random_wavelet_requires_per_encode_selection");
        return WaveletType::HAAR_ORTHOGONAL;
    }
    if (originalId >= 0 && originalId <= 30)
        return static_cast<WaveletType>(originalId);
    if (originalId >= 31 && originalId <= 40) {
        raiseMappingFidelity(info, PresetMappingFidelity::APPROXIMATED,
                             "symlet11_to_symlet20_not_implemented");
        return WaveletType::SYMLET10;
    }
    if (originalId >= 41 && originalId <= 43) {
        raiseMappingFidelity(info, PresetMappingFidelity::APPROXIMATED,
                             "legendre_wavelet_not_implemented");
        return WaveletType::BIORTHOGONAL11;
    }
    if (originalId >= 44 && originalId <= 52) {
        return static_cast<WaveletType>(
            static_cast<int>(WaveletType::DAUBECHIES2) + originalId - 44);
    }
    if (originalId >= 53 && originalId <= 62) {
        raiseMappingFidelity(info, PresetMappingFidelity::APPROXIMATED,
                             "daubechies11_to_daubechies20_not_implemented");
        return WaveletType::DAUBECHIES10;
    }
    if (originalId >= 63 && originalId <= 66) {
        raiseMappingFidelity(info, PresetMappingFidelity::APPROXIMATED,
                             "special_wavelet_not_implemented");
        return WaveletType::BIORTHOGONAL11;
    }
    if (originalId == 67)
        return WaveletType::HAAR;

    raiseMappingFidelity(info, PresetMappingFidelity::UNSUPPORTED,
                         "invalid_original_wavelet_id");
    return WaveletType::NONE;
}

} // namespace

float PresetLoader::readJavaFloat(const uint8_t* data) {
    // Java floats are stored in big-endian format
    uint32_t bits = (static_cast<uint32_t>(data[0]) << 24) |
                    (static_cast<uint32_t>(data[1]) << 16) |
                    (static_cast<uint32_t>(data[2]) << 8) |
                    static_cast<uint32_t>(data[3]);

    // C++20: use std::bit_cast for type-safe reinterpretation
    return std::bit_cast<float>(bits);
}

int PresetLoader::findString(const std::vector<uint8_t>& data, const std::string& str, int startPos) {
    if (str.empty() || data.size() < str.size()) [[unlikely]] return -1;

    for (size_t i = startPos; i <= data.size() - str.size(); ++i) {
        bool found = true;
        for (size_t j = 0; j < str.size(); ++j) {
            if (data[i + j] != static_cast<uint8_t>(str[j])) {
                found = false;
                break;
            }
        }
        if (found) [[unlikely]] return static_cast<int>(i);
    }
    return -1;
}

bool PresetLoader::parseJavaHashMap(const std::vector<uint8_t>& data, PresetData& preset) {
    // Check Java serialization magic number (0xACED)
    if (data.size() < 4 || data[0] != 0xAC || data[1] != 0xED) [[unlikely]] {
        std::cerr << "Warning: Not a valid Java serialized file\n";
        return false;
    }

    // Parse float values
    for (const auto& key : FLOAT_KEYS) {
        int pos = findString(data, key);
        if (pos != -1) {
            size_t searchStart = pos + key.length();
            size_t searchEnd = std::min(searchStart + 50, data.size() - 4);

            for (size_t i = searchStart; i < searchEnd; ++i) {
                // Pattern 1: Reference to Float class
                if (i + 6 <= data.size() && data[i] == 0x73 && data[i + 1] == 0x71) {
                    float val = readJavaFloat(&data[i + 6]);
                    if (!std::isnan(val) && !std::isinf(val) && val >= -10000.0f && val <= 10000.0f) {
                        preset.floatValues[key] = val;
                        break;
                    }
                }
                // Pattern 2: Direct Float object
                if (i + 5 <= data.size() && data[i] == 'x' && data[i + 1] == 'p') {
                    float val = readJavaFloat(&data[i + 2]);
                    if (!std::isnan(val) && !std::isinf(val) && val >= -10000.0f && val <= 10000.0f) {
                        preset.floatValues[key] = val;
                        break;
                    }
                }
            }
        }
    }

    // Parse float array values using lambda
    auto tryParseFloatArray = [&data](size_t searchStart, size_t searchEnd) -> std::optional<std::vector<float>> {
        for (size_t i = searchStart; i < searchEnd; ++i) {
            if (i + 2 < data.size() && data[i] == 'u' && (data[i + 1] == 'r' || data[i + 1] == 'q')) {
                for (size_t j = i + 2; j < i + 20 && j + 1 < data.size(); ++j) {
                    if (data[j] == '[' && data[j + 1] == 'F') {
                        for (size_t k = j + 2; k < j + 30 && k + 4 < data.size(); ++k) {
                            if (data[k] == 'x' && data[k + 1] == 'p') {
                                size_t arrLen = (static_cast<size_t>(data[k + 2]) << 24) |
                                               (static_cast<size_t>(data[k + 3]) << 16) |
                                               (static_cast<size_t>(data[k + 4]) << 8) |
                                               static_cast<size_t>(data[k + 5]);
                                if (arrLen > 0 && arrLen <= 10) {
                                    std::vector<float> arr;
                                    arr.reserve(arrLen);
                                    size_t arrStart = k + 6;
                                    for (size_t a = 0; a < arrLen && arrStart + 4 <= data.size(); ++a) {
                                        arr.push_back(readJavaFloat(&data[arrStart]));
                                        arrStart += 4;
                                    }
                                    if (!arr.empty()) {
                                        return arr;
                                    }
                                }
                                return std::nullopt;
                            }
                        }
                    }
                }
            }
        }
        return std::nullopt;
    };

    for (const auto& key : FLOAT_ARRAY_KEYS) {
        int pos = findString(data, key);
        if (pos != -1) {
            size_t searchStart = pos + key.length();
            size_t searchEnd = std::min(searchStart + 60, data.size() - 12);
            if (auto result = tryParseFloatArray(searchStart, searchEnd)) {
                preset.floatArrayValues[key] = std::move(*result);
            }
        }
    }

    return !preset.floatValues.empty();
}

std::optional<PresetData> PresetLoader::parsePreset(const std::string& presetPath) {
    std::ifstream file(presetPath, std::ios::binary);
    if (!file.is_open()) [[unlikely]] {
        std::cerr << "Error: Cannot open preset file: " << presetPath << '\n';
        return std::nullopt;
    }

    std::vector<uint8_t> data((std::istreambuf_iterator<char>(file)),
                               std::istreambuf_iterator<char>());

    PresetData preset;
    if (!parseJavaHashMap(data, preset)) {
        return std::nullopt;
    }

    return preset;
}

const char* presetMappingFidelityName(
    PresetMappingFidelity fidelity) noexcept {
    switch (fidelity) {
    case PresetMappingFidelity::EXACT_COMPATIBLE:
        return "exact-compatible";
    case PresetMappingFidelity::APPROXIMATED:
        return "approximated";
    case PresetMappingFidelity::UNSUPPORTED:
        return "unsupported";
    }
    return "unsupported";
}

std::optional<OriginalPresetConfig> PresetLoader::parseOriginalPreset(
    const std::string& presetPath) {
    std::ifstream file(presetPath, std::ios::binary);
    if (!file.is_open()) [[unlikely]] {
        std::cerr << "Error: Cannot open preset file: " << presetPath << '\n';
        return std::nullopt;
    }

    const std::vector<uint8_t> data(
        (std::istreambuf_iterator<char>(file)),
        std::istreambuf_iterator<char>());
    if (data.size() < 4 || data[0] != 0xAC || data[1] != 0xED) {
        std::cerr << "Warning: Not a valid Java serialized file\n";
        return std::nullopt;
    }

    PresetData controllerState;
    for (const std::string_view key : ORIGINAL_FLOAT_KEYS) {
        const auto position = findSerializedString(data, key);
        if (!position) {
            std::cerr << "Warning: Missing upstream preset field: " << key
                      << '\n';
            return std::nullopt;
        }
        const auto value = serializedFloatAfterKey(
            data, *position + key.size());
        if (!value) {
            std::cerr << "Warning: Invalid upstream preset float: " << key
                      << '\n';
            return std::nullopt;
        }
        controllerState.floatValues.emplace(key, *value);
    }

    for (const std::string_view key : ORIGINAL_FLOAT_ARRAY_KEYS) {
        const auto position = findSerializedString(data, key);
        if (!position) {
            // Presets written before separate_channels was added are decoded
            // by GUI.pde as separate_channels=false.
            if (key == "separate_channels")
                continue;
            std::cerr << "Warning: Missing upstream preset array: " << key
                      << '\n';
            return std::nullopt;
        }
        const auto value = serializedFloatArrayAfterKey(
            data, *position + key.size());
        if (!value) {
            std::cerr << "Warning: Invalid upstream preset array: " << key
                      << '\n';
            return std::nullopt;
        }
        controllerState.floatArrayValues.emplace(key, *value);
    }

    const auto floatValue = [&controllerState](const std::string& key) {
        return controllerState.floatValues.at(key);
    };
    const auto arrayValue = [&controllerState](const std::string& key)
        -> const std::vector<float>& {
        return controllerState.floatArrayValues.at(key);
    };

    OriginalPresetConfig original;
    const int colorSpace = static_cast<int>(floatValue("colorspace"));
    if (colorSpace < 0 || colorSpace >= static_cast<int>(ColorSpace::COUNT)) {
        std::cerr << "Warning: Invalid upstream colorspace: " << colorSpace
                  << '\n';
        return std::nullopt;
    }
    original.colorSpace = static_cast<ColorSpace>(colorSpace);
    original.borderColorR = static_cast<uint8_t>(std::clamp(
        floatValue("color_outside_r"), 0.0f, 255.0f));
    original.borderColorG = static_cast<uint8_t>(std::clamp(
        floatValue("color_outside_g"), 0.0f, 255.0f));
    original.borderColorB = static_cast<uint8_t>(std::clamp(
        floatValue("color_outside_b"), 0.0f, 255.0f));

    if (const auto separate =
            controllerState.floatArrayValues.find("separate_channels");
        separate != controllerState.floatArrayValues.end()) {
        original.separateChannels =
            activeRadioIndex(separate->second, 0) == 0 &&
            !separate->second.empty() && separate->second[0] > 0.5f;
    }

    for (size_t channelIndex = 0; channelIndex < original.channels.size();
         ++channelIndex) {
        // GUI.pde selects chmap[0] for every output channel unless the
        // separate_channels toggle is active. Values stored in ch1/ch2 are
        // merely stale UI state in the common, non-separated case.
        const size_t sourceIndex =
            original.separateChannels ? channelIndex : 0;
        const std::string prefix = "ch" + std::to_string(sourceIndex);
        auto& channel = original.channels[channelIndex];

        channel.minBlockExponent = floatValue(prefix + "min");
        channel.maxBlockExponent = floatValue(prefix + "max");
        const auto minBlock =
            powerOfTwoControllerValue(channel.minBlockExponent);
        const auto maxBlock =
            powerOfTwoControllerValue(channel.maxBlockExponent);
        if (!minBlock || !maxBlock) {
            std::cerr << "Warning: Invalid upstream block exponent in channel "
                      << sourceIndex << '\n';
            return std::nullopt;
        }
        channel.minBlockSize = *minBlock;
        channel.maxBlockSize = *maxBlock;
        channel.segmentationPrecision = floatValue(prefix + "thr");

        channel.predictionListIndex =
            static_cast<int>(floatValue(prefix + "pred"));
        const auto prediction =
            originalPredictionMethod(channel.predictionListIndex);
        if (!prediction) {
            std::cerr << "Warning: Invalid upstream prediction list index: "
                      << channel.predictionListIndex << '\n';
            return std::nullopt;
        }
        channel.predictionMethod = *prediction;
        channel.quantizationControllerValue = floatValue(prefix + "quant");
        channel.quantizationValue = std::clamp(
            static_cast<int>(channel.quantizationControllerValue), 0, 255);
        channel.quantizationStep = channel.quantizationValue / 2.0f;

        const int clampIndex =
            activeRadioIndex(arrayValue(prefix + "clamp"), 0);
        if (clampIndex < 0 || clampIndex > 1) {
            std::cerr << "Warning: Invalid upstream clamp radio state\n";
            return std::nullopt;
        }
        channel.clampMethod = static_cast<ClampMethod>(clampIndex);

        const int waveletListIndex =
            static_cast<int>(floatValue(prefix + "trans"));
        if (waveletListIndex < 0 || waveletListIndex > 68) {
            std::cerr << "Warning: Invalid upstream wavelet list index: "
                      << waveletListIndex << '\n';
            return std::nullopt;
        }
        channel.originalWaveletId =
            waveletListIndex == 68 ? -1 : waveletListIndex;
        channel.transformCompressControllerValue =
            floatValue(prefix + "compress");
        // readValues() stores this controller float directly in ccfg. The
        // nonlinear conversion happens later in codec.pde when constructing
        // CompressorMagnitude.
        channel.transformCompress =
            channel.transformCompressControllerValue;
        const float normalizedCompression =
            channel.transformCompressControllerValue / 255.0f;
        channel.transformCompressionThreshold =
            50.0f * normalizedCompression * normalizedCompression;
        channel.transformScaleExponent = floatValue(prefix + "scale");
        const auto transformScale = transformScaleControllerValue(
            channel.transformScaleExponent);
        if (!transformScale) {
            std::cerr << "Warning: Invalid upstream transform scale exponent\n";
            return std::nullopt;
        }
        channel.transformScale = *transformScale;

        const int transformRadio =
            activeRadioIndex(arrayValue(prefix + "transtype"), 0);
        if (transformRadio < 0 || transformRadio > 2) {
            std::cerr << "Warning: Invalid upstream transform radio state\n";
            return std::nullopt;
        }
        channel.originalTransformType =
            transformRadio == 2 ? -1 : transformRadio;

        const int encoding =
            static_cast<int>(floatValue(prefix + "encoding"));
        if (encoding < 0 || encoding > 2) {
            std::cerr << "Warning: Invalid upstream encoding index: "
                      << encoding << '\n';
            return std::nullopt;
        }
        channel.encodingMethod = static_cast<EncodingMethod>(encoding);
    }

    return original;
}

bool PresetLoader::loadOriginalPreset(const std::string& presetPath,
                                      OriginalPresetConfig& config) {
    const auto original = parseOriginalPreset(presetPath);
    if (!original)
        return false;
    config = *original;
    return true;
}

bool PresetLoader::loadOriginalPresetByName(
    const std::string& presetsDir, const std::string& presetName,
    OriginalPresetConfig& config) {
    const std::filesystem::path presetPath =
        std::filesystem::path(presetsDir) / presetName;
    return loadOriginalPreset(presetPath.string(), config);
}

CodecConfig PresetLoader::projectOriginalPresetToRealtime(
    const OriginalPresetConfig& original, PresetMappingInfo* mappingInfo) {
    PresetMappingInfo localInfo;
    CodecConfig result;
    result.colorSpace = original.colorSpace;
    result.borderColorR = original.borderColorR;
    result.borderColorG = original.borderColorG;
    result.borderColorB = original.borderColorB;

    for (size_t index = 0; index < result.channels.size(); ++index) {
        const auto& source = original.channels[index];
        auto& destination = result.channels[index];
        destination.minBlockSize = source.minBlockSize;
        destination.maxBlockSize = source.maxBlockSize;
        if (source.maxBlockSize > 256) {
            raiseMappingFidelity(localInfo,
                                 PresetMappingFidelity::APPROXIMATED,
                                 "realtime_backend_clamps_block_size_to_256");
        }
        destination.segmentationPrecision = source.segmentationPrecision;
        destination.predictionMethod = source.predictionMethod;
        destination.quantizationValue = source.quantizationValue;
        destination.clampMethod = source.clampMethod;
        destination.waveletType =
            projectOriginalWavelet(source.originalWaveletId, localInfo);
        destination.transformCompress = source.transformCompress;
        destination.transformScale = source.transformScale;
        if (source.originalTransformType == -1) {
            raiseMappingFidelity(
                localInfo, PresetMappingFidelity::UNSUPPORTED,
                "random_transform_requires_per_encode_selection");
            destination.transformType = TransformType::FWT;
        } else {
            destination.transformType =
                static_cast<TransformType>(source.originalTransformType);
        }
        destination.encodingMethod = source.encodingMethod;
    }

    if (mappingInfo)
        *mappingInfo = std::move(localInfo);
    return result;
}

bool PresetLoader::loadOriginalPreset(const std::string& presetPath,
                                      CodecConfig& config,
                                      PresetMappingInfo* mappingInfo) {
    OriginalPresetConfig original;
    if (!loadOriginalPreset(presetPath, original))
        return false;
    config = projectOriginalPresetToRealtime(original, mappingInfo);
    return true;
}

bool PresetLoader::loadOriginalPresetByName(
    const std::string& presetsDir, const std::string& presetName,
    CodecConfig& config, PresetMappingInfo* mappingInfo) {
    const std::filesystem::path presetPath =
        std::filesystem::path(presetsDir) / presetName;
    return loadOriginalPreset(presetPath.string(), config, mappingInfo);
}

void PresetLoader::applyPresetToConfig(const PresetData& preset, CodecConfig& config) {
    // Apply colorspace
    if (auto it = preset.floatValues.find("colorspace"); it != preset.floatValues.end()) {
        int cs = static_cast<int>(it->second);
        if (cs >= 0 && cs < static_cast<int>(ColorSpace::COUNT)) {
            config.colorSpace = static_cast<ColorSpace>(cs);
        }
    }

    // Apply border colors using C++20 designated pattern
    if (auto it = preset.floatValues.find("color_outside_r"); it != preset.floatValues.end()) {
        config.borderColorR = static_cast<uint8_t>(std::clamp(it->second, 0.0f, 255.0f));
    }
    if (auto it = preset.floatValues.find("color_outside_g"); it != preset.floatValues.end()) {
        config.borderColorG = static_cast<uint8_t>(std::clamp(it->second, 0.0f, 255.0f));
    }
    if (auto it = preset.floatValues.find("color_outside_b"); it != preset.floatValues.end()) {
        config.borderColorB = static_cast<uint8_t>(std::clamp(it->second, 0.0f, 255.0f));
    }

    // Apply channel-specific settings
    constexpr std::array channels = {"ch0", "ch1", "ch2"};
    for (size_t i = 0; i < 3; ++i) {
        std::string prefix = channels[i];
        auto& ch = config.channels[i];

        if (auto it = preset.floatValues.find(prefix + "min"); it != preset.floatValues.end()) {
            ch.minBlockSize = static_cast<int>(std::max(1.0f, it->second));
        }
        if (auto it = preset.floatValues.find(prefix + "max"); it != preset.floatValues.end()) {
            ch.maxBlockSize = static_cast<int>(std::max(1.0f, it->second));
        }
        if (auto it = preset.floatValues.find(prefix + "pred"); it != preset.floatValues.end()) {
            int pm = static_cast<int>(it->second);
            if (pm >= -3 && pm < static_cast<int>(PredictionMethod::COUNT)) {
                ch.predictionMethod = static_cast<PredictionMethod>(pm);
            }
        }
        if (auto it = preset.floatValues.find(prefix + "quant"); it != preset.floatValues.end()) {
            ch.quantizationValue = static_cast<int>(std::clamp(it->second, 0.0f, 255.0f));
        }
        if (auto it = preset.floatValues.find(prefix + "scale"); it != preset.floatValues.end()) {
            ch.transformScale = static_cast<int>(it->second);
        }
        if (auto it = preset.floatValues.find(prefix + "compress"); it != preset.floatValues.end()) {
            ch.transformCompress = it->second;
        }
        if (auto it = preset.floatValues.find(prefix + "thr"); it != preset.floatValues.end()) {
            ch.segmentationPrecision = it->second;
        }
        if (auto it = preset.floatValues.find(prefix + "trans"); it != preset.floatValues.end()) {
            int wt = static_cast<int>(it->second);
            if (wt >= 0 && wt < static_cast<int>(WaveletType::COUNT)) {
                ch.waveletType = static_cast<WaveletType>(wt);
            }
        }
        if (auto it = preset.floatValues.find(prefix + "encoding"); it != preset.floatValues.end()) {
            if (it->second < 0.25f) {
                ch.encodingMethod = EncodingMethod::RAW;
            } else if (it->second < 0.75f) {
                ch.encodingMethod = EncodingMethod::PACKED;
            } else {
                ch.encodingMethod = EncodingMethod::RLE;
            }
        }

        // Clamp and transform type arrays
        if (auto arrIt = preset.floatArrayValues.find(prefix + "clamp");
            arrIt != preset.floatArrayValues.end() && !arrIt->second.empty()) {
            ch.clampMethod = (arrIt->second[0] > 0.5f) ? ClampMethod::MOD256 : ClampMethod::NONE;
        }
        if (auto arrIt = preset.floatArrayValues.find(prefix + "transtype");
            arrIt != preset.floatArrayValues.end() && !arrIt->second.empty()) {
            ch.transformType = (arrIt->second[0] > 0.5f) ? TransformType::WPT : TransformType::FWT;
        }
    }
}

bool PresetLoader::loadPreset(const std::string& presetPath, CodecConfig& config) {
    auto preset = parsePreset(presetPath);
    if (!preset) [[unlikely]] {
        return false;
    }
    applyPresetToConfig(*preset, config);
    return true;
}

bool PresetLoader::loadPresetByName(const std::string& presetsDir, const std::string& presetName, CodecConfig& config) {
    std::filesystem::path presetPath = std::filesystem::path(presetsDir) / presetName;
    return loadPreset(presetPath.string(), config);
}

std::vector<std::string> PresetLoader::listPresets(const std::string& presetsDir) {
    std::vector<std::string> presets;

    try {
        namespace fs = std::filesystem;
        for (const auto& entry : fs::directory_iterator(presetsDir)) {
            if (entry.is_regular_file()) {
                auto filename = entry.path().filename().string();
                if (!filename.empty() && !filename.starts_with('.')) {
                    presets.push_back(std::move(filename));
                }
            }
        }
        std::ranges::sort(presets);
    } catch (const std::exception& e) {
        std::cerr << "Error listing presets: " << e.what() << '\n';
    }

    return presets;
}

} // namespace glic
