from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def command_available(name: str) -> bool:
    return (
        subprocess.run(
            ["sh", "-c", f"command -v {name}"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def main() -> int:
    if not all(command_available(name) for name in ("ffmpeg", "pkg-config")):
        print("SKIP codec motion extractor dependencies are unavailable")
        return 0
    if (
        subprocess.run(
            [
                "pkg-config",
                "--exists",
                "libavformat",
                "libavcodec",
                "libavutil",
            ],
            check=False,
        ).returncode
        != 0
    ):
        print("SKIP FFmpeg development packages are unavailable")
        return 0

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        build = root / "tools"
        subprocess.run(
            [str(ROOT / "scripts" / "build_codec_lab_native.sh"), str(build)],
            check=True,
        )
        video = root / "motion.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=160x90:rate=12:duration=1",
                "-vf",
                "scroll=horizontal=0.02",
                "-c:v",
                "libx264",
                "-bf",
                "2",
                "-g",
                "12",
                str(video),
            ],
            check=True,
        )
        result = subprocess.run(
            [str(build / "glic_extract_mvs"), str(video)],
            check=True,
            capture_output=True,
            text=True,
        )
        frames = [json.loads(line) for line in result.stdout.splitlines()]
        assert len(frames) == 12
        assert frames[0]["pict_type"] == "I"
        vector_frames = [frame for frame in frames if frame["vectors"]]
        assert vector_frames
        vector = vector_frames[0]["vectors"][0]
        assert {
            "source",
            "w",
            "h",
            "src_x",
            "src_y",
            "dst_x",
            "dst_y",
            "motion_x",
            "motion_y",
            "motion_scale",
            "flags",
        } <= vector.keys()

    print("PASS native decoder motion-vector extraction")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
