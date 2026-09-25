# ADR-0001: append-only stores, and Git no longer carries them

**Status:** accepted for append-only history. Using Git to move personal stores is
retired.

## Context

Venator's history lives in JSONL files under `data/`: Postings, Filter Decisions, Jev
results, Track events and run heartbeats. At first, Git carried that history between
machines. It no longer does. Personal stores now live only in the Install's
application data directory, outside Git, and no checkout or cloud job holds them.

## Decision

- History is append-only and split into JSONL files.
- A replay adds rows and never rewrites an earlier day file. Old rows stay readable.
- Readers take the latest row per key as the current state.
- `build/venator.db` is a view built from the Profile and the stores. It can always
  be rebuilt and is never a second source of truth.
- Every run, including one started inside a checkout, reads and writes the Install's
  application data directory, or `VENATOR_HOME` if you set it.
- The loop's `commit` stage commits `data/` only when that directory is inside a Git
  work tree. An Install's directory isn't, so the stage skips. It never pushes.

## Consequences

The stores hold evidence derived from a person's Profile and must not be committed.
Importing another Install's history deduplicates rows and appends what is missing.
The boundary between people is the application data directory, not Git and not a
checkout. ADR-0002 defines it.
