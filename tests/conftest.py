"""Keep every test process and its children off a person's Install."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from venator import paths


def _real_install_roots(environ: dict[str, str]) -> tuple[Path, ...]:
    """Compute protected roots from the inherited environment without opening them."""
    without_override = {key: value for key, value in environ.items() if key != paths.HOME_ENV}
    roots = [paths.install_data_dir(environ), paths.install_data_dir(without_override)]
    # On macOS the first path is the real default; include the platform's
    # alternate roots so a test that overrides platform cannot reach them either.
    for platform in ("darwin", "linux", "win32"):
        roots.append(paths.install_data_dir(without_override, platform=platform))
    return tuple(root for root in roots if root is not None)


def _assert_not_real_install(path: Path | None, protected: tuple[Path, ...]) -> None:
    if path is not None and any(path == root or root in path.parents for root in protected):
        pytest.fail(f"a test resolved the real Install: {path}")


@pytest.fixture(scope="session", autouse=True)
def isolated_install(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, ...]]:
    inherited = dict(os.environ)
    protected = _real_install_roots(inherited)
    root = tmp_path_factory.mktemp("isolated-install")
    with pytest.MonkeyPatch.context() as patch:
        # Playwright resolves its installed binaries through HOME by default.
        # Preserve the browser cache, not the application-data directory.
        if "PLAYWRIGHT_BROWSERS_PATH" not in inherited and inherited.get("HOME"):
            cache = (Path(inherited["HOME"]) / "Library" / "Caches" / "ms-playwright"
                     if sys.platform == "darwin" else
                     Path(inherited["HOME"]) / ".cache" / "ms-playwright")
            patch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
        patch.setenv(paths.HOME_ENV, str(root / "Install"))
        patch.setenv("HOME", str(root / "home"))
        patch.setenv("USERPROFILE", str(root / "home"))
        patch.setenv("XDG_DATA_HOME", str(root / "xdg"))
        patch.setenv("APPDATA", str(root / "appdata"))
        yield protected


@pytest.fixture(autouse=True)
def no_real_install_resolution(
    isolated_install: tuple[Path, ...], monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[Path | None, tuple[Path, ...]], None]]:
    """Catch accidental default-path reads before the caller can touch the store."""
    original = paths.install_data_dir

    def checked(environ: dict[str, str] | None = None, *, platform: str | None = None) -> Path | None:
        result = original(environ, platform=platform)
        _assert_not_real_install(result, isolated_install)
        return result

    monkeypatch.setattr(paths, "install_data_dir", checked)
    # Also assert process defaults at the boundaries of tests that change HOME.
    _assert_not_real_install(original(), isolated_install)
    yield _assert_not_real_install
