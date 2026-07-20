#pragma once

#include "config.hpp"
#include <string>
#include <vector>
#include <map>
#include <optional>
#include <cstdint>

namespace glic {

// Preset data parsed from Java serialized format
struct PresetData {
    std::map<std::string, float> floatValues;
    std::map<std::string, std::vector<float>> floatArrayValues;
};

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
