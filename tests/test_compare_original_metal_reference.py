#!/usr/bin/env python3
"""Smoke-test numeric and morphology gates for Metal reference comparison."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile

import cv2
import numpy as np


def fnv1a64_file(path: Path) -> str:
    value = 14695981039346656037
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            for byte in chunk:
                value ^= byte
                value = (value * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def make_fixture() -> np.ndarray:
    image = np.zeros((96, 128, 3), dtype=np.uint8)
    for y in range(image.shape[0]):
        for x in range(image.shape[1]):
            band = ((x // 8) + (y // 12)) & 3
            image[y, x] = (
                40 + ((band * 23 + x * 2 + y) % 100),
                60 + ((band * 31 + x + y * 2) % 100),
                90 + ((band * 17 + x + y) % 100),
            )
    return image


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "compare_original_metal_reference.py"
    with tempfile.TemporaryDirectory(prefix="glic-metal-reference-test-") as text:
        work = Path(text)
        cpu = work / "cpu"
        metal = work / "metal"
        cpu.mkdir()
        metal.mkdir()
        fixture = make_fixture()
        cv2.imwrite(str(cpu / "integer.png"), fixture)
        cv2.imwrite(str(metal / "integer.png"), fixture)
        cv2.imwrite(str(cpu / "cdf.png"), fixture)
        # Move along an integer B/R chroma axis that keeps OpenCV luma within
        # one code value (29 * +77 + 77 * -29 == 0). This deliberately fails
        # the RGB numeric gate while preserving edges and block morphology.
        cdf_candidate = fixture.astype(np.int16)
        cdf_candidate[:, :, 0] += 77
        cdf_candidate[:, :, 2] -= 29
        cv2.imwrite(str(metal / "cdf.png"), cdf_candidate.astype(np.uint8))

        benchmark = work / "benchmark.json"
        cpu_benchmark = work / "cpu-benchmark.json"
        provenance = {
            "input_decoded_color_fnv1a64": "0123456789abcdef",
            "width": 128,
            "height": 96,
            "frames": 120,
            "warmup_frames": 10,
            "output_preview_semantics": "last_measured_frame",
            "output_preview_frame_index": 129,
        }
        default_config_hashes = {
            "integer": "1111111111111111",
            "cdf": "2222222222222222",
        }

        def rows(directory: Path, config_hashes: dict[str, str]) -> list[dict]:
            return [
                {
                    "preset": preset,
                    "uses_cdf97": uses_cdf97,
                    "preset_config_fnv1a64": config_hashes[preset],
                    "output_preview_file_fnv1a64": fnv1a64_file(
                        directory / f"{preset}.png"
                    ),
                }
                for preset, uses_cdf97 in (("integer", False), ("cdf", True))
            ]

        def write_benchmarks(
            *,
            cpu_input_hash: str = "0123456789abcdef",
            cpu_config_hashes: dict[str, str] | None = None,
        ) -> None:
            benchmark.write_text(
                json.dumps(
                    {
                        **provenance,
                        "schema": "glic-original-realtime-metal-benchmark-v1",
                        "results": rows(metal, default_config_hashes),
                    }
                ),
                encoding="utf-8",
            )
            cpu_benchmark.write_text(
                json.dumps(
                    {
                        **provenance,
                        "schema": "glic-original-realtime-cpu-benchmark-v1",
                        "input_decoded_color_fnv1a64": cpu_input_hash,
                        "results": rows(
                            cpu, cpu_config_hashes or default_config_hashes
                        ),
                    }
                ),
                encoding="utf-8",
            )

        write_benchmarks()
        report = work / "comparison.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--cpu-dir",
                str(cpu),
                "--metal-dir",
                str(metal),
                "--cpu-benchmark",
                str(cpu_benchmark),
                "--benchmark",
                str(benchmark),
                "--output-json",
                str(report),
                "--cdf-min-blurred-ssim",
                "0.0",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stdout + completed.stderr)
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["schema"] == "glic-original-metal-reference-comparison-v1"
        assert payload["passed_count"] == 1
        assert payload["failed_presets"] == ["cdf"]
        assert payload["style_passed_count"] == 2
        assert payload["original_style_match_passed"] is True
        assert payload["provenance_match_verified"] is True

        # Reordering 32 px tiles preserves global blur/orientation/edge-energy
        # statistics, but must fail the aligned spatial-edge morphology gate.
        shuffled = np.empty_like(fixture)
        coordinates = [
            (y, x)
            for y in range(0, fixture.shape[0], 32)
            for x in range(0, fixture.shape[1], 32)
        ]
        tiles = [
            fixture[y : y + 32, x : x + 32].copy()
            for y, x in coordinates
        ]
        for (y, x), tile in zip(coordinates, reversed(tiles)):
            shuffled[y : y + tile.shape[0], x : x + tile.shape[1]] = tile
        cv2.imwrite(str(metal / "cdf.png"), shuffled)
        write_benchmarks()
        shuffled_result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--cpu-dir",
                str(cpu),
                "--metal-dir",
                str(metal),
                "--cpu-benchmark",
                str(cpu_benchmark),
                "--benchmark",
                str(benchmark),
                "--output-json",
                str(report),
                "--cdf-min-blurred-ssim",
                "0.0",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert shuffled_result.returncode == 1
        shuffled_payload = json.loads(report.read_text(encoding="utf-8"))
        assert shuffled_payload["style_failed_presets"] == ["cdf"]

        write_benchmarks(cpu_input_hash="fedcba9876543210")
        mismatch_result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--cpu-dir",
                str(cpu),
                "--metal-dir",
                str(metal),
                "--cpu-benchmark",
                str(cpu_benchmark),
                "--benchmark",
                str(benchmark),
                "--output-json",
                str(report),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert mismatch_result.returncode == 2
        assert "provenance mismatch" in mismatch_result.stdout
        write_benchmarks(
            cpu_config_hashes={
                **default_config_hashes,
                "cdf": "3333333333333333",
            }
        )
        config_mismatch_result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--cpu-dir",
                str(cpu),
                "--metal-dir",
                str(metal),
                "--cpu-benchmark",
                str(cpu_benchmark),
                "--benchmark",
                str(benchmark),
                "--output-json",
                str(report),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert config_mismatch_result.returncode == 2
        assert "preset configuration differs" in config_mismatch_result.stdout

        write_benchmarks()
        stale_preview = shuffled.copy()
        stale_preview[0, 0, 0] ^= 1
        cv2.imwrite(str(metal / "cdf.png"), stale_preview)
        preview_mismatch_result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--cpu-dir",
                str(cpu),
                "--metal-dir",
                str(metal),
                "--cpu-benchmark",
                str(cpu_benchmark),
                "--benchmark",
                str(benchmark),
                "--output-json",
                str(report),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert preview_mismatch_result.returncode == 2
        assert "preview FNV-1a64 mismatch" in preview_mismatch_result.stdout

        cv2.imwrite(str(metal / "cdf.png"), shuffled)
        write_benchmarks()

        benchmark.write_text(
            json.dumps(
                {
                    **provenance,
                    "schema": "glic-original-realtime-metal-benchmark-v1",
                    "results": [],
                }
            ),
            encoding="utf-8",
        )
        empty_result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--cpu-dir",
                str(cpu),
                "--metal-dir",
                str(metal),
                "--cpu-benchmark",
                str(cpu_benchmark),
                "--benchmark",
                str(benchmark),
                "--output-json",
                str(report),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert empty_result.returncode == 2
        assert "contains no preset results" in empty_result.stdout

    print("PASS original Metal numeric/style comparison gates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
