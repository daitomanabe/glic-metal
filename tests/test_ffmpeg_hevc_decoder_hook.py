from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_ffmpeg_hevc_glitch_reference as builder  # noqa: E402
import native_syntax_glitch as syntax  # noqa: E402
import process_native_syntax_glitch as process  # noqa: E402


def main() -> int:
    patch, header, implementation = builder.find_assets(
        ROOT / "scripts" / "build_ffmpeg_hevc_glitch_reference.py"
    )
    assert patch.is_file() and header.is_file() and implementation.is_file()
    patch_text = patch.read_text(encoding="utf-8")
    implementation_text = implementation.read_text(encoding="utf-8")
    assert "ff_glic_hevc_mutate_mvd" in patch_text
    assert "ff_glic_hevc_mutate_quantized_coefficient" in patch_text
    assert "emits_mutated_hevc_bitstream" in Path(builder.__file__).read_text(
        encoding="utf-8"
    )
    for effect in syntax.MOTION_EFFECTS + syntax.COEFFICIENT_EFFECTS:
        assert effect in implementation_text

    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        binary = directory / "ffmpeg-glic-hevc-decoder"
        binary.write_bytes(b"synthetic verified binary")
        marker = Path(str(binary) + ".glic-hook.json")
        marker.write_text(
            json.dumps(
                {
                    "schema": process.FFMPEG_HEVC_DECODER_HOOK_SCHEMA,
                    "ffmpeg_commit": (
                        process.FFMPEG_HEVC_DECODER_HOOK_COMMIT
                    ),
                    "binary_sha256": hashlib.sha256(
                        binary.read_bytes()
                    ).hexdigest(),
                    "emits_mutated_hevc_bitstream": False,
                }
            ),
            encoding="utf-8",
        )
        assert (
            process.load_ffmpeg_hevc_decoder_hook_contract(str(binary))
            is not None
        )
        binary.write_bytes(b"changed")
        assert (
            process.load_ffmpeg_hevc_decoder_hook_contract(str(binary))
            is None
        )

        log = directory / "hook.log"
        log.write_text(
            "decoder output\n"
            "[glic-ffmpeg-hevc-hook] "
            "effect=compressed_motion_vector_mirror "
            "candidates=120 selected=90 changed=44\n",
            encoding="utf-8",
        )
        evidence = process.parse_ffmpeg_hevc_decoder_evidence(
            log,
            "compressed_motion_vector_mirror",
            0.75,
            7,
        )
        assert evidence["feature"] == "hevc_decoder_parsed_cabac_mvd"
        assert evidence["changed_values"] == 44
        assert evidence["source_reencoded"] is False
        assert evidence["emits_mutated_hevc_bitstream"] is False

    inherited_hook_environment = {
        "GLIC_FFMPEG_HEVC_HOOK_EFFECT": "compressed_motion_vector_vortex",
        "GLIC_FFMPEG_HEVC_HOOK_AMOUNT": "1",
        "GLIC_FFMPEG_HEVC_HOOK_SEED": "9",
    }
    previous_values = {
        name: os.environ.get(name) for name in inherited_hook_environment
    }
    try:
        os.environ.update(inherited_hook_environment)
        clean_environment = (
            process.clean_ffmpeg_hevc_decoder_environment()
        )
        for name in inherited_hook_environment:
            assert name not in clean_environment
    finally:
        for name, value in previous_values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    print("PASS pinned FFmpeg existing-HEVC decoder syntax hook contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
