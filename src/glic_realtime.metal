#include <metal_stdlib>
using namespace metal;

struct ChannelUniform {
    uint minBlockSize;
    uint maxBlockSize;
    int predictionMethod;
    uint quantizationValue;

    uint waveletType;
    uint transformType;
    uint clampMethod;
    int transformScale;

    float segmentationPrecision;
    float transformCompress;
    float waveletStrength;
    uint encodingMethod;
};

struct PresetUniform {
    uint width;
    uint height;
    uint colorSpace;
    uint seed;

    float borderR;
    float borderG;
    float borderB;
    float effectStrength;

    ChannelUniform channels[3];

    uint effectFamily;
    float effectAmount;
    float effectScale;
    float effectRate;
};

struct FrameUniform {
    uint frameIndex;
    uint reserved0;
    uint reserved1;
    uint reserved2;
};

static uint hash32(uint value) {
    value ^= value >> 16;
    value *= 0x7feb352du;
    value ^= value >> 15;
    value *= 0x846ca68bu;
    value ^= value >> 16;
    return value;
}

static uint pixelHash(int x, int y, int channel, uint frameIndex, uint seed) {
    uint value = seed;
    value ^= uint(x) * 0x9e3779b9u;
    value ^= uint(y) * 0x85ebca6bu;
    value ^= uint(channel) * 0xc2b2ae35u;
    value ^= frameIndex * 0x27d4eb2du;
    return hash32(value);
}

static float3 rgbToHsv(float3 color) {
    float4 K = float4(0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0);
    float4 p = mix(float4(color.bg, K.wz), float4(color.gb, K.xy), step(color.b, color.g));
    float4 q = mix(float4(p.xyw, color.r), float4(color.r, p.yzx), step(p.x, color.r));
    float d = q.x - min(q.w, q.y);
    float e = 1.0e-7;
    return float3(abs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}

static float3 hsvToRgb(float3 color) {
    float3 p = abs(fract(color.xxx + float3(0.0, 2.0 / 3.0, 1.0 / 3.0)) * 6.0 - 3.0);
    return color.z * mix(float3(1.0), clamp(p - 1.0, 0.0, 1.0), color.y);
}

static float3 rgbToXyz(float3 color) {
    return float3(
        dot(color, float3(0.4124564, 0.3575761, 0.1804375)),
        dot(color, float3(0.2126729, 0.7151522, 0.0721750)),
        dot(color, float3(0.0193339, 0.1191920, 0.9503041))
    );
}

static float3 xyzToRgb(float3 color) {
    return float3(
        dot(color, float3(3.2404542, -1.5371385, -0.4985314)),
        dot(color, float3(-0.9692660, 1.8760108, 0.0415560)),
        dot(color, float3(0.0556434, -0.2040259, 1.0572252))
    );
}

static float labForward(float value) {
    return value > 0.008856 ? pow(value, 1.0 / 3.0) : 7.787 * value + 16.0 / 116.0;
}

static float labReverse(float value) {
    float cube = value * value * value;
    return cube > 0.008856 ? cube : (value - 16.0 / 116.0) / 7.787;
}

static float3 rgbToLab(float3 color) {
    float3 xyz = rgbToXyz(color) / float3(0.95047, 1.0, 1.08883);
    float fx = labForward(xyz.x);
    float fy = labForward(xyz.y);
    float fz = labForward(xyz.z);
    float L = (116.0 * fy - 16.0) / 100.0;
    float a = (500.0 * (fx - fy) + 128.0) / 255.0;
    float b = (200.0 * (fy - fz) + 128.0) / 255.0;
    return clamp(float3(L, a, b), 0.0, 1.0);
}

static float3 labToRgb(float3 color) {
    float L = color.x * 100.0;
    float a = color.y * 255.0 - 128.0;
    float b = color.z * 255.0 - 128.0;
    float fy = (L + 16.0) / 116.0;
    float fx = fy + a / 500.0;
    float fz = fy - b / 200.0;
    float3 xyz = float3(labReverse(fx), labReverse(fy), labReverse(fz)) * float3(0.95047, 1.0, 1.08883);
    return clamp(xyzToRgb(xyz), 0.0, 1.0);
}

static float3 toSpace(float3 rgb, uint colorSpace) {
    rgb = clamp(rgb, 0.0, 1.0);
    switch (colorSpace) {
        case 0: { // OHTA
            return clamp(float3((rgb.r + rgb.g + rgb.b) / 3.0,
                                (rgb.r - rgb.b) * 0.5 + 0.5,
                                (2.0 * rgb.g - rgb.r - rgb.b) * 0.25 + 0.5), 0.0, 1.0);
        }
        case 1: return rgb; // RGB
        case 2: return 1.0 - rgb; // CMY
        case 3: return rgbToHsv(rgb); // HSB
        case 4: return clamp(rgbToXyz(rgb), 0.0, 1.0); // XYZ
        case 5: { // YXY
            float3 xyz = rgbToXyz(rgb);
            float sum = max(1.0e-6, xyz.x + xyz.y + xyz.z);
            return clamp(float3(xyz.y, xyz.x / sum, xyz.y / sum), 0.0, 1.0);
        }
        case 6: { // HCL
            float3 hsv = rgbToHsv(rgb);
            return float3(hsv.x, hsv.y * hsv.z, dot(rgb, float3(0.2126, 0.7152, 0.0722)));
        }
        case 7: return rgbToLab(rgb).xzy; // LUV approximation with distinct opponent ordering
        case 8: return rgbToLab(rgb); // LAB
        case 9: { // HWB
            float hue = rgbToHsv(rgb).x;
            return float3(hue, min(rgb.r, min(rgb.g, rgb.b)), 1.0 - max(rgb.r, max(rgb.g, rgb.b)));
        }
        case 10: return clamp(float3((rgb.r - rgb.g) * 0.5 + 0.5, rgb.g, (rgb.b - rgb.g) * 0.5 + 0.5), 0.0, 1.0); // R-GGB-G
        case 11: return clamp(float3(dot(rgb, float3(0.299, 0.587, 0.114)),
                                           dot(rgb, float3(-0.168736, -0.331264, 0.5)) + 0.5,
                                           dot(rgb, float3(0.5, -0.418688, -0.081312)) + 0.5), 0.0, 1.0); // YPbPr
        case 12: return clamp(float3(dot(rgb, float3(0.299, 0.587, 0.114)),
                                           dot(rgb, float3(-0.168736, -0.331264, 0.5)) + 0.5,
                                           dot(rgb, float3(0.5, -0.418688, -0.081312)) + 0.5), 0.0, 1.0); // YCbCr
        case 13: return clamp(float3(dot(rgb, float3(0.299, 0.587, 0.114)),
                                           dot(rgb, float3(-0.450, -0.883, 1.333)) * 0.375 + 0.5,
                                           dot(rgb, float3(-1.333, 1.116, 0.217)) * 0.375 + 0.5), 0.0, 1.0); // YDbDr
        case 14: { float y = dot(rgb, float3(0.299, 0.587, 0.114)); return float3(y); } // GS
        case 15: return clamp(float3(dot(rgb, float3(0.299, 0.587, 0.114)),
                                           dot(rgb, float3(-0.14713, -0.28886, 0.436)) + 0.5,
                                           dot(rgb, float3(0.615, -0.51499, -0.10001)) + 0.5), 0.0, 1.0); // YUV
        default: return rgb;
    }
}

static float3 fromSpace(float3 value, uint colorSpace) {
    value = clamp(value, 0.0, 1.0);
    switch (colorSpace) {
        case 0: { // OHTA
            float i1 = value.x;
            float i2 = value.y - 0.5;
            float i3 = value.z - 0.5;
            float g = i1 + (4.0 / 3.0) * i3;
            float r = i1 + i2 - (2.0 / 3.0) * i3;
            float b = i1 - i2 - (2.0 / 3.0) * i3;
            return clamp(float3(r, g, b), 0.0, 1.0);
        }
        case 1: return value;
        case 2: return 1.0 - value;
        case 3: return hsvToRgb(value);
        case 4: return clamp(xyzToRgb(value), 0.0, 1.0);
        case 5: {
            float Y = value.x;
            float x = value.y;
            float y = max(1.0e-5, value.z);
            float X = x * Y / y;
            float Z = max(0.0, (1.0 - x - y) * Y / y);
            return clamp(xyzToRgb(float3(X, Y, Z)), 0.0, 1.0);
        }
        case 6: return hsvToRgb(float3(value.x, clamp(value.y / max(value.z, 0.05), 0.0, 1.0), value.z));
        case 7: return labToRgb(value.xzy);
        case 8: return labToRgb(value);
        case 9: {
            float3 pure = hsvToRgb(float3(value.x, 1.0, 1.0));
            float scale = max(0.0, 1.0 - value.y - value.z);
            return clamp(pure * scale + value.y, 0.0, 1.0);
        }
        case 10: { float g = value.y; return clamp(float3(g + (value.x - 0.5) * 2.0, g, g + (value.z - 0.5) * 2.0), 0.0, 1.0); }
        case 11:
        case 12: {
            float y = value.x;
            float pb = value.y - 0.5;
            float pr = value.z - 0.5;
            return clamp(float3(y + 1.402 * pr, y - 0.344136 * pb - 0.714136 * pr, y + 1.772 * pb), 0.0, 1.0);
        }
        case 13: {
            float y = value.x;
            float db = (value.y - 0.5) / 0.375;
            float dr = (value.z - 0.5) / 0.375;
            return clamp(float3(y + 0.0000923037 * db - 0.52591263 * dr,
                                y - 0.1291329 * db + 0.26789933 * dr,
                                y + 0.66467906 * db - 0.0000792025 * dr), 0.0, 1.0);
        }
        case 14: return float3(value.x);
        case 15: {
            float y = value.x;
            float u = value.y - 0.5;
            float v = value.z - 0.5;
            return clamp(float3(y + 1.13983 * v, y - 0.39465 * u - 0.58060 * v, y + 2.03211 * u), 0.0, 1.0);
        }
        default: return value;
    }
}

static float3 borderSpace(constant PresetUniform& preset) {
    return toSpace(float3(preset.borderR, preset.borderG, preset.borderB), preset.colorSpace);
}

static float3 spaceAt(texture2d<float, access::read> input, int2 coordinate, constant PresetUniform& preset) {
    if (coordinate.x < 0 || coordinate.y < 0 || coordinate.x >= int(preset.width) || coordinate.y >= int(preset.height)) {
        return borderSpace(preset);
    }
    return toSpace(input.read(uint2(coordinate)).rgb, preset.colorSpace);
}

static float channelAt(texture2d<float, access::read> input, int2 coordinate, int channel, constant PresetUniform& preset) {
    return spaceAt(input, coordinate, preset)[channel];
}

static int wrapCoordinate(int value, int size) {
    int wrapped = value % size;
    return wrapped < 0 ? wrapped + size : wrapped;
}

static float channelAtWrapped(texture2d<float, access::read> input,
                              int2 coordinate,
                              int channel,
                              constant PresetUniform& preset) {
    int2 wrapped = int2(wrapCoordinate(coordinate.x, int(preset.width)),
                        wrapCoordinate(coordinate.y, int(preset.height)));
    return channelAt(input, wrapped, channel, preset);
}

static float effectChannelAtWrapped(texture2d<float, access::read> input,
                                    int2 coordinate,
                                    int channel,
                                    constant PresetUniform& preset) {
    int2 wrapped = int2(wrapCoordinate(coordinate.x, int(preset.width)),
                        wrapCoordinate(coordinate.y, int(preset.height)));
    return input.read(uint2(wrapped)).rgb[channel];
}

constant ushort kBayer4x4[16] = {
    0, 8, 2, 10,
    12, 4, 14, 6,
    3, 11, 1, 9,
    15, 7, 13, 5
};

static float triangleWave(int value, int halfPeriod) {
    halfPeriod = max(1, halfPeriod);
    int period = halfPeriod * 2;
    int position = wrapCoordinate(value, period);
    int ramp = position <= halfPeriod ? position : period - position;
    return float(ramp * 2 - halfPeriod) / float(halfPeriod);
}

static int triangleOffset(int value, int halfPeriod, int amplitude, int divisor) {
    halfPeriod = max(1, halfPeriod);
    divisor = max(1, divisor);
    int period = halfPeriod * 2;
    int position = wrapCoordinate(value, period);
    int ramp = position <= halfPeriod ? position : period - position;
    int numerator = (ramp * 2 - halfPeriod) * amplitude;
    int denominator = halfPeriod * divisor;
    return numerator >= 0 ? (numerator + denominator / 2) / denominator
                          : -((-numerator + denominator / 2) / denominator);
}

static uint heldEffectFrame(constant PresetUniform& preset,
                            constant FrameUniform& frame) {
    float rate = clamp(preset.effectRate, 0.0, 1.0);
    uint holdFrames = 1u + uint(round((1.0 - rate) * 11.0));
    return frame.frameIndex / max(1u, holdFrames);
}

static float realtimeFamilyValue(texture2d<float, access::read> input,
                                 int2 point,
                                 int channel,
                                 float current,
                                 constant PresetUniform& preset,
                                 constant FrameUniform& frame) {
    float amount = clamp(preset.effectAmount, 0.0, 1.0);
    float scale = clamp(preset.effectScale, 0.0, 1.0);
    float mixAmount = clamp(amount * preset.effectStrength, 0.0, 1.0);
    uint heldFrame = heldEffectFrame(preset, frame);
    float affected = current;

    switch (preset.effectFamily) {
        case 1u: { // LINE_TEAR: thin horizontal bands with long horizontal displacement.
            int bandHeight = 1 + int(round(scale * 15.0));
            int band = point.y / bandHeight;
            uint bandHash = pixelHash(0, band, 0, heldFrame, preset.seed);
            float density = 0.10 + amount * 0.65;
            if (float(bandHash & 0xffffu) < density * 65535.0) {
                int maximum = min(320, max(4, int(preset.width) / 3));
                int maximumShift = 4 + int(round(amount * float(maximum - 4)));
                int shift = 1 + int((bandHash >> 16u) % uint(max(1, maximumShift)));
                if ((bandHash & 0x80000000u) != 0u) shift = -shift;
                affected = effectChannelAtWrapped(input, point + int2(shift, 0), channel, preset);
            }
            break;
        }
        case 2u: { // CHANNEL_SHEAR: independently separate RGB/opponent channels.
            int halfPeriod = 8 + int(round(scale * 120.0));
            int phase = int(heldFrame % uint(max(1, halfPeriod * 2)));
            float wave = triangleWave(point.y + phase, halfPeriod);
            int maximumOffset = 2 + int(round(amount * 96.0));
            int channelDirection = channel - 1;
            int offset = channelDirection * maximumOffset +
                         int(round(float(channelDirection * maximumOffset) * wave * 0.5));
            affected = effectChannelAtWrapped(input, point + int2(offset, 0), channel, preset);
            break;
        }
        case 3u: { // ANALOG_SYNC: shared raster wobble, roll, jitter and scanline loss.
            int halfPeriod = 6 + int(round(scale * 72.0));
            int speed = 1 + int(round(preset.effectRate * 3.0));
            int phase = int(heldFrame) * speed;
            float wave = triangleWave(point.y + phase, halfPeriod);
            int amplitude = 1 + int(round(amount * 32.0));
            int wobble = int(round(wave * float(amplitude)));
            int lineGroup = point.y / max(1, 1 + int(round(scale * 5.0)));
            uint lineHash = pixelHash(0, lineGroup, 0, heldFrame / 2u, preset.seed);
            if (float(lineHash & 0xffu) < amount * 90.0) {
                int jitter = 1 + int((lineHash >> 8u) % uint(max(1, amplitude * 2)));
                wobble += (lineHash & 0x10000u) == 0u ? -jitter : jitter;
            }
            int rollSpeed = 1 + int(round(preset.effectRate * 4.0));
            int roll = int((heldFrame * uint(rollSpeed)) % max(1u, preset.height));
            int chromaOffset = (channel - 1) * max(1, int(round(amount * 3.0)));
            affected = effectChannelAtWrapped(
                input, point + int2(wobble + chromaOffset, roll), channel, preset);
            if (((point.y + phase) & 1) != 0)
                affected *= 1.0 - amount * 0.25;
            break;
        }
        case 4u: { // MIRROR_FOLD: wide mirrored ribbons rather than square blocks.
            int halfPeriod = min(12 + int(round(scale * 148.0)),
                                 max(2, int(preset.width) / 2));
            int period = halfPeriod * 2;
            int phase = int(heldFrame % uint(max(1, period)));
            int shiftedX = point.x + phase;
            int cell = shiftedX >= 0 ? shiftedX / period
                                     : -((-shiftedX + period - 1) / period);
            int local = wrapCoordinate(shiftedX, period);
            int folded = local <= halfPeriod ? local : period - 1 - local;
            int sampleX = cell * period + folded - phase;
            affected = effectChannelAtWrapped(input, int2(sampleX, point.y), channel, preset);
            break;
        }
        case 5u: { // EDGE_ECHO: displace only where source gradients are present.
            float left = effectChannelAtWrapped(input, point + int2(-1, 0), channel, preset);
            float right = effectChannelAtWrapped(input, point + int2(1, 0), channel, preset);
            float top = effectChannelAtWrapped(input, point + int2(0, -1), channel, preset);
            float bottom = effectChannelAtWrapped(input, point + int2(0, 1), channel, preset);
            float edge = (abs(right - left) + abs(bottom - top)) * 0.5;
            int distance = 2 + int(round(scale * 46.0));
            int direction = (heldFrame & 1u) == 0u ? -1 : 1;
            float echo = effectChannelAtWrapped(
                input, point + int2(direction * distance, distance / 2), channel, preset);
            float displacementEdge = abs(echo - current);
            float threshold = mix(0.12, 0.012, scale);
            float edgeMix = clamp(max((edge - threshold) * 10.0,
                                      (displacementEdge - threshold * 0.55) * 4.5),
                                  0.0, 1.0);
            affected = mix(current, echo, edgeMix);
            break;
        }
        case 6u: { // BITPLANE_DITHER: ordered bit-plane damage without resampling.
            int grainPower = clamp(int(round(scale * 2.0)), 0, 2);
            int grain = 1 << grainPower;
            int matrixX = (point.x / grain) & 3;
            int matrixY = (point.y / grain) & 3;
            uint threshold = uint(kBayer4x4[matrixY * 4 + matrixX]);
            uint coverage = uint(clamp(int(round(amount * 16.0)), 0, 16));
            if (threshold < coverage) {
                int baseBit = clamp(1 + int(floor(amount * 6.0)), 1, 6);
                int bit = (baseBit + channel + int(heldFrame & 1u)) % 7;
                uint byteValue = uint(clamp(int(round(current * 255.0)), 0, 255));
                affected = float(byteValue ^ (1u << uint(bit))) / 255.0;
            }
            break;
        }
        case 7u: { // WAVE_WARP: continuous two-axis displacement.
            int halfPeriod = 12 + int(round(scale * 120.0));
            int speed = 1 + int(round(preset.effectRate * 3.0));
            int phase = int(heldFrame) * speed;
            int amplitude = 1 + int(round(amount * 48.0));
            // Integer ratio rounding keeps source coordinates bit-exact with
            // the CPU implementation at half-integer boundaries.
            int offsetX = triangleOffset(point.y + phase, halfPeriod, amplitude, 1);
            int offsetY = triangleOffset(point.x - phase, halfPeriod, amplitude, 2);
            affected = effectChannelAtWrapped(
                input, point + int2(offsetX, offsetY), channel, preset);
            break;
        }
        case 8u: { // POSTER_SOLAR: palette reduction plus animated solarization.
            int levels = 2 + int(round(scale * 14.0));
            float stepSize = 1.0 / float(max(1, levels - 1));
            float quantized = round(current / stepSize) * stepSize;
            float drift = triangleWave(int(heldFrame), 32) * 0.15;
            float threshold = clamp(0.25 + scale * 0.50 + drift, 0.10, 0.90);
            affected = quantized > threshold ? 1.0 - quantized : quantized;
            break;
        }
        default:
            break;
    }
    return clamp(mix(current, affected, mixAmount), 0.0, 1.0);
}

static float predictorValue(texture2d<float, access::read> input,
                            int requested,
                            int channel,
                            int2 point,
                            int2 origin,
                            int blockSize,
                            float current,
                            constant PresetUniform& preset,
                            constant FrameUniform& frame) {
    int2 local = point - origin;
    float left = channelAt(input, int2(origin.x - 1, point.y), channel, preset);
    float top = channelAt(input, int2(point.x, origin.y - 1), channel, preset);
    float corner = channelAt(input, origin - 1, channel, preset);
    float top2 = channelAt(input, int2(point.x, origin.y - 2), channel, preset);
    float left2 = channelAt(input, int2(origin.x - 2, point.y), channel, preset);

    int method = requested;
    if (method == -3) method = int(pixelHash(origin.x, origin.y, channel, frame.frameIndex, preset.seed) % 16u);

    switch (method) {
        case 0: return 0.0;
        case 1: return corner;
        case 2: return left;
        case 3: return top;
        case 4: return (left + top + corner) / 3.0;
        case 5:
        case 6: return max(min(left, top), min(max(left, top), corner));
        case 7: return (left + top) * 0.5;
        case 8: return clamp(left + top - corner, 0.0, 1.0);
        case 9: {
            float candidate = left + top - corner;
            float3 distance = abs(candidate - float3(left, top, corner));
            return distance.x <= distance.y && distance.x <= distance.z ? left : (distance.y <= distance.z ? top : corner);
        }
        case 10: {
            int sum = local.x + local.y;
            float topSample = channelAt(input, int2(origin.x + min(sum + 1, blockSize - 1), origin.y - 1), channel, preset);
            float leftSample = channelAt(input, int2(origin.x - 1, origin.y + min(sum, blockSize - 1)), channel, preset);
            return ((local.x + 1) * topSample + (local.y + 1) * leftSample) / float(max(1, local.x + local.y + 2));
        }
        case 11: return local.x > local.y ? top : (local.y > local.x ? left : (left + top) * 0.5);
        case 12:
            if (corner >= max(left, top)) return min(left, top);
            if (corner <= min(left, top)) return max(left, top);
            return left + top - corner;
        case 13: return clamp((left2 + left2 - left + top2 + top2 - top) * 0.5, 0.0, 1.0);
        case 14: {
            uint hash = pixelHash(origin.x, origin.y, channel, frame.frameIndex / 4u, preset.seed);
            int blocksBack = 1 + int(hash % 4u);
            int2 ref = int2(origin.x - blocksBack * blockSize + local.x,
                            origin.y - (((hash >> 3u) & 1u) != 0u ? blockSize : 0) + local.y);
            return channelAt(input, ref, channel, preset);
        }
        case 15: {
            uint hash = pixelHash(origin.x, origin.y, channel, frame.frameIndex / 6u, preset.seed);
            int slope = 1 + int(hash % uint(max(1, blockSize)));
            if ((hash & 1u) == 0u) {
                return channelAt(input, int2(origin.x + (local.x + local.y * slope) % blockSize, origin.y - 1), channel, preset);
            }
            return channelAt(input, int2(origin.x - 1, origin.y + (local.y + local.x * slope) % blockSize), channel, preset);
        }
        case -1:
        case -2: {
            float candidates[5] = {left, top, corner, (left + top) * 0.5,
                                   predictorValue(input, 9, channel, point, origin, blockSize, current, preset, frame)};
            float best = candidates[0];
            float bestDistance = abs(current - best);
            for (int i = 1; i < 5; ++i) {
                float distance = abs(current - candidates[i]);
                bool replace = method == -1 ? distance < bestDistance : distance > bestDistance;
                if (replace) { best = candidates[i]; bestDistance = distance; }
            }
            return best;
        }
        case 16: {
            float2 delta = float2(local - blockSize / 2);
            float angle = atan2(delta.y, delta.x) + float(frame.frameIndex % 360u) * 0.01;
            int offset = int((angle + M_PI_F) * blockSize / (2.0 * M_PI_F));
            offset = ((offset % blockSize) + blockSize) % blockSize;
            return local.x + local.y < blockSize
                ? channelAt(input, int2(origin.x + offset, origin.y - 1), channel, preset)
                : channelAt(input, int2(origin.x - 1, origin.y + offset), channel, preset);
        }
        case 17: {
            float noise = (float(pixelHash(point.x, point.y, channel, frame.frameIndex, preset.seed) & 63u) - 32.0) / 255.0;
            return clamp(corner + noise, 0.0, 1.0);
        }
        case 18: {
            float topRight = channelAt(input, int2(origin.x + blockSize - 1, origin.y - 1), channel, preset);
            float bottomLeft = channelAt(input, int2(origin.x - 1, origin.y + blockSize - 1), channel, preset);
            float fx = blockSize > 1 ? float(local.x) / float(blockSize - 1) : 0.0;
            float fy = blockSize > 1 ? float(local.y) / float(blockSize - 1) : 0.0;
            return clamp(((corner + (topRight - corner) * fx) + (corner + (bottomLeft - corner) * fy)) * 0.5, 0.0, 1.0);
        }
        case 19: return channelAt(input, int2(origin.x - 1, origin.y + blockSize - 1 - local.y), channel, preset);
        case 20: {
            float phase = float(frame.frameIndex) * 0.08;
            float wave = sin((float(local.x) + phase) * 2.0 * M_PI_F / blockSize) +
                         sin((float(local.y) + phase) * 2.0 * M_PI_F / blockSize);
            return clamp(corner + wave * (32.0 / 255.0), 0.0, 1.0);
        }
        case 21: return (((local.x / 4 + local.y / 4 + int(frame.frameIndex / 8u)) & 1) != 0) ? left : top;
        case 22: {
            float2 delta = float2(local - blockSize / 2);
            float distance = length(delta) / max(1.0, float(blockSize) * 0.7071);
            return clamp(mix(corner, (left + top) * 0.5, distance), 0.0, 1.0);
        }
        case 23: {
            float edge = current * 5.0 - channelAt(input, point + int2(-1, 0), channel, preset) -
                         channelAt(input, point + int2(1, 0), channel, preset) -
                         channelAt(input, point + int2(0, -1), channel, preset) -
                         channelAt(input, point + int2(0, 1), channel, preset);
            return clamp(edge, 0.0, 1.0);
        }
        default: return (left + top) * 0.5;
    }
}

kernel void glicRealtime(texture2d<float, access::read> input [[texture(0)]],
                         texture2d<float, access::write> output [[texture(1)]],
                         constant PresetUniform& preset [[buffer(0)]],
                         constant FrameUniform& frame [[buffer(1)]],
                         uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= preset.width || gid.y >= preset.height) return;

    int2 point = int2(gid);
    float4 sourcePixel = input.read(gid);
    if (preset.effectStrength <= 0.0) {
        output.write(sourcePixel, gid);
        return;
    }
    float3 currentSpace = preset.effectFamily == 0u
                              ? toSpace(sourcePixel.rgb, preset.colorSpace)
                              : sourcePixel.rgb;
    float3 reconstructed;

    if (preset.effectFamily != 0u) {
        for (int channel = 0; channel < 3; ++channel) {
            reconstructed[channel] = realtimeFamilyValue(
                input, point, channel, currentSpace[channel], preset, frame);
        }
        output.write(float4(reconstructed, sourcePixel.a), gid);
        return;
    }

    for (int channel = 0; channel < 3; ++channel) {
        ChannelUniform config = preset.channels[channel];
        float current = currentSpace[channel];
        float leftPixel = channelAt(input, point + int2(-1, 0), channel, preset);
        float topPixel = channelAt(input, point + int2(0, -1), channel, preset);
        float edge = (abs(current - leftPixel) + abs(current - topPixel)) * 127.5;
        int blockSize = int(edge > config.segmentationPrecision ? config.minBlockSize : config.maxBlockSize);
        blockSize = clamp(blockSize, 1, 256);
        int2 origin = (point / blockSize) * blockSize;

        float predicted = predictorValue(input, config.predictionMethod, channel, point, origin,
                                         blockSize, current, preset, frame);
        float residual = current - predicted;

        if (config.waveletType != 0u) {
            float neighborAverage = 0.25 * (
                channelAt(input, point + int2(-1, 0), channel, preset) +
                channelAt(input, point + int2(1, 0), channel, preset) +
                channelAt(input, point + int2(0, -1), channel, preset) +
                channelAt(input, point + int2(0, 1), channel, preset));
            float transformGain = config.waveletStrength * clamp(abs(float(config.transformScale)) / 20.0, 0.25, 4.0);
            if (config.transformType == 1u) transformGain *= 1.2;
            residual += (current - neighborAverage) * transformGain;
            float threshold = (50.0 / 255.0) * pow(config.transformCompress / 255.0, 2.0);
            if (abs(residual) < threshold) residual = 0.0;
        }

        float quantizer = max(1.0, float(config.quantizationValue) * 0.5) / 255.0;
        if (quantizer > (1.0 / 255.0)) residual = round(residual / quantizer) * quantizer;

        float quantizationDrive = clamp(float(config.quantizationValue) / 255.0, 0.0, 1.0);
        float compressionDrive = clamp(config.transformCompress / 255.0, 0.0, 1.0);
        float presetDrive = 0.55 + quantizationDrive * 0.25 + compressionDrive * 0.15 +
                            (config.predictionMethod == 0 ? 0.0 : 0.15) +
                            (config.waveletType == 0u ? 0.0 : 0.10);
        float drive = clamp(preset.effectStrength * presetDrive, 0.0, 1.35);
        float density = preset.effectStrength <= 0.0
                            ? 0.0
                            : clamp(0.30 + 0.42 * min(drive, 1.0) +
                                        0.10 * quantizationDrive,
                                    0.25, 0.90);
        float residualKeep = clamp(0.58 - drive * 0.40 - quantizationDrive * 0.18,
                                   0.04, 0.58);
        float corruptionMix = clamp(0.55 + 0.35 * min(drive, 1.0), 0.55, 0.90);
        int predictionCode = abs(config.predictionMethod);
        uint holdFrames = 3u + uint((predictionCode + int(config.encodingMethod)) % 6);
        uint heldFrame = frame.frameIndex / holdFrames;

        ChannelUniform anchorConfig = preset.channels[0];
        int effectBlock = clamp(max(int(anchorConfig.minBlockSize) * 8,
                                    min(int(anchorConfig.maxBlockSize) * 2, 64)),
                                16, 64);
        int2 effectOrigin = (point / effectBlock) * effectBlock;
        uint blockHash = pixelHash(effectOrigin.x, effectOrigin.y, 0, heldFrame, preset.seed);
        bool affected = float(blockHash & 0xffffu) < density * 65535.0;

        float value = current;
        if (affected) {
            int direction = (blockHash & 0x10000u) == 0u ? -1 : 1;
            int distance = effectBlock * (1 + int((blockHash >> 17u) % 3u));
            int channelShift = (channel - 1) *
                (1 + int((blockHash >> 21u) % uint(max(2, effectBlock / 4))));
            int2 samplePoint = point + int2(direction * distance + channelShift,
                                             (int((blockHash >> 25u) % 3u) - 1) * effectBlock);
            int mode = (int(config.encodingMethod) + predictionCode) % 6;
            if (mode == 2) {
                samplePoint.x = effectOrigin.x +
                    int((blockHash >> 9u) % uint(max(1, effectBlock / 4))) + channelShift;
            } else if (mode == 5 && ((effectOrigin.y / effectBlock) & 1) != 0) {
                samplePoint = point + int2(-direction * distance + channelShift,
                    -(int((blockHash >> 25u) % 3u) - 1) * effectBlock);
            }

            float displaced = channelAtWrapped(input, samplePoint, channel, preset);
            float broken = predicted + residual * residualKeep;
            float corrupted = displaced;
            switch (mode) {
                case 0: corrupted = broken * 0.55 + displaced * 0.45; break;
                case 1: corrupted = displaced; break;
                case 2: corrupted = displaced; break;
                case 3: corrupted = current + (displaced - predicted) * (0.70 + drive * 0.22); break;
                case 4: {
                    int currentByte = clamp(int(round(current * 255.0)), 0, 255);
                    int displacedByte = clamp(int(round(displaced * 255.0)), 0, 255);
                    corrupted = float(currentByte ^ displacedByte) / 255.0;
                    break;
                }
                case 5: corrupted = displaced * 0.80 + broken * 0.20; break;
                default: break;
            }
            float colorSpaceDamageScale = preset.colorSpace <= 2u ? 1.0 : 0.22;
            float bitPlaneDamage = colorSpaceDamageScale * ((12.0 + 24.0 * drive) / 255.0) *
                (0.65 + 0.35 * float((blockHash >> 8u) & 0xffu) / 255.0);
            bool positiveDamage =
                ((blockHash >> uint(3 + channel * 5)) & 1u) != 0u;
            corrupted += positiveDamage ? bitPlaneDamage : -bitPlaneDamage;
            value = mix(current, corrupted, corruptionMix);
        }
        if (config.clampMethod == 1u) value = fract(value + 16.0);
        else value = clamp(value, 0.0, 1.0);
        reconstructed[channel] = value;
    }

    output.write(float4(fromSpace(reconstructed, preset.colorSpace), sourcePixel.a), gid);
}
