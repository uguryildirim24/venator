# ADR-0002: the application-data directory is the tenant boundary

**Status:** accepted; supersedes the original checkout-boundary wording

Venator is a personal application, not a multi-tenant service. One Install
belongs to one person and may contain several Profiles for that person's
searches. There is no user table, account boundary or shared server. A second
person uses a separate Install and store root.

## Decision

The Install's application-data directory is the boundary:

- `$VENATOR_HOME` when set;
- `~/Library/Application Support/Venator` on macOS;
- `$XDG_DATA_HOME/venator` or `~/.local/share/venator` on Linux;
- `%APPDATA%\Venator` on Windows.

`src/venator/paths.py` is the only module that resolves those platform paths.
Even a developer command inside a checkout uses the Install directory for
personal Profiles, append-only `data/`, and the disposable `build/` view.
Checkouts hold only source and the fictional example Profile.

A Profile is a directory of `targeting.yaml`, `constraints.yaml` and
`resume.yaml`. `--profile NAME` is a name, never a filesystem path: an empty
value, path separator or leading dot is refused. A directory without
`targeting.yaml` is not a Profile because it has no Hard Filter policy. The
Install Profile wins on a name collision, and every stage announces the resolved Profile.

The explicit development/test escape hatch `--profile-dir PATH` accepts a
Profile directory anywhere. It does not change what `--profile` means and does
not create another person inside the Install.

## Store ownership

One decisions directory and one Track directory belong to one Profile. Each
has a `.profile` stamp containing the Profile's opaque identifier. The stamp is
the enforcement:

- a second Profile is refused before an append;
- renaming a Profile directory does not change ownership;
- an unclaimed historical store remains readable;
- a `profile_id` on a row is evidence and a missing value means unknown.

Effective state remains keyed by Posting (and stage where applicable). Adding
Profile to the key would hide an accidentally interleaved store rather than
prevent it.

Profile identifiers are local opaque values, not person identifiers across
Installs. They must not contain a name, email or other personal value.

## Consequences

The loop's commit stage does not commit personal stores: the application-data
directory is outside Git. The
loopback dashboard needs no hosted authentication because it serves one local
Install; onboarding writes only beneath that Install's Profile directory.

A future hosted product would require tenant identifiers on every record,
per-tenant roots, authentication and authorization, credential isolation and a
new transport design. That is a different architecture, not a configuration
option.
