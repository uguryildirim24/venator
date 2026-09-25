"""Choose which Profile a command runs for.

Order: an explicit ``--profile-dir``, then ``--profile``, then
``VENATOR_PROFILE``, then the single Profile that is not a scaffold. Anything
else is an error that lists the choices, because guessing whose Postings a run
belongs to is worse than stopping.

A Profile *name* is looked for in the Install's application data directory
first, then in ``profiles/`` beside the working directory (the checkout holds
the fictional example). A caller passing an explicit ``profiles_dir`` searches
that directory first instead. Where a name is not found, the error names both
places it was looked for.

Guessing is refused literally, and that costs two rules worth stating:

* ``--profile`` names a Profile in one of those two places. It is a name, not a
  path — an empty value, a path separator, or a leading dot is rejected rather
  than resolved. ``--profile ""`` used to resolve to the working directory and
  load a nameless Profile with no Hard Filter, which `apply_filters` then
  reports as "no Hard Filter is configured; passed": every Posting through to
  assessment and Jev, silently.
* ``--profile-dir`` is the escape hatch for a Profile directory anywhere else,
  inside a checkout or well outside one — the boundary between people is the
  Install's data directory, not a checkout (ADR-0002), so containment in a
  working tree is not a property worth enforcing. It must still actually hold
  Profile files, because a directory that holds none loads as an empty Profile
  — which fails open the same way ``--profile ""`` did.

A directory that holds no ``targeting.yaml`` is not a Profile, on **every**
route: ``--profile-dir``, ``--profile``, ``$VENATOR_PROFILE``, and the implied
single Profile alike. ``targeting.yaml`` is the file that decides it because it
is where the Hard Filter wording lives; without it a Profile loads with
``filters.enabled == ()``, kills nothing, and passes every Posting to
assessment and Jev on the Owner's budget. Two ways in made that reachable: an
``<application data directory>/profiles/<name>/`` that the shipped onboarding
creates before it writes the first file — a crash in between leaves exactly that
— and a ``profiles/`` beside whatever directory a command was started in, which
is a common enough name to collide by accident and is searched first. Neither is
a Profile now; both are skipped and the search goes on to a directory that is
one.

Skipping is not silent. The chosen directory is printed once per stage
(`announce_profile`); a same-named Profile in the checkout must not quietly
supersede the Install's Profile.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TextIO

from venator.paths import HOME_ENV, InstallPathError, install_profiles_dir
from venator.profile.loader import PROFILE_FILES, ProfileError, load_profile
from venator.profile.schema import Profile

PROFILES_DIR = Path("profiles")
PROFILE_ENV = "VENATOR_PROFILE"
REQUIRED_PROFILE_FILE = "targeting.yaml"
FAILS_OPEN = (
    f"a directory with no {REQUIRED_PROFILE_FILE} loads as a Profile with no Hard Filter and "
    "would pass every Posting"
)


def holds_profile_files(directory: Path) -> bool:
    """Whether ``directory`` is a Profile directory rather than a bare directory.

    One file decides it, and it is ``targeting.yaml``: the Hard Filter wording,
    the search terms, the board registry, the Hard Filter wording and the ``scaffold``
    flag all live there, and a Profile without it fails open. ``resume.yaml``
    and ``constraints.yaml`` are allowed to be missing — the loader reads a
    missing Profile file as empty, and an absent resume makes a Profile
    incomplete, not dangerous.
    """
    return (Path(directory) / REQUIRED_PROFILE_FILE).is_file()


def profile_search_path(
    profiles_dir: Path = PROFILES_DIR,
    environ: dict[str, str] | None = None,
) -> tuple[Path, ...]:
    """Every directory a Profile name is looked for in, Install first by default."""
    environ = os.environ if environ is None else environ
    locations = [Path(profiles_dir)]
    try:
        shipped = install_profiles_dir(environ)
    except InstallPathError as error:
        raise ProfileError(str(error)) from error
    if shipped is not None and shipped.resolve() != Path(profiles_dir).resolve():
        if Path(profiles_dir) == PROFILES_DIR:
            locations.insert(0, shipped)
        else:
            locations.append(shipped)
    return tuple(locations)


def available_profiles(
    profiles_dir: Path = PROFILES_DIR,
    environ: dict[str, str] | None = None,
) -> list[Path]:
    """Every Profile directory, scaffolds included, in stable name order.

    A name found in more than one location appears once, resolved to the first
    location that holds it.
    """
    found: dict[str, Path] = {}
    for location in profile_search_path(profiles_dir, environ):
        if not location.is_dir():
            continue
        for entry in sorted(location.iterdir()):
            if entry.name.startswith(".") or entry.name in found:
                continue
            # A directory that holds no Profile files is not a Profile and is
            # not a candidate for the implied one either: an unrelated folder
            # someone dropped in here must not turn one Profile into two and
            # stop every stage, and an unpopulated one must never be implied.
            if entry.is_dir() and holds_profile_files(entry):
                found[entry.name] = entry
    return [found[name] for name in sorted(found)]


def _is_scaffold(directory: Path) -> bool:
    try:
        return load_profile(directory).scaffold
    except ProfileError as error:
        raise ProfileError(
            f"{directory} is not a loadable Profile, so which Profile is implied cannot be "
            f"decided: {error}"
        ) from error


# Every character that separates one path component from another, or a drive
# from the rest of a path, on any platform this runs on.
#
# This used to be `name != Path(name).name`, which is correct but only by
# accident of which flavour `Path` happens to be: on Windows `Path` is
# `WindowsPath` and refuses `a\\b`, `C:foo` and `\\\\server\\share\\x`, while on
# Linux `Path` is `PosixPath` and accepts all three. So the same `--profile`
# value meant two different things on two machines, the stricter half was never
# exercised by a test suite that runs on Linux, and one refactor to
# `PurePosixPath` would have opened it silently. The rejection is spelled out
# here instead, and means the same thing everywhere.
NAME_SEPARATORS = ("/", "\\", ":")

# Win32 strips trailing dots and spaces while normalizing a path, so
# `--profile "sample "` and `--profile sample.` would quietly open `profiles\\sample`
# and load a Profile whose name is not the one that was typed — while
# `announce_profile` prints the directory it resolved to, not the value it was
# given. Quiet aliasing is the one thing this module exists to prevent.
NAME_TRAILING = ". "


def _is_not_a_bare_name(name: str) -> bool:
    """True for anything that is a path, or normalizes to a different name."""
    if name.startswith("."):
        return True
    if any(separator in name for separator in NAME_SEPARATORS):
        return True
    return name != name.rstrip(NAME_TRAILING)


def _named_profile(
    named: str | Path,
    profiles_dir: Path,
    source: str,
    environ: dict[str, str] | None = None,
) -> Path:
    name = str(named).strip()
    locations = profile_search_path(profiles_dir, environ)
    if not name:
        raise ProfileError(
            f"{source} is empty — name a Profile in {_locations(locations)}, or leave it unset "
            f"to use the only Profile there; {_choices(profiles_dir, environ)}"
        )
    if _is_not_a_bare_name(name):
        raise ProfileError(
            f"{source} takes the name of a Profile in {_locations(locations)}, not a path "
            f"(got {name!r}) — use --profile-dir for a Profile directory elsewhere"
        )
    hollow: list[Path] = []
    for location in locations:
        candidate = location / name
        if not candidate.is_dir():
            continue
        if holds_profile_files(candidate):
            return candidate
        hollow.append(candidate)
    if hollow:
        raise ProfileError(
            f"{_locations(tuple(hollow))} exists but holds no {REQUIRED_PROFILE_FILE}, so it is "
            f"not a Profile directory — {FAILS_OPEN}; "
            f"{_choices(profiles_dir, environ)}"
        )
    raise ProfileError(
        f"no Profile named {name!r} in {_locations(locations)} — {_choices(profiles_dir, environ)}"
    )


def _profile_directory(value: str | Path) -> Path:
    text = str(value).strip()
    if not text:
        raise ProfileError(
            "--profile-dir is empty — name the directory holding "
            f"{', '.join(PROFILE_FILES)}"
        )
    candidate = Path(text).expanduser()
    if not candidate.is_dir():
        raise ProfileError(f"no Profile directory at {candidate}")
    if not holds_profile_files(candidate):
        raise ProfileError(
            f"{candidate} holds no {REQUIRED_PROFILE_FILE}, so it is not a Profile "
            f"directory — {FAILS_OPEN}; a Profile directory holds "
            f"{', '.join(PROFILE_FILES)}"
        )
    return candidate


def resolve_profile_dir(
    requested: str | Path | None = None,
    *,
    directory: str | Path | None = None,
    profiles_dir: Path = PROFILES_DIR,
    environ: dict[str, str] | None = None,
) -> Path:
    """Resolve the Profile directory to run against, or explain why it cannot."""
    environ = os.environ if environ is None else environ
    if directory is not None:
        return _profile_directory(directory)
    if requested is not None:
        return _named_profile(requested, profiles_dir, "--profile", environ)
    if PROFILE_ENV in environ:
        return _named_profile(environ[PROFILE_ENV], profiles_dir, f"${PROFILE_ENV}", environ)

    directories = available_profiles(profiles_dir, environ)
    selectable = [entry for entry in directories if not _is_scaffold(entry)]
    if len(selectable) == 1:
        return selectable[0]
    locations = profile_search_path(profiles_dir, environ)
    if not directories:
        raise ProfileError(
            f"no Profile exists — create {profiles_dir}/<name>/ "
            f"(copy {profiles_dir}/example/), or pass --profile; looked in "
            f"{_locations(locations)} (${HOME_ENV} names the application data directory)"
        )
    raise ProfileError(
        f"more than one Profile exists, so none is implied — pass --profile "
        f"or set {PROFILE_ENV}; {_choices(profiles_dir, environ)}"
    )


def _locations(locations: tuple[Path, ...]) -> str:
    return ", ".join(f"{location}/" for location in locations)


def _choices(profiles_dir: Path, environ: dict[str, str] | None = None) -> str:
    entries = available_profiles(profiles_dir, environ)
    if not entries:
        return f"no Profile in {_locations(profile_search_path(profiles_dir, environ))}"
    return f"available: {', '.join(entry.name for entry in entries)}"


def resolve_profile(
    requested: str | Path | None = None,
    *,
    directory: str | Path | None = None,
    profiles_dir: Path = PROFILES_DIR,
) -> Profile:
    """Resolve and load the Profile a command runs for."""
    return load_profile(
        resolve_profile_dir(requested, directory=directory, profiles_dir=profiles_dir)
    )


def announce_profile(profile: Profile, *, stream: TextIO | None = None) -> None:
    """Say, once, which directory this stage's Profile was resolved from.

    The Install's Profile wins over a same-named checkout Profile by default.
    State the chosen directory because an explicit profiles_dir can change it.

    It goes to stderr: ``venator.track.status`` writes JSON to stdout and a
    caller piping it into ``jq`` must keep getting JSON.
    """
    print(
        f"Profile {profile.name!r} from {Path(profile.directory)}/",
        file=sys.stderr if stream is None else stream,
        flush=True,
    )


def add_profile_argument(parser: argparse.ArgumentParser) -> None:
    """Add the shared ``--profile`` / ``--profile-dir`` flags to a stage CLI."""
    parser.add_argument(
        "--profile",
        default=None,
        help=(
            f"name of a Profile under {PROFILES_DIR}/ or this Install's application data "
            f"directory (default: ${PROFILE_ENV}, else the only Profile that exists)"
        ),
    )
    parser.add_argument(
        "--profile-dir",
        default=None,
        help=(
            "path to a Profile directory anywhere on this machine; it must hold "
            f"{', '.join(PROFILE_FILES)}"
        ),
    )


def profile_from_arguments(
    args: argparse.Namespace, *, profiles_dir: Path = PROFILES_DIR
) -> Profile:
    """Resolve the Profile named by the shared flags this module adds."""
    return resolve_profile(args.profile, directory=args.profile_dir, profiles_dir=profiles_dir)


def profile_for_constraints(
    constraints_path: Path | None = None,
    *,
    requested: str | Path | None = None,
    directory: str | Path | None = None,
    profiles_dir: Path = PROFILES_DIR,
) -> Profile:
    """Resolve the Profile behind a ``--constraints`` path.

    ``--constraints`` predates Profiles and stays supported: a path inside a
    Profile directory selects that Profile, and a bare constraints file
    elsewhere (what the test suites pass) keeps the historical behaviour of
    versioning against that file while the filter policy comes from the
    implied Profile.
    """
    if requested is not None or directory is not None or constraints_path is None:
        return resolve_profile(requested, directory=directory, profiles_dir=profiles_dir)
    parent = Path(constraints_path).parent
    if (parent / "targeting.yaml").exists():
        return load_profile(parent)
    try:
        return resolve_profile(None, profiles_dir=profiles_dir)
    except ProfileError:
        return Profile(name=parent.name or "unconfigured", directory=parent)
