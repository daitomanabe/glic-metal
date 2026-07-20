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
    uint reserved;
};

struct PresetUniform {
    uint width;
    uint height;
    uint colorSpace;
    uint seed;

    float borderR;
    float borderG;
    float borderB;
    float reserved;

    ChannelUniform channels[3];
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
    float3 currentSpace = toSpace(sourcePixel.rgb, preset.colorSpace);
    float3 reconstructed;

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
        float value = predicted + residual;
        if (config.clampMethod == 1u) value = fract(value + 16.0);
        else value = clamp(value, 0.0, 1.0);
        reconstructed[channel] = value;
    }

    output.write(float4(fromSpace(reconstructed, preset.colorSpace), sourcePixel.a), gid);
}
