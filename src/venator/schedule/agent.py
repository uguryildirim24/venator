"""Generate the launchd agent for the scheduled loop, resolved for this install.

The agent needs absolute paths — launchd has no shell, no PATH, and no working
directory of its own. Committing them meant committing one person's home
directory, so the paths are resolved here from the checkout and the environment
instead, and the plist is written at install time rather than shipped.

Usage:
    python -m venator.schedule.agent                    # print the plist
    python -m venator.schedule.agent --out FILE         # write it
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import sys
from pathlib import Path

# launchd is macOS's own scheduler and nothing else consumes a plist. Without
# this guard the generator succeeds anywhere: on Windows it prints a plist full
# of `C:\Users\...\Library\Logs\venator\loop.stdout.log` and a PATH joined
# with `:` instead of `;`, confidently wrong and inert. There is deliberately no
# Windows equivalent — `venator.schedule.loop` still runs by hand on any
# platform, and whoever wants it unattended there wires it up themselves.
SUPPORTED_PLATFORM = "darwin"
WRONG_PLATFORM = (
    "the scheduled loop's agent is a macOS launchd job, and this is {platform}. "
    "launchd exists only on macOS and nothing else reads a plist, so there is "
    "nothing useful to write here. Venator has no scheduler of its own for any "
    "other platform: run `python -m venator.schedule.loop` by hand, or hand it "
    "to whatever your system already uses to run things on a timer."
)

LABEL = "dev.venator.loop"
INTERVAL_SECONDS = 6 * 60 * 60
REPOSITORY = Path(__file__).resolve().parents[3]
SEARCH_PATH_SUFFIX = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")


def find_uv(home: Path) -> Path:
    """The uv launchd should run. launchd inherits no PATH, so this is absolute."""
    found = shutil.which("uv")
    if found:
        return Path(found).resolve()
    fallback = home / ".local" / "bin" / "uv"
    if fallback.is_file():
        return fallback
    raise FileNotFoundError(
        "uv is not on PATH and is not at ~/.local/bin/uv — launchd needs an "
        "absolute path to it. Install uv, or pass --uv."
    )


def launch_agent(
    *,
    repository: Path = REPOSITORY,
    home: Path | None = None,
    uv: Path | None = None,
    profile: str | None = None,
    interval: int = INTERVAL_SECONDS,
) -> dict:
    """Build the launchd agent definition for this checkout and this account."""
    home = Path(home if home is not None else os.environ.get("HOME") or Path.home())
    uv = Path(uv) if uv is not None else find_uv(home)
    log_dir = home / "Library" / "Logs" / "venator"
    arguments = [str(uv), "run", "python", "-m", "venator.schedule.loop"]
    if profile:
        arguments += ["--profile", profile]
    return {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(Path(repository).resolve()),
        "StartInterval": interval,
        "EnvironmentVariables": {
            "HOME": str(home),
            "PATH": ":".join([str(uv.parent), *SEARCH_PATH_SUFFIX]),
        },
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "loop.stdout.log"),
        "StandardErrorPath": str(log_dir / "loop.stderr.log"),
    }


def render(agent: dict) -> bytes:
    return plistlib.dumps(agent, sort_keys=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write the plist here instead of stdout")
    parser.add_argument("--repository", type=Path, default=REPOSITORY)
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument("--uv", type=Path, default=None, help="absolute path to the uv binary")
    parser.add_argument("--profile", default=None, help="Profile the scheduled loop runs for")
    parser.add_argument("--interval", type=int, default=INTERVAL_SECONDS)
    args = parser.parse_args()
    if sys.platform != SUPPORTED_PLATFORM:
        parser.error(WRONG_PLATFORM.format(platform=sys.platform))
    try:
        agent = launch_agent(
            repository=args.repository,
            home=args.home,
            uv=args.uv,
            profile=args.profile,
            interval=args.interval,
        )
    except FileNotFoundError as error:
        parser.error(str(error))
    payload = render(agent)
    if args.out is None:
        sys.stdout.write(payload.decode("utf-8"))
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(payload)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
