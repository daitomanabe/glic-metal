#include "quantization.hpp"
#include "processing_math.hpp"
#include <cmath>

namespace glic {

void quantize(Planes& planes, int channel, const Segment& segment, float val, bool forward) {
    if (val <= 1) return;

    for (int x = 0; x < segment.size; x++) {
        for (int y = 0; y < segment.size; y++) {
            float col = static_cast<float>(planes.get(channel, x + segment.x, y + segment.y));

            if (forward) {
                col = col / val;
            } else {
                col = col * val;
            }

            planes.set(channel, x + segment.x, y + segment.y,
                       processingRound(col));
        }
    }
}

} // namespace glic
