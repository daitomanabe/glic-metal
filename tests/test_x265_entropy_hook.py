from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_x265_glitch_reference as builder  # noqa: E402
import native_syntax_glitch as syntax  # noqa: E402
import process_native_syntax_glitch as process  # noqa: E402


def main() -> int:
    patch, header = builder.find_assets(
        ROOT / "scripts" / "build_x265_glitch_reference.py"
    )
    assert patch.is_file() and header.is_file()
    patch_text = patch.read_text(encoding="utf-8")
    header_text = header.read_text(encoding="utf-8")
    assert "glicMutateMvd" in patch_text
    assert "glicMutateCoefficients" in patch_text
    assert "--unidiff-zero" in Path(builder.__file__).read_text(
        encoding="utf-8"
    )
    for effect in syntax.MOTION_EFFECTS + syntax.COEFFICIENT_EFFECTS:
        assert effect in header_text
        assert "hevc" in process.SUPPORTED_CODECS_BY_EFFECT[effect]

    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        binary = directory / "x265-glic-entropy"
        binary.write_bytes(b"synthetic verified binary")
        marker = Path(str(binary) + ".glic-hook.json")
        marker.write_text(
            json.dumps(
                {
                    "schema": process.X265_HOOK_SCHEMA,
                    "x265_commit": process.X265_HOOK_COMMIT,
                    "binary_sha256": hashlib.sha256(
                        binary.read_bytes()
                    ).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        assert process.load_x265_hook_contract(str(binary)) is not None
        binary.write_bytes(b"changed")
        assert process.load_x265_hook_contract(str(binary)) is None

        log = directory / "hook.log"
        log.write_text(
            "x265 output\n"
            "[glic-x265-hook] "
            "effect=compressed_coefficient_sign_flip "
            "candidates=120 selected=90 changed=44\n",
            encoding="utf-8",
        )
        evidence = process.parse_x265_hook_evidence(
            log,
            "compressed_coefficient_sign_flip",
            0.75,
            7,
        )
        assert evidence["feature"] == "hevc_cabac_quantized_coefficient"
        assert evidence["changed_values"] == 44
        assert evidence["late_entropy_injection"] is True

    command = process.x265_entropy_command(
        "x265", Path("source.y4m"), Path("output.hevc")
    )
    assert "--frame-threads" in command and "1" in command
    assert "--no-wpp" in command
    assert command[command.index("--pools") + 1] == "none"

    inherited_hook_environment = {
        "GLIC_X265_HOOK_EFFECT": "compressed_motion_vector_vortex",
        "GLIC_X265_HOOK_AMOUNT": "1",
        "GLIC_X265_HOOK_SEED": "9",
    }
    previous_values = {
        name: os.environ.get(name) for name in inherited_hook_environment
    }
    try:
        os.environ.update(inherited_hook_environment)
        clean_environment = process.clean_x265_environment()
        for name in inherited_hook_environment:
            assert name not in clean_environment
    finally:
        for name, value in previous_values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    print("PASS pinned x265 late-entropy hook contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
