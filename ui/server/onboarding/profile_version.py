"""Read the Filter Decision version of an existing Profile without writing stores."""
from __future__ import annotations

import argparse
from pathlib import Path

from venator.paths import install_stores
from venator.profile.loader import load_profile
from venator.qualify.jev_effective import current_filter_version, jev_promotion_state
from venator.qualify.jev_release import JEV_RELEASE
from venator.qualify.store import promotion_events


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    profile = load_profile(args.directory)
    promotion = None
    release = None
    if profile.filters.qualification_mode == "jev":
        qualifications = install_stores().qualifications_dir
        promotion = jev_promotion_state(promotion_events(qualifications), profile.identifier)
        release = JEV_RELEASE
    print(current_filter_version(profile, promotion, release))


if __name__ == "__main__":
    main()
