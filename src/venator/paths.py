"""Where one Install keeps its Profiles and its stores.

An Install is one person's boundary, and the boundary is the application data
directory, not a Git checkout (docs/adr/0002-checkout-is-the-tenant-boundary.md).
Someone who installs Venator as an application has no working tree, no
`profiles/` beside the source, and nowhere inside a checkout to put anything, so
every path that belongs to a person resolves through here rather than through
the source tree.

The directory is the platform's usual one, with `$VENATOR_HOME` overriding it
outright:

* macOS — `~/Library/Application Support/Venator`
* Linux — `$XDG_DATA_HOME/venator` when that is an absolute path (the XDG
  basedir spec says to ignore a relative one), else `~/.local/share/venator`
* Windows — `%APPDATA%\\Venator`

Home comes from the environment mapping that is passed in, never from
`Path.home()` and never from the process environment, so a caller that passes
`environ={}` gets no application data directory at all instead of silently
reading whoever is running the tests. That holds for a leading `~` too: a
`$VENATOR_HOME` of `~/x` is expanded against the `HOME` (or `%USERPROFILE%`) in
the mapping that was passed, not against the one the process inherited, and
`~someone-else` is refused with a message rather than resolved off the machine's
password database.

The second half of this module answers the same question for the *stores* — the
append-only Postings, Filter Decisions and TrackEvents, the scheduler heartbeat,
and the disposable SQLite view. See the rule stated in full above
`install_stores`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

HOME_ENV = "VENATOR_HOME"
DIRECTORY_NAME = "Venator"
XDG_DIRECTORY_NAME = "venator"
PROFILES_SUBDIR = "profiles"


class InstallPathError(ValueError):
    """An environment variable names a path this Install cannot resolve.

    A `ValueError`, so the stage CLIs that already turn a bad Profile into a
    message turn this into one too; `venator.profile.select` re-raises it as a
    `ProfileError` on the Profile routes.
    """


def _expand_home(value: str, environ: Mapping[str, str]) -> Path:
    """A configured path with a leading `~` expanded from `environ`.

    `Path.expanduser()` reads the *process* environment and falls back to the
    password database, which would quietly hand back a path under whoever is
    running the command — the one thing this module promises not to do.
    """
    if not value.startswith("~"):
        return Path(value)
    head, separator, rest = value.partition("/")
    if head != "~":
        raise InstallPathError(
            f"${HOME_ENV} is {value!r}: another account's home directory is not this Install's "
            "— give the path in full"
        )
    home = environ.get("HOME", "").strip() or environ.get("USERPROFILE", "").strip()
    if not home:
        raise InstallPathError(
            f"${HOME_ENV} is {value!r} but the environment names no home directory to expand "
            "`~` against — give the path in full"
        )
    return Path(home) / rest if separator else Path(home)


def install_data_dir(
    environ: dict[str, str] | None = None,
    *,
    platform: str | None = None,
) -> Path | None:
    """The application data directory of this Install, or None if unknowable.

    None is a real answer, not a failure: it means the environment names no home
    and no override, so this Install has no application data directory and only
    a checkout's `profiles/` can be searched.
    """
    environ = os.environ if environ is None else environ
    override = environ.get(HOME_ENV, "").strip()
    if override:
        return _expand_home(override, environ)
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        appdata = environ.get("APPDATA", "").strip()
        return Path(appdata) / DIRECTORY_NAME if appdata else None
    home = environ.get("HOME", "").strip()
    if platform == "darwin":
        return Path(home) / "Library" / "Application Support" / DIRECTORY_NAME if home else None
    xdg = environ.get("XDG_DATA_HOME", "").strip()
    # The XDG basedir spec says a relative $XDG_DATA_HOME is invalid and must be
    # ignored — and a relative one here would be resolved against whatever
    # directory the command happened to start in, which is the failure mode this
    # whole module exists to remove.
    if xdg and Path(xdg).is_absolute():
        return Path(xdg) / XDG_DIRECTORY_NAME
    return Path(home) / ".local" / "share" / XDG_DIRECTORY_NAME if home else None


def install_profiles_dir(
    environ: dict[str, str] | None = None,
    *,
    platform: str | None = None,
) -> Path | None:
    """Where an Install with no checkout keeps its Profiles, or None."""
    data_dir = install_data_dir(environ, platform=platform)
    return None if data_dir is None else data_dir / PROFILES_SUBDIR


# --------------------------------------------------------------------------
# Where the stores live
# --------------------------------------------------------------------------
#
# Everything above answers "where are this Install's Profiles". Everything below
# answers the same question for its *stores* — the append-only Postings, Filter
# Decisions and TrackEvents, the scheduler heartbeat, and the disposable
# SQLite view. Those used to be literal relative paths (`data/postings`,
# `build/venator.db`), which meant they resolved against whatever directory the
# process happened to be started in: `cd ui && python -m venator.view.build`
# wrote `ui/build/venator.db` while the dashboard read `<checkout>/build/
# venator.db`, and a shipped Install writes its Owner's Postings beside
# whichever folder the launcher happened to inherit. Two directories, two
# half-populated stores, no error.
#
# The application data directory always wins, including inside a checkout.
# $VENATOR_HOME explicitly selects another Install for isolated runs. If no
# application data directory is available, refuse rather than write personal
# stores beside the current working directory. An explicit store path still
# means exactly what it says. Announce the chosen root once on stderr.

DATA_SUBDIR = "data"
BUILD_SUBDIR = "build"
POSTINGS_SUBDIR = "postings"
DECISIONS_SUBDIR = "decisions"
TRACK_SUBDIR = "track"
QUALIFICATIONS_SUBDIR = "qualifications"
HEARTBEAT_FILE = "runs.jsonl"
DATABASE_FILE = "venator.db"
SUGGESTIONS_FILE = "board-suggestions.yaml"

#: What every ``--postings-dir`` / ``--decisions-dir`` / ``--track-dir`` /
#: ``--database`` flag says for itself, now that leaving it off no longer means
#: "a path under the working directory".
STORE_HELP = (
    "path to this store; leave it off and the store follows this Install — the "
    "application data directory (set $VENATOR_HOME to select a different Install)"
)

INSTALL_RULE = "install"


class StoreRootError(ValueError):
    """Which directory this Install's stores live in cannot be decided.

    A `ValueError`, so the stage CLIs that already turn a bad Profile into a
    message turn this into one too.
    """


@dataclass(frozen=True)
class InstallStores:
    """The directory this Install's stores hang off, and which rule chose it.

    The layout under `path` is `data/postings`, `data/decisions`, `data/track`,
    `data/runs.jsonl`, and `build/venator.db`.
    """

    path: Path
    rule: str
    because: str

    @property
    def data_dir(self) -> Path:
        return self.path / DATA_SUBDIR

    @property
    def build_dir(self) -> Path:
        return self.path / BUILD_SUBDIR

    @property
    def postings_dir(self) -> Path:
        return self.data_dir / POSTINGS_SUBDIR

    @property
    def decisions_dir(self) -> Path:
        return self.data_dir / DECISIONS_SUBDIR

    @property
    def track_dir(self) -> Path:
        return self.data_dir / TRACK_SUBDIR

    @property
    def qualifications_dir(self) -> Path:
        return self.data_dir / QUALIFICATIONS_SUBDIR

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / HEARTBEAT_FILE

    #: `view.build` calls the same file `--runs-file`.
    @property
    def runs_file(self) -> Path:
        return self.heartbeat_path

    @property
    def database_path(self) -> Path:
        return self.build_dir / DATABASE_FILE

    @property
    def suggestions_path(self) -> Path:
        return self.data_dir / SUGGESTIONS_FILE

    #: `schedule.loop` runs each stage here; store selection is independent of cwd.
    @property
    def repository(self) -> Path:
        return self.path

    def announce(self, *, stream: TextIO | None = None) -> None:
        """Say, once, where this stage's stores resolved to and why.

        stderr, for the reason `announce_profile` uses it: `track.status` writes
        JSON to stdout and a caller piping it into `jq` must keep getting JSON.
        """
        print(
            f"Stores under {self.path}/ — {self.because}",
            file=sys.stderr if stream is None else stream,
            flush=True,
        )


def install_stores(
    environ: dict[str, str] | None = None,
    *,
    platform: str | None = None,
    start: str | Path | None = None,
) -> InstallStores:
    """Where this Install's stores live, independent of the working directory."""
    environ = os.environ if environ is None else environ
    if environ.get(HOME_ENV, "").strip():
        data_dir = install_data_dir(environ, platform=platform)
        assert data_dir is not None
        return InstallStores(
            path=data_dir,
            rule=INSTALL_RULE,
            because=f"${HOME_ENV} explicitly names this Install's application data directory",
        )
    data_dir = install_data_dir(environ, platform=platform)
    if data_dir is not None:
        return InstallStores(
            path=data_dir,
            rule=INSTALL_RULE,
            because="this Install's application data directory",
        )
    raise StoreRootError(
        f"this environment names no application data directory (set ${HOME_ENV}); "
        "refusing to write personal stores into a checkout"
    )


def resolve_store_paths(
    *,
    stream: TextIO | None = None,
    environ: dict[str, str] | None = None,
    platform: str | None = None,
    start: str | Path | None = None,
    **requested: Path | str | None,
) -> dict[str, Path]:
    """Fill in every store path the caller left as None, and say where from.

    Each keyword names an `InstallStores` attribute; a value that is not None is
    returned untouched, which is how `--postings-dir` and friends keep meaning
    exactly what they always meant. When every path was named there is no
    default in play: no store root is resolved and nothing is announced.
    """
    for name in requested:
        if not isinstance(getattr(InstallStores, name, None), property):
            raise AttributeError(f"no store named {name!r}")
    if all(value is not None for value in requested.values()):
        return {name: Path(value) for name, value in requested.items() if value is not None}
    stores = install_stores(environ, platform=platform, start=start)
    stores.announce(stream=stream)
    return {
        name: getattr(stores, name) if value is None else Path(value)
        for name, value in requested.items()
    }
