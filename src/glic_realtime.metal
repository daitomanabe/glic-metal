#include <metal_stdlib>
using namespace metal;

kernel void glicCodecBgraToNv12(
    texture2d<float, access::read> input [[texture(0)]],
    texture2d<float, access::write> outputY [[texture(1)]],
    texture2d<float, access::write> outputCbCr [[texture(2)]],
    uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= outputY.get_width() || gid.y >= outputY.get_height())
        return;

    float3 rgb = input.read(gid).rgb;
    float y = 16.0 / 255.0 +
              (219.0 / 255.0) *
                  dot(rgb, float3(0.2126, 0.7152, 0.0722));
    outputY.write(float4(clamp(y, 0.0, 1.0)), gid);

    if ((gid.x & 1u) != 0u || (gid.y & 1u) != 0u)
        return;
    uint2 maximum = uint2(input.get_width() - 1u, input.get_height() - 1u);
    float3 average = float3(0.0);
    average += input.read(min(gid, maximum)).rgb;
    average += input.read(min(gid + uint2(1u, 0u), maximum)).rgb;
    average += input.read(min(gid + uint2(0u, 1u), maximum)).rgb;
    average += input.read(min(gid + uint2(1u, 1u), maximum)).rgb;
    average *= 0.25;
    float cb = 128.0 / 255.0 +
               (224.0 / 255.0) *
                   dot(average, float3(-0.114572, -0.385428, 0.5));
    float cr = 128.0 / 255.0 +
               (224.0 / 255.0) *
                   dot(average, float3(0.5, -0.454153, -0.045847));
    outputCbCr.write(float4(clamp(float2(cb, cr), 0.0, 1.0), 0.0, 1.0),
                     gid / 2u);
}

kernel void glicCodecNv12ToBgra(
    texture2d<float, access::read> inputY [[texture(0)]],
    texture2d<float, access::read> inputCbCr [[texture(1)]],
    texture2d<float, access::write> output [[texture(2)]],
    uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= output.get_width() || gid.y >= output.get_height())
        return;
    float y = (inputY.read(gid).r - 16.0 / 255.0) * (255.0 / 219.0);
    float2 chroma =
        (inputCbCr.read(gid / 2u).rg - float2(128.0 / 255.0)) *
        (255.0 / 224.0);
    float3 rgb = float3(y + 1.5748 * chroma.y,
                        y - 0.187324 * chroma.x - 0.468124 * chroma.y,
                        y + 1.8556 * chroma.x);
    output.write(float4(clamp(rgb, 0.0, 1.0), 1.0), gid);
}

struct CodecEffectUniform {
    uint outputWidth;
    uint outputHeight;
    uint inputWidth;
    uint inputHeight;
    uint effect;
    uint frameIndex;
    uint hasNearHistory;
    uint hasFarHistory;
    float amount;
    float rate;
    float feedback;
    float reducedResolutionScale;
    ulong seed;
    uint reserved0;
    uint reserved1;
};

static uint codecHash32(uint value) {
    value ^= value >> 16u;
    value *= 0x7feb352du;
    value ^= value >> 15u;
    value *= 0x846ca68bu;
    return value ^ (value >> 16u);
}

static uint codecPixelHash(int2 point, uint frame, ulong seed) {
    uint value = uint(seed) ^ uint(seed >> 32u);
    value ^= uint(point.x) * 0x9e3779b9u;
    value ^= uint(point.y) * 0x85ebca6bu;
    value ^= frame * 0xc2b2ae35u;
    return codecHash32(value);
}

static int codecWrap(int value, int size) {
    int wrapped = value % max(1, size);
    return wrapped < 0 ? wrapped + size : wrapped;
}

static int2 codecWrappedPoint(int2 point, constant CodecEffectUniform& uniform) {
    return int2(codecWrap(point.x, int(uniform.outputWidth)),
                codecWrap(point.y, int(uniform.outputHeight)));
}

static float3 codecYcbcrToRgb(float y, float2 chroma) {
    y = (y - 16.0 / 255.0) * (255.0 / 219.0);
    chroma = (chroma - float2(128.0 / 255.0)) * (255.0 / 224.0);
    return clamp(float3(y + 1.5748 * chroma.y,
                        y - 0.187324 * chroma.x - 0.468124 * chroma.y,
                        y + 1.8556 * chroma.x), 0.0, 1.0);
}

static float3 codecRgbToYcbcr(float3 rgb) {
    return float3(dot(rgb, float3(0.2126, 0.7152, 0.0722)),
                  dot(rgb, float3(-0.114572, -0.385428, 0.5)) + 0.5,
                  dot(rgb, float3(0.5, -0.454153, -0.045847)) + 0.5);
}

static float3 codecCurrentAt(
    texture2d<float, access::read> currentY,
    texture2d<float, access::read> currentCbCr,
    int2 outputPoint,
    constant CodecEffectUniform& uniform) {
    int2 wrapped = codecWrappedPoint(outputPoint, uniform);
    uint2 source = uint2(
        min(uniform.inputWidth - 1u,
            uint((ulong(wrapped.x) * uniform.inputWidth) /
                 max(1u, uniform.outputWidth))),
        min(uniform.inputHeight - 1u,
            uint((ulong(wrapped.y) * uniform.inputHeight) /
                 max(1u, uniform.outputHeight))));
    return codecYcbcrToRgb(currentY.read(source).r,
                           currentCbCr.read(source / 2u).rg);
}

static float3 codecHistoryAt(
    texture2d<float, access::read> history,
    texture2d<float, access::read> currentY,
    texture2d<float, access::read> currentCbCr,
    int2 point,
    bool available,
    constant CodecEffectUniform& uniform) {
    if (!available)
        return codecCurrentAt(currentY, currentCbCr, point, uniform);
    int2 wrapped = codecWrappedPoint(point, uniform);
    return history.read(uint2(wrapped)).rgb;
}

kernel void glicCodecFusedEffect(
    texture2d<float, access::read> currentY [[texture(0)]],
    texture2d<float, access::read> currentCbCr [[texture(1)]],
    texture2d<float, access::read> nearHistory [[texture(2)]],
    texture2d<float, access::read> farHistory [[texture(3)]],
    texture2d<float, access::write> output [[texture(4)]],
    constant CodecEffectUniform& uniform [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= uniform.outputWidth || gid.y >= uniform.outputHeight)
        return;

    int2 point = int2(gid);
    bool hasNear = uniform.hasNearHistory != 0u;
    bool hasFar = uniform.hasFarHistory != 0u;
    float amount = clamp(uniform.amount, 0.0, 1.0);
    float feedback = clamp(uniform.feedback, 0.0, 0.98);
    float phase = float(uniform.frameIndex) * (0.025 + uniform.rate * 0.19);
    uint hash = codecPixelHash(point, uniform.frameIndex, uniform.seed);
    float3 current = codecCurrentAt(currentY, currentCbCr, point, uniform);
    float3 nearColor = codecHistoryAt(nearHistory, currentY, currentCbCr,
                                      point, hasNear, uniform);
    float3 farColor = codecHistoryAt(farHistory, currentY, currentCbCr,
                                     point, hasFar, uniform);
    float3 result = current;

    switch (uniform.effect) {
        case 0u: // QP pump: VideoToolbox has already applied QP variation.
        case 5u:
            break;
        case 1u:
        case 9u: { // Bitrate crush / generation cascade.
            int block = 3 + int(round(amount * (uniform.effect == 9u ? 42.0 : 24.0)));
            int2 origin = (point / block) * block;
            float3 sampled = codecCurrentAt(currentY, currentCbCr, origin, uniform);
            float levels = uniform.effect == 9u ? 5.0 : 9.0;
            levels = max(2.0, levels - amount * 4.0);
            result = round(sampled * levels) / levels;
            break;
        }
        case 2u: { // Slice dropout.
            int bandHeight = 2 + int(round((1.0 - uniform.rate) * 20.0));
            uint bandHash = codecPixelHash(int2(0, point.y / bandHeight),
                                           uniform.frameIndex / 2u, uniform.seed);
            if (float(bandHash & 0xffffu) / 65535.0 < amount * 0.72)
                result = (bandHash & 1u) != 0u
                             ? codecHistoryAt(nearHistory, currentY, currentCbCr,
                                              point + int2(int(bandHash % 81u) - 40, 0),
                                              hasNear, uniform)
                             : current * (0.08 + feedback * 0.35);
            break;
        }
        case 3u: { // Slice transplant.
            int bandHeight = 3 + int(round((1.0 - uniform.rate) * 26.0));
            uint bandHash = codecPixelHash(int2(0, point.y / bandHeight),
                                           uniform.frameIndex / 3u, uniform.seed);
            if (float(bandHash & 0xffffu) / 65535.0 < 0.12 + amount * 0.78) {
                int shift = int((bandHash >> 16u) % 241u) - 120;
                result = codecHistoryAt(farHistory, currentY, currentCbCr,
                                        point + int2(shift, 0), hasFar, uniform);
            }
            break;
        }
        case 4u: { // Safe P-frame loss hold for codecs with fragile refs.
            uint frameHash = codecHash32(
                uint(uniform.seed) ^ uint(uniform.seed >> 32u) ^
                uniform.frameIndex * 0x9e3779b9u);
            float gate = float(frameHash & 0xffffu) / 65535.0;
            if (gate < amount * (0.15 + 0.55 * uniform.rate))
                result = nearColor;
            break;
        }
        case 6u: { // Payload XOR reconstruction.
            int block = 4 + int(round((1.0 - uniform.rate) * 28.0));
            uint blockHash = codecPixelHash(point / block,
                                            uniform.frameIndex / 2u, uniform.seed);
            if (float(blockHash & 0xffffu) / 65535.0 < amount * 0.88) {
                uint3 bytes = uint3(clamp(current * 255.0, 0.0, 255.0));
                uint mask = 1u << ((blockHash >> 18u) % 7u);
                bytes ^= uint3(mask, mask << 1u, mask << 2u);
                result = float3(bytes & 255u) / 255.0;
                if ((blockHash & 1u) != 0u)
                    result = result.brg;
            }
            break;
        }
        case 7u: { // Reference timewarp.
            float gate = float(hash & 0xffffu) / 65535.0;
            if (gate < 0.16 + amount * (0.54 + 0.24 * uniform.rate))
                result = mix(current, farColor, 0.35 + feedback * 0.65);
            break;
        }
        case 8u: // Codec feedback is also fed into the encoder.
            result = mix(current, nearColor, amount * feedback * 0.48);
            break;
        case 10u: { // Resolution hop, with explicit nearest-neighbor blocks.
            int block = 2 + int(round(amount * 13.0));
            result = codecCurrentAt(currentY, currentCbCr,
                                    (point / block) * block, uniform);
            break;
        }
        case 11u: { // Chroma codec echo.
            float3 currentYC = codecRgbToYcbcr(current);
            float3 historyYC = codecRgbToYcbcr(codecHistoryAt(
                nearHistory, currentY, currentCbCr,
                point + int2(int(sin(phase) * amount * 23.0), 0),
                hasNear, uniform));
            result = codecYcbcrToRgb(
                16.0 / 255.0 + currentYC.x * (219.0 / 255.0),
                float2(128.0 / 255.0) +
                    (mix(currentYC.yz, historyYC.yz, amount * feedback) - 0.5) *
                        (224.0 / 255.0));
            break;
        }
        case 12u: // Temporal polyphony.
            result = float3(current.r, nearColor.g, farColor.b);
            result = mix(current, result, 0.25 + amount * 0.72);
            break;
        case 13u: { // Intra cannibalism.
            int block = 12 + int(round((1.0 - uniform.rate) * 52.0));
            int2 origin = (point / block) * block;
            int2 local = point - origin;
            int2 source = origin + int2((local.y + int(hash & 15u)) % block,
                                         (local.x + int((hash >> 4u) & 15u)) % block);
            result = mix(current,
                         codecCurrentAt(currentY, currentCbCr, source, uniform),
                         amount);
            break;
        }
        case 14u: { // Residual rift.
            int shift = 2 + int(round(amount * 34.0));
            float3 displaced = codecHistoryAt(
                nearHistory, currentY, currentCbCr,
                point + int2((point.y & 1) != 0 ? shift : -shift, 0),
                hasNear, uniform);
            result = clamp(current + (current - displaced) *
                                         (0.45 + amount * 1.65), 0.0, 1.0);
            break;
        }
        case 15u: { // Codec grain synth.
            int block = 2 + int(round((1.0 - uniform.rate) * 9.0));
            uint grainHash = codecPixelHash(point / block,
                                            uniform.frameIndex, uniform.seed);
            float grain = (float(grainHash & 255u) / 255.0 - 0.5) *
                          amount * 0.42;
            result = clamp(current + float3(grain, -grain * 0.45, grain * 0.72),
                           0.0, 1.0);
            break;
        }
        case 16u: { // Recursive codec skin.
            int radius = 1 + int(round(amount * 12.0));
            float3 echo = codecHistoryAt(
                nearHistory, currentY, currentCbCr,
                point + int2(int(sin(phase + point.y * 0.013) * radius),
                             int(cos(phase + point.x * 0.009) * radius)),
                hasNear, uniform);
            result = mix(current, echo, 0.18 + feedback * amount * 0.72);
            break;
        }
        case 17u: { // Concealment choreography.
            int tile = 18 + int(round((1.0 - uniform.rate) * 72.0));
            uint tileHash = codecPixelHash(point / tile,
                                           uniform.frameIndex / 3u, uniform.seed);
            float gate = float(tileHash & 0xffffu) / 65535.0;
            if (gate < amount * 0.82)
                result = (tileHash & 1u) != 0u ? nearColor : farColor;
            break;
        }
        case 18u: { // Dual codec crossbreed.
            bool alternate = ((point.x / 48 + point.y / 48) & 1) != 0;
            result = mix(current, alternate ? nearColor : farColor,
                         amount * (0.35 + feedback * 0.55));
            break;
        }
        case 19u: { // Codec ping-pong.
            bool useFar = ((point.y / max(2, 4 + int((1.0 - uniform.rate) * 28.0)) +
                            int(uniform.frameIndex / 2u)) & 1) != 0;
            result = mix(current, useFar ? farColor : nearColor, amount);
            break;
        }
        case 20u: { // GOP accordion.
            int span = 10 + int(round((1.0 - uniform.rate) * 70.0));
            int foldedX = abs(codecWrap(point.x + int(phase * 24.0), span * 2) - span);
            result = mix(current,
                         codecHistoryAt(farHistory, currentY, currentCbCr,
                                        int2((point.x / span) * span + foldedX,
                                             point.y), hasFar, uniform),
                         amount);
            break;
        }
        case 21u: { // B-frame braid.
            int braid = codecWrap(point.y + int(uniform.frameIndex), 6);
            result = braid < 2 ? nearColor : (braid < 4 ? current : farColor);
            break;
        }
        case 22u: // Plane split codec.
            result = mix(current, float3(current.r, nearColor.g, farColor.b),
                         amount);
            break;
        case 23u: { // ROI quality islands.
            float2 center = float2(uniform.outputWidth, uniform.outputHeight) * 0.5;
            float distance = length((float2(point) - center) / center);
            int block = 5 + int(round(amount * 34.0));
            float3 coarse = codecHistoryAt(
                farHistory, currentY, currentCbCr,
                (point / block) * block, hasFar, uniform);
            result = distance < 0.28 + (1.0 - amount) * 0.25
                         ? current
                         : mix(current, coarse, amount);
            break;
        }
        case 24u: { // Codec phase mosaic.
            int tile = 18 + int(round((1.0 - uniform.rate) * 62.0));
            uint selector = codecPixelHash(point / tile,
                                           uniform.frameIndex / 2u, uniform.seed) % 3u;
            result = selector == 0u ? current : (selector == 1u ? nearColor : farColor);
            break;
        }
        case 25u: { // Encoder hot swap.
            int region = codecWrap(point.x + int(phase * 90.0),
                                   max(1, int(uniform.outputWidth)));
            result = region < int(uniform.outputWidth / 2u) ? current : farColor;
            result = mix(current, result, amount);
            break;
        }
        case 26u: { // PTS rubberband.
            int shift = int(sin(phase + point.y * 0.016) *
                            amount * float(uniform.outputWidth) * 0.10);
            result = codecHistoryAt(nearHistory, currentY, currentCbCr,
                                    point + int2(shift, 0), hasNear, uniform);
            break;
        }
        case 27u: { // Bitrate raster.
            int rowGroup = max(1, 2 + int((1.0 - uniform.rate) * 18.0));
            float levels = 3.0 + float((point.y / rowGroup +
                                       int(uniform.frameIndex)) % 8);
            result = mix(current, round(current * levels) / levels, amount);
            break;
        }
        case 28u: { // Plane time split.
            float3 currentYC = codecRgbToYcbcr(current);
            float3 farYC = codecRgbToYcbcr(farColor);
            float3 combined = float3(currentYC.x, farYC.yz);
            float y = 16.0 / 255.0 + combined.x * (219.0 / 255.0);
            float2 uv = float2(128.0 / 255.0) +
                        (combined.yz - 0.5) * (224.0 / 255.0);
            result = codecYcbcrToRgb(y, uv);
            break;
        }
        case 29u: { // Reference atlas.
            int tileWidth = max(1, int(uniform.outputWidth) / 6);
            int tileHeight = max(1, int(uniform.outputHeight) / 4);
            int2 tile = point / int2(tileWidth, tileHeight);
            uint tileHash = codecPixelHash(tile, 0u, uniform.seed);
            if (float(tileHash & 0xffffu) / 65535.0 <
                0.20 + amount * 0.72) {
                int2 offset = int2(int((tileHash >> 16u) % uint(tileWidth * 2 + 1)) - tileWidth,
                                   int(codecHash32(tileHash) % uint(tileHeight * 2 + 1)) - tileHeight);
                if ((tileHash & 1u) != 0u)
                    result = codecHistoryAt(nearHistory, currentY, currentCbCr,
                                            point + offset, hasNear, uniform);
                else
                    result = codecHistoryAt(farHistory, currentY, currentCbCr,
                                            point + offset, hasFar, uniform);
            }
            break;
        }
        case 30u: { // Flow lattice.
            int cellWidth = max(1, int(uniform.outputWidth) / 6);
            int cellHeight = max(1, int(uniform.outputHeight) / 4);
            int2 cell = point / int2(cellWidth, cellHeight);
            int shiftX = int(sin(phase + float(cell.x) * 0.91 +
                                 float(cell.y) * 1.37) *
                             amount * float(cellWidth) * 0.72);
            int shiftY = int(cos(phase * 0.73 + float(cell.x - cell.y)) *
                             amount * float(cellHeight) * 0.48);
            float3 warped = codecCurrentAt(currentY, currentCbCr,
                                           point - int2(shiftX, shiftY), uniform);
            result = mix(farColor, warped, 0.62 + amount * 0.38);
            break;
        }
        case 31u: { // Scan-order fold.
            int strips = amount > 0.58 ? 16 : 8;
            int bits = strips == 16 ? 4 : 3;
            int destination = min(strips - 1,
                                  point.y * strips / int(uniform.outputHeight));
            int source = 0;
            for (int bit = 0; bit < bits; ++bit)
                source |= ((destination >> bit) & 1) << (bits - bit - 1);
            int sourceY = source * int(uniform.outputHeight) / strips +
                          point.y % max(1, int(uniform.outputHeight) / strips);
            result = codecCurrentAt(currentY, currentCbCr,
                                    int2(point.x, sourceY), uniform);
            break;
        }
        case 32u: { // Regional GOP clock.
            int2 tileSize = int2(max(1, int(uniform.outputWidth) / 5),
                                 max(1, int(uniform.outputHeight) / 3));
            int2 tile = point / tileSize;
            float local = sin(phase + float(tile.y) * 1.71 +
                              float(tile.x) * 0.93);
            result = local < -0.28 ? farColor : (local < 0.34 ? nearColor : current);
            break;
        }
        case 33u: { // Entropy feedback.
            int2 cellSize = int2(max(1, int(uniform.outputWidth) / 6),
                                 max(1, int(uniform.outputHeight) / 4));
            uint cellHash = codecPixelHash(point / cellSize, 0u, uniform.seed);
            float density = float(cellHash & 0xffffu) / 65535.0;
            float temporal = 0.5 + 0.5 * sin(phase * 2.2 +
                                             density * 6.2831853);
            if (density * temporal > 0.36 - amount * 0.22) {
                float3 source = (cellHash & 1u) != 0u ? nearColor : farColor;
                float contrast = 0.85 + density * (0.25 + amount * 0.35);
                result = clamp((source - 0.5) * contrast + 0.5, 0.0, 1.0);
            }
            break;
        }
        case 34u: { // Rolling time shutter.
            int strips = amount > 0.65 ? 36 : 24;
            int strip = point.y * strips / int(uniform.outputHeight);
            float sweep = fmod(float(uniform.frameIndex) *
                                   (0.35 + uniform.rate * 1.65),
                               float(strips));
            float distance = fmod(float(strip) - sweep + float(strips),
                                  float(strips));
            int shift = int(sin(float(strip) * 0.77 + sweep * 0.21) *
                            amount * float(uniform.outputWidth) * 0.045);
            if (distance < float(strips) * 0.22)
                result = codecHistoryAt(farHistory, currentY, currentCbCr,
                                        point + int2(shift, 0), hasFar, uniform);
            else if (distance < float(strips) * 0.56)
                result = codecHistoryAt(nearHistory, currentY, currentCbCr,
                                        point + int2(shift, 0), hasNear, uniform);
            break;
        }
        case 35u: { // Asymmetric plane codec.
            float3 currentYC = codecRgbToYcbcr(current);
            int block = 3 + int(round(amount * 25.0));
            float3 history = codecHistoryAt(
                farHistory, currentY, currentCbCr,
                (point / block) * block +
                    int2(int(sin(phase) * amount * 19.0),
                         int(-sin(phase) * amount * 7.0)),
                hasFar, uniform);
            float3 historyYC = codecRgbToYcbcr(history);
            float y = 16.0 / 255.0 + currentYC.x * (219.0 / 255.0);
            float2 uv = float2(128.0 / 255.0) +
                        (historyYC.yz - 0.5) * (224.0 / 255.0);
            result = codecYcbcrToRgb(y, uv);
            break;
        }
        default:
            break;
    }
    output.write(float4(clamp(result, 0.0, 1.0), 1.0), gid);
}

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
        case 9u: { // TILE_SHUFFLE: move coherent cells to neighboring cells.
            int tileSize = 8 + int(round(scale * 88.0));
            int tileX = point.x / tileSize;
            int tileY = point.y / tileSize;
            uint tileHash = pixelHash(tileX, tileY, 0, heldFrame, preset.seed);
            float density = 0.15 + amount * 0.75;
            if (float(tileHash & 0xffffu) < density * 65535.0) {
                int radius = 1 + int(round(amount * 4.0));
                int span = radius * 2 + 1;
                int offsetX = int((tileHash >> 16u) % uint(span)) - radius;
                uint secondHash = pixelHash(tileY, tileX, 1, heldFrame, preset.seed);
                int offsetY = int((secondHash >> 16u) % uint(span)) - radius;
                affected = effectChannelAtWrapped(
                    input, point + int2(offsetX * tileSize, offsetY * tileSize),
                    channel, preset);
            }
            break;
        }
        case 10u: { // VERTICAL_TEAR: narrow columns displaced vertically.
            int bandWidth = 1 + int(round(scale * 15.0));
            int band = point.x / bandWidth;
            uint bandHash = pixelHash(band, 0, 0, heldFrame, preset.seed);
            float density = 0.10 + amount * 0.65;
            if (float(bandHash & 0xffffu) < density * 65535.0) {
                int maximum = min(240, max(4, int(preset.height) / 3));
                int maximumShift = 4 + int(round(amount * float(maximum - 4)));
                int shift = 1 + int((bandHash >> 16u) % uint(max(1, maximumShift)));
                if ((bandHash & 0x80000000u) != 0u) shift = -shift;
                affected = effectChannelAtWrapped(
                    input, point + int2(0, shift), channel, preset);
            }
            break;
        }
        case 11u: { // DIAGONAL_SLIP: diagonal bands slide in opposing directions.
            int bandWidth = 4 + int(round(scale * 60.0));
            int band = (point.x + point.y) / bandWidth;
            uint bandHash = pixelHash(band, bandWidth, 0, heldFrame, preset.seed);
            float density = 0.12 + amount * 0.68;
            if (float(bandHash & 0xffffu) < density * 65535.0) {
                int maximum = min(
                    180, max(4, min(int(preset.width), int(preset.height)) / 4));
                int maximumShift = 4 + int(round(amount * float(maximum - 4)));
                int shift = 1 + int((bandHash >> 16u) % uint(max(1, maximumShift)));
                if ((bandHash & 0x80000000u) != 0u) shift = -shift;
                int chroma = (channel - 1) * int(round(amount * 5.0));
                affected = effectChannelAtWrapped(
                    input, point + int2(shift + chroma, -(shift / 2)),
                    channel, preset);
            }
            break;
        }
        case 12u: { // SCANLINE_WEAVE: alternating rows pull from opposite sides.
            int groupHeight = 1 + int(round(scale * 7.0));
            int group = point.y / groupHeight;
            int direction = ((uint(group) + heldFrame) & 1u) == 0u ? -1 : 1;
            int shift = direction * (2 + int(round(amount * 72.0)));
            int rowOffset = ((uint(group) + heldFrame / 2u) & 1u) == 0u ? -1 : 1;
            int chroma = (channel - 1) * int(round(amount * 6.0));
            affected = effectChannelAtWrapped(
                input, point + int2(shift + chroma, rowOffset), channel, preset);
            if (((point.y + int(heldFrame)) & 1) != 0)
                affected *= 1.0 - amount * 0.18;
            break;
        }
        case 13u: { // QUAD_MIRROR: fold both axes into animated mirror tiles.
            int halfWidth = 10 + int(round(scale * 90.0));
            int halfHeight = 10 + int(round((1.0 - scale) * 70.0));
            int periodX = halfWidth * 2;
            int periodY = halfHeight * 2;
            int phaseX = int(heldFrame % uint(periodX));
            int phaseY = int((heldFrame * 2u) % uint(periodY));
            int shiftedX = point.x + phaseX;
            int shiftedY = point.y + phaseY;
            int localX = shiftedX % periodX;
            int localY = shiftedY % periodY;
            int foldedX = localX <= halfWidth ? localX : periodX - 1 - localX;
            int foldedY = localY <= halfHeight ? localY : periodY - 1 - localY;
            int cellX = shiftedX / periodX;
            int cellY = shiftedY / periodY;
            affected = effectChannelAtWrapped(
                input,
                int2(cellX * periodX + foldedX - phaseX,
                     cellY * periodY + foldedY - phaseY),
                channel, preset);
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
