#include "glic.hpp"
#include "effects.hpp"
#include "colorspaces.hpp"
#include "preset_loader.hpp"
#include <iostream>
#include <string>
#include <string_view>
#include <vector>
#include <algorithm>
#include <ranges>
#include <filesystem>
#include <sstream>
#include <optional>
#include <span>
#include <charconv>

using namespace glic;

// Safe string to int conversion using std::from_chars (C++17/20)
[[nodiscard]] constexpr std::optional<int> safeStoi(std::string_view str) noexcept {
    int result{};
    auto [ptr, ec] = std::from_chars(str.data(), str.data() + str.size(), result);
    if (ec == std::errc{} && ptr == str.data() + str.size()) {
        return result;
    }
    return std::nullopt;
}

// Safe string to float conversion
[[nodiscard]] std::optional<float> safeStof(std::string_view str) noexcept {
    // std::from_chars for float may not be available on all platforms
    // Fall back to strtof for portability
    if (str.empty()) return std::nullopt;

    std::string temp(str);
    char* end = nullptr;
    float result = std::strtof(temp.c_str(), &end);

    if (end == temp.c_str() + temp.size()) {
        return result;
    }
    return std::nullopt;
}

// Parse comma-separated RGB values (e.g., "128,128,128")
[[nodiscard]] bool parseRGB(std::string_view str, int& r, int& g, int& b) noexcept {
    std::vector<int> values;
    values.reserve(3);

    size_t start = 0;
    size_t pos = 0;

    while ((pos = str.find(',', start)) != std::string_view::npos) {
        if (auto val = safeStoi(str.substr(start, pos - start))) {
            values.push_back(*val);
        } else {
            return false;
        }
        start = pos + 1;
    }

    // Last token
    if (auto val = safeStoi(str.substr(start))) {
        values.push_back(*val);
    } else {
        return false;
    }

    if (values.size() != 3) return false;
    r = values[0];
    g = values[1];
    b = values[2];
    return true;
}

// Parse comma-separated XY values (e.g., "2,0")
[[nodiscard]] bool parseXY(std::string_view str, int& x, int& y) noexcept {
    std::vector<int> values;
    values.reserve(2);

    size_t start = 0;
    size_t pos = 0;

    while ((pos = str.find(',', start)) != std::string_view::npos) {
        if (auto val = safeStoi(str.substr(start, pos - start))) {
            values.push_back(*val);
        } else {
            return false;
        }
        start = pos + 1;
    }

    // Last token
    if (auto val = safeStoi(str.substr(start))) {
        values.push_back(*val);
    } else {
        return false;
    }

    if (values.empty()) return false;
    x = values[0];
    y = (values.size() >= 2) ? values[1] : 0;
    return true;
}

// Get default presets directory (relative to executable or current directory)
[[nodiscard]] std::string getDefaultPresetsDir(std::string_view programPath) {
    namespace fs = std::filesystem;

    fs::path execPath(programPath);
    fs::path presetsPath = execPath.parent_path() / "presets";

    if (fs::exists(presetsPath)) {
        return presetsPath.string();
    }
    if (fs::exists("presets")) {
        return "presets";
    }
    if (fs::exists("../presets")) {
        return "../presets";
    }
    return "presets";
}

void printUsage(std::string_view programName) {
    std::cout << "GLIC - GLitch Image Codec (C++20 Version)\n\n"
              << "Usage:\n"
              << "  " << programName << " encode <input.png> <output.glic> [options]\n"
              << "  " << programName << " decode <input.glic> <output.png> [options]\n"
              << "  " << programName << " --list-presets [--presets-dir <path>]\n\n"
              << "Preset Options:\n"
              << "  --preset <name>          Load preset by name (e.g., 'default', 'colour_waves')\n"
              << "  --presets-dir <path>     Directory containing presets (default: ./presets)\n"
              << "  --list-presets           List all available presets\n\n"
              << "Encode Options:\n"
              << "  --colorspace <name>      Color space (default: HWB)\n"
              << "                           Options: RGB, HSB, HWB, OHTA, CMY, XYZ, YXY, LAB, LUV,\n"
              << "                                    HCL, YUV, YPbPr, YCbCr, YDbDr, GS, R-GGB-G\n"
              << "  --min-block <size>       Min block size (default: 2)\n"
              << "  --max-block <size>       Max block size (default: 256)\n"
              << "  --threshold <value>      Segmentation threshold (default: 15)\n"
              << "  --prediction <method>    Prediction method (default: PAETH)\n"
              << "                           Options: NONE, CORNER, H, V, DC, DCMEDIAN, MEDIAN, AVG,\n"
              << "                                    TRUEMOTION, PAETH, LDIAG, HV, JPEGLS, DIFF,\n"
              << "                                    REF, ANGLE, SAD, BSAD, RANDOM,\n"
              << "                                    SPIRAL, NOISE, GRADIENT, MIRROR, WAVE,\n"
              << "                                    CHECKERBOARD, RADIAL, EDGE\n"
              << "  --quantization <value>   Quantization value 0-255 (default: 110)\n"
              << "  --clamp <method>         Clamp method: none, mod256 (default: none)\n"
              << "  --wavelet <name>         Wavelet type (default: SYMLET8)\n"
              << "                           Options: NONE, HAAR, DB2-DB10, SYM2-SYM10, COIF1-COIF5\n"
              << "  --transform <type>       Transform type: fwt, wpt (default: fwt)\n"
              << "  --scale <value>          Transform scale (default: 20)\n"
              << "  --encoding <method>      Encoding method (default: packed)\n"
              << "                           Options: raw, packed, rle, delta, xor, zigzag\n"
              << "  --border <r,g,b>         Border color RGB (default: 128,128,128)\n"
              << "\nDecode Options (Post-Effects):\n"
              << "  --effect <name>          Apply post effect (can be used multiple times)\n"
              << "                           Basic: pixelate, scanline, chromatic, dither, posterize, glitch\n"
              << "                           Advanced: dct, sort, leak\n"
              << "  --effect-intensity <n>   Effect intensity 0-100 (default: 50)\n"
              << "  --effect-blocksize <n>   Block size for pixelate/glitch/dct/leak (default: 8)\n"
              << "  --effect-offset <x,y>    Chromatic aberration offset (default: 2,0)\n"
              << "  --effect-levels <n>      Posterize levels (default: 4)\n"
              << "  --effect-threshold <n>   Pixel sort threshold 0-255 (default: 50)\n"
              << "  --effect-sortmode <m>    Sort mode: brightness, hue, saturation, red, green, blue\n"
              << "  --effect-vertical        Enable vertical sorting (default: horizontal)\n"
              << "  --effect-leak <f>        Prediction leak amount 0.0-1.0 (default: 0.5)\n"
              << "\nExamples:\n"
              << "  " << programName << " encode photo.png glitched.glic\n"
              << "  " << programName << " encode photo.png glitched.glic --colorspace HWB --prediction SPIRAL\n"
              << "  " << programName << " decode glitched.glic result.png --effect scanline --effect chromatic\n";
}

[[nodiscard]] bool parseArgs(std::span<char*> args, std::string& command, std::string& input,
                             std::string& output, CodecConfig& config, PostEffectsConfig& postEffects,
                             std::string& presetsDir, std::string& presetName) {
    if (args.size() < 2) {
        return false;
    }

    // Check for --list-presets first
    for (size_t i = 1; i < args.size(); ++i) {
        std::string_view arg = args[i];
        if (arg == "--list-presets") {
            command = "list-presets";
            // Look for --presets-dir
            for (size_t j = 1; j < args.size(); ++j) {
                if (std::string_view(args[j]) == "--presets-dir" && j + 1 < args.size()) {
                    presetsDir = args[j + 1];
                }
            }
            return true;
        }
    }

    if (args.size() < 4) {
        return false;
    }

    command = args[1];
    input = args[2];
    output = args[3];

    // Default config with designated initializers
    config = CodecConfig{};
    postEffects = PostEffectsConfig{};

    // Default effect config
    EffectConfig currentEffect{
        .type = EffectType::NONE,
        .intensity = 50,
        .blockSize = 8,
        .offsetX = 2,
        .offsetY = 0,
        .levels = 4,
        .seed = 12345,
        .sortMode = PixelSortMode::BRIGHTNESS,
        .threshold = 50,
        .sortVertical = false,
        .leakAmount = 0.5f
    };

    // Parse options
    for (size_t i = 4; i < args.size(); ++i) {
        std::string_view arg = args[i];

        // Preset options
        if (arg == "--preset" && i + 1 < args.size()) {
            presetName = args[++i];
        }
        else if (arg == "--presets-dir" && i + 1 < args.size()) {
            presetsDir = args[++i];
        }
        else if (arg == "--colorspace" && i + 1 < args.size()) {
            config.colorSpace = colorSpaceFromName(args[++i]);
        }
        else if (arg == "--min-block" && i + 1 < args.size()) {
            if (auto val = safeStoi(args[++i])) {
                std::ranges::for_each(config.channels, [v = *val](auto& ch) { ch.minBlockSize = v; });
            }
        }
        else if (arg == "--max-block" && i + 1 < args.size()) {
            if (auto val = safeStoi(args[++i])) {
                std::ranges::for_each(config.channels, [v = *val](auto& ch) { ch.maxBlockSize = v; });
            }
        }
        else if (arg == "--threshold" && i + 1 < args.size()) {
            if (auto val = safeStof(args[++i])) {
                std::ranges::for_each(config.channels, [v = *val](auto& ch) { ch.segmentationPrecision = v; });
            }
        }
        else if (arg == "--prediction" && i + 1 < args.size()) {
            auto val = predictionFromName(args[++i]);
            std::ranges::for_each(config.channels, [val](auto& ch) { ch.predictionMethod = val; });
        }
        else if (arg == "--quantization" && i + 1 < args.size()) {
            if (auto val = safeStoi(args[++i])) {
                std::ranges::for_each(config.channels, [v = *val](auto& ch) { ch.quantizationValue = v; });
            }
        }
        else if (arg == "--clamp" && i + 1 < args.size()) {
            std::string_view val = args[++i];
            ClampMethod cm = (val == "mod256") ? ClampMethod::MOD256 : ClampMethod::NONE;
            std::ranges::for_each(config.channels, [cm](auto& ch) { ch.clampMethod = cm; });
        }
        else if (arg == "--wavelet" && i + 1 < args.size()) {
            auto val = waveletFromName(args[++i]);
            std::ranges::for_each(config.channels, [val](auto& ch) { ch.waveletType = val; });
        }
        else if (arg == "--transform" && i + 1 < args.size()) {
            std::string_view val = args[++i];
            TransformType tt = (val == "wpt") ? TransformType::WPT : TransformType::FWT;
            std::ranges::for_each(config.channels, [tt](auto& ch) { ch.transformType = tt; });
        }
        else if (arg == "--scale" && i + 1 < args.size()) {
            if (auto val = safeStoi(args[++i])) {
                std::ranges::for_each(config.channels, [v = *val](auto& ch) { ch.transformScale = v; });
            }
        }
        else if (arg == "--encoding" && i + 1 < args.size()) {
            auto val = encodingFromName(args[++i]);
            std::ranges::for_each(config.channels, [val](auto& ch) { ch.encodingMethod = val; });
        }
        else if (arg == "--border" && i + 1 < args.size()) {
            std::string_view colorStr = args[++i];
            int r = 128, g = 128, b = 128;
            if (parseRGB(colorStr, r, g, b)) {
                config.borderColorR = static_cast<uint8_t>(std::clamp(r, 0, 255));
                config.borderColorG = static_cast<uint8_t>(std::clamp(g, 0, 255));
                config.borderColorB = static_cast<uint8_t>(std::clamp(b, 0, 255));
            }
        }
        // Post-effect options
        else if (arg == "--effect" && i + 1 < args.size()) {
            std::string effectName = args[++i];
            EffectConfig effect = currentEffect;
            effect.type = effectFromName(effectName);
            if (effect.type != EffectType::NONE) [[likely]] {
                postEffects.effects.push_back(effect);
                postEffects.enabled = true;
            }
        }
        else if (arg == "--effect-intensity" && i + 1 < args.size()) {
            if (auto val = safeStoi(args[++i])) {
                currentEffect.intensity = *val;
            }
        }
        else if (arg == "--effect-blocksize" && i + 1 < args.size()) {
            if (auto val = safeStoi(args[++i])) {
                currentEffect.blockSize = *val;
            }
        }
        else if (arg == "--effect-offset" && i + 1 < args.size()) {
            std::string_view offsetStr = args[++i];
            int x = 2, y = 0;
            if (parseXY(offsetStr, x, y)) {
                currentEffect.offsetX = x;
                currentEffect.offsetY = y;
            }
        }
        else if (arg == "--effect-levels" && i + 1 < args.size()) {
            if (auto val = safeStoi(args[++i])) {
                currentEffect.levels = *val;
            }
        }
        else if (arg == "--effect-threshold" && i + 1 < args.size()) {
            if (auto val = safeStoi(args[++i])) {
                currentEffect.threshold = std::clamp(*val, 0, 255);
            }
        }
        else if (arg == "--effect-sortmode" && i + 1 < args.size()) {
            std::string_view mode = args[++i];
            if (mode == "brightness") currentEffect.sortMode = PixelSortMode::BRIGHTNESS;
            else if (mode == "hue") currentEffect.sortMode = PixelSortMode::HUE;
            else if (mode == "saturation") currentEffect.sortMode = PixelSortMode::SATURATION;
            else if (mode == "red") currentEffect.sortMode = PixelSortMode::RED;
            else if (mode == "green") currentEffect.sortMode = PixelSortMode::GREEN;
            else if (mode == "blue") currentEffect.sortMode = PixelSortMode::BLUE;
        }
        else if (arg == "--effect-vertical") {
            currentEffect.sortVertical = true;
        }
        else if (arg == "--effect-leak" && i + 1 < args.size()) {
            if (auto val = safeStof(args[++i])) {
                currentEffect.leakAmount = std::clamp(*val, 0.0f, 1.0f);
            }
        }
        else if (arg == "--help" || arg == "-h") {
            return false;
        }
    }

    return true;
}

int main(int argc, char* argv[]) {
    std::span args(argv, static_cast<size_t>(argc));

    std::string command, input, output;
    std::string presetsDir, presetName;
    CodecConfig config;
    PostEffectsConfig postEffects;

    // Get default presets directory
    presetsDir = getDefaultPresetsDir(args[0]);

    if (!parseArgs(args, command, input, output, config, postEffects, presetsDir, presetName)) {
        printUsage(args[0]);
        return 1;
    }

    // Convert command to lowercase using ranges
    std::ranges::transform(command, command.begin(), ::tolower);

    // Handle list-presets command
    if (command == "list-presets") {
        auto presets = PresetLoader::listPresets(presetsDir);
        if (presets.empty()) [[unlikely]] {
            std::cout << "No presets found in: " << presetsDir << '\n';
            return 1;
        }
        std::cout << "Available presets (" << presets.size() << "):\n";
        std::ranges::for_each(presets, [](const auto& p) {
            std::cout << "  " << p << '\n';
        });
        return 0;
    }

    // Load preset if specified
    if (!presetName.empty()) {
        std::cout << "Loading preset: " << presetName << '\n';
        if (!PresetLoader::loadPresetByName(presetsDir, presetName, config)) [[unlikely]] {
            std::cerr << "Warning: Failed to load preset '" << presetName << "' from " << presetsDir << '\n';
            std::cerr << "Continuing with default settings...\n";
        } else {
            std::cout << "Preset loaded successfully\n";
        }
    }

    GlicCodec codec(config);
    codec.setPostEffects(postEffects);

    if (command == "encode") {
        // Load input image
        std::vector<Color> pixels;
        int width, height;

        if (!loadImage(input, pixels, width, height)) [[unlikely]] {
            std::cerr << "Error: Failed to load image: " << input << '\n';
            return 1;
        }

        std::cout << "Loaded image: " << width << "x" << height << '\n';

        // Encode
        auto result = codec.encode(pixels.data(), width, height, output);

        if (!result.success) [[unlikely]] {
            std::cerr << "Error: " << result.error << '\n';
            return 1;
        }

        std::cout << "Encoded to: " << output << '\n';
    }
    else if (command == "decode") {
        // Decode
        auto result = codec.decode(input);

        if (!result.success) [[unlikely]] {
            std::cerr << "Error: " << result.error << '\n';
            return 1;
        }

        // Save output image
        if (!saveImage(output, result.pixels, result.width, result.height)) [[unlikely]] {
            std::cerr << "Error: Failed to save image: " << output << '\n';
            return 1;
        }

        std::cout << "Decoded to: " << output << " (" << result.width << "x" << result.height << ")\n";
    }
    else {
        std::cerr << "Error: Unknown command: " << command << '\n';
        printUsage(args[0]);
        return 1;
    }

    return 0;
}
