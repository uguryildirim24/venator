# ADR-0001: append-only stores and optional Git transport

**Status:** accepted for append-only history; Git transport retired for personal stores

Venator's source of truth is the JSONL history under `data/`. Postings, Filter
Decisions, qualifications, Track events and run heartbeats append new records;
a replay never rewrites an earlier day file. Readers derive effective state from
the latest row for the relevant key. `build/venator.db` is a disposable
materialized view, not a second source of truth.

The original deployment used Git to carry the history between machines. Personal
stores now live only in the Install's application-data directory, outside Git.
No checkout or cloud agent carries this corpus.

## Decision

- Operational history is append-only and partitioned into JSONL files.
- Replays append. Historical rows remain readable.
- The SQLite dashboard database can always be rebuilt from Profile and store
  data.
- Every run, including one started inside a checkout, reads and writes the
  Install's application-data directory (or an explicitly selected `VENATOR_HOME`).
- The commit stage skips personal stores; it never pushes them.

## Consequences

Stores contain sensitive Profile-derived evidence and must not be committed.
Concurrent imports deduplicate observations and append missing history. The application-data directory,
not Git or a checkout, is the boundary between people; ADR-0002 defines it.
