#!/usr/bin/env python3
"""Verify that the original-Metal manifest refuses untracked source files."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile


def run(*arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    writer = root / "scripts" / "write_original_metal_manifest.py"

    with tempfile.TemporaryDirectory(prefix="glic-metal-manifest-test-") as text:
        work = Path(text)
        repository = work / "repo"
        build = work / "build"
        output = work / "output"
        repository.mkdir()
        build.mkdir()
        output.mkdir()

        initialized = run("git", "init", "-q", str(repository))
        if initialized.returncode != 0:
            raise AssertionError(initialized.stdout + initialized.stderr)
        (repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        staged = run("git", "add", "tracked.txt", cwd=repository)
        if staged.returncode != 0:
            raise AssertionError(staged.stdout + staged.stderr)
        committed = run(
            "git",
            "-c",
            "user.name=GLIC Test",
            "-c",
            "user.email=glic-test@example.invalid",
            "commit",
            "-q",
            "-m",
            "baseline",
            cwd=repository,
        )
        if committed.returncode != 0:
            raise AssertionError(committed.stdout + committed.stderr)

        command = (
            sys.executable,
            str(writer),
            "--repo-root",
            str(repository),
            "--build-dir",
            str(build),
            "--output-dir",
            str(output),
        )

        clean = run(*command)
        if (
            clean.returncode != 1
            or "certified benchmark artifact does not exist" not in clean.stdout
        ):
            raise AssertionError(
                "clean repository did not pass the source-status gate:\n"
                + clean.stdout
                + clean.stderr
            )

        source = repository / "src"
        source.mkdir()
        (source / "untracked.hpp").write_text("#pragma once\n", encoding="utf-8")
        dirty = run(*command)
        if dirty.returncode != 1:
            raise AssertionError("untracked source was certified")
        if "refusing to certify a dirty source tree" not in dirty.stdout:
            raise AssertionError(dirty.stdout + dirty.stderr)
        if "?? src/untracked.hpp" not in dirty.stdout:
            raise AssertionError(
                "dirty-source diagnostic omitted the untracked file:\n"
                + dirty.stdout
                + dirty.stderr
            )
        if "certified benchmark artifact does not exist" in dirty.stdout:
            raise AssertionError("manifest advanced past the dirty-source gate")

    print("PASS original Metal manifest rejects untracked source")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
