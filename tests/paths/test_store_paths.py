"""Where a store lands when nobody names one.

`data/postings`, `data/decisions`, `data/track`, the scheduler heartbeat and
`build/venator.db` used to be literal relative paths, so they resolved against
whatever directory the process happened to start in. Two directories meant two
half-populated stores and no error anywhere — `cd ui && python -m
venator.view.build` wrote `ui/build/venator.db` while the dashboard read the
checkout's own. `data/` is append-only (ADR-0001), so every row that lands in
the wrong one stays there.

The rule under test: the Install's application data directory wins regardless
of the working directory. With no application data directory, do not guess.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from venator.paths import (
    HOME_ENV,
    INSTALL_RULE,
    StoreRootError,
    install_stores,
    resolve_store_paths,
)

LINUX, MACOS, WINDOWS = "linux", "darwin", "win32"

#: How this host's `pathlib` writes a separator, and a path it agrees is
#: absolute. Both are stated here rather than asked of `Path`, for the reason
#: `tests/profile/test_install_paths.py` gives at greater length: an expectation
#: assembled with the call under test agrees with that call whatever it does.
#:
#: `/data/xdg` is the one that actually bites. It is absolute on Unix and
#: **relative** on Windows, where a rooted path with no drive letter is not
#: absolute — and `install_data_dir` asks `Path.is_absolute`, so the two hosts
#: send it down different branches. No Install ever sees that, because the Linux
#: branch is only ever taken on Linux; this file sees it, because it runs the
#: Linux branch wherever CI happens to be.
SEPARATOR = "\\" if os.name == "nt" else "/"
ABSOLUTE_XDG = "D:\\data\\xdg" if os.name == "nt" else "/data/xdg"


def checkout(root: Path, *, name: str = "venator") -> Path:
    """A directory shaped like a Venator source checkout."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    package = root / "src" / "venator"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    return root.resolve()


def work_tree(root: Path, *, stores: bool = True) -> Path:
    """A Git work tree that is somebody else's project."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    if stores:
        (root / "data" / "postings").mkdir(parents=True, exist_ok=True)
    return root.resolve()


def linux(home: Path | str) -> dict[str, str]:
    return {"HOME": str(home)}


# --------------------------------------------------------------------------
# Inside a Venator checkout
# --------------------------------------------------------------------------


def test_every_directory_in_a_checkout_resolves_to_one_store(tmp_path: Path) -> None:
    """The defect, stated as a test: two working directories, one store.

    Before this rule, `ui/` and `src/venator/` each grew a `data/` of their own.
    """
    root = checkout(tmp_path / "checkout")
    (root / "ui").mkdir()
    (root / "src" / "venator" / "match").mkdir(parents=True)

    from_root = install_stores(linux(tmp_path / "home"), platform=LINUX, start=root)
    from_ui = install_stores(linux(tmp_path / "home"), platform=LINUX, start=root / "ui")
    from_deep = install_stores(
        linux(tmp_path / "home"), platform=LINUX, start=root / "src" / "venator" / "match"
    )

    assert from_root.rule == INSTALL_RULE
    assert from_root.postings_dir == from_ui.postings_dir == from_deep.postings_dir
    assert from_root.database_path == from_ui.database_path == from_deep.database_path
    assert from_root.postings_dir == tmp_path / "home/.local/share/venator/data/postings"
    assert from_root.database_path == tmp_path / "home/.local/share/venator/build/venator.db"


def test_explicit_home_selects_another_install(tmp_path: Path) -> None:
    root = checkout(tmp_path / "checkout")
    (root / "data" / "postings").mkdir(parents=True)
    environ = {HOME_ENV: str(tmp_path / "elsewhere"), "HOME": str(tmp_path / "home")}

    stores = install_stores(environ, platform=LINUX, start=root)

    assert stores.rule == INSTALL_RULE
    assert stores.postings_dir == tmp_path / "elsewhere" / "data" / "postings"
    assert stores.database_path == tmp_path / "elsewhere" / "build" / "venator.db"
    assert stores.path != root


def test_install_wins_over_checkout_data(tmp_path: Path) -> None:
    root = checkout(tmp_path / "checkout")
    (root / "data/postings").mkdir(parents=True)
    stores = install_stores(linux(tmp_path / "home"), platform=LINUX, start=root)
    assert stores.rule == INSTALL_RULE
    assert stores.path == tmp_path / "home/.local/share/venator"


# --------------------------------------------------------------------------
# Outside any checkout
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "environ", "expected"),
    [
        (LINUX, {"HOME": "/home/sample"}, Path("/home/sample/.local/share/venator")),
        (
            LINUX,
            {"HOME": "/home/sample", "XDG_DATA_HOME": ABSOLUTE_XDG},
            Path(f"{ABSOLUTE_XDG}{SEPARATOR}venator"),
        ),
        (
            MACOS,
            {"HOME": "/mock-macos-home/sample"},
            Path("/mock-macos-home/sample/Library/Application Support/Venator"),
        ),
        # A `Path` comparison says nothing about which separator the answer was
        # written with — `WindowsPath` normalises both spellings to the same
        # value — so the separator is not asserted here at all. It is asserted
        # once, as a literal per host, where the answer is actually built:
        # `tests/profile/test_install_paths.py`, which `install_stores` calls.
        (
            WINDOWS,
            {"APPDATA": r"C:\Users\sample\AppData\Roaming"},
            Path(f"C:\\Users\\sample\\AppData\\Roaming{SEPARATOR}Venator"),
        ),
        (LINUX, {"HOME": "/home/sample", HOME_ENV: "/opt/venator"}, Path("/opt/venator")),
    ],
)
def test_a_shipped_install_keeps_its_stores_in_the_application_data_directory(
    tmp_path: Path, platform: str, environ: dict[str, str], expected: Path
) -> None:
    outside = tmp_path / "somewhere"
    outside.mkdir()

    stores = install_stores(environ, platform=platform, start=outside)

    assert stores.rule == INSTALL_RULE
    assert stores.path == expected
    assert stores.postings_dir == expected / "data" / "postings"
    assert stores.decisions_dir == expected / "data" / "decisions"
    assert stores.track_dir == expected / "data" / "track"
    assert stores.heartbeat_path == expected / "data" / "runs.jsonl"
    assert stores.database_path == expected / "build" / "venator.db"


def test_an_unrelated_work_tree_with_no_store_in_it_is_not_ambiguous(tmp_path: Path) -> None:
    """A home directory kept in Git is common and must not stop every run."""
    dotfiles = work_tree(tmp_path / "home", stores=False)

    stores = install_stores(linux(dotfiles), platform=LINUX, start=dotfiles / "notes")

    assert stores.rule == INSTALL_RULE
    assert stores.path == dotfiles / ".local" / "share" / "venator"


def test_no_home_refuses_to_write_to_working_directory(tmp_path: Path) -> None:
    """Without a resolvable Install, personal stores must not land in the checkout."""
    outside = (tmp_path / "somewhere")
    outside.mkdir()
    outside = outside.resolve()

    with pytest.raises(StoreRootError, match="refusing to write personal stores"):
        install_stores({}, platform=LINUX, start=outside)


# --------------------------------------------------------------------------
# Other Git work trees also use the Install
# --------------------------------------------------------------------------


def test_an_unrelated_work_tree_holding_a_store_still_uses_the_install(tmp_path: Path) -> None:
    other = work_tree(tmp_path / "other-project")
    home = tmp_path / "home"

    stores = install_stores(linux(home), platform=LINUX, start=other)
    assert stores.path == home / ".local/share/venator"


def test_subdirectory_of_another_work_tree_uses_the_install(tmp_path: Path) -> None:
    other = work_tree(tmp_path / "other-project")
    (other / "deep" / "deeper").mkdir(parents=True)

    stores = install_stores(linux(tmp_path / "home"), platform=LINUX, start=other / "deep" / "deeper")
    assert stores.rule == INSTALL_RULE


def test_a_venator_checkout_inside_a_work_tree_uses_the_install(tmp_path: Path) -> None:
    root = work_tree(tmp_path / "checkout")
    checkout(root)

    stores = install_stores(linux(tmp_path / "home"), platform=LINUX, start=root)

    assert stores.rule == INSTALL_RULE


# --------------------------------------------------------------------------
# Explicit paths, and saying which rule decided
# --------------------------------------------------------------------------


def test_an_explicit_path_is_returned_untouched_and_announces_nothing(tmp_path: Path) -> None:
    stream = io.StringIO()
    named = tmp_path / "run" / "marketing" / "postings"

    resolved = resolve_store_paths(postings_dir=named, stream=stream, environ={}, start=tmp_path)

    assert resolved == {"postings_dir": named}
    assert stream.getvalue() == ""


def test_naming_every_store_never_resolves_a_root_at_all(tmp_path: Path) -> None:
    """When every store path is explicit, no default Install is needed."""
    other = work_tree(tmp_path / "other-project")

    resolved = resolve_store_paths(
        postings_dir=other / "p",
        decisions_dir=other / "d",
        environ=linux(tmp_path / "home"),
        start=other,
        stream=io.StringIO(),
    )

    assert resolved["postings_dir"] == other / "p"


def test_one_missing_path_is_filled_in_and_the_rest_are_left_alone(tmp_path: Path) -> None:
    root = checkout(tmp_path / "checkout")
    stream = io.StringIO()

    resolved = resolve_store_paths(
        postings_dir=tmp_path / "mine",
        decisions_dir=None,
        environ=linux(tmp_path / "home"),
        platform=LINUX,
        start=root,
        stream=stream,
    )

    assert resolved["postings_dir"] == tmp_path / "mine"
    assert resolved["decisions_dir"] == tmp_path / "home/.local/share/venator/data/decisions"
    assert stream.getvalue().count("\n") == 1


def test_a_store_that_does_not_exist_names_a_typo_rather_than_defaulting() -> None:
    with pytest.raises(AttributeError):
        resolve_store_paths(postings_directory=None)


def test_the_resolved_root_is_said_once(tmp_path: Path) -> None:
    """The silent half of the defect is the bad half.

    One line on stderr is what turns "my Postings went somewhere else" into
    something a person notices on the first run rather than the fiftieth.
    """
    stream = io.StringIO()
    stores = install_stores(linux(tmp_path / "home"), platform=LINUX, start=tmp_path)
    stores.announce(stream=stream)

    said = stream.getvalue()
    assert stores.rule == INSTALL_RULE
    assert said.count("\n") == 1
    assert str(stores.path) in said
    assert "application data directory" in said
