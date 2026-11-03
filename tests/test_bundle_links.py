"""Offline archives must not retain the builder's temporary interpreter path."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "relativize_bundle.py"


def run(bundle):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(bundle)], capture_output=True, text=True
    )


def test_internal_absolute_alias_survives_moving_the_entire_bundle(tmp_path):
    bundle = tmp_path / "original"
    python = bundle / "runtime" / "python"
    python.mkdir(parents=True)
    target = python / "cpython-3.12.16"
    target.mkdir()
    (target / "marker").write_text("packaged interpreter")
    (python / "cpython-3.12").symlink_to(target, target_is_directory=True)
    (python / "relative").symlink_to("cpython-3.12.16", target_is_directory=True)
    assert run(bundle).returncode == 0
    assert not os.path.isabs(os.readlink(python / "cpython-3.12"))
    moved = tmp_path / "moved"
    bundle.rename(moved)
    for alias in ("cpython-3.12", "relative"):
        assert (
            moved / "runtime" / "python" / alias / "marker"
        ).read_text() == "packaged interpreter"


@pytest.mark.parametrize("relative", [False, True])
def test_links_to_external_files_cannot_enter_the_archive(tmp_path, relative):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    external = tmp_path / "external"
    external.write_text("outside")
    alias = bundle / "alias"
    alias.symlink_to("../external" if relative else external)
    assert run(bundle).returncode != 0
    assert external.read_text() == "outside"


def test_broken_alias_cannot_enter_the_archive(tmp_path):
    (tmp_path / "broken").symlink_to("missing")
    assert run(tmp_path).returncode != 0
