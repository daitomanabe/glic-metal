#pragma once

#include "config.hpp"
#include <array>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace glic {

// Preset data parsed from Java serialized format
struct PresetData {
    std::map<std::string, float> floatValues;
    std::map<std::string, std::vector<float>> floatArrayValues;
};

// The files in presets/ store ControlP5 controller state rather than the
// values consumed directly by codec.pde. These structures expose the effective
// upstream codec values after applying GUI.pde's readValues() conversions.
// Keep originalWaveletId separate from WaveletType: upstream GLIC has IDs
// 0..67, while the historical C++ enum has a different layout above ID 30.
struct OriginalPresetChannel {
    float minBlockExponent = 0.0f;
    float maxBlockExponent = 0.0f;
    int minBlockSize = 1;
    int maxBlockSize = 1;
    float segmentationPrecision = 15.0f;
    int predictionListIndex = 0;
    PredictionMethod predictionMethod = PredictionMethod::NONE;
    float quantizationControllerValue = 0.0f;
    int quantizationValue = 0;
    float quantizationStep = 0.0f; // codec.pde quant_value(value).
    ClampMethod clampMethod = ClampMethod::NONE;
    int originalWaveletId = 0; // -1 is WAVELET_RANDOM.
    float transformCompressControllerValue = 0.0f;
    float transformCompress = 0.0f;
    float transformCompressionThreshold = 0.0f;
    float transformScaleExponent = 0.0f;
    int transformScale = 1;
    int originalTransformType = 0; // -1 random, 0 FWT, 1 WPT.
    EncodingMethod encodingMethod = EncodingMethod::RAW;
};

struct OriginalPresetConfig {
    ColorSpace colorSpace = ColorSpace::RGB;
    uint8_t borderColorR = 128;
    uint8_t borderColorG = 128;
    uint8_t borderColorB = 128;
    bool separateChannels = false;
    std::array<OriginalPresetChannel, 3> channels{};
};

enum class PresetMappingFidelity : uint8_t {
    EXACT_COMPATIBLE = 0,
    APPROXIMATED = 1,
    UNSUPPORTED = 2
};

struct PresetMappingInfo {
    PresetMappingFidelity fidelity = PresetMappingFidelity::EXACT_COMPATIBLE;
    std::vector<std::string> reasons;
};

const char* presetMappingFidelityName(PresetMappingFidelity fidelity) noexcept;

// Preset loader for GLIC Java serialized presets
class PresetLoader {
public:
    // Load preset from file and apply to config
    static bool loadPreset(const std::string& presetPath, CodecConfig& config);

    // Load preset from presets directory by name
    static bool loadPresetByName(const std::string& presetsDir, const std::string& presetName, CodecConfig& config);

    // List all available presets in a directory
    static std::vector<std::string> listPresets(const std::string& presetsDir);

    // Parse preset file to raw data
    static std::optional<PresetData> parsePreset(const std::string& presetPath);

    // Decode the upstream Processing/ControlP5 preset semantics exactly. This
    // is deliberately separate from parsePreset/loadPreset so existing C++
    // realtime recipes retain their historical behaviour.
    static std::optional<OriginalPresetConfig> parseOriginalPreset(
        const std::string& presetPath);

    static bool loadOriginalPreset(const std::string& presetPath,
                                   OriginalPresetConfig& config);

    static bool loadOriginalPresetByName(const std::string& presetsDir,
                                         const std::string& presetName,
                                         OriginalPresetConfig& config);

    // Project correctly decoded upstream values into the historical realtime
    // CodecConfig. The returned metadata reports fields that cannot be
    // represented faithfully; it does not claim that the realtime visual
    // algorithm is equivalent to upstream codec.pde.
    static CodecConfig projectOriginalPresetToRealtime(
        const OriginalPresetConfig& original,
        PresetMappingInfo* mappingInfo = nullptr);

    static bool loadOriginalPreset(const std::string& presetPath,
                                   CodecConfig& config,
                                   PresetMappingInfo* mappingInfo = nullptr);

    static bool loadOriginalPresetByName(const std::string& presetsDir,
                                         const std::string& presetName,
                                         CodecConfig& config,
                                         PresetMappingInfo* mappingInfo = nullptr);

    // Apply preset data to config
    static void applyPresetToConfig(const PresetData& preset, CodecConfig& config);

private:
    // Parse Java serialized HashMap
    static bool parseJavaHashMap(const std::vector<uint8_t>& data, PresetData& preset);

    // Read float from Java serialized data
    static float readJavaFloat(const uint8_t* data);

    // Find string in data
    static int findString(const std::vector<uint8_t>& data, const std::string& str, int startPos = 0);
};

} // namespace glic
