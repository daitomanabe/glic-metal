#include <metal_stdlib>
using namespace metal;

// Keep these layouts byte-for-byte aligned with original_realtime_metal.mm.
struct OriginalChannelUniform {
    int predictionMethod;
    uint quantizationValue;
    uint clampMethod;
    uint originalWaveletId;

    uint transformType;
    int transformScale;
    float transformCompress;
    float compressionThreshold;
};

struct OriginalPresetUniform {
    uint width;
    uint height;
    uint rootSize;
    uint colorSpace;

    int reference0;
    int reference1;
    int reference2;
    uint reserved;

    OriginalChannelUniform channels[3];
};

struct OriginalSegmentDescriptor {
    uint x;
    uint y;
    uint size;
    uint channel;
};

constant float kOriginalCdf97Scaling[9] = {
    0.026748757411f,  -0.016864118443f, -0.078223266529f,
    0.266864118443f, 0.602949018236f,  0.266864118443f,
    -0.078223266529f, -0.016864118443f, 0.026748757411f,
};
constant float kOriginalCdf97Wavelet[9] = {
    0.0f, 0.091271763114f, -0.057543526229f,
    -0.591271763114f, 1.11508705f, -0.591271763114f,
    -0.057543526229f, 0.091271763114f, 0.0f,
};
static int originalReference(constant OriginalPresetUniform &preset,
                             uint channel) {
    return channel == 0u ? preset.reference0
         : channel == 1u ? preset.reference1
                         : preset.reference2;
}

static int originalPlaneAt(device const int *planes,
                           int x, int y, uint channel,
                           constant OriginalPresetUniform &preset) {
    if (x < 0 || y < 0 || x >= int(preset.width) ||
        y >= int(preset.height))
        return originalReference(preset, channel);
    uint pixelCount = preset.width * preset.height;
    return planes[channel * pixelCount + uint(y) * preset.width + uint(x)];
}

static int originalMedian3(int a, int b, int c) {
    return max(min(a, b), min(max(a, b), c));
}

static int originalPrediction(device const int *planes,
                              OriginalSegmentDescriptor segment,
                              int x, int y, int dcValue, int cornerValue,
                              constant OriginalPresetUniform &preset) {
    uint channel = segment.channel;
    int top = originalPlaneAt(planes, int(segment.x) + x,
                              int(segment.y) - 1, channel, preset);
    int left = originalPlaneAt(planes, int(segment.x) - 1,
                               int(segment.y) + y, channel, preset);
    int method = preset.channels[channel].predictionMethod;
    switch (method) {
        case 1: return cornerValue;
        case 2: return left;
        case 3: return top;
        case 4: return dcValue;
        case 5: return originalMedian3(dcValue, top, left);
        case 6: return originalMedian3(cornerValue, top, left);
        case 7: return (top + left) >> 1;
        case 8: return clamp(top + left - cornerValue, 0, 255);
        case 9: {
            int estimate = top + left - cornerValue;
            int distanceLeft = abs(estimate - left);
            int distanceTop = abs(estimate - top);
            int distanceCorner = abs(estimate - cornerValue);
            return clamp((distanceLeft <= distanceTop &&
                          distanceLeft <= distanceCorner)
                             ? left
                             : (distanceTop <= distanceCorner ? top
                                                              : cornerValue),
                         0, 255);
        }
        case 10: {
            int diagonal = x + y;
            int size = int(segment.size);
            int topValue = originalPlaneAt(
                planes, int(segment.x) +
                            (diagonal + 1 < size ? diagonal + 1 : size - 1),
                int(segment.y) - 1, channel, preset);
            int leftValue = originalPlaneAt(
                planes, int(segment.x) - 1,
                int(segment.y) + (diagonal < size ? diagonal : size - 1),
                channel, preset);
            return ((x + 1) * topValue + (y + 1) * leftValue) /
                   (x + y + 2);
        }
        case 11:
            return x > y ? top : (y > x ? left : ((top + left) >> 1));
        case 12: {
            int upperLeft = originalPlaneAt(
                planes, int(segment.x) + x - 1, int(segment.y) - 1,
                channel, preset);
            if (upperLeft >= max(top, left)) return min(top, left);
            if (upperLeft <= min(top, left)) return max(top, left);
            return top + left - upperLeft;
        }
        case 13: {
            int top2 = originalPlaneAt(planes, int(segment.x) + x,
                                       int(segment.y) - 2, channel, preset);
            int left2 = originalPlaneAt(planes, int(segment.x) - 2,
                                        int(segment.y) + y, channel, preset);
            return clamp((left2 + left2 - left + top2 + top2 - top) >> 1,
                         0, 255);
        }
        default: return 0;
    }
}

static float originalRoundAway(float value) {
    return value >= 0.0f ? floor(value + 0.5f) : ceil(value - 0.5f);
}

static void originalCdfPass(device float *matrix,
                            device float *scratch,
                            OriginalSegmentDescriptor segment,
                            constant OriginalPresetUniform &preset,
                            bool columns, bool reverse, uint length,
                            bool allPackets, uint tid, uint threadCount) {
    uint size = segment.size;
    uint transformedPositions = allPackets ? size : length;
    uint jobs = size * transformedPositions;
    uint halfLength = length >> 1u;
    uint mask = length - 1u;
    uint matrixBase = segment.channel * preset.rootSize * preset.rootSize;

    for (uint job = tid; job < jobs; job += threadCount) {
        uint line = job / transformedPositions;
        uint position = job - line * transformedPositions;
        uint packetOffset = allPackets ? (position / length) * length : 0u;
        uint output = position - packetOffset;
        float value = 0.0f;

        if (!reverse) {
            bool high = output >= halfLength;
            uint coefficient = high ? output - halfLength : output;
            for (uint tap = 0u; tap < 9u; ++tap) {
                uint source = packetOffset + ((coefficient * 2u + tap) & mask);
                uint localX = columns ? source : line;
                uint localY = columns ? line : source;
                uint index = matrixBase + (segment.x + localX) * preset.rootSize +
                             segment.y + localY;
                value += matrix[index] *
                         (high ? kOriginalCdf97Wavelet[tap]
                               : kOriginalCdf97Scaling[tap]);
            }
        } else {
            uint destination = output;
            for (uint tap = 0u; tap < 9u; ++tap) {
                uint delta = (destination - tap) & mask;
                if ((delta & 1u) != 0u) continue;
                uint coefficient = delta >> 1u;
                uint lowPosition = packetOffset + coefficient;
                uint highPosition = lowPosition + halfLength;
                uint lowX = columns ? lowPosition : line;
                uint lowY = columns ? line : lowPosition;
                uint highX = columns ? highPosition : line;
                uint highY = columns ? line : highPosition;
                uint lowIndex = matrixBase +
                    (segment.x + lowX) * preset.rootSize + segment.y + lowY;
                uint highIndex = matrixBase +
                    (segment.x + highX) * preset.rootSize + segment.y + highY;
                value += matrix[lowIndex] * kOriginalCdf97Scaling[tap] +
                         matrix[highIndex] * kOriginalCdf97Wavelet[tap];
            }
        }

        uint localX = columns ? packetOffset + output : line;
        uint localY = columns ? line : packetOffset + output;
        uint index = matrixBase + (segment.x + localX) * preset.rootSize +
                     segment.y + localY;
        scratch[index] = value;
    }
    threadgroup_barrier(mem_flags::mem_device);

    for (uint job = tid; job < jobs; job += threadCount) {
        uint line = job / transformedPositions;
        uint position = job - line * transformedPositions;
        uint localX = columns ? position : line;
        uint localY = columns ? line : position;
        uint index = matrixBase + (segment.x + localX) * preset.rootSize +
                     segment.y + localY;
        matrix[index] = scratch[index];
    }
    threadgroup_barrier(mem_flags::mem_device);
}

kernel void glicOriginalSegments(
    device int *planes [[buffer(0)]],
    device float *matrix [[buffer(1)]],
    device float *scratch [[buffer(2)]],
    device const OriginalSegmentDescriptor *segments [[buffer(3)]],
    constant OriginalPresetUniform &preset [[buffer(4)]],
    constant uint &segmentOffset [[buffer(5)]],
    uint group [[threadgroup_position_in_grid]],
    uint tid [[thread_index_in_threadgroup]],
    uint threadCount [[threads_per_threadgroup]],
    threadgroup int &dcValue [[threadgroup(0)]],
    threadgroup float *reduction [[threadgroup(1)]]) {
    OriginalSegmentDescriptor segment = segments[segmentOffset + group];
    OriginalChannelUniform config = preset.channels[segment.channel];
    uint pixelCount = preset.width * preset.height;
    uint planeBase = segment.channel * pixelCount;
    uint usedWidth = min(segment.size, preset.width - segment.x);
    uint usedHeight = min(segment.size, preset.height - segment.y);
    uint usedPixels = usedWidth * usedHeight;
    int corner = originalPlaneAt(planes, int(segment.x) - 1,
                                 int(segment.y) - 1, segment.channel, preset);

    if (tid == 0u) {
        int dc = 0;
        if (config.predictionMethod == 4 || config.predictionMethod == 5) {
            for (uint offset = 0u; offset < segment.size; ++offset) {
                dc += originalPlaneAt(planes, int(segment.x) - 1,
                                      int(segment.y + offset),
                                      segment.channel, preset);
                dc += originalPlaneAt(planes, int(segment.x + offset),
                                      int(segment.y) - 1,
                                      segment.channel, preset);
            }
            dc += corner;
            dc /= int(segment.size * 2u + 1u);
        }
        dcValue = dc;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    float quantization = float(config.quantizationValue) * 0.5f;

    // No-wavelet presets are an exact-integer path. Every pixel in a segment
    // depends only on already completed top/left segment boundaries.
    if (config.originalWaveletId == 0u) {
        for (uint job = tid; job < usedPixels; job += threadCount) {
            uint x = job % usedWidth;
            uint y = job / usedWidth;
            uint index = planeBase + (segment.y + y) * preset.width +
                         segment.x + x;
            int prediction = originalPrediction(planes, segment, int(x), int(y),
                                                dcValue, corner, preset);
            int residual = planes[index] - prediction;
            if (config.clampMethod == 1u)
                residual = residual < 0 ? residual + 256
                         : residual > 255 ? residual - 256 : residual;
            if (quantization > 1.0f)
                residual = int(originalRoundAway(
                    originalRoundAway(float(residual) / quantization) *
                    quantization));
            int reconstructed = residual + prediction;
            if (config.clampMethod == 1u)
                reconstructed = reconstructed < 0 ? reconstructed + 256
                              : reconstructed > 255 ? reconstructed - 256
                                                    : reconstructed;
            else
                reconstructed = clamp(reconstructed, 0, 255);
            planes[index] = reconstructed;
        }
        return;
    }

    uint matrixBase = segment.channel * preset.rootSize * preset.rootSize;
    float border = float(originalReference(preset, segment.channel)) / 255.0f;
    uint logicalPixels = segment.size * segment.size;

    // Prediction and forward quantization.
    for (uint job = tid; job < usedPixels; job += threadCount) {
        uint x = job % usedWidth;
        uint y = job / usedWidth;
        uint planeIndex = planeBase + (segment.y + y) * preset.width +
                          segment.x + x;
        int prediction = originalPrediction(planes, segment, int(x), int(y),
                                            dcValue, corner, preset);
        int residual = planes[planeIndex] - prediction;
        if (config.clampMethod == 1u)
            residual = residual < 0 ? residual + 256
                     : residual > 255 ? residual - 256 : residual;
        if (quantization > 1.0f)
            residual = int(originalRoundAway(float(residual) / quantization));
        planes[planeIndex] = residual;
    }
    threadgroup_barrier(mem_flags::mem_device);

    for (uint job = tid; job < logicalPixels; job += threadCount) {
        uint x = job / segment.size;
        uint y = job - x * segment.size;
        uint matrixIndex = matrixBase + (segment.x + x) * preset.rootSize +
                           segment.y + y;
        if (x < usedWidth && y < usedHeight) {
            uint planeIndex = planeBase + (segment.y + y) * preset.width +
                              segment.x + x;
            matrix[matrixIndex] = float(planes[planeIndex]) / 255.0f;
        } else {
            matrix[matrixIndex] = border;
        }
    }
    threadgroup_barrier(mem_flags::mem_device);

    bool allPackets = config.transformType == 1u;
    for (uint length = segment.size; length >= 2u; length >>= 1u)
        originalCdfPass(matrix, scratch, segment, preset, false, false,
                        length, allPackets, tid, threadCount);
    for (uint length = segment.size; length >= 2u; length >>= 1u)
        originalCdfPass(matrix, scratch, segment, preset, true, false,
                        length, allPackets, tid, threadCount);

    if (config.transformCompress > 0.0f) {
        float magnitude = 0.0f;
        for (uint job = tid; job < logicalPixels; job += threadCount) {
            uint x = job / segment.size;
            uint y = job - x * segment.size;
            uint index = matrixBase + (segment.x + x) * preset.rootSize +
                         segment.y + y;
            magnitude += abs(matrix[index]);
        }
        reduction[tid] = magnitude;
        threadgroup_barrier(mem_flags::mem_threadgroup);
        for (uint stride = threadCount >> 1u; stride > 0u; stride >>= 1u) {
            if (tid < stride) reduction[tid] += reduction[tid + stride];
            threadgroup_barrier(mem_flags::mem_threadgroup);
        }
        float cutoff = reduction[0] / float(logicalPixels) *
                       config.compressionThreshold;
        for (uint job = tid; job < logicalPixels; job += threadCount) {
            uint x = job / segment.size;
            uint y = job - x * segment.size;
            uint index = matrixBase + (segment.x + x) * preset.rootSize +
                         segment.y + y;
            if (abs(matrix[index]) < cutoff) matrix[index] = 0.0f;
        }
        threadgroup_barrier(mem_flags::mem_device);
    }

    float scale = float(config.transformScale);
    for (uint job = tid; job < usedPixels; job += threadCount) {
        uint x = job % usedWidth;
        uint y = job / usedWidth;
        uint planeIndex = planeBase + (segment.y + y) * preset.width +
                          segment.x + x;
        uint matrixIndex = matrixBase + (segment.x + x) * preset.rootSize +
                           segment.y + y;
        planes[planeIndex] = int(originalRoundAway(
            matrix[matrixIndex] * scale / float(segment.size)));
    }
    threadgroup_barrier(mem_flags::mem_device);

    for (uint job = tid; job < logicalPixels; job += threadCount) {
        uint x = job / segment.size;
        uint y = job - x * segment.size;
        uint matrixIndex = matrixBase + (segment.x + x) * preset.rootSize +
                           segment.y + y;
        if (x < usedWidth && y < usedHeight) {
            uint planeIndex = planeBase + (segment.y + y) * preset.width +
                              segment.x + x;
            matrix[matrixIndex] = float(segment.size) *
                                  float(planes[planeIndex]) / scale;
        } else {
            matrix[matrixIndex] = border;
        }
    }
    threadgroup_barrier(mem_flags::mem_device);

    for (uint length = 2u; length <= segment.size; length <<= 1u)
        originalCdfPass(matrix, scratch, segment, preset, true, true,
                        length, allPackets, tid, threadCount);
    for (uint length = 2u; length <= segment.size; length <<= 1u)
        originalCdfPass(matrix, scratch, segment, preset, false, true,
                        length, allPackets, tid, threadCount);

    for (uint job = tid; job < usedPixels; job += threadCount) {
        uint x = job % usedWidth;
        uint y = job / usedWidth;
        uint planeIndex = planeBase + (segment.y + y) * preset.width +
                          segment.x + x;
        uint matrixIndex = matrixBase + (segment.x + x) * preset.rootSize +
                           segment.y + y;
        int value = int(originalRoundAway(matrix[matrixIndex] * 255.0f));
        value = config.clampMethod == 1u ? clamp(value, 0, 255)
                                         : clamp(value, -255, 255);
        if (quantization > 1.0f)
            value = int(originalRoundAway(float(value) * quantization));
        planes[planeIndex] = value;
    }
    threadgroup_barrier(mem_flags::mem_device);

    for (uint job = tid; job < usedPixels; job += threadCount) {
        uint x = job % usedWidth;
        uint y = job / usedWidth;
        uint planeIndex = planeBase + (segment.y + y) * preset.width +
                          segment.x + x;
        int prediction = originalPrediction(planes, segment, int(x), int(y),
                                            dcValue, corner, preset);
        int reconstructed = planes[planeIndex] + prediction;
        if (config.clampMethod == 1u)
            reconstructed = reconstructed < 0 ? reconstructed + 256
                          : reconstructed > 255 ? reconstructed - 256
                                                : reconstructed;
        else
            reconstructed = clamp(reconstructed, 0, 255);
        planes[planeIndex] = reconstructed;
    }
}
