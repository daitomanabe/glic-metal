from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_x264_glitch_reference as builder  # noqa: E402
import native_syntax_glitch as syntax  # noqa: E402
import process_native_syntax_glitch as process  # noqa: E402


def main() -> int:
    patch, header, implementation = builder.find_assets(
        ROOT / "scripts" / "build_x264_glitch_reference.py"
    )
    assert patch.is_file() and header.is_file() and implementation.is_file()
    patch_text = patch.read_text(encoding="utf-8")
    implementation_text = implementation.read_text(encoding="utf-8")
    assert "x264_glic_mutate_mvd" in patch_text
    assert "x264_glic_mutate_coefficients" in patch_text
    assert "RDO_SKIP_BS" in patch_text
    assert "--unidiff-zero" in Path(builder.__file__).read_text(
        encoding="utf-8"
    )
    for effect in syntax.MOTION_EFFECTS + syntax.COEFFICIENT_EFFECTS:
        assert effect in implementation_text
        assert "h264" in process.SUPPORTED_CODECS_BY_EFFECT[effect]

    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        binary = directory / "x264-glic-entropy"
        binary.write_bytes(b"synthetic verified binary")
        marker = Path(str(binary) + ".glic-hook.json")
        marker.write_text(
            json.dumps(
                {
                    "schema": process.X264_HOOK_SCHEMA,
                    "x264_commit": process.X264_HOOK_COMMIT,
                    "binary_sha256": hashlib.sha256(
                        binary.read_bytes()
                    ).hexdigest(),
                    "supported_entropy_modes": ["cabac", "cavlc"],
                }
            ),
            encoding="utf-8",
        )
        assert process.load_x264_hook_contract(str(binary)) is not None
        binary.write_bytes(b"changed")
        assert process.load_x264_hook_contract(str(binary)) is None

        log = directory / "hook.log"
        log.write_text(
            "x264 output\n"
            "[glic-x264-hook] "
            "effect=compressed_coefficient_sign_flip "
            "candidates=120 selected=90 changed=44\n",
            encoding="utf-8",
        )
        evidence = process.parse_x264_hook_evidence(
            log,
            "compressed_coefficient_sign_flip",
            0.75,
            7,
            "cavlc",
        )
        assert evidence["feature"] == "h264_cavlc_quantized_coefficient"
        assert evidence["changed_values"] == 44
        assert evidence["late_entropy_injection"] is True

    cabac = process.x264_entropy_command(
        "x264",
        Path("source.y4m"),
        Path("output.h264"),
        entropy_mode="cabac",
        fps=30,
    )
    cavlc = process.x264_entropy_command(
        "x264",
        Path("source.y4m"),
        Path("output.h264"),
        entropy_mode="cavlc",
        fps=30,
    )
    assert cabac[cabac.index("--threads") + 1] == "1"
    assert cabac[cabac.index("--lookahead-threads") + 1] == "1"
    assert "--sliced-threads" not in cabac
    assert "--no-cabac" not in cabac
    assert "--no-cabac" in cavlc

    inherited_hook_environment = {
        "GLIC_X264_HOOK_EFFECT": "compressed_motion_vector_vortex",
        "GLIC_X264_HOOK_AMOUNT": "1",
        "GLIC_X264_HOOK_SEED": "9",
    }
    previous_values = {
        name: os.environ.get(name) for name in inherited_hook_environment
    }
    try:
        os.environ.update(inherited_hook_environment)
        clean_environment = process.clean_x264_environment()
        for name in inherited_hook_environment:
            assert name not in clean_environment
    finally:
        for name, value in previous_values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    print("PASS pinned x264 CABAC/CAVLC late-entropy hook contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
