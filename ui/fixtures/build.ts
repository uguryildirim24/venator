/**
 * `pnpm fixture` — rebuilds the fixture view from scratch.
 *
 * The builder itself is a library with no side effects on import: the server imports it to
 * fall back when no pipeline view exists, and the desktop bundle inlines it, so running work
 * at import time would fire in both places.
 */

import {
	buildFixtureDatabase,
	FIXTURE_DECISION_COUNT,
	FIXTURE_POSTING_COUNT,
} from "./make-fixture.ts";
import { FIXTURE_DATABASE_PATH } from "../server/locations.ts";

buildFixtureDatabase(FIXTURE_DATABASE_PATH);
process.stdout.write(
	`fixture rebuilt: ${FIXTURE_DATABASE_PATH} (${FIXTURE_POSTING_COUNT} postings, ${FIXTURE_DECISION_COUNT} decisions)\n`,
);
