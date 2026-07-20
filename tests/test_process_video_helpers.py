#!/usr/bin/env python3
"""Unit checks for video-wrapper rate parsing and the explicit 30 fps gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "process_video.py"
SPEC = importlib.util.spec_from_file_location("glic_process_video", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def main() -> int:
    assert abs(MODULE.parse_frame_rate("30000/1001") - 29.97002997) < 1e-6
    assert MODULE.parse_frame_rate("30/1") == 30.0
    assert MODULE.parse_frame_rate("24") == 24.0
    assert MODULE.parse_frame_rate("0/0") == 0.0
    assert MODULE.parse_frame_rate("invalid") == 0.0
    assert MODULE.parse_frame_count("166") == 166
    assert MODULE.parse_frame_count("N/A") is None
    assert MODULE.parse_frame_count("0") is None
    assert MODULE.parse_frame_count("2000001") is None
    assert MODULE.first_valid_duration("N/A", "5.533333") == 5.533333
    assert MODULE.first_valid_duration(None, "nan", "0", "5.0") == 5.0
    assert MODULE.first_valid_duration("N/A", None) is None
    assert MODULE.estimate_frame_capacity("5.533333", 30.0) == 168
    assert MODULE.estimate_frame_capacity("N/A", 30.0) is None
    assert MODULE.select_frame_capacity("166", "5.533333", 30.0, False) == 168
    assert MODULE.select_frame_capacity("166", "N/A", 30.0, False) == 166
    assert MODULE.select_frame_capacity("166", "5.533333", 60.0, True) == 334
    assert MODULE.resolve_backend("original_visual", None, "darwin") == "metal"
    assert MODULE.resolve_backend("original_visual", "auto", "darwin") == "metal"
    assert MODULE.resolve_backend("original_visual", "auto", "linux") == "cpu"
    assert MODULE.resolve_backend("original_visual", "cpu", "darwin") == "cpu"
    assert MODULE.resolve_backend("compat_realtime", "auto", "linux") == "auto"

    common = {
        "width": 960,
        "height": 540,
        "frames": 166,
        "elapsed_seconds": 5.0,
    }
    assert MODULE.passes_end_to_end_average_30fps(
        **common, target_fps=30.0, output_fps=30.0
    )
    assert not MODULE.passes_end_to_end_average_30fps(
        **common, target_fps=24.0, output_fps=24.0
    )
    assert not MODULE.passes_end_to_end_average_30fps(
        **common, target_fps=30.0, output_fps=29.97002997
    )
    assert not MODULE.passes_end_to_end_average_30fps(
        **{**common, "elapsed_seconds": 6.0}, target_fps=30.0, output_fps=30.0
    )
    print("PASS process_video frame-rate helpers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
