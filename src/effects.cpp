#include "effects.hpp"
#include <cmath>
#include <algorithm>
#include <random>
#include <numeric>

namespace glic {

std::string effectName(EffectType et) {
    switch (et) {
        case EffectType::NONE: return "NONE";
        case EffectType::PIXELATE: return "PIXELATE";
        case EffectType::SCANLINE: return "SCANLINE";
        case EffectType::CHROMATIC_ABERRATION: return "CHROMATIC_ABERRATION";
        case EffectType::DITHER: return "DITHER";
        case EffectType::POSTERIZE: return "POSTERIZE";
        case EffectType::GLITCH_SHIFT: return "GLITCH_SHIFT";
        case EffectType::DCT_CORRUPT: return "DCT_CORRUPT";
        case EffectType::PIXEL_SORT: return "PIXEL_SORT";
        case EffectType::PREDICTION_LEAK: return "PREDICTION_LEAK";
        default: return "NONE";
    }
}

EffectType effectFromName(const std::string& name) {
    if (name == "PIXELATE") return EffectType::PIXELATE;
    if (name == "SCANLINE") return EffectType::SCANLINE;
    if (name == "CHROMATIC_ABERRATION" || name == "CHROMATIC") return EffectType::CHROMATIC_ABERRATION;
    if (name == "DITHER") return EffectType::DITHER;
    if (name == "POSTERIZE") return EffectType::POSTERIZE;
    if (name == "GLITCH_SHIFT" || name == "GLITCH") return EffectType::GLITCH_SHIFT;
    if (name == "DCT_CORRUPT" || name == "DCT") return EffectType::DCT_CORRUPT;
    if (name == "PIXEL_SORT" || name == "SORT") return EffectType::PIXEL_SORT;
    if (name == "PREDICTION_LEAK" || name == "LEAK") return EffectType::PREDICTION_LEAK;
    return EffectType::NONE;
}

void applyEffect(
    std::vector<Color>& pixels,
    int width,
    int height,
    const EffectConfig& config
) {
    switch (config.type) {
        case EffectType::PIXELATE:
            effectPixelate(pixels, width, height, config.blockSize);
            break;
        case EffectType::SCANLINE:
            effectScanline(pixels, width, height, config.intensity);
            break;
        case EffectType::CHROMATIC_ABERRATION:
            effectChromaticAberration(pixels, width, height, config.offsetX, config.offsetY);
            break;
        case EffectType::DITHER:
            effectDither(pixels, width, height, config.intensity);
            break;
        case EffectType::POSTERIZE:
            effectPosterize(pixels, width, height, config.levels);
            break;
        case EffectType::GLITCH_SHIFT:
            effectGlitchShift(pixels, width, height, config.blockSize, config.seed);
            break;
        case EffectType::DCT_CORRUPT:
            effectDctCorrupt(pixels, width, height, config.blockSize, config.intensity, config.seed);
            break;
        case EffectType::PIXEL_SORT:
            effectPixelSort(pixels, width, height, config.sortMode, config.threshold, config.sortVertical);
            break;
        case EffectType::PREDICTION_LEAK:
            effectPredictionLeak(pixels, width, height, config.blockSize, config.leakAmount, config.seed);
            break;
        default:
            break;
    }
}

void applyEffects(
    std::vector<Color>& pixels,
    int width,
    int height,
    const std::vector<EffectConfig>& effects
) {
    for (const auto& effect : effects) {
        applyEffect(pixels, width, height, effect);
    }
}

// PIXELATE - Reduce resolution in blocks
void effectPixelate(std::vector<Color>& pixels, int w, int h, int blockSize) {
    if (blockSize < 2) return;

    for (int by = 0; by < h; by += blockSize) {
        for (int bx = 0; bx < w; bx += blockSize) {
            // Calculate average color for block
            int sumR = 0, sumG = 0, sumB = 0, sumA = 0;
            int count = 0;

            for (int y = by; y < std::min(by + blockSize, h); y++) {
                for (int x = bx; x < std::min(bx + blockSize, w); x++) {
                    Color c = pixels[static_cast<size_t>(y * w + x)];
                    sumR += getR(c);
                    sumG += getG(c);
                    sumB += getB(c);
                    sumA += getA(c);
                    count++;
                }
            }

            Color avg = makeColor(
                static_cast<uint8_t>(sumR / count),
                static_cast<uint8_t>(sumG / count),
                static_cast<uint8_t>(sumB / count),
                static_cast<uint8_t>(sumA / count)
            );

            // Fill block with average
            for (int y = by; y < std::min(by + blockSize, h); y++) {
                for (int x = bx; x < std::min(bx + blockSize, w); x++) {
                    pixels[static_cast<size_t>(y * w + x)] = avg;
                }
            }
        }
    }
}

// SCANLINE - Add scanline effect
void effectScanline(std::vector<Color>& pixels, int w, int h, int intensity) {
    float factor = 1.0f - (static_cast<float>(intensity) / 100.0f) * 0.5f;

    for (int y = 0; y < h; y++) {
        if (y % 2 == 1) {  // Darken odd lines
            for (int x = 0; x < w; x++) {
                Color c = pixels[static_cast<size_t>(y * w + x)];
                pixels[static_cast<size_t>(y * w + x)] = makeColor(
                    static_cast<uint8_t>(getR(c) * factor),
                    static_cast<uint8_t>(getG(c) * factor),
                    static_cast<uint8_t>(getB(c) * factor),
                    getA(c)
                );
            }
        }
    }
}

// CHROMATIC_ABERRATION - Offset color channels
void effectChromaticAberration(std::vector<Color>& pixels, int w, int h, int offsetX, int offsetY) {
    std::vector<Color> result = pixels;

    for (int y = 0; y < h; y++) {
        for (int x = 0; x < w; x++) {
            // Red channel - offset in one direction
            int rxr = std::max(0, std::min(w - 1, x - offsetX));
            int ryr = std::max(0, std::min(h - 1, y - offsetY));
            uint8_t r = getR(pixels[static_cast<size_t>(ryr * w + rxr)]);

            // Green channel - no offset
            uint8_t g = getG(pixels[static_cast<size_t>(y * w + x)]);

            // Blue channel - offset in opposite direction
            int rxb = std::max(0, std::min(w - 1, x + offsetX));
            int ryb = std::max(0, std::min(h - 1, y + offsetY));
            uint8_t b = getB(pixels[static_cast<size_t>(ryb * w + rxb)]);

            result[static_cast<size_t>(y * w + x)] = makeColor(r, g, b, getA(pixels[static_cast<size_t>(y * w + x)]));
        }
    }

    pixels = std::move(result);
}

// DITHER - Add ordered dithering pattern
void effectDither(std::vector<Color>& pixels, int w, int h, int intensity) {
    // 4x4 Bayer matrix
    static const int bayer[4][4] = {
        {  0,  8,  2, 10 },
        { 12,  4, 14,  6 },
        {  3, 11,  1,  9 },
        { 15,  7, 13,  5 }
    };

    float scale = (static_cast<float>(intensity) / 100.0f) * 32.0f;

    for (int y = 0; y < h; y++) {
        for (int x = 0; x < w; x++) {
            Color c = pixels[static_cast<size_t>(y * w + x)];
            float threshold = static_cast<float>(bayer[y % 4][x % 4] - 8) * scale / 16.0f;

            int r = std::max(0, std::min(255, static_cast<int>(getR(c) + threshold)));
            int g = std::max(0, std::min(255, static_cast<int>(getG(c) + threshold)));
            int b = std::max(0, std::min(255, static_cast<int>(getB(c) + threshold)));

            pixels[static_cast<size_t>(y * w + x)] = makeColor(
                static_cast<uint8_t>(r),
                static_cast<uint8_t>(g),
                static_cast<uint8_t>(b),
                getA(c)
            );
        }
    }
}

// POSTERIZE - Reduce color levels
void effectPosterize(std::vector<Color>& pixels, int w, int h, int levels) {
    if (levels < 2) levels = 2;
    if (levels > 256) levels = 256;

    float step = 255.0f / static_cast<float>(levels - 1);

    for (int y = 0; y < h; y++) {
        for (int x = 0; x < w; x++) {
            Color c = pixels[static_cast<size_t>(y * w + x)];

            int r = static_cast<int>(std::round(getR(c) / step) * step);
            int g = static_cast<int>(std::round(getG(c) / step) * step);
            int b = static_cast<int>(std::round(getB(c) / step) * step);

            pixels[static_cast<size_t>(y * w + x)] = makeColor(
                static_cast<uint8_t>(std::max(0, std::min(255, r))),
                static_cast<uint8_t>(std::max(0, std::min(255, g))),
                static_cast<uint8_t>(std::max(0, std::min(255, b))),
                getA(c)
            );
        }
    }
}

// GLITCH_SHIFT - Random block displacement
void effectGlitchShift(std::vector<Color>& pixels, int w, int h, int blockSize, uint32_t seed) {
    std::mt19937 rng(seed);
    std::uniform_int_distribution<int> shiftDist(-blockSize * 2, blockSize * 2);
    std::uniform_int_distribution<int> probDist(0, 100);

    std::vector<Color> result = pixels;

    for (int by = 0; by < h; by += blockSize) {
        if (probDist(rng) < 30) {  // 30% chance to glitch this row
            int shift = shiftDist(rng);

            for (int y = by; y < std::min(by + blockSize, h); y++) {
                for (int x = 0; x < w; x++) {
                    int srcX = ((x - shift) % w + w) % w;
                    result[static_cast<size_t>(y * w + x)] = pixels[static_cast<size_t>(y * w + srcX)];
                }
            }
        }
    }

    pixels = std::move(result);
}

// ============================================================================
// DCT_CORRUPT - Rosa Menkman style DCT block corruption
// Simulates JPEG compression artifacts by corrupting 8x8 DCT-like blocks
// ============================================================================
void effectDctCorrupt(std::vector<Color>& pixels, int w, int h, int blockSize, int intensity, uint32_t seed) {
    if (blockSize < 2) blockSize = 8;  // Default DCT block size

    std::mt19937 rng(seed);
    std::uniform_int_distribution<int> corruptDist(0, 100);
    std::uniform_int_distribution<int> modeDist(0, 5);
    std::uniform_real_distribution<float> scaleDist(0.5f, 2.0f);

    // 8x8 DCT basis pattern weights (simplified)
    // These simulate the effect of corrupting DCT coefficients
    static const float dctBasis[8][8] = {
        {1.0f, 0.98f, 0.92f, 0.83f, 0.71f, 0.56f, 0.38f, 0.20f},
        {0.98f, 0.92f, 0.83f, 0.71f, 0.56f, 0.38f, 0.20f, 0.0f},
        {0.92f, 0.83f, 0.71f, 0.56f, 0.38f, 0.20f, 0.0f, -0.20f},
        {0.83f, 0.71f, 0.56f, 0.38f, 0.20f, 0.0f, -0.20f, -0.38f},
        {0.71f, 0.56f, 0.38f, 0.20f, 0.0f, -0.20f, -0.38f, -0.56f},
        {0.56f, 0.38f, 0.20f, 0.0f, -0.20f, -0.38f, -0.56f, -0.71f},
        {0.38f, 0.20f, 0.0f, -0.20f, -0.38f, -0.56f, -0.71f, -0.83f},
        {0.20f, 0.0f, -0.20f, -0.38f, -0.56f, -0.71f, -0.83f, -0.92f}
    };

    float corruptProb = static_cast<float>(intensity) / 100.0f;

    for (int by = 0; by < h; by += blockSize) {
        for (int bx = 0; bx < w; bx += blockSize) {
            // Randomly decide whether to corrupt this block
            if (corruptDist(rng) > static_cast<int>(corruptProb * 100)) continue;

            int mode = modeDist(rng);
            float scale = scaleDist(rng);

            // Calculate block average for DC component
            int sumR = 0, sumG = 0, sumB = 0;
            int count = 0;
            for (int y = by; y < std::min(by + blockSize, h); y++) {
                for (int x = bx; x < std::min(bx + blockSize, w); x++) {
                    Color c = pixels[static_cast<size_t>(y * w + x)];
                    sumR += getR(c);
                    sumG += getG(c);
                    sumB += getB(c);
                    count++;
                }
            }
            int avgR = sumR / count;
            int avgG = sumG / count;
            int avgB = sumB / count;

            // Apply corruption based on mode
            for (int y = by; y < std::min(by + blockSize, h); y++) {
                for (int x = bx; x < std::min(bx + blockSize, w); x++) {
                    int lx = (x - bx) % 8;
                    int ly = (y - by) % 8;
                    float basis = dctBasis[ly][lx];

                    Color c = pixels[static_cast<size_t>(y * w + x)];
                    int r = getR(c), g = getG(c), b = getB(c);

                    switch (mode) {
                        case 0: // Kill high frequencies (blur-like)
                            r = static_cast<int>(avgR + (r - avgR) * 0.3f);
                            g = static_cast<int>(avgG + (g - avgG) * 0.3f);
                            b = static_cast<int>(avgB + (b - avgB) * 0.3f);
                            break;
                        case 1: // Amplify high frequencies (sharpen/ring)
                            r = static_cast<int>(r + (r - avgR) * scale * basis);
                            g = static_cast<int>(g + (g - avgG) * scale * basis);
                            b = static_cast<int>(b + (b - avgB) * scale * basis);
                            break;
                        case 2: // Quantize heavily (banding)
                            r = (r / 32) * 32;
                            g = (g / 32) * 32;
                            b = (b / 32) * 32;
                            break;
                        case 3: // Shift block color (green unchanged)
                            r = (r + static_cast<int>(basis * 64 * scale)) % 256;
                            b = (b - static_cast<int>(basis * 64 * scale) + 256) % 256;
                            break;
                        case 4: // Posterize with DCT pattern
                            {
                                int levels = 4 + static_cast<int>(basis * 4);
                                float step = 255.0f / levels;
                                r = static_cast<int>(std::round(r / step) * step);
                                g = static_cast<int>(std::round(g / step) * step);
                                b = static_cast<int>(std::round(b / step) * step);
                            }
                            break;
                        case 5: // Complete block replacement (macroblock error)
                            r = avgR;
                            g = avgG;
                            b = avgB;
                            break;
                    }

                    pixels[static_cast<size_t>(y * w + x)] = makeColor(
                        static_cast<uint8_t>(std::clamp(r, 0, 255)),
                        static_cast<uint8_t>(std::clamp(g, 0, 255)),
                        static_cast<uint8_t>(std::clamp(b, 0, 255)),
                        getA(c)
                    );
                }
            }
        }
    }
}

// ============================================================================
// PIXEL_SORT - Kim Asendorf style pixel sorting
// Sorts pixels within intervals based on brightness, hue, or other criteria
// ============================================================================

namespace {
    // Helper function to get sort value based on mode
    float getSortValue(Color c, PixelSortMode mode) {
        uint8_t r = getR(c), g = getG(c), b = getB(c);

        switch (mode) {
            case PixelSortMode::BRIGHTNESS:
                return 0.299f * r + 0.587f * g + 0.114f * b;
            case PixelSortMode::HUE: {
                float rf = r / 255.0f, gf = g / 255.0f, bf = b / 255.0f;
                float maxC = std::max({rf, gf, bf});
                float minC = std::min({rf, gf, bf});
                float delta = maxC - minC;
                if (delta < 0.001f) return 0.0f;
                float hue;
                if (maxC == rf) hue = std::fmod((gf - bf) / delta, 6.0f);
                else if (maxC == gf) hue = (bf - rf) / delta + 2.0f;
                else hue = (rf - gf) / delta + 4.0f;
                return hue * 60.0f;
            }
            case PixelSortMode::SATURATION: {
                float maxC = std::max({r, g, b}) / 255.0f;
                float minC = std::min({r, g, b}) / 255.0f;
                if (maxC < 0.001f) return 0.0f;
                return (maxC - minC) / maxC * 255.0f;
            }
            case PixelSortMode::RED:
                return static_cast<float>(r);
            case PixelSortMode::GREEN:
                return static_cast<float>(g);
            case PixelSortMode::BLUE:
                return static_cast<float>(b);
            default:
                return 0.299f * r + 0.587f * g + 0.114f * b;
        }
    }

    // Check if pixel should start/end an interval
    bool isIntervalBoundary(Color c, int threshold, bool isStart) {
        float brightness = 0.299f * getR(c) + 0.587f * getG(c) + 0.114f * getB(c);
        if (isStart) {
            return brightness > threshold;  // Start sorting when bright enough
        } else {
            return brightness < threshold;  // Stop sorting when too dark
        }
    }
}

void effectPixelSort(std::vector<Color>& pixels, int w, int h, PixelSortMode mode, int threshold, bool vertical) {
    std::vector<Color> result = pixels;

    if (!vertical) {
        // Horizontal sorting (row by row)
        for (int y = 0; y < h; y++) {
            int x = 0;
            while (x < w) {
                // Find start of sorting interval
                while (x < w && !isIntervalBoundary(pixels[static_cast<size_t>(y * w + x)], threshold, true)) {
                    x++;
                }
                int start = x;

                // Find end of sorting interval
                while (x < w && !isIntervalBoundary(pixels[static_cast<size_t>(y * w + x)], threshold, false)) {
                    x++;
                }
                int end = x;

                // Sort the interval
                if (end > start + 1) {
                    std::vector<std::pair<float, Color>> sortable;
                    sortable.reserve(end - start);

                    for (int i = start; i < end; i++) {
                        Color c = pixels[static_cast<size_t>(y * w + i)];
                        sortable.emplace_back(getSortValue(c, mode), c);
                    }

                    std::sort(sortable.begin(), sortable.end(),
                        [](const auto& a, const auto& b) { return a.first < b.first; });

                    for (int i = start; i < end; i++) {
                        result[static_cast<size_t>(y * w + i)] = sortable[i - start].second;
                    }
                }
            }
        }
    } else {
        // Vertical sorting (column by column)
        for (int x = 0; x < w; x++) {
            int y = 0;
            while (y < h) {
                // Find start of sorting interval
                while (y < h && !isIntervalBoundary(pixels[static_cast<size_t>(y * w + x)], threshold, true)) {
                    y++;
                }
                int start = y;

                // Find end of sorting interval
                while (y < h && !isIntervalBoundary(pixels[static_cast<size_t>(y * w + x)], threshold, false)) {
                    y++;
                }
                int end = y;

                // Sort the interval
                if (end > start + 1) {
                    std::vector<std::pair<float, Color>> sortable;
                    sortable.reserve(end - start);

                    for (int i = start; i < end; i++) {
                        Color c = pixels[static_cast<size_t>(i * w + x)];
                        sortable.emplace_back(getSortValue(c, mode), c);
                    }

                    std::sort(sortable.begin(), sortable.end(),
                        [](const auto& a, const auto& b) { return a.first < b.first; });

                    for (int i = start; i < end; i++) {
                        result[static_cast<size_t>(i * w + x)] = sortable[i - start].second;
                    }
                }
            }
        }
    }

    pixels = std::move(result);
}

// ============================================================================
// PREDICTION_LEAK - Datamoshing-inspired prediction leak effect
// Simulates video codec P-frame errors where motion vectors "leak" across blocks
// ============================================================================
void effectPredictionLeak(std::vector<Color>& pixels, int w, int h, int blockSize, float leakAmount, uint32_t seed) {
    if (blockSize < 2) blockSize = 16;
    leakAmount = std::clamp(leakAmount, 0.0f, 1.0f);

    std::mt19937 rng(seed);
    std::uniform_int_distribution<int> dirDist(0, 3);
    std::uniform_int_distribution<int> leakDist(0, 100);
    std::uniform_int_distribution<int> strengthDist(1, 3);

    std::vector<Color> result = pixels;

    // Process blocks
    for (int by = 0; by < h; by += blockSize) {
        for (int bx = 0; bx < w; bx += blockSize) {
            // Randomly decide to leak this block
            if (leakDist(rng) > static_cast<int>(leakAmount * 100)) continue;

            int direction = dirDist(rng);
            int strength = strengthDist(rng);

            // Calculate source block offset based on "motion vector"
            int srcOffX = 0, srcOffY = 0;
            switch (direction) {
                case 0: srcOffX = -blockSize * strength; break;  // From left
                case 1: srcOffX = blockSize * strength; break;   // From right
                case 2: srcOffY = -blockSize * strength; break;  // From above
                case 3: srcOffY = blockSize * strength; break;   // From below
            }

            // Apply leaked prediction
            for (int y = by; y < std::min(by + blockSize, h); y++) {
                for (int x = bx; x < std::min(bx + blockSize, w); x++) {
                    int srcX = std::clamp(x + srcOffX, 0, w - 1);
                    int srcY = std::clamp(y + srcOffY, 0, h - 1);

                    Color srcC = pixels[static_cast<size_t>(srcY * w + srcX)];
                    Color dstC = pixels[static_cast<size_t>(y * w + x)];

                    // Blend source (leaked) with destination
                    // This simulates incomplete motion compensation
                    float blend = 0.3f + (leakAmount * 0.5f);
                    int r = static_cast<int>(getR(dstC) * (1.0f - blend) + getR(srcC) * blend);
                    int g = static_cast<int>(getG(dstC) * (1.0f - blend) + getG(srcC) * blend);
                    int b = static_cast<int>(getB(dstC) * (1.0f - blend) + getB(srcC) * blend);

                    result[static_cast<size_t>(y * w + x)] = makeColor(
                        static_cast<uint8_t>(std::clamp(r, 0, 255)),
                        static_cast<uint8_t>(std::clamp(g, 0, 255)),
                        static_cast<uint8_t>(std::clamp(b, 0, 255)),
                        getA(dstC)
                    );
                }
            }
        }
    }

    pixels = std::move(result);
}

} // namespace glic
