"""The test harness must catch an accidental default Install resolution."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from venator.paths import install_data_dir, install_stores


def test_default_store_and_home_paths_are_isolated() -> None:
    stores = install_stores()
    assert stores.path == Path(os.environ["VENATOR_HOME"])
    assert stores.path.is_relative_to(Path(os.environ["HOME"]).parent)
    assert install_data_dir({"HOME": os.environ["HOME"]}, platform="darwin").is_relative_to(
        Path(os.environ["HOME"])
    )
    assert install_data_dir({"HOME": os.environ["HOME"]}, platform="linux").is_relative_to(
        Path(os.environ["HOME"])
    )


def test_guard_rejects_a_fake_persons_install_without_opening_it(
    tmp_path: Path, no_real_install_resolution: Callable[[Path | None, tuple[Path, ...]], None],
) -> None:
    home = tmp_path / "fake-person"
    fake_install = install_data_dir({"HOME": str(home)}, platform="darwin")
    assert fake_install is not None
    with pytest.raises(pytest.fail.Exception, match="real Install"):
        no_real_install_resolution(fake_install / "data" / "track", (fake_install,))


def test_track_status_child_never_reads_the_fake_default_install(tmp_path: Path) -> None:
    """An unstated --track-dir must not reach a claimed Track store under HOME."""
    fake_home = tmp_path / "fake-home"
    fake_install = install_data_dir({"HOME": str(fake_home)}, platform=sys.platform)
    assert fake_install is not None
    track = fake_install / "data" / "track"
    track.mkdir(parents=True)
    (track / ".profile").write_text("somebody-else\n", encoding="utf-8")
    postings = tmp_path / "postings"
    decisions = tmp_path / "decisions"
    postings.mkdir()
    decisions.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "venator.track.status", "--profile", "example",
         "--postings-dir", str(postings), "--decisions-dir", str(decisions)],
        env={**os.environ, "HOME": str(fake_home), "XDG_DATA_HOME": ""},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
