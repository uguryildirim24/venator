"""Generate the launchd agent for the installed app's daily loop.

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
DEFAULT_HOUR = 6
DEFAULT_MINUTE = 30
SEARCH_PATH_SUFFIX = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")


def launch_agent(
    *,
    home: Path | None = None,
    profile: str | None = None,
    interpreter: Path | None = None,
    claude: Path | None = None,
    install: Path | None = None,
    hour: int = DEFAULT_HOUR,
    minute: int = DEFAULT_MINUTE,
    daily_usd: float = 1.0,
    monthly_usd: float = 10.0,
) -> dict:
    """Build the launchd agent definition for this Install."""
    home = Path(home if home is not None else os.environ.get("HOME") or Path.home())
    install = Path(install) if install is not None else home / 'Library/Application Support/Venator'
    log_dir = home / "Library" / "Logs" / "venator"
    if interpreter is None:
        # The shipped app owns the scheduled code, not a moving checkout.
        interpreter = Path('/Applications/Venator.app/Contents/Resources/resources/python/aarch64-apple-darwin/python/bin/python3')
    arguments = [str(interpreter), "-m", "venator.schedule.loop"]
    if profile:
        arguments += ["--profile", profile]
    return {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "EnvironmentVariables": {
            "HOME": str(home),
            "VENATOR_HOME": str(install),
            "VENATOR_SCORE_DAILY_USD": str(daily_usd),
            "VENATOR_SCORE_MONTHLY_USD": str(monthly_usd),
            "PATH": ":".join([str(Path(claude or shutil.which('claude') or home / '.local/bin/claude').parent), *SEARCH_PATH_SUFFIX]),
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
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument("--profile", default=None, help="Profile the scheduled loop runs for")
    parser.add_argument("--interpreter", type=Path)
    parser.add_argument("--claude", type=Path)
    parser.add_argument("--install", type=Path)
    parser.add_argument("--time", default="06:30")
    parser.add_argument("--daily-usd", type=float, default=1.0)
    parser.add_argument("--monthly-usd", type=float, default=10.0)
    args = parser.parse_args()
    if sys.platform != SUPPORTED_PLATFORM:
        parser.error(WRONG_PLATFORM.format(platform=sys.platform))
    try:
        hour, minute = (int(part) for part in args.time.split(':'))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError('outside the day')
    except ValueError:
        parser.error('--time must be HH:MM')
    agent = launch_agent(
        home=args.home,
        profile=args.profile,
        interpreter=args.interpreter,
        claude=args.claude,
        install=args.install,
        hour=hour,
        minute=minute,
        daily_usd=args.daily_usd,
        monthly_usd=args.monthly_usd,
    )
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
