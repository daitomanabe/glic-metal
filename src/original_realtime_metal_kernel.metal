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
// Low components split from the exact decimal doubles in JWave's CDF97.
// Together with the arrays above they retain roughly 48 significant bits in
// each convolution without requiring unsupported fp64 Metal arithmetic.
constant float kOriginalCdf97ScalingLow[9] = {
    -3.98400559e-10f, 6.54590448e-10f, -8.21507168e-10f,
    -2.51723559e-09f, -5.01076514e-09f, -2.51723559e-09f,
    -8.21507168e-10f, 6.54590448e-10f, -3.98400559e-10f,
};
constant float kOriginalCdf97WaveletLow[9] = {
    0.0f, -2.41610941e-09f, 1.10592852e-09f,
    -5.03447117e-09f, 1.76818848e-08f, -5.03447117e-09f,
    1.10592852e-09f, -2.41610941e-09f, 0.0f,
};

// Strict and Fast Match are specialized into distinct pipeline states.
constant bool kOriginalFastCdf97 [[function_constant(0)]];

static void originalAccumulateCdfProduct(thread float2 &sum, float value,
                                         float coefficientHigh,
                                         float coefficientLow) {
    if (kOriginalFastCdf97) {
        sum.x = fma(value, coefficientHigh + coefficientLow, sum.x);
        return;
    }
    float productHigh = value * coefficientHigh;
    float productLow = fma(value, coefficientHigh, -productHigh) +
                       value * coefficientLow;
    float combined = sum.x + productHigh;
    float recovered = combined - sum.x;
    float error = (sum.x - (combined - recovered)) +
                  (productHigh - recovered) + sum.y + productLow;
    float high = combined + error;
    sum = float2(high, error - (high - combined));
}
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
    int method = preset.channels[channel].predictionMethod;
    switch (method) {
        case 1: return cornerValue;
        case 2:
            return originalPlaneAt(planes, int(segment.x) - 1,
                                   int(segment.y) + y, channel, preset);
        case 3:
            return originalPlaneAt(planes, int(segment.x) + x,
                                   int(segment.y) - 1, channel, preset);
        case 4: return dcValue;
        case 5:
        case 6:
        case 7:
        case 8:
        case 9: {
            int top = originalPlaneAt(planes, int(segment.x) + x,
                                      int(segment.y) - 1, channel, preset);
            int left = originalPlaneAt(planes, int(segment.x) - 1,
                                       int(segment.y) + y, channel, preset);
            if (method == 5) return originalMedian3(dcValue, top, left);
            if (method == 6)
                return originalMedian3(cornerValue, top, left);
            if (method == 7) return (top + left) >> 1;
            if (method == 8)
                return clamp(top + left - cornerValue, 0, 255);
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
        case 11: {
            int top = originalPlaneAt(planes, int(segment.x) + x,
                                      int(segment.y) - 1, channel, preset);
            int left = originalPlaneAt(planes, int(segment.x) - 1,
                                       int(segment.y) + y, channel, preset);
            return x > y ? top : (y > x ? left : ((top + left) >> 1));
        }
        case 12: {
            int top = originalPlaneAt(planes, int(segment.x) + x,
                                      int(segment.y) - 1, channel, preset);
            int left = originalPlaneAt(planes, int(segment.x) - 1,
                                       int(segment.y) + y, channel, preset);
            int upperLeft = originalPlaneAt(
                planes, int(segment.x) + x - 1, int(segment.y) - 1,
                channel, preset);
            if (upperLeft >= max(top, left)) return min(top, left);
            if (upperLeft <= min(top, left)) return max(top, left);
            return top + left - upperLeft;
        }
        case 13: {
            int top = originalPlaneAt(planes, int(segment.x) + x,
                                      int(segment.y) - 1, channel, preset);
            int left = originalPlaneAt(planes, int(segment.x) - 1,
                                       int(segment.y) + y, channel, preset);
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

static int originalPredictionCached(
    device const int *planes,
    OriginalSegmentDescriptor segment,
    int x, int y, int dcValue, int cornerValue,
    threadgroup const int *topBoundary,
    threadgroup const int *leftBoundary,
    constant OriginalPresetUniform &preset) {
    int method = preset.channels[segment.channel].predictionMethod;
    int top = topBoundary[x];
    int left = leftBoundary[y];
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
            int topValue = topBoundary[
                diagonal + 1 < size ? diagonal + 1 : size - 1];
            int leftValue =
                leftBoundary[diagonal < size ? diagonal : size - 1];
            return ((x + 1) * topValue + (y + 1) * leftValue) /
                   (x + y + 2);
        }
        case 11:
            return x > y ? top : (y > x ? left : ((top + left) >> 1));
        case 12: {
            int upperLeft = x > 0 ? topBoundary[x - 1] : cornerValue;
            if (upperLeft >= max(top, left)) return min(top, left);
            if (upperLeft <= min(top, left)) return max(top, left);
            return top + left - upperLeft;
        }
        case 13: {
            int top2 = originalPlaneAt(
                planes, int(segment.x) + x, int(segment.y) - 2,
                segment.channel, preset);
            int left2 = originalPlaneAt(
                planes, int(segment.x) - 2, int(segment.y) + y,
                segment.channel, preset);
            return clamp((left2 + left2 - left + top2 + top2 - top) >> 1,
                         0, 255);
        }
        default: return 0;
    }
}

static int originalProcessingRound(float value) {
    // Processing round(float) is Java Math.round: floor(x + 0.5), including
    // negative half-integers, NaN -> 0, and saturating float-to-int conversion.
    // It is deliberately not round-away-from-zero.
    if (isnan(value)) return 0;
    if (value <= -2147483648.0f) return (-2147483647 - 1);
    if (value >= 2147483648.0f) return 2147483647;
    return int(floor(value + 0.5f));
}

static uint originalPowerOfTwoShift(uint value) {
    return 31u - clz(value);
}

static void originalCdfPass(device float *source,
                            device float *destination,
                            OriginalSegmentDescriptor segment,
                            constant OriginalPresetUniform &preset,
                            bool columns, bool reverse, uint length,
                            bool allPackets, uint tid, uint threadCount) {
    uint size = segment.size;
    uint transformedPositions = allPackets ? size : length;
    uint jobs = size * transformedPositions;
    uint halfLength = length >> 1u;
    uint mask = length - 1u;
    uint transformedMask = transformedPositions - 1u;
    uint transformedShift = originalPowerOfTwoShift(transformedPositions);
    uint matrixBase = segment.channel * preset.rootSize * preset.rootSize;

    for (uint job = tid; job < jobs; job += threadCount) {
        uint line = job >> transformedShift;
        uint position = job & transformedMask;
        uint packetOffset = allPackets ? (position & ~mask) : 0u;
        uint output = position & mask;
        float2 accumulator = float2(0.0f);

        if (!reverse) {
            bool high = output >= halfLength;
            uint coefficient = high ? output - halfLength : output;
            for (uint tap = 0u; tap < 9u; ++tap) {
                uint sourcePosition =
                    packetOffset + ((coefficient * 2u + tap) & mask);
                uint localX = columns ? sourcePosition : line;
                uint localY = columns ? line : sourcePosition;
                uint index = matrixBase + (segment.x + localX) * preset.rootSize +
                             segment.y + localY;
                originalAccumulateCdfProduct(
                    accumulator, source[index],
                    high ? kOriginalCdf97Wavelet[tap]
                         : kOriginalCdf97Scaling[tap],
                    high ? kOriginalCdf97WaveletLow[tap]
                         : kOriginalCdf97ScalingLow[tap]);
            }
        } else {
            uint destinationPosition = output;
            // The inverse branch accepts only taps with the destination's
            // parity. Step through exactly that ascending subsequence so the
            // compensated accumulation order is unchanged while eliminating
            // four/five dead loop iterations and branches per output.
            for (uint tap = destinationPosition & 1u; tap < 9u; tap += 2u) {
                uint delta = (destinationPosition - tap) & mask;
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
                originalAccumulateCdfProduct(
                    accumulator, source[lowIndex],
                    kOriginalCdf97Scaling[tap],
                    kOriginalCdf97ScalingLow[tap]);
                originalAccumulateCdfProduct(
                    accumulator, source[highIndex],
                    kOriginalCdf97Wavelet[tap],
                    kOriginalCdf97WaveletLow[tap]);
            }
        }

        uint localX = columns ? packetOffset + output : line;
        uint localY = columns ? line : packetOffset + output;
        uint index = matrixBase + (segment.x + localX) * preset.rootSize +
                     segment.y + localY;
        destination[index] = accumulator.x + accumulator.y;
    }
    threadgroup_barrier(mem_flags::mem_device);
    if (!allPackets) {
        // FWT updates only the current low-frequency prefix. Preserve every
        // already-emitted high-frequency band in the source matrix exactly as
        // upstream does; WPT covers the full matrix and can ping-pong directly.
        for (uint job = tid; job < jobs; job += threadCount) {
            uint line = job >> transformedShift;
            uint position = job & transformedMask;
            uint localX = columns ? position : line;
            uint localY = columns ? line : position;
            uint index = matrixBase +
                (segment.x + localX) * preset.rootSize + segment.y + localY;
            source[index] = destination[index];
        }
        threadgroup_barrier(mem_flags::mem_device);
    }
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
                residual = originalProcessingRound(
                    float(originalProcessingRound(
                        float(residual) / quantization)) * quantization);
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
    uint segmentShift = originalPowerOfTwoShift(segment.size);
    uint segmentMask = segment.size - 1u;

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
            residual = originalProcessingRound(float(residual) / quantization);
        planes[planeIndex] = residual;
    }
    threadgroup_barrier(mem_flags::mem_device);

    for (uint job = tid; job < logicalPixels; job += threadCount) {
        uint x = job >> segmentShift;
        uint y = job & segmentMask;
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
    device float *cdfSource = matrix;
    device float *cdfDestination = scratch;
    if (allPackets) {
        for (uint length = segment.size; length >= 2u; length >>= 1u) {
            originalCdfPass(cdfSource, cdfDestination, segment, preset,
                            false, false, length, true, tid, threadCount);
            device float *swap = cdfSource;
            cdfSource = cdfDestination;
            cdfDestination = swap;
        }
        for (uint length = segment.size; length >= 2u; length >>= 1u) {
            originalCdfPass(cdfSource, cdfDestination, segment, preset, true,
                            false, length, true, tid, threadCount);
            device float *swap = cdfSource;
            cdfSource = cdfDestination;
            cdfDestination = swap;
        }
    } else {
        for (uint length = segment.size; length >= 2u; length >>= 1u)
            originalCdfPass(matrix, scratch, segment, preset, false, false,
                            length, false, tid, threadCount);
        for (uint length = segment.size; length >= 2u; length >>= 1u)
            originalCdfPass(matrix, scratch, segment, preset, true, false,
                            length, false, tid, threadCount);
    }

    if (config.transformCompress > 0.0f) {
        // The previous parallel reduction changed addition order with the
        // occupancy heuristic (32/64/128/256 threads), moving coefficients
        // across the compression cutoff. Small leaves use a matrix-order
        // compensated sum. Larger leaves always use the same 32 compensated
        // lanes and fixed tree, independent of threadgroup occupancy.
        if (logicalPixels <= 32u && tid == 0u) {
            float magnitude = 0.0f;
            float compensation = 0.0f;
            for (uint job = 0u; job < logicalPixels; ++job) {
                uint x = job >> segmentShift;
                uint y = job & segmentMask;
                uint index = matrixBase + (segment.x + x) * preset.rootSize +
                             segment.y + y;
                float corrected = abs(cdfSource[index]) - compensation;
                float next = magnitude + corrected;
                compensation = (next - magnitude) - corrected;
                magnitude = next;
            }
            reduction[0] = magnitude / float(logicalPixels) *
                           config.compressionThreshold;
        } else if (logicalPixels > 32u && tid < 32u) {
            float magnitude = 0.0f;
            float compensation = 0.0f;
            for (uint job = tid; job < logicalPixels; job += 32u) {
                uint x = job >> segmentShift;
                uint y = job & segmentMask;
                uint index = matrixBase + (segment.x + x) * preset.rootSize +
                             segment.y + y;
                float corrected = abs(cdfSource[index]) - compensation;
                float next = magnitude + corrected;
                compensation = (next - magnitude) - corrected;
                magnitude = next;
            }
            reduction[tid] = magnitude;
        }
        threadgroup_barrier(mem_flags::mem_threadgroup);
        if (logicalPixels > 32u) {
            for (uint stride = 16u; stride > 0u; stride >>= 1u) {
                if (tid < stride)
                    reduction[tid] += reduction[tid + stride];
                threadgroup_barrier(mem_flags::mem_threadgroup);
            }
            if (tid == 0u)
                reduction[0] = reduction[0] / float(logicalPixels) *
                               config.compressionThreshold;
            threadgroup_barrier(mem_flags::mem_threadgroup);
        }
        float cutoff = reduction[0];
        for (uint job = tid; job < logicalPixels; job += threadCount) {
            uint x = job >> segmentShift;
            uint y = job & segmentMask;
            uint index = matrixBase + (segment.x + x) * preset.rootSize +
                         segment.y + y;
            if (abs(cdfSource[index]) < cutoff) cdfSource[index] = 0.0f;
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
        planes[planeIndex] = originalProcessingRound(
            cdfSource[matrixIndex] * scale / float(segment.size));
    }
    threadgroup_barrier(mem_flags::mem_device);

    for (uint job = tid; job < logicalPixels; job += threadCount) {
        uint x = job >> segmentShift;
        uint y = job & segmentMask;
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

    cdfSource = matrix;
    cdfDestination = scratch;
    if (allPackets) {
        for (uint length = 2u; length <= segment.size; length <<= 1u) {
            originalCdfPass(cdfSource, cdfDestination, segment, preset, true,
                            true, length, true, tid, threadCount);
            device float *swap = cdfSource;
            cdfSource = cdfDestination;
            cdfDestination = swap;
        }
        for (uint length = 2u; length <= segment.size; length <<= 1u) {
            originalCdfPass(cdfSource, cdfDestination, segment, preset,
                            false, true, length, true, tid, threadCount);
            device float *swap = cdfSource;
            cdfSource = cdfDestination;
            cdfDestination = swap;
        }
    } else {
        for (uint length = 2u; length <= segment.size; length <<= 1u)
            originalCdfPass(matrix, scratch, segment, preset, true, true,
                            length, false, tid, threadCount);
        for (uint length = 2u; length <= segment.size; length <<= 1u)
            originalCdfPass(matrix, scratch, segment, preset, false, true,
                            length, false, tid, threadCount);
    }

    for (uint job = tid; job < usedPixels; job += threadCount) {
        uint x = job % usedWidth;
        uint y = job / usedWidth;
        uint planeIndex = planeBase + (segment.y + y) * preset.width +
                          segment.x + x;
        uint matrixIndex = matrixBase + (segment.x + x) * preset.rootSize +
                           segment.y + y;
        int value = originalProcessingRound(cdfSource[matrixIndex] * 255.0f);
        value = config.clampMethod == 1u ? clamp(value, 0, 255)
                                         : clamp(value, -255, 255);
        if (quantization > 1.0f)
            value = originalProcessingRound(float(value) * quantization);
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

// Small CDF97 leaves fit entirely in Apple GPU threadgroup memory. Keeping the
// coefficient matrix and pass scratch local removes the repeated private-buffer
// round trips that dominate 16x16/32x32 original presets. Operation order,
// split coefficients, and compensated float-float accumulation intentionally
// match originalCdfPass above.
static void originalCdfPassThreadgroup(
    threadgroup float *source,
    threadgroup float *destination,
    uint size, bool columns, bool reverse, uint length,
    bool allPackets, uint tid, uint threadCount) {
    uint transformedPositions = allPackets ? size : length;
    uint jobs = size * transformedPositions;
    uint halfLength = length >> 1u;
    uint mask = length - 1u;
    uint transformedMask = transformedPositions - 1u;
    uint transformedShift = originalPowerOfTwoShift(transformedPositions);

    for (uint job = tid; job < jobs; job += threadCount) {
        uint line = job >> transformedShift;
        uint position = job & transformedMask;
        uint packetOffset = allPackets ? (position & ~mask) : 0u;
        uint output = position & mask;
        float2 accumulator = float2(0.0f);

        if (!reverse) {
            bool high = output >= halfLength;
            uint coefficient = high ? output - halfLength : output;
            for (uint tap = 0u; tap < 9u; ++tap) {
                uint sourcePosition =
                    packetOffset + ((coefficient * 2u + tap) & mask);
                uint localX = columns ? sourcePosition : line;
                uint localY = columns ? line : sourcePosition;
                originalAccumulateCdfProduct(
                    accumulator, source[localX * size + localY],
                    high ? kOriginalCdf97Wavelet[tap]
                         : kOriginalCdf97Scaling[tap],
                    high ? kOriginalCdf97WaveletLow[tap]
                         : kOriginalCdf97ScalingLow[tap]);
            }
        } else {
            uint destinationPosition = output;
            for (uint tap = destinationPosition & 1u; tap < 9u; tap += 2u) {
                uint delta = (destinationPosition - tap) & mask;
                uint coefficient = delta >> 1u;
                uint lowPosition = packetOffset + coefficient;
                uint highPosition = lowPosition + halfLength;
                uint lowX = columns ? lowPosition : line;
                uint lowY = columns ? line : lowPosition;
                uint highX = columns ? highPosition : line;
                uint highY = columns ? line : highPosition;
                originalAccumulateCdfProduct(
                    accumulator, source[lowX * size + lowY],
                    kOriginalCdf97Scaling[tap],
                    kOriginalCdf97ScalingLow[tap]);
                originalAccumulateCdfProduct(
                    accumulator, source[highX * size + highY],
                    kOriginalCdf97Wavelet[tap],
                    kOriginalCdf97WaveletLow[tap]);
            }
        }

        uint localX = columns ? packetOffset + output : line;
        uint localY = columns ? line : packetOffset + output;
        destination[localX * size + localY] = accumulator.x + accumulator.y;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);
    if (!allPackets) {
        for (uint job = tid; job < jobs; job += threadCount) {
            uint line = job >> transformedShift;
            uint position = job & transformedMask;
            uint localX = columns ? position : line;
            uint localY = columns ? line : position;
            source[localX * size + localY] =
                destination[localX * size + localY];
        }
        threadgroup_barrier(mem_flags::mem_threadgroup);
    }
}

kernel void glicOriginalSegmentsThreadgroupCdf(
    device int *planes [[buffer(0)]],
    device const OriginalSegmentDescriptor *segments [[buffer(3)]],
    constant OriginalPresetUniform &preset [[buffer(4)]],
    constant uint &segmentOffset [[buffer(5)]],
    uint group [[threadgroup_position_in_grid]],
    uint tid [[thread_index_in_threadgroup]],
    uint threadCount [[threads_per_threadgroup]],
    threadgroup int &dcValue [[threadgroup(0)]],
    threadgroup float *working [[threadgroup(1)]]) {
    OriginalSegmentDescriptor segment = segments[segmentOffset + group];
    OriginalChannelUniform config = preset.channels[segment.channel];
    uint pixelCount = preset.width * preset.height;
    uint planeBase = segment.channel * pixelCount;
    uint usedWidth = min(segment.size, preset.width - segment.x);
    uint usedHeight = min(segment.size, preset.height - segment.y);
    uint usedPixels = usedWidth * usedHeight;
    uint logicalPixels = segment.size * segment.size;
    uint scratchFloats = max(logicalPixels, 32u);
    threadgroup int *boundary = reinterpret_cast<threadgroup int *>(
        working + logicalPixels + scratchFloats);
    threadgroup int *topBoundary = boundary;
    threadgroup int *leftBoundary = boundary + segment.size;
    int corner = originalPlaneAt(planes, int(segment.x) - 1,
                                 int(segment.y) - 1, segment.channel, preset);

    for (uint offset = tid; offset < segment.size; offset += threadCount) {
        topBoundary[offset] = originalPlaneAt(
            planes, int(segment.x + offset), int(segment.y) - 1,
            segment.channel, preset);
        leftBoundary[offset] = originalPlaneAt(
            planes, int(segment.x) - 1, int(segment.y + offset),
            segment.channel, preset);
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    if (config.predictionMethod == 4 || config.predictionMethod == 5) {
        if (tid == 0u) {
            int dc = 0;
            for (uint offset = 0u; offset < segment.size; ++offset) {
                dc += leftBoundary[offset];
                dc += topBoundary[offset];
            }
            dc += corner;
            dc /= int(segment.size * 2u + 1u);
            dcValue = dc;
        }
        threadgroup_barrier(mem_flags::mem_threadgroup);
    }

    float quantization = float(config.quantizationValue) * 0.5f;
    if (config.originalWaveletId == 0u) {
        for (uint job = tid; job < usedPixels; job += threadCount) {
            uint x = job % usedWidth;
            uint y = job / usedWidth;
            uint index = planeBase + (segment.y + y) * preset.width +
                         segment.x + x;
            int prediction = originalPredictionCached(
                planes, segment, int(x), int(y), dcValue, corner,
                topBoundary, leftBoundary, preset);
            int residual = planes[index] - prediction;
            if (config.clampMethod == 1u)
                residual = residual < 0 ? residual + 256
                         : residual > 255 ? residual - 256 : residual;
            if (quantization > 1.0f)
                residual = originalProcessingRound(
                    float(originalProcessingRound(
                        float(residual) / quantization)) * quantization);
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

    uint size = segment.size;
    uint segmentShift = originalPowerOfTwoShift(size);
    uint segmentMask = size - 1u;
    threadgroup float *matrix = working;
    threadgroup float *scratch = working + logicalPixels;
    // Compression happens between transform phases, so scratch can be reused
    // as the deterministic per-thread reduction workspace.
    threadgroup float *reduction = scratch;
    float border = float(originalReference(preset, segment.channel)) / 255.0f;

    for (uint job = tid; job < usedPixels; job += threadCount) {
        uint x = job % usedWidth;
        uint y = job / usedWidth;
        uint planeIndex = planeBase + (segment.y + y) * preset.width +
                          segment.x + x;
        int prediction = originalPredictionCached(
            planes, segment, int(x), int(y), dcValue, corner, topBoundary,
            leftBoundary, preset);
        int residual = planes[planeIndex] - prediction;
        if (config.clampMethod == 1u)
            residual = residual < 0 ? residual + 256
                     : residual > 255 ? residual - 256 : residual;
        if (quantization > 1.0f)
            residual = originalProcessingRound(float(residual) / quantization);
        planes[planeIndex] = residual;
    }
    threadgroup_barrier(mem_flags::mem_device);

    for (uint job = tid; job < logicalPixels; job += threadCount) {
        uint x = job >> segmentShift;
        uint y = job & segmentMask;
        if (x < usedWidth && y < usedHeight) {
            uint planeIndex = planeBase + (segment.y + y) * preset.width +
                              segment.x + x;
            matrix[x * size + y] = float(planes[planeIndex]) / 255.0f;
        } else {
            matrix[x * size + y] = border;
        }
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    bool allPackets = config.transformType == 1u;
    threadgroup float *cdfSource = matrix;
    threadgroup float *cdfDestination = scratch;
    if (allPackets) {
        for (uint length = size; length >= 2u; length >>= 1u) {
            originalCdfPassThreadgroup(cdfSource, cdfDestination, size,
                                       false, false, length, true, tid,
                                       threadCount);
            threadgroup float *swap = cdfSource;
            cdfSource = cdfDestination;
            cdfDestination = swap;
        }
        for (uint length = size; length >= 2u; length >>= 1u) {
            originalCdfPassThreadgroup(cdfSource, cdfDestination, size, true,
                                       false, length, true, tid,
                                       threadCount);
            threadgroup float *swap = cdfSource;
            cdfSource = cdfDestination;
            cdfDestination = swap;
        }
    } else {
        for (uint length = size; length >= 2u; length >>= 1u)
            originalCdfPassThreadgroup(matrix, scratch, size, false, false,
                                       length, false, tid, threadCount);
        for (uint length = size; length >= 2u; length >>= 1u)
            originalCdfPassThreadgroup(matrix, scratch, size, true, false,
                                       length, false, tid, threadCount);
    }

    if (config.transformCompress > 0.0f) {
        if (logicalPixels <= 32u && tid == 0u) {
            float magnitude = 0.0f;
            float compensation = 0.0f;
            for (uint job = 0u; job < logicalPixels; ++job) {
                float corrected = abs(cdfSource[job]) - compensation;
                float next = magnitude + corrected;
                compensation = (next - magnitude) - corrected;
                magnitude = next;
            }
            reduction[0] = magnitude / float(logicalPixels) *
                           config.compressionThreshold;
        } else if (logicalPixels > 32u && tid < 32u) {
            float magnitude = 0.0f;
            float compensation = 0.0f;
            for (uint job = tid; job < logicalPixels; job += 32u) {
                float corrected = abs(cdfSource[job]) - compensation;
                float next = magnitude + corrected;
                compensation = (next - magnitude) - corrected;
                magnitude = next;
            }
            reduction[tid] = magnitude;
        }
        threadgroup_barrier(mem_flags::mem_threadgroup);
        if (logicalPixels > 32u) {
            for (uint stride = 16u; stride > 0u; stride >>= 1u) {
                if (tid < stride)
                    reduction[tid] += reduction[tid + stride];
                threadgroup_barrier(mem_flags::mem_threadgroup);
            }
            if (tid == 0u)
                reduction[0] = reduction[0] / float(logicalPixels) *
                               config.compressionThreshold;
            threadgroup_barrier(mem_flags::mem_threadgroup);
        }
        float cutoff = reduction[0];
        for (uint job = tid; job < logicalPixels; job += threadCount) {
            if (abs(cdfSource[job]) < cutoff) cdfSource[job] = 0.0f;
        }
        threadgroup_barrier(mem_flags::mem_threadgroup);
    }

    float scale = float(config.transformScale);
    for (uint job = tid; job < usedPixels; job += threadCount) {
        uint x = job % usedWidth;
        uint y = job / usedWidth;
        uint planeIndex = planeBase + (segment.y + y) * preset.width +
                          segment.x + x;
        planes[planeIndex] = originalProcessingRound(
            cdfSource[x * size + y] * scale / float(size));
    }
    threadgroup_barrier(mem_flags::mem_device);

    for (uint job = tid; job < logicalPixels; job += threadCount) {
        uint x = job >> segmentShift;
        uint y = job & segmentMask;
        if (x < usedWidth && y < usedHeight) {
            uint planeIndex = planeBase + (segment.y + y) * preset.width +
                              segment.x + x;
            matrix[x * size + y] =
                float(size) * float(planes[planeIndex]) / scale;
        } else {
            matrix[x * size + y] = border;
        }
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    cdfSource = matrix;
    cdfDestination = scratch;
    if (allPackets) {
        for (uint length = 2u; length <= size; length <<= 1u) {
            originalCdfPassThreadgroup(cdfSource, cdfDestination, size, true,
                                       true, length, true, tid, threadCount);
            threadgroup float *swap = cdfSource;
            cdfSource = cdfDestination;
            cdfDestination = swap;
        }
        for (uint length = 2u; length <= size; length <<= 1u) {
            originalCdfPassThreadgroup(cdfSource, cdfDestination, size,
                                       false, true, length, true, tid,
                                       threadCount);
            threadgroup float *swap = cdfSource;
            cdfSource = cdfDestination;
            cdfDestination = swap;
        }
    } else {
        for (uint length = 2u; length <= size; length <<= 1u)
            originalCdfPassThreadgroup(matrix, scratch, size, true, true,
                                       length, false, tid, threadCount);
        for (uint length = 2u; length <= size; length <<= 1u)
            originalCdfPassThreadgroup(matrix, scratch, size, false, true,
                                       length, false, tid, threadCount);
    }

    for (uint job = tid; job < usedPixels; job += threadCount) {
        uint x = job % usedWidth;
        uint y = job / usedWidth;
        uint planeIndex = planeBase + (segment.y + y) * preset.width +
                          segment.x + x;
        int value = originalProcessingRound(cdfSource[x * size + y] * 255.0f);
        value = config.clampMethod == 1u ? clamp(value, 0, 255)
                                         : clamp(value, -255, 255);
        if (quantization > 1.0f)
            value = originalProcessingRound(float(value) * quantization);
        planes[planeIndex] = value;
    }
    threadgroup_barrier(mem_flags::mem_device);

    for (uint job = tid; job < usedPixels; job += threadCount) {
        uint x = job % usedWidth;
        uint y = job / usedWidth;
        uint planeIndex = planeBase + (segment.y + y) * preset.width +
                          segment.x + x;
        int prediction = originalPredictionCached(
            planes, segment, int(x), int(y), dcValue, corner, topBoundary,
            leftBoundary, preset);
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
