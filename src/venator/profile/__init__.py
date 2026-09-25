"""The Profile: one person's canonical facts, constraints, and targeting.

A Profile directory holds ``resume.yaml``, ``constraints.yaml``, and
``targeting.yaml``. It lives under ``profiles/`` in a checkout or under this
Install's application data directory (``venator.paths``); every stage reads a
Profile rather than a hardcoded path, so the same Install runs for whoever owns
it.
"""

from __future__ import annotations

from venator.profile.loader import PROFILE_FILES, ProfileError, load_profile
from venator.profile.schema import (
    DEGREE_LEVELS,
    JEV_DOMAIN_TOKENS,
    JEV_EXCLUDE_OR_REVIEW,
    JEV_RETAIN_OR_REVIEW,
    SHIFT_PREFERENCES,
    BoardRegistry,
    EducationFitPolicy,
    FilterPolicy,
    JevPolicy,
    Matcher,
    Profile,
    RoleLevel,
    RoleTargetPolicy,
    SearchTargeting,
    TimingPolicy,
    WorkAuthorizationPolicy,
    new_profile_identifier,
)
from venator.profile.select import (
    PROFILE_ENV,
    PROFILES_DIR,
    REQUIRED_PROFILE_FILE,
    add_profile_argument,
    announce_profile,
    available_profiles,
    holds_profile_files,
    profile_for_constraints,
    profile_from_arguments,
    profile_search_path,
    resolve_profile,
    resolve_profile_dir,
)

__all__ = [
    "DEGREE_LEVELS",
    "JEV_DOMAIN_TOKENS",
    "JEV_EXCLUDE_OR_REVIEW",
    "JEV_RETAIN_OR_REVIEW",
    "JevPolicy",
    "SHIFT_PREFERENCES",
    "PROFILES_DIR",
    "PROFILE_ENV",
    "PROFILE_FILES",
    "REQUIRED_PROFILE_FILE",
    "BoardRegistry",
    "EducationFitPolicy",
    "FilterPolicy",
    "Matcher",
    "Profile",
    "ProfileError",
    "RoleLevel",
    "RoleTargetPolicy",
    "SearchTargeting",
    "TimingPolicy",
    "WorkAuthorizationPolicy",
    "add_profile_argument",
    "announce_profile",
    "available_profiles",
    "holds_profile_files",
    "load_profile",
    "new_profile_identifier",
    "profile_for_constraints",
    "profile_from_arguments",
    "profile_search_path",
    "resolve_profile",
    "resolve_profile_dir",
]
