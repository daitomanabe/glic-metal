#!/bin/bash
# GLIC Preset Test Script
# Tests various presets with a sample image

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GLIC_ROOT="$(dirname "$SCRIPT_DIR")"
GLIC_BIN="${GLIC_ROOT}/build/glic"
OUTPUT_DIR="${GLIC_ROOT}/examples/output"

# Check if glic binary exists
if [ ! -f "$GLIC_BIN" ]; then
    echo "Error: glic binary not found at $GLIC_BIN"
    echo "Please build the project first: cd build && cmake .. && make"
    exit 1
fi

# Create output directory
mkdir -p "$OUTPUT_DIR"

# Check for input image
INPUT_IMAGE="$1"
if [ -z "$INPUT_IMAGE" ]; then
    echo "Usage: $0 <input_image.png>"
    echo ""
    echo "Example:"
    echo "  $0 photo.png"
    exit 1
fi

if [ ! -f "$INPUT_IMAGE" ]; then
    echo "Error: Input image not found: $INPUT_IMAGE"
    exit 1
fi

# Get base name without extension
BASENAME=$(basename "$INPUT_IMAGE" | sed 's/\.[^.]*$//')

echo "============================================"
echo "GLIC Preset Test"
echo "============================================"
echo "Input: $INPUT_IMAGE"
echo "Output directory: $OUTPUT_DIR"
echo ""

# List of presets to test
PRESETS=(
    "default"
    "colour_waves"
    "cubism"
    "8-b1tz"
    "bl33dyl1n3z"
    "high_compression"
    "abstract_expressionism"
    "blocks"
    "scanlined"
    "webp"
)

for preset in "${PRESETS[@]}"; do
    echo "--------------------------------------------"
    echo "Testing preset: $preset"
    echo "--------------------------------------------"

    GLIC_FILE="${OUTPUT_DIR}/${BASENAME}_${preset}.glic"
    PNG_FILE="${OUTPUT_DIR}/${BASENAME}_${preset}.png"

    # Encode
    echo "  Encoding..."
    "$GLIC_BIN" encode "$INPUT_IMAGE" "$GLIC_FILE" --preset "$preset"

    # Decode
    echo "  Decoding..."
    "$GLIC_BIN" decode "$GLIC_FILE" "$PNG_FILE"

    # Show file sizes
    GLIC_SIZE=$(ls -lh "$GLIC_FILE" | awk '{print $5}')
    PNG_SIZE=$(ls -lh "$PNG_FILE" | awk '{print $5}')
    echo "  Output: $PNG_FILE ($PNG_SIZE)"
    echo ""
done

echo "============================================"
echo "All presets tested successfully!"
echo "Output files are in: $OUTPUT_DIR"
echo "============================================"
