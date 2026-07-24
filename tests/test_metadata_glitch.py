from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "process_metadata_glitch",
    ROOT / "scripts" / "process_metadata_glitch.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def main() -> int:
    assert MODULE.EFFECTS == (
        "color_vui_oscillator",
        "hdr_metadata_pulse",
    )
    assert MODULE.SUPPORT["hdr_metadata_pulse"] == {"hevc"}
    profiles = MODULE.metadata_profiles("color_vui_oscillator")
    assert {profile["name"] for profile in profiles} == {
        "bt709_limited",
        "bt2020_pq_limited",
        "smpte170m_full",
    }
    h264 = MODULE.segment_encoder("h264", profiles[0], 30, 30)
    assert "-bsf:v" in h264
    assert any("h264_metadata=" in value for value in h264)
    hdr = MODULE.metadata_profiles("hdr_metadata_pulse")[1]
    hevc = MODULE.segment_encoder("hevc", hdr, 30, 30)
    assert any("master-display=" in value for value in hevc)
    assert any("hevc_metadata=" in value for value in hevc)
    assert all(
        "native_" in level
        for level in MODULE.IMPLEMENTATION_LEVEL.values()
    )
    assert "zscale=" in MODULE.DISPLAY_NORMALIZATION_FILTER
    assert "contrast=0.82" in MODULE.DISPLAY_NORMALIZATION_FILTER
    assert "colorlevels=" in MODULE.DISPLAY_NORMALIZATION_FILTER
    print("PASS temporal metadata profiles and native field labels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
