from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "process_multicodec_glitch",
    ROOT / "scripts" / "process_multicodec_glitch.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def main() -> int:
    assert MODULE.CODECS == (
        "av1",
        "av2",
        "hevc",
        "vp9",
        "prores",
        "vvc",
        "theora",
        "dirac",
    )
    assert MODULE.SOFTWARE_EFFECTS == (
        "generation_cascade",
        "temporal_echo",
        "chroma_drift",
        "residual_noise",
    )
    assert MODULE.NATIVE_EFFECT_MAP["temporal_echo"] == "temporal_polyphony"
    assert MODULE.NATIVE_EFFECT_MAP["chroma_drift"] == "chroma_codec_echo"
    av1 = MODULE.ffmpeg_encoder("av1", 44, 30, 8)
    vp9 = MODULE.ffmpeg_encoder("vp9", 44, 30, 8)
    theora = MODULE.ffmpeg_encoder("theora", 4, 30, 8)
    dirac = MODULE.ffmpeg_encoder("dirac", 4_000_000, 30, 8)
    assert av1[av1.index("-c:v") + 1] == "libaom-av1"
    assert "-row-mt" in av1
    assert vp9[vp9.index("-c:v") + 1] == "libvpx-vp9"
    assert "realtime" in vp9
    assert theora[theora.index("-c:v") + 1] == "libtheora"
    assert dirac[dirac.index("-c:v") + 1] == "vc2"
    assert MODULE.ffmpeg_container("theora") == (".ogv", "theora")
    assert MODULE.ffmpeg_container("dirac") == (".mkv", "dirac")
    assert MODULE.effect_filter("generation_cascade", 0.5, 1) == "null"
    assert MODULE.effect_filter("temporal_echo", 0.8, 2).startswith("tmix=")
    assert MODULE.effect_filter("chroma_drift", 0.8, 2).startswith("chromashift=")
    assert MODULE.effect_filter("residual_noise", 0.8, 2).startswith("noise=")
    assert 0 <= MODULE.quality_for("av2", 0.0, 1) <= 255
    assert 0 <= MODULE.quality_for("av2", 1.0, 3) <= 255
    assert 0 <= MODULE.quality_for("av1", 1.0, 3) <= 63
    print("PASS multi-codec command and effect helpers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
