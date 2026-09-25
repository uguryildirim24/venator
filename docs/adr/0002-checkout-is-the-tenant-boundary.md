# ADR-0002: the application data directory is the boundary between people

**Status:** accepted. It replaces the original wording, which put the boundary at the
checkout.

## Context

Venator is a personal app, not a service with many users. One Install belongs to one
person and can hold several Profiles for that person's searches. There are no user
accounts, no user table and no shared server. A second person gets a separate Install
with its own stores.

## Decision

The Install's application data directory is the boundary:

- `$VENATOR_HOME` when it is set;
- `~/Library/Application Support/Venator` on macOS;
- `$XDG_DATA_HOME/venator` or `~/.local/share/venator` on Linux;
- `%APPDATA%\Venator` on Windows.

`src/venator/paths.py` is the only Python module that works out these paths. Even a
developer command run inside a checkout uses the Install directory for personal
Profiles, the append-only `data/` and the disposable `build/` view. A checkout holds
only source code and the fictional example Profile.

A Profile is a directory with `targeting.yaml`, `constraints.yaml` and `resume.yaml`.
`--profile NAME` is a name, never a path: an empty value, a path separator or a
leading dot is refused. A directory without `targeting.yaml` is not a Profile,
because that file holds the Hard Filter policy. Names are looked up in the Install
first, so the Install's Profile wins if a checkout has one with the same name. Every
stage prints which Profile it resolved.

For development and tests, `--profile-dir PATH` accepts a Profile directory anywhere.
It doesn't change what `--profile` means and doesn't add a person to the Install.

## Store ownership

Each decisions directory and each Track directory belongs to one Profile. The
directory's `.profile` stamp holds that Profile's opaque identifier, and the stamp is
what enforces ownership:

- a second Profile is refused before it can append;
- renaming a Profile directory doesn't change who owns the store;
- an old store with no stamp is still readable;
- a `profile_id` on a row is evidence only, and a missing one means unknown.

The current state is still keyed by Posting (and by stage where that applies). Adding
the Profile to the key would hide two Profiles writing into one store instead of
stopping it.

Profile identifiers are local, opaque values, not a way to identify a person across
Installs. They must never contain a name, an email or anything else personal.

## Consequences

The loop's `commit` stage doesn't commit personal stores, because the application
data directory is outside Git. The dashboard listens only on loopback and serves one
Install, so it needs no login. Onboarding writes only inside that Install's Profile
directory.

A hosted version would need tenant identifiers on every record, a root per tenant,
login and permissions, separate credentials per tenant, and a new way to move data.
That is a different design, not a setting.
