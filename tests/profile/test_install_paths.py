"""Resolving a Profile that lives outside any checkout.

The boundary between people is the Install's application data directory, not a
Git checkout (ADR-0002, as amended), so a shipped Install with no clone anywhere
must still find its Profile — while a checkout that has `profiles/<name>/`
beside it keeps resolving exactly as it did before that directory existed.

The failure this file is really about: a Profile that resolves to the wrong
directory, or to a directory holding no Profile files at all, which loads as a
Profile with no Hard Filter and passes every Posting.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from venator.paths import HOME_ENV, InstallPathError, install_data_dir, install_profiles_dir
from venator.profile import (
    PROFILE_ENV,
    REQUIRED_PROFILE_FILE,
    ProfileError,
    announce_profile,
    available_profiles,
    holds_profile_files,
    load_profile,
    profile_search_path,
    resolve_profile_dir,
)

LINUX, MACOS, WINDOWS = "linux", "darwin", "win32"

#: How this host's `pathlib` writes a separator. Stated outright rather than
#: asked of `Path`, which is the whole point: an expectation assembled with the
#: same call the code under test makes is true whatever that call does, and
#: these cases state a machine rather than being one.
SEPARATOR = "\\" if os.name == "nt" else "/"

#: A path this host agrees is absolute. `/data/ada` is absolute on Unix and
#: **relative** on Windows, where a rooted path with no drive letter is not
#: absolute — and `install_data_dir` asks `pathlib` that question, so the two
#: hosts genuinely disagree about the string. That disagreement never reaches an
#: Install, because the Linux branch is only ever taken on Linux; it reaches
#: this file, which runs the Linux branch wherever CI happens to be. So the
#: value is written for the host, and the branch under test is the one taken.
ABSOLUTE_XDG = "D:\\data\\ada" if os.name == "nt" else "/data/ada"


def native(path: str) -> str:
    """`path` written the way this host's `pathlib` writes one."""
    return path.replace("/", SEPARATOR)


def write_profile(directory: Path, **files: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / f"{name}.yaml").write_text(text, encoding="utf-8")
    return directory


def install(root: Path, name: str, **files: str) -> Path:
    """A Profile inside an application data directory named by $VENATOR_HOME."""
    return write_profile(root / "profiles" / name, **files)


@pytest.mark.parametrize("platform", [LINUX, MACOS, WINDOWS])
def test_the_environment_override_names_the_application_data_directory(platform: str) -> None:
    environ = {
        HOME_ENV: "/opt/venator",
        "HOME": "/home/somebody",
        "APPDATA": r"C:\Users\somebody\AppData\Roaming",
        "XDG_DATA_HOME": "/home/somebody/.local/share",
    }

    assert install_data_dir(environ, platform=platform) == Path("/opt/venator")


@pytest.mark.parametrize(
    ("platform", "environ", "expected"),
    [
        (MACOS, {"HOME": "/mock-macos-home/ada"}, "/mock-macos-home/ada/Library/Application Support/Venator"),
        (LINUX, {"HOME": "/home/ada"}, "/home/ada/.local/share/venator"),
        (
            LINUX,
            {"HOME": "/home/ada", "XDG_DATA_HOME": ABSOLUTE_XDG},
            f"{ABSOLUTE_XDG}{SEPARATOR}venator",
        ),
        # A Windows-shaped %APPDATA%, because that is the only kind there is.
        # The POSIX-shaped value this used to carry compared equal on every host
        # and so said nothing about Windows at all.
        (
            WINDOWS,
            {"APPDATA": r"C:\Users\ada\AppData\Roaming"},
            f"C:\\Users\\ada\\AppData\\Roaming{SEPARATOR}Venator",
        ),
    ],
)
def test_each_platform_gets_its_usual_application_data_directory(
    platform: str, environ: dict[str, str], expected: str
) -> None:
    resolved = install_data_dir(environ, platform=platform)
    assert resolved == Path(expected)
    # The string too, not only the Path: two Paths compare equal without ever
    # saying which separator the answer was written with, and the separator is
    # half of what a Windows Install has to get right.
    assert str(resolved) == native(expected)


def test_a_windows_install_gets_a_windows_shaped_path() -> None:
    """The one expectation written out in full, per host, with nothing computed.

    `install_data_dir` builds its answer with `pathlib`, and `pathlib` answers
    for the *host* rather than for the platform it was asked about — so on Linux
    the Windows branch joins with a forward slash. That configuration never
    reaches an Install, because `platform` defaults to `sys.platform` and no
    caller passes another machine's; it reaches this file, which is the only
    place the two can differ. Which is exactly why the shape is pinned here as a
    literal rather than assembled from the same call that produced it: an
    expectation built with `Path.__truediv__` agrees with `Path.__truediv__`
    whatever it does, and this suite spent a release believing it had checked
    the Windows answer when it had checked nothing of the kind.
    """
    resolved = str(
        install_data_dir({"APPDATA": r"C:\Users\ada\AppData\Roaming"}, platform=WINDOWS)
    )

    if os.name == "nt":
        assert resolved == "C:\\Users\\ada\\AppData\\Roaming\\Venator"
    else:
        assert resolved == "C:\\Users\\ada\\AppData\\Roaming/Venator"


@pytest.mark.parametrize("platform", [LINUX, MACOS, WINDOWS])
def test_an_environment_naming_no_home_has_no_application_data_directory(platform: str) -> None:
    """None is an answer: only a checkout's profiles/ is searched."""
    assert install_data_dir({}, platform=platform) is None
    assert install_profiles_dir({}, platform=platform) is None
    assert install_data_dir({HOME_ENV: "   "}, platform=platform) is None


def test_an_empty_environment_searches_only_the_checkout(tmp_path: Path) -> None:
    assert profile_search_path(tmp_path / "profiles", {}) == (tmp_path / "profiles",)


def test_a_profile_resolves_from_the_application_data_directory_with_no_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped Install: no clone, no profiles/ beside the source, one Profile."""
    data_dir = tmp_path / "Venator"
    ada = install(data_dir, "ada", targeting="profile:\n  name: ada\n")
    monkeypatch.chdir(tmp_path)
    environ = {HOME_ENV: str(data_dir)}

    assert resolve_profile_dir("ada", environ=environ) == ada
    assert resolve_profile_dir(environ=environ) == ada
    assert resolve_profile_dir(environ={**environ, PROFILE_ENV: "ada"}) == ada
    assert load_profile(resolve_profile_dir("ada", environ=environ)).name == "ada"


def test_a_profile_under_profiles_resolves_exactly_as_it_did_before(tmp_path: Path) -> None:
    """The existing Install: profiles/<name>/ in a checkout, nothing else moves."""
    profiles = tmp_path / "checkout" / "profiles"
    sample = write_profile(profiles / "sample", targeting="profile:\n  name: sample\n")
    write_profile(profiles / "example", targeting="profile:\n  scaffold: true\n")
    data_dir = tmp_path / "Venator"
    (data_dir / "profiles").mkdir(parents=True)
    environ = {HOME_ENV: str(data_dir)}

    assert resolve_profile_dir("sample", profiles_dir=profiles, environ=environ) == sample
    assert resolve_profile_dir(profiles_dir=profiles, environ=environ) == sample


def test_the_checkout_wins_when_one_name_exists_in_both_places(tmp_path: Path) -> None:
    """Fixed order, not a refusal: profiles/ is nearer than the data directory."""
    profiles = tmp_path / "checkout" / "profiles"
    checkout_ada = write_profile(profiles / "ada", targeting="search:\n  queries: [checkout]\n")
    data_dir = tmp_path / "Venator"
    install(data_dir, "ada", targeting="search:\n  queries: [application-data]\n")
    environ = {HOME_ENV: str(data_dir)}

    resolved = resolve_profile_dir("ada", profiles_dir=profiles, environ=environ)

    assert resolved == checkout_ada
    assert load_profile(resolved).search.queries == ("checkout",)
    assert available_profiles(profiles, environ) == [checkout_ada]


def test_the_implied_profile_may_live_in_the_application_data_directory(tmp_path: Path) -> None:
    """A checkout holding only the scaffold implies the Install's own Profile."""
    profiles = tmp_path / "checkout" / "profiles"
    write_profile(profiles / "example", targeting="profile:\n  scaffold: true\n")
    data_dir = tmp_path / "Venator"
    ada = install(data_dir, "ada", targeting="profile:\n  name: ada\n")
    environ = {HOME_ENV: str(data_dir)}

    assert resolve_profile_dir(profiles_dir=profiles, environ=environ) == ada
    assert available_profiles(profiles, environ) == [ada, profiles / "example"]


def test_two_profiles_one_in_each_place_are_still_refused_rather_than_guessed(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "checkout" / "profiles"
    write_profile(profiles / "ada", targeting="")
    data_dir = tmp_path / "Venator"
    install(data_dir, "grace", targeting="")

    with pytest.raises(ProfileError) as error:
        resolve_profile_dir(profiles_dir=profiles, environ={HOME_ENV: str(data_dir)})

    message = str(error.value)
    assert "more than one Profile exists" in message
    assert "available: ada, grace" in message


def test_an_unknown_name_says_both_places_it_was_looked_for(tmp_path: Path) -> None:
    profiles = tmp_path / "checkout" / "profiles"
    write_profile(profiles / "ada", targeting="")
    data_dir = tmp_path / "Venator"
    install(data_dir, "grace", targeting="")

    with pytest.raises(ProfileError) as error:
        resolve_profile_dir("nobody", profiles_dir=profiles, environ={HOME_ENV: str(data_dir)})

    message = str(error.value)
    assert "no Profile named 'nobody'" in message
    assert str(profiles) in message
    assert str(data_dir / "profiles") in message


@pytest.mark.parametrize("named", ["", "   ", ".", "..", "../x", "profiles/ada", "/etc", "~/ada"])
def test_a_profile_is_still_a_name_and_never_a_path(tmp_path: Path, named: str) -> None:
    """Relaxing containment must not relax this: `--profile ''` once resolved to
    the working directory and loaded a Profile with no Hard Filter, which passes
    every Posting."""
    profiles = tmp_path / "checkout" / "profiles"
    write_profile(profiles / "ada", targeting="")
    data_dir = tmp_path / "Venator"
    install(data_dir, "ada", targeting="")

    with pytest.raises(ProfileError) as error:
        resolve_profile_dir(named, profiles_dir=profiles, environ={HOME_ENV: str(data_dir)})

    message = str(error.value)
    assert "is empty" in message or "takes the name of a Profile" in message


def test_a_profile_directory_outside_every_checkout_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outside = write_profile(tmp_path / "Venator" / "profiles" / "ada", targeting="")
    monkeypatch.chdir(checkout)

    assert resolve_profile_dir(directory=outside, environ={}) == outside


def test_a_directory_outside_the_checkout_holding_no_profile_files_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    hollow = tmp_path / "Venator" / "profiles" / "ada"
    hollow.mkdir(parents=True)
    monkeypatch.chdir(checkout)

    with pytest.raises(ProfileError) as error:
        resolve_profile_dir(directory=hollow, environ={})

    message = str(error.value)
    assert "is not a Profile directory" in message
    assert "pass every Posting" in message


# --- A directory is not a Profile until it holds targeting.yaml ---------------
#
# The failure being closed here is the one this repo has already been burned by
# once: a Profile with no Hard Filter loads, kills nothing, and passes every
# Posting ever discovered. `--profile-dir` was
# guarded against it; `--profile`, `$VENATOR_PROFILE` and the implied Profile
# were not, and this branch adds a second, product-created location that the
# unguarded route searches.


def test_a_named_profile_directory_holding_no_profile_files_is_refused(
    tmp_path: Path,
) -> None:
    """The onboarding crash: `mkdir` ran, the first file was never written.

    An empty `<application data directory>/profiles/<name>/` is exactly the
    state a shipped Install is left in by a crash or a partial write between
    creating the directory and writing `targeting.yaml`.
    """
    profiles = tmp_path / "checkout" / "profiles"
    profiles.mkdir(parents=True)
    data_dir = tmp_path / "Venator"
    (data_dir / "profiles" / "ada").mkdir(parents=True)
    environ = {HOME_ENV: str(data_dir)}

    with pytest.raises(ProfileError) as error:
        resolve_profile_dir("ada", profiles_dir=profiles, environ=environ)

    message = str(error.value)
    assert REQUIRED_PROFILE_FILE in message
    assert "is not a Profile directory" in message
    assert "pass every Posting" in message


def test_an_unpopulated_directory_is_never_the_implied_profile(tmp_path: Path) -> None:
    """Not implied, and not counted: it is a directory, not a Profile."""
    profiles = tmp_path / "checkout" / "profiles"
    profiles.mkdir(parents=True)
    data_dir = tmp_path / "Venator"
    (data_dir / "profiles" / "ada").mkdir(parents=True)
    environ = {HOME_ENV: str(data_dir)}

    assert available_profiles(profiles, environ) == []
    with pytest.raises(ProfileError) as error:
        resolve_profile_dir(profiles_dir=profiles, environ=environ)
    assert "no Profile exists" in str(error.value)


def test_the_environment_variable_route_is_guarded_the_same_way(tmp_path: Path) -> None:
    profiles = tmp_path / "checkout" / "profiles"
    profiles.mkdir(parents=True)
    data_dir = tmp_path / "Venator"
    (data_dir / "profiles" / "ada").mkdir(parents=True)

    with pytest.raises(ProfileError) as error:
        resolve_profile_dir(
            profiles_dir=profiles, environ={HOME_ENV: str(data_dir), PROFILE_ENV: "ada"}
        )

    assert REQUIRED_PROFILE_FILE in str(error.value)


def test_an_empty_profiles_directory_beside_the_cwd_cannot_outrank_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`profiles/` is a common directory name and is searched first.

    Start a stage from any directory that happens to have one and the Install's
    real Profile was shadowed by it — silently, because a directory with no
    `targeting.yaml` loads with `filters.enabled == ()` and passes every
    Posting. A directory that holds no Profile is now skipped, and the search
    goes on to the one that does.
    """
    somewhere = tmp_path / "somewhere-else"
    (somewhere / "profiles" / "ada").mkdir(parents=True)
    monkeypatch.chdir(somewhere)
    data_dir = tmp_path / "Venator"
    real = install(data_dir, "ada", targeting="filters:\n  enabled: [education_fit]\n")
    environ = {HOME_ENV: str(data_dir)}

    assert resolve_profile_dir("ada", environ=environ) == real
    assert resolve_profile_dir(environ=environ) == real
    assert load_profile(resolve_profile_dir("ada", environ=environ)).filters.enabled == (
        "education_fit",
    )


def test_a_stray_directory_does_not_turn_one_profile_into_two(tmp_path: Path) -> None:
    """Someone's `Downloads/` under profiles/ must not stop every stage."""
    profiles = tmp_path / "checkout" / "profiles"
    ada = write_profile(profiles / "ada", targeting="profile:\n  name: ada\n")
    (profiles / "Downloads").mkdir()
    (profiles / "Downloads" / "notes.txt").write_text("unrelated\n", encoding="utf-8")

    assert available_profiles(profiles, {}) == [ada]
    assert resolve_profile_dir(profiles_dir=profiles, environ={}) == ada


def test_malformed_yaml_in_a_real_profile_is_a_message_not_a_parser_dump(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "checkout" / "profiles"
    write_profile(profiles / "ada", targeting="profile: [unclosed\n")

    with pytest.raises(ProfileError) as error:
        resolve_profile_dir(profiles_dir=profiles, environ={})

    message = str(error.value)
    assert "is not a loadable Profile" in message
    assert "which Profile is implied cannot be decided" in message


def test_holds_profile_files_asks_for_targeting_and_not_merely_something(
    tmp_path: Path,
) -> None:
    """The Hard Filter wording lives in targeting.yaml; the rest may be absent.

    A directory holding `resume.yaml` and `constraints.yaml` but no
    `targeting.yaml` used to satisfy the `--profile-dir` guard, and loaded with
    `filters.enabled == ()` — the guard passing on exactly the file whose
    absence fails open.
    """
    half = write_profile(tmp_path / "half", resume="", constraints="")
    whole = write_profile(tmp_path / "whole", targeting="")

    assert holds_profile_files(half) is False
    assert holds_profile_files(whole) is True

    with pytest.raises(ProfileError) as error:
        resolve_profile_dir(directory=half, environ={})
    assert REQUIRED_PROFILE_FILE in str(error.value)
    assert load_profile(half).filters.enabled == ()

    assert resolve_profile_dir(directory=whole, environ={}) == whole


def test_a_profile_dir_that_is_not_a_directory_is_refused(tmp_path: Path) -> None:
    not_a_directory = tmp_path / "targeting.yaml"
    not_a_directory.write_text("profile:\n  name: ada\n", encoding="utf-8")

    with pytest.raises(ProfileError) as error:
        resolve_profile_dir(directory=not_a_directory, environ={})

    assert "no Profile directory at" in str(error.value)


def test_a_profile_dir_expands_a_leading_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--profile-dir ~/Venator/profiles/ada` is what a person actually types."""
    home = tmp_path / "home" / "ada"
    ada = write_profile(home / "Venator" / "profiles" / "ada", targeting="")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    assert resolve_profile_dir(directory="~/Venator/profiles/ada", environ={}) == ada


def test_one_directory_named_twice_is_searched_once(tmp_path: Path) -> None:
    """A checkout that *is* the application data directory searches one place."""
    data_dir = tmp_path / "Venator"
    profiles = data_dir / "profiles"
    profiles.mkdir(parents=True)
    environ = {HOME_ENV: str(data_dir)}

    assert profile_search_path(profiles, environ) == (profiles,)


# --- Which directory decided ------------------------------------------------


def test_the_resolved_profile_directory_is_announced(tmp_path: Path, capsys) -> None:
    """The same name in both places is the same run to everything downstream.

    Filter Decisions carry the same `profile_id`, `data/decisions/.profile`
    matches either, so which directory won has to be said out loud. It goes to
    stderr because `venator.track.status` writes JSON to stdout.
    """
    ada = write_profile(tmp_path / "profiles" / "ada", targeting="profile:\n  name: ada\n")

    announce_profile(load_profile(ada))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"Profile 'ada' from {ada}/" in captured.err


# --- Home never comes from the process environment ---------------------------


def test_a_tilde_in_the_override_expands_from_the_mapping_not_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/home/whoever-is-running-this")

    assert install_data_dir(
        {HOME_ENV: "~/venator", "HOME": "/home/ada"}, platform=LINUX
    ) == Path("/home/ada/venator")
    assert install_data_dir({HOME_ENV: "~", "HOME": "/home/ada"}, platform=LINUX) == Path(
        "/home/ada"
    )


def test_a_tilde_with_no_home_in_the_mapping_is_a_message_not_a_traceback() -> None:
    with pytest.raises(InstallPathError) as error:
        install_data_dir({HOME_ENV: "~/venator"}, platform=LINUX)

    assert "names no home directory" in str(error.value)


def test_another_accounts_home_directory_is_refused_rather_than_resolved() -> None:
    """`~someone/x` used to raise an uncaught RuntimeError off the password
    database — a traceback, and an answer about a different account."""
    with pytest.raises(InstallPathError) as error:
        install_data_dir({HOME_ENV: "~nonexistentuser42/x", "HOME": "/home/ada"}, platform=LINUX)

    assert "another account's home directory" in str(error.value)


def test_an_unresolvable_override_is_a_profile_error_on_the_profile_routes(
    tmp_path: Path,
) -> None:
    """A stage CLI already turns a ProfileError into a message; this becomes one."""
    with pytest.raises(ProfileError) as error:
        resolve_profile_dir("ada", profiles_dir=tmp_path, environ={HOME_ENV: "~ghost/x"})

    assert "another account's home directory" in str(error.value)


def test_a_relative_xdg_data_home_is_ignored_as_the_spec_says(tmp_path: Path) -> None:
    """A relative one would resolve against wherever the command was started."""
    environ = {"HOME": "/home/ada", "XDG_DATA_HOME": "relative/share"}

    assert install_data_dir(environ, platform=LINUX) == Path("/home/ada/.local/share/venator")
    # And an absolute one is honoured. `ABSOLUTE_XDG` is written for the host
    # because `Path.is_absolute` is a question about the host, not about the
    # platform this case names.
    assert install_data_dir(
        {"HOME": "/home/ada", "XDG_DATA_HOME": ABSOLUTE_XDG}, platform=LINUX
    ) == Path(f"{ABSOLUTE_XDG}{SEPARATOR}venator")
