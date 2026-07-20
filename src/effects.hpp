#pragma once

#include "config.hpp"
#include <vector>
#include <cstdint>
#include <string>

namespace glic {

// Post-processing effect types
enum class EffectType : uint8_t {
    NONE = 0,
    PIXELATE = 1,
    SCANLINE = 2,
    CHROMATIC_ABERRATION = 3,
    DITHER = 4,
    POSTERIZE = 5,
    GLITCH_SHIFT = 6,
    // New glitch effects inspired by classic techniques
    DCT_CORRUPT = 7,        // Rosa Menkman style DCT block corruption
    PIXEL_SORT = 8,         // Kim Asendorf style pixel sorting
    PREDICTION_LEAK = 9,    // Datamoshing-inspired prediction leak
    COUNT = 10
};

std::string effectName(EffectType et);
EffectType effectFromName(const std::string& name);

// Pixel sort mode
enum class PixelSortMode : uint8_t {
    BRIGHTNESS = 0,
    HUE = 1,
    SATURATION = 2,
    RED = 3,
    GREEN = 4,
    BLUE = 5
};

// Effect configuration
struct EffectConfig {
    EffectType type = EffectType::NONE;
    int intensity = 50;        // 0-100, effect strength
    int blockSize = 8;         // For pixelate, glitch_shift, dct_corrupt
    int offsetX = 2;           // For chromatic aberration
    int offsetY = 0;           // For chromatic aberration
    int levels = 4;            // For posterize
    uint32_t seed = 12345;     // For randomized effects
    // New parameters for advanced effects
    PixelSortMode sortMode = PixelSortMode::BRIGHTNESS;  // For pixel_sort
    int threshold = 50;        // For pixel_sort interval detection (0-255)
    bool sortVertical = false; // For pixel_sort direction
    float leakAmount = 0.5f;   // For prediction_leak (0.0-1.0)
};

// Post-effects configuration
struct PostEffectsConfig {
    std::vector<EffectConfig> effects;
    bool enabled = false;
};

// Apply single effect to pixel buffer
void applyEffect(
    std::vector<Color>& pixels,
    int width,
    int height,
    const EffectConfig& config
);

// Apply multiple effects in sequence
void applyEffects(
    std::vector<Color>& pixels,
    int width,
    int height,
    const std::vector<EffectConfig>& effects
);

// Individual effect functions
void effectPixelate(std::vector<Color>& pixels, int w, int h, int blockSize);
void effectScanline(std::vector<Color>& pixels, int w, int h, int intensity);
void effectChromaticAberration(std::vector<Color>& pixels, int w, int h, int offsetX, int offsetY);
void effectDither(std::vector<Color>& pixels, int w, int h, int intensity);
void effectPosterize(std::vector<Color>& pixels, int w, int h, int levels);
void effectGlitchShift(std::vector<Color>& pixels, int w, int h, int blockSize, uint32_t seed);

// New glitch effect functions
void effectDctCorrupt(std::vector<Color>& pixels, int w, int h, int blockSize, int intensity, uint32_t seed);
void effectPixelSort(std::vector<Color>& pixels, int w, int h, PixelSortMode mode, int threshold, bool vertical);
void effectPredictionLeak(std::vector<Color>& pixels, int w, int h, int blockSize, float leakAmount, uint32_t seed);

} // namespace glic
