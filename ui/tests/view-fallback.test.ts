/**
 * What a person is served when this Install has no view database of its own.
 *
 * The answer used to be the dashboard's sample database — nineteen Postings from real
 * employers, none of them the Owner's — and the only thing saying so was a badge in the
 * header. The queue's own first-run screen could not help: it renders when
 * `funnel.discovered === 0`, and nineteen is not zero. So somebody who had just installed the
 * app landed on a full review queue of a stranger's job search.
 *
 * `resolveView` in `server/db.ts` is where that is decided now, and these are the cases it
 * decides. The rule has two halves and both are asserted below:
 *
 * - Sample data is served **only to a run that asked for it** (`$VENATOR_SAMPLE_DATA`), and
 *   asking is something only `pnpm dev`, `pnpm smoke` and `pnpm snapshot` do.
 * - The check is on the data rather than on the route it arrived by, so a fixture named
 *   outright in `$VENATOR_VIEW_DB` is refused by the same line. That case is not theoretical:
 *   the desktop host used to resolve its bundled copy and pass it in exactly that way.
 *
 * What an Install with no view gets instead is the contract's schema with no rows in it —
 * every route renders the truth, and nothing renders as a failure.
 */

import assert from "node:assert/strict";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";

import {
	buildEmptyViewDatabase,
	buildFixtureDatabase,
	emptyViewDatabase,
	FIXTURE_ON_SCREEN_TEXT,
	FIXTURE_POSTING_COUNT,
} from "../fixtures/make-fixture.ts";
import { openViewDatabase, resolveView } from "../server/db.ts";
import { FIXTURE_DATABASE_PATH, type LocationContext } from "../server/locations.ts";

/** Pinned rather than inherited: an older builder's file would make the counts below lie. */
buildFixtureDatabase(FIXTURE_DATABASE_PATH);

/**
 * A machine, stated. `checkoutRoot` is null throughout: a developer's clone is a candidate
 * view location, and a case that let this host's own checkout answer would pass or fail
 * depending on whether whoever ran it had built one.
 *
 * The platform is stated too, and the two cases below that turn on a path run on all three of
 * them. That is not thoroughness for its own sake — the first version of this file took
 * `process.platform` and wrote the Linux answer down, which passed here and failed on the
 * Windows runner against code that was right on both. A stated platform is the only way a
 * machine other than this one gets checked at all.
 */
function machine(
	home: string,
	environment: Readonly<Record<string, string>>,
	platform: NodeJS.Platform = process.platform,
): LocationContext {
	return {
		platform,
		home,
		workingDirectory: home,
		checkoutRoot: null,
		environment: (name) => environment[name],
	};
}

/**
 * The three this app is packaged for. `node:path` is still the host's — a case naming `win32`
 * on Linux exercises the Windows *branch* and writes the host's separators, which is the same
 * split `tests/locations.test.ts` sets out at length.
 */
const PLATFORMS: readonly NodeJS.Platform[] = ["linux", "darwin", "win32"];

function temporaryHome(): string {
	return mkdtempSync(join(tmpdir(), "venator-view-"));
}

/**
 * This Install's data directory, named outright.
 *
 * `$VENATOR_HOME` rather than the platform's usual place, and that is not a shortcut: the
 * usual place is three different directories, and it is resolved ahead of every one of them on
 * every platform. Pinning the layout per platform is `tests/locations.test.ts`'s job; what is
 * under test here is which database is served out of a data directory, whichever one it is —
 * so the cases say which directory rather than working it out and getting it wrong.
 */
function dataDirectory(home: string) {
	return { VENATOR_HOME: join(home, "install-data") };
}

/** Where `python -m venator.view.build` writes this Install's view. */
function viewIn(home: string): string {
	return resolve(home, "install-data", "build", "venator.db");
}

test("an Install that has run nothing is served an empty view, not somebody else's Postings", () => {
	const home = temporaryHome();
	try {
		for (const platform of PLATFORMS) {
			const view = resolveView(machine(home, dataDirectory(home), platform));
			assert.equal(view.kind, "empty", `on ${platform}`);
			// The path names where a view will appear, so the header and the onboarding state have
			// something true to say rather than going silent.
			assert.equal(view.path, viewIn(home), `on ${platform}`);
			assert.equal(existsSync(view.path), false, "and nothing was created to make that true");
		}
	} finally {
		rmSync(home, { recursive: true, force: true });
	}
});

test("the sample Postings are served to a run that asks for them, and only to one", () => {
	const home = temporaryHome();
	try {
		const environment = dataDirectory(home);
		const asked = resolveView(machine(home, { ...environment, VENATOR_SAMPLE_DATA: "1" }));
		assert.deepEqual(asked, { path: FIXTURE_DATABASE_PATH, kind: "fixture" });

		// The same machine, one variable apart.
		assert.equal(resolveView(machine(home, environment)).kind, "empty");

		// Blank is not asking. `configured()` treats a whitespace-only value as unset
		// everywhere else in locations.ts, and a variable that is set to nothing is somebody's
		// empty shell export rather than a request for a stranger's job search.
		assert.equal(resolveView(machine(home, { ...environment, VENATOR_SAMPLE_DATA: "   " })).kind, "empty");
	} finally {
		rmSync(home, { recursive: true, force: true });
	}
});

test("a view this Install built wins, and asking for sample data does not take it away", () => {
	const home = temporaryHome();
	try {
		const environment = dataDirectory(home);
		buildEmptyViewDatabase(viewIn(home));

		for (const platform of PLATFORMS) {
			assert.deepEqual(
				resolveView(machine(home, environment, platform)),
				{ path: viewIn(home), kind: "pipeline" },
				`on ${platform}`,
			);
			// `pnpm dev` sets the ask on every run; a developer with a real view must still get it.
			assert.deepEqual(
				resolveView(machine(home, { ...environment, VENATOR_SAMPLE_DATA: "1" }, platform)),
				{ path: viewIn(home), kind: "pipeline" },
				`on ${platform}`,
			);
		}
	} finally {
		rmSync(home, { recursive: true, force: true });
	}
});

test("a fixture named in VENATOR_VIEW_DB is refused too, which is what the desktop host did", () => {
	const home = temporaryHome();
	try {
		const environment = { ...dataDirectory(home), VENATOR_VIEW_DB: FIXTURE_DATABASE_PATH };
		// Named by a host on somebody's behalf is not that person asking. The bundled copy is
		// gone as well (`src-tauri/src/lib.rs`), but the gate does not depend on that.
		assert.deepEqual(resolveView(machine(home, environment)), { path: FIXTURE_DATABASE_PATH, kind: "empty" });
		assert.deepEqual(resolveView(machine(home, { ...environment, VENATOR_SAMPLE_DATA: "1" })), {
			path: FIXTURE_DATABASE_PATH,
			kind: "fixture",
		});
	} finally {
		rmSync(home, { recursive: true, force: true });
	}
});

test("sample data is recognised by the file's name, and that is the whole of the heuristic", () => {
	const home = temporaryHome();
	try {
		// A copy of the fixture under another name is served as a pipeline view. Stated rather
		// than glossed: recognition is `basename`, the same test that badges the header, and
		// putting a copy somewhere and naming it is a deliberate act by whoever did it.
		const copy = join(home, "venator.db");
		buildFixtureDatabase(copy);
		const view = resolveView(machine(home, { ...dataDirectory(home), VENATOR_VIEW_DB: copy }));
		assert.deepEqual(view, { path: copy, kind: "pipeline" });
	} finally {
		rmSync(home, { recursive: true, force: true });
	}
});

/**
 * The end of the pipe: what the API actually opens, through `process.env` rather than a stated
 * machine, because that is how the server reads its world. `$VENATOR_VIEW_DB` names the
 * fixture in both cases, so neither depends on what this machine has built.
 */
function withEnvironment<Result>(overrides: Readonly<Record<string, string | undefined>>, run: () => Result): Result {
	const previous = new Map<string, string | undefined>();
	for (const [name, value] of Object.entries(overrides)) {
		previous.set(name, process.env[name]);
		if (value === undefined) delete process.env[name];
		else process.env[name] = value;
	}
	try {
		return run();
	} finally {
		for (const [name, value] of previous) {
			if (value === undefined) delete process.env[name];
			else process.env[name] = value;
		}
	}
}

function postingCount() {
	const view = openViewDatabase();
	try {
		const row = view.database.prepare("SELECT COUNT(*) AS count FROM postings").get();
		return { kind: view.kind, postings: Number(row?.["count"]) };
	} finally {
		view.database.close();
	}
}

test("the API opens no Posting it was not asked for, and every one it was", () => {
	const refused = withEnvironment(
		{ VENATOR_VIEW_DB: FIXTURE_DATABASE_PATH, VENATOR_SAMPLE_DATA: undefined },
		postingCount,
	);
	assert.deepEqual(refused, { kind: "empty", postings: 0 });

	const asked = withEnvironment({ VENATOR_VIEW_DB: FIXTURE_DATABASE_PATH, VENATOR_SAMPLE_DATA: "1" }, postingCount);
	assert.deepEqual(asked, { kind: "fixture", postings: FIXTURE_POSTING_COUNT });
	assert.ok(FIXTURE_POSTING_COUNT > 0, "the refused case above is only evidence if there was something to refuse");
});

test("the words the containment check looks for are every word the fixture could show", () => {
	/*
	 * `tools/smoke.ts` proves the containment by rendering the routes against a fixture nobody
	 * asked for and requiring that none of `FIXTURE_ON_SCREEN_TEXT` is in the markup. That is
	 * only as good as the list, and a list that had quietly gone empty would pass every check
	 * in it while looking exactly like a list that was doing its job.
	 *
	 * So the list is checked against the database it describes rather than against itself:
	 * every employer and every title the fixture actually holds has to be in it. Derived on
	 * both sides, so a Posting added to the fixture is covered without anyone remembering.
	 */
	const view = withEnvironment(
		{ VENATOR_VIEW_DB: FIXTURE_DATABASE_PATH, VENATOR_SAMPLE_DATA: "1" },
		() => openViewDatabase(),
	);
	try {
		// The employer is nullable in the view and a board with no name really does arrive as
		// NULL, so SQL drops those rather than the reader guessing at what a value is.
		const employers = view.database.prepare("SELECT DISTINCT company FROM postings WHERE company IS NOT NULL").all();
		const titles = view.database.prepare("SELECT DISTINCT title FROM postings").all();
		assert.ok(employers.length > 0 && titles.length > 0, "the fixture holds Postings to be protected from");
		for (const row of [...employers, ...titles]) {
			const word = String(row["company"] ?? row["title"]);
			assert.ok(FIXTURE_ON_SCREEN_TEXT.includes(word), `“${word}” is looked for`);
		}
	} finally {
		view.database.close();
	}
});

test("the empty view answers every table the contract names, with nothing in any of them", () => {
	// It is served to a real Install, so it is held to the schema `assertContractSchema` checks
	// rather than to being merely openable — a view missing a column fails at open, and an
	// Install that has run nothing would then show the dashboard's red banner instead of its
	// first-run screen. `openViewDatabase` runs that check; this states what has to be in it.
	const database = emptyViewDatabase();
	try {
		for (const table of ["postings", "decisions", "application_states", "track_events", "runs"]) {
			const row = database.prepare(`SELECT COUNT(*) AS count FROM ${table}`).get();
			assert.equal(Number(row?.["count"]), 0, `${table} is empty`);
		}
	} finally {
		database.close();
	}
});
