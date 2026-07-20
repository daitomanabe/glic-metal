#!/bin/bash
# GLIC Quick Start Examples
# Demonstrates basic usage of GLIC with presets

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GLIC_ROOT="$(dirname "$SCRIPT_DIR")"
GLIC_BIN="${GLIC_ROOT}/build/glic"

# Check if glic binary exists
if [ ! -f "$GLIC_BIN" ]; then
    echo "Error: glic binary not found at $GLIC_BIN"
    echo "Please build the project first:"
    echo "  cd ${GLIC_ROOT}/build && cmake .. && make"
    exit 1
fi

echo "============================================"
echo "GLIC Quick Start"
echo "============================================"
echo ""

# 1. List available presets
echo "1. List available presets:"
echo "   \$ ./build/glic --list-presets"
echo ""
"$GLIC_BIN" --list-presets | head -20
echo "   ... (144 presets total)"
echo ""

# 2. Show help
echo "2. Show help:"
echo "   \$ ./build/glic --help"
echo ""

# 3. Example commands
echo "3. Example commands:"
echo ""
echo "   # Basic encode/decode"
echo "   ./build/glic encode input.png output.glic"
echo "   ./build/glic decode output.glic result.png"
echo ""
echo "   # Encode with preset"
echo "   ./build/glic encode input.png output.glic --preset colour_waves"
echo "   ./build/glic encode input.png output.glic --preset cubism"
echo "   ./build/glic encode input.png output.glic --preset 8-b1tz"
echo ""
echo "   # Encode with custom options"
echo "   ./build/glic encode input.png output.glic --colorspace YUV --prediction SPIRAL"
echo ""
echo "   # Decode with post effects"
echo "   ./build/glic decode output.glic result.png --effect scanline --effect chromatic"
echo ""
echo "============================================"
echo "For more examples, run: ./examples/test_presets.sh <input.png>"
echo "============================================"
