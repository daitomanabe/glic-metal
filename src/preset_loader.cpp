#include "preset_loader.hpp"
#include <fstream>
#include <algorithm>
#include <ranges>
#include <iostream>
#include <filesystem>
#include <cmath>
#include <bit>
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
