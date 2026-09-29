"""Return the Hard Filter version of a Profile after an edit."""
from __future__ import annotations

import sys
from pathlib import Path

from venator.match.store import filters_version
from venator.profile import load_profile


def main() -> None:
    profile = load_profile(Path(sys.argv[1]))
    print(filters_version(profile.constraints_path, profile.targeting_path))


if __name__ == "__main__":
    main()
