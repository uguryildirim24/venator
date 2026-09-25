/**
 * One writer at a time, per Profile, inside this server process.
 *
 * **The defect this closes.** Both writers on this surface read, decide, and then replace a
 * file with a rename. Between the read and the rename each of them now `await`s the load-back
 * — a real subprocess — and an `await` is a yield. Two `POST /api/onboarding/employers`
 * arriving together therefore both read the same `targeting.yaml`, both compute an edit against
 * it, and both rename their own staging file over the target: the second write wins, the first
 * employer is gone, and *both* requests answered `200` with `"changed": true` and their own
 * board under `"added"`. That is a route reporting a write that did not happen, on the one
 * route that exists to rescue somebody stranded with no employer registered, in a repository
 * whose standing rule is that there are no silent drops anywhere.
 *
 * It was not there before the load-back: the old code was synchronous end to end, and Node's
 * single thread was the serialisation. Making the function `async` removed the guarantee
 * without changing a line of the read-modify-write.
 *
 * **Why serialising rather than re-checking.** A re-read after the `await` narrows the window
 * to the rename itself; it does not close it, and the next `await` anybody adds reopens it. A
 * queue closes it by construction and keeps closing it however the body is rewritten, so the
 * property does not have to be re-derived every time one of these functions grows a step.
 *
 * **What the key is.** The Profile's own directory under this Install's writable Profiles root,
 * which both writers can name before they touch anything — `writeProfile` creating one and
 * `registerEmployers` editing one file of one that exists. Keying on the directory rather than
 * on the file means creating a Profile and registering an employer into it are ordered against
 * each other too, and keying on the *Profile* rather than on a single global lock means one
 * person's two Profiles do not wait on each other.
 *
 * **Why the key is canonicalised, and what that form is for.** A directory path is a byte-exact
 * string here and a directory on the filesystem there, and on two of the three platforms this
 * ships to those are not the same thing: `sample`, `Sample` and `sample.` are three names
 * `validateProfileName` accepts and three keys a byte-exact map holds, but one directory on
 * APFS and on NTFS, which compare case-insensitively and — on Windows — strip a trailing dot.
 * Two requests meaning one Profile would serialise against nothing there, which is the lost
 * update this file exists to close, wearing a spelling. So the key is folded to lower case
 * with trailing dots stripped. It is deliberately blunt rather than exact: over-merging two
 * genuinely distinct Profiles on Linux costs a little needless serialisation that nobody
 * notices, and under-merging costs a write, so this errs toward over-merging on purpose.
 *
 * That canonical form is **a map key and nothing else**. Resolving an existing prefix may
 * identify the directory, but it must never create, open for writing or name the write target:
 * the moment it decides where bytes land, somebody's Profile is written to a directory they did
 * not name, which is worse than the defect above. `profileDirectoryFor` and every path that
 * touches the filesystem keep using the name exactly as it was given.
 *
 * **What must not be nested.** `oneWriterPerProfile` inside `oneWriterPerProfile` for the same
 * key deadlocks permanently — the inner call waits on a tail that only settles when the outer
 * `work` returns — and pins that map entry for the life of the process. Both call sites are
 * top-level and nothing else in `ui/server/` writes into `profiles/` at all; this sentence is
 * here to keep that true.
 *
 * **What `work` must not do.** The map entry is dropped once `work`'s promise settles, and that
 * eviction is safe only because settling means the side effects are done. A `work` that started
 * a rename and returned without awaiting it would be evicted while still writing, and the next
 * request would run beside it — the original defect, reintroduced from the inside.
 *
 * **What it does not close, deliberately.** Two server *processes* against the same
 * application data directory still race: the run surface's port is fixed and an existing
 * listener is adopted rather than a second one started, so this does not normally arise, but
 * nothing here prevents it. Closing that needs a lock on the filesystem and a decision about
 * what a stale one means after a crash — a wider design question than this surface.
 */

import { realpathSync } from "node:fs";
import { basename, dirname, join } from "node:path";

/**
 * The tail of each Profile's queue: a promise that settles when the write in front is done.
 *
 * It never rejects — a failed write must not take the queue down with it — and the entry is
 * dropped once nothing is behind it, so the map holds one key per write in flight rather than
 * one per Profile name anybody has ever named.
 */
const queues = new Map<string, Promise<void>>();

/**
 * The map key for a Profile directory: its real existing prefix, lower-cased, with trailing dots
 * stripped. Resolving only the existing prefix also covers a Profile that is not created yet.
 *
 * **Never a write path.** The real path identifies the queue only. Nothing may create, open for
 * writing or name a directory from this key.
 *
 * Blunt on purpose. `validateProfileName` confines a name to `[A-Za-z0-9._-]`, so lower-casing
 * is an ASCII fold over the part that varies, and a trailing dot is the other thing Windows
 * drops. Anything cleverer buys exactness this does not need and surprises nobody wants: two
 * distinct Profiles sharing a key merely wait on each other, and that is the safe mistake.
 */
function queueKey(profileDirectory: string): string {
	const missing: string[] = [];
	let existing = profileDirectory;
	for (;;) {
		try {
			return join(realpathSync(existing), ...missing).replace(/\.+$/u, "").toLowerCase();
		} catch (cause) {
			// SAFETY: Node filesystem errors expose `code`; a non-filesystem error has no code.
			const code = cause instanceof Error ? (cause as NodeJS.ErrnoException).code : undefined;
			if (code !== "ENOENT") return profileDirectory.replace(/\.+$/u, "").toLowerCase();
		}
		const parent = dirname(existing);
		if (parent === existing) return profileDirectory.replace(/\.+$/u, "").toLowerCase();
		missing.unshift(basename(existing));
		existing = parent;
	}
}

/** Runs `work` after every write already queued for this Profile, and never beside one. */
export function oneWriterPerProfile<T>(profileDirectory: string, work: () => Promise<T>): Promise<T> {
	const key = queueKey(profileDirectory);
	const tail = queues.get(key) ?? Promise.resolve();
	// `tail` cannot reject, so `work` always runs; `mine` carries the caller's own outcome.
	const mine = tail.then(work);
	const settled = mine.then(
		() => undefined,
		() => undefined,
	);
	queues.set(key, settled);
	void settled.then(() => {
		// Only if nothing queued behind this one, which would have replaced the entry already.
		if (queues.get(key) === settled) queues.delete(key);
	});
	return mine;
}

/** How many Profiles have a write in flight or queued. For the tests that assert no leak. */
export function writesInFlight(): number {
	return queues.size;
}
