/**
 * `pnpm smoke` — starts the API against the fixture, renders every route of the real app, and
 * asserts the data reached the screen. This is the stand-in for clicking through the
 * dashboard: the same components the browser mounts, failing loudly if a view throws or comes
 * up empty. It pins the fixture so the assertions describe known data rather than whatever the
 * pipeline last wrote.
 *
 * Three passes, because two of them are about an Install at a particular moment of its life and
 * one is about an Install that must not be shown somebody else's:
 *
 * 1. The fixture — a pipeline mid-flight — for the dashboard's routes, and for setting up a
 *    Profile, which is readable whatever the database holds. **These passes ask for the
 *    sample data**, which is now the only way to be served it: the server refuses it to a run
 *    that did not ask, however the fixture reached it (`server/db.ts`). Asking out loud is
 *    what keeps these checks about the dashboard rather than about the fallback.
 * 2. A view with the schema and no rows — a brand new Install — for the screen somebody sees
 *    straight after finishing setup. An empty pipeline is not an error and this is what pins
 *    that it does not render as one.
 * 3. The fixture again, with nobody asking for it: exactly what a shipped Install is, sat on
 *    a machine where the sample database happens to exist. Nothing of it may reach the screen.
 *    That pass is the one this file gained when the fallback changed, and it is what stops the
 *    change from being a fixture nobody looks at: the same fixture, the same routes, and the
 *    assertion inverted.
 *
 * Then the fixture twice more as the desktop host, once as a Mac and once as Windows, for the
 * one property of the window that is not words: the frame.
 *
 * Every check in every pass is also held to one rule the design makes absolute: **no Jev
 * number and no score, anywhere.** The view still holds both, so the values are read out of the
 * database the pass serves and looked for on every screen, and the first pass also asks the
 * API for every Posting and proves neither number crosses the wire. See `HELD_NUMBERS`.
 */

import { readFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";

import {
	buildEmptyViewDatabase,
	buildFixtureDatabase,
	FIXTURE_BOARDS,
	FIXTURE_ON_SCREEN_TEXT,
} from "../fixtures/make-fixture.ts";
import { FIXTURE_DATABASE_PATH } from "../server/locations.ts";
import { formOf } from "../server/onboarding/profile-form.ts";
import type { PlanResponse, RunPlan } from "../shared/runs.ts";
import { SLOW_MS } from "../src/delayed.ts";
import { SAMPLE_DATA_STAMP } from "../src/labels.ts";
import { CONNECT, FIRST_RUN, PROFILE, refreshBody, RESUME, TARGETING, THIS_MAC_CAPTION } from "../src/views/onboarding/copy.ts";
import { startApi } from "./api-process.ts";
import { startRenderer, type FixtureResponse, type Host, type RouteRenderer } from "./dom-harness.ts";
import { checkLabelContract } from "./label-contract.ts";

const SMOKE_PORT = 5171;
const EMPTY_SMOKE_PORT = 5172;
const SHIPPED_SMOKE_PORT = 5176;
const WINDOWS_DESKTOP_PORT = 5174;
const MACOS_DESKTOP_PORT = 5175;

/** Built beside the fixture, and gitignored by the same rule. */
const EMPTY_DATABASE_PATH = join(dirname(FIXTURE_DATABASE_PATH), "venator.empty.db");

type RouteCheck = {
	readonly hash: string;
	readonly label: string;
	readonly expected: readonly string[];
	/** Navigate actual tabs/disclosures before checking the resulting screen. */
	readonly press?: string | readonly string[];
	/**
	 * Text that must NOT be in the markup. Used where the absence is the property under test —
	 * the runtime step renders without probing anything, so nothing it could only know from a
	 * probe may be on screen.
	 */
	readonly absent?: readonly string[];
	/**
	 * Visible text that must appear, in this order, down the screen. No active check uses this
	 * legacy hook; it remains available to tests that need to pin a non-ranking sequence.
	 *
	 * `expected` cannot say anything about order, and order is the whole of some properties.
	 * No numerical score is rendered or used for ordering. Status labels and evidence summaries
	 * are asserted directly instead.
	 */
	readonly ordered?: readonly string[];
	/**
	 * Board tokens this screen is allowed to show because the person put them there.
	 *
	 * There is exactly one: a Posting opened by URL that the view does not hold. The app
	 * answers by quoting the key it was asked for, and a key carries its board token — so the
	 * token on screen came out of the address bar rather than out of a Posting the app
	 * captioned, which is the thing `leakedBoardTokens` exists to catch.
	 *
	 * It buys nothing for a check that renders a Posting: the fixture's every employer and
	 * every title is still in that check's `absent`, so a Posting that actually leaked fails
	 * on its own content whatever this list says.
	 */
	readonly echoed?: readonly string[];
};

/**
 * Machine vocabulary that must never reach the owner's eyes. Checked against visible text
 * only — the raw values are still allowed in attributes and hrefs, which is where the app
 * keeps them for traceability.
 *
 * Every entry is a word no one would say out loud: a snake_case rule name, a status slug —
 * or "fixture", which is this repository's name for the sample database and testing jargon
 * besides. The `duplicate` rule is deliberately NOT one — "duplicate" is a plain English word
 * the owner reads and understands, `visibleText` covers Posting titles and reasons where it
 * can legitimately appear, and the rendered label is capital-D "Duplicate" anyway. Banning
 * it would fail the build on the very copy it is supposed to protect.
 *
 * Board tokens used to be listed here too, five of them, hand-picked from the fixture. That
 * is the shape of check that let the bug live: it could only see the boards someone had
 * already thought of, so when the Profile grew to thirty-five boards across Workday and
 * Lever, `Amgen.wd1~Careers` went on screen and this list had nothing to say about it. They
 * are now derived instead — see `leakedBoardTokens`.
 *
 * **One deliberate exemption, and it is the only one.** A run that did not succeed can show
 * the tail of what the pipeline printed (`src/runs/status.tsx`), folded away and captioned as
 * the pipeline's own output rather than as dashboard prose. Discover prints a line per board,
 * so a board token can appear in it — and that is the point: text this repository wrote for
 * this person, about the run that just failed, beats a generic apology. It applies to that
 * one block and to nothing else, no run fails during these checks, and every other sentence
 * the run controls render is held to this list like all the rest. `plan.notes` in particular
 * carries no filesystem path for exactly this reason.
 */
const FORBIDDEN_ON_SCREEN: readonly string[] = [
	"fixture",
	"education_fit",
	"work_authorization",
	"role_target",
	"hard-killed",
	"needs-review",
	"qualifier_version",
	"req-",
	"ob-",
	"exp-",
	"edu-",
	"hist",
	"fit_probability",
	"fit_score",
	"primary_rule",
	"as_of_month",
	"typesafe_jev",
	"jev-assessment",
	"jev_triage",
	"qualifier_kind",
];

/**
 * Board tokens are shortest-lived vocabulary in the app and the easiest to leak: they arrive
 * from the ATS, they are the only identifier a Posting carries that the owner has no reason
 * to recognise, and every one of them appears in some href. So rather than name them, take
 * them from the fixture — whatever Postings it holds today — and let the condition be
 * structural: a board must not appear in visible text, whatever board it is.
 *
 * Two tokens are unscannable rather than allowed, and both are stated rather than quietly
 * skipped:
 *
 * - A token under three characters can be a historical aggregator's region code. "us" is a
 *   substring of ordinary prose, so its presence is not evidence of anything. Nothing
 *   captions an aggregator's board with it either — `employerLabel` returns the employer the
 *   view carries or null.
 * - A token that case-folds to exactly the employer's own name — board `asimov`, employer
 *   "Asimov" — is indistinguishable from the correct caption once folded, so it cannot be
 *   evidence either way. The tokens that matter are precisely the ones that are not names:
 *   `ginkgobioworks`, `LyciaTherapeutics`, `amgen.wd1~Careers`.
 *
 * Both sides are compared through `folded`, because a token does not reach the screen
 * verbatim — the fallback that caused this ran it through `sentenceCase`, which capitalises
 * the first letter and turns `-` and `_` into spaces. A literal scan for
 * `roche.wd3~ROG-A2O-GENE` sails straight past the `Roche.wd3~ROG A2O GENE` actually
 * rendered, which is how a guard comes to pass while looking directly at the bug.
 *
 * Folding costs one more class of token: a board whose dashes are its word breaks, like
 * `sequel-med-tech` for "Sequel Med Tech", folds onto its own employer name and is skipped by
 * the same rule that skips `asimov`. It is the same trade and it is the honest one — after
 * folding there is nothing left to tell the leak from the correct caption.
 */
function folded(value: string): string {
	return value.toLowerCase().replaceAll("_", " ").replaceAll("-", " ");
}

const SCANNED_BOARD_TOKENS: readonly string[] = FIXTURE_BOARDS.filter(
	(entry) => entry.board.length >= 3 && folded(entry.board) !== folded(entry.company ?? ""),
).map((entry) => entry.board);

function leakedBoardTokens(text: string): readonly string[] {
	const haystack = folded(text);
	return SCANNED_BOARD_TOKENS.filter((token) => haystack.includes(folded(token)));
}

/**
 * The numbers the view holds that no screen may show.
 *
 * Jev writes `fit_probability` and `fit_score` for every Posting it assessed, and a historical
 * `llm_score` row carries a Match Score. The view keeps all three, because the data is
 * append-only and a view is a projection of it. None of them may be rendered (ui/DESIGN.md:
 * "No Jev number and no score is rendered anywhere"). So they are read out of the database
 * this run is about to serve — not typed out, for the reason the board tokens are not — and
 * every way one could be written is looked for in visible text:
 *
 * - any fraction written as a decimal, and any percentage, whatever the fixture holds: no
 *   screen here has a reason to print either;
 * - every held score, and every held probability as a whole number out of 100, as a bare
 *   number or "out of 100", wherever it is not part of a longer number, a time or a date.
 *
 * A dollar amount is not a fraction and passes: a Jev it plan may say what it would spend, and
 * the count it states is not a Jev result.
 */
type HeldNumbers = {
	readonly fractions: readonly number[];
	readonly scores: readonly number[];
};

function readHeldNumbers(path: string): HeldNumbers {
	const database = new DatabaseSync(path, { readOnly: true });
	try {
		const fractions = database
			.prepare(
				"SELECT fit_probability AS value FROM jev_triage WHERE fit_probability IS NOT NULL " +
					"UNION SELECT fit_score AS value FROM jev_triage WHERE fit_score IS NOT NULL",
			)
			.all()
			.map((row) => Number(row.value));
		const scores = database
			.prepare("SELECT DISTINCT score AS value FROM decisions WHERE score IS NOT NULL")
			.all()
			.map((row) => Number(row.value));
		return { fractions, scores };
	} finally {
		database.close();
	}
}

const DECIMAL_FRACTION = /(?<![\w.$])0?\.\d+/u;
const PERCENTAGE = /\d+(?:\.\d+)?\s?%/u;

function wholeNumbersOf(held: HeldNumbers): readonly number[] {
	return [...new Set([...held.scores, ...held.fractions.map((value) => Math.round(value * 100))])];
}

function numbersOnScreen(text: string, wholes: readonly number[]): readonly string[] {
	const found: string[] = [];
	for (const refused of [DECIMAL_FRACTION, PERCENTAGE]) {
		const match = refused.exec(text);
		if (match !== null) found.push(match[0]);
	}
	for (const whole of wholes) {
		const match = new RegExp(`(?<![\\w:.,$/-])${whole}(?:/100)?(?![\\w:])`, "u").exec(text);
		if (match !== null) found.push(match[0]);
	}
	return found;
}

/**
 * Every key the API could name a Jev number or a score by, in either spelling, as it appears in
 * the body: a quoted name followed by a colon. Scanning the text rather than a parsed value is
 * the point — a field nothing on screen reads is still on the wire.
 */
const NUMBER_FIELD = /"(?:fit_?probability|fit_?score|score|probability)"\s*:/giu;

/**
 * The entries of `ordered` that are missing, or that arrive before the one meant to precede
 * them. Scanning forward from the last match is what makes this an order check rather than
 * six independent ones.
 */
function outOfOrder(text: string, ordered: readonly string[]): readonly string[] {
	const failures: string[] = [];
	let cursor = 0;
	for (const needle of ordered) {
		const at = text.indexOf(needle, cursor);
		if (at === -1) failures.push(needle);
		else cursor = at + needle.length;
	}
	return failures;
}

function visibleText(markup: string): string {
	return markup.replaceAll(/<iframe[\s\S]*?<\/iframe>/gu, " ").replaceAll(/<[^>]*>/gu, " ");
}

const DETAIL_KEY = encodeURIComponent("greenhouse:generatebiomedicines:4728990");
const TESSERA_KEY = encodeURIComponent("greenhouse:tesseratherapeutics:4901233");
const WORKDAY_KEY = encodeURIComponent("workday:amgen.wd1~Careers:R-98765");

/** Markup escapes these three in text, so an expected title has to be written the same way. */
function asMarkup(text: string): string {
	return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

const CHECKS: readonly RouteCheck[] = [
	{
		hash: "#/queue",
		label: "For you: the sidebar, the list, and the selected Posting as a page with its margin",
		expected: [
			// The sidebar's three sections, the sample-data stamp, and the footer's two run controls.
			"For you",
			"Explore",
			"Awaiting Jev",
			"Jev diagnostics",
			"Applications",
			"Saved",
			"Dismissed",
			"Excluded",
			"Employers",
			"Profile",
			SAMPLE_DATA_STAMP,
			'aria-label="Refresh jobs"',
			">Jev it<",
			// The list: a toolbar count with the new set, and the first cell selected.
			"1 Posting",
			'role="listbox"',
			'aria-selected="true"',
			"Summer 2027 Intern, Automation &amp; Lab Informatics",
			"Jev: look first — An estimate, not a verdict",
			// The page, and Venator's notes in the margin beside it.
			"Verified open",
			"Prepare Application…",
			"You press submit",
			"Hard Filters passed",
			"Read Full Description",
		],
		// A browser tab draws its own window; only the desktop host is native.
		absent: ["app-native", "Match strength", "Priority rank"],
	},
	{
		hash: "#/queue?page=1",
		label: "For you: a page past the end says so rather than repeating the first",
		expected: ["No Postings on this page", "Go back a page to see it."],
		absent: ["Process Development Intern, Drug Substance Technologies"],
	},
	{
		hash: "#/queue?list=needs-review",
		label: "Explore: Postings whose evidence needs a look, with the reason in the margin",
		expected: [
			"1 Posting",
			"Data Engineering Intern (Remote, US)",
			"Jev: review — An estimate, not a verdict",
		],
	},
	{
		hash: "#/queue?list=applied",
		label: "Applications: empty, said as a fact with where the rest went",
		expected: ["Applications is empty", "No application is in progress.", "Saved and dismissed Postings have their own lists."],
	},
	{
		hash: "#/queue?list=saved",
		label: "Saved: the saved Posting, with résumé evidence and early review in the margin",
		expected: [
			"1 Posting",
			"Undergraduate Summer Research Program 2027 (Housing Provided)",
			"On your résumé",
		],
	},
	{
		hash: "#/queue?list=dismissed",
		label: "Dismissed: empty, and restorable when it is not",
		expected: ["Dismissed is empty", "A dismissed Posting keeps its place here and can be restored."],
	},
	{
		hash: "#/queue?list=filtered",
		label: "Excluded: each Posting with its rule, and no application offered",
		expected: [
			"9 Postings",
			"Jev: skip — An estimate, not a verdict",
			"Location",
			"Duplicate",
			"Education fit",
			"Work authorization",
			"Excluded by a Hard Filter",
			"On-site in Basel with no relocation support for interns",
			"Everything this rule excluded",
			"The employer listing is closed.",
		],
		absent: ["Prepare Application…", ">Open Application<", "You press submit"],
	},
	{
		hash: "#/triage",
		label: "Jev diagnostics: only unavailable and stale results, without a score",
		expected: [
			"Jev diagnostics",
			"2 Postings",
			"Unavailable",
			"Out of date",
			"Jev unavailable",
			"Intern, Gene Writing Analytics",
		],
		absent: ["Match strength", "Priority rank"],
	},
	{
		hash: "#/inspector",
		label: "Filter inspector: every Posting and its Filter Decision, as a table",
		expected: [
			"Filter inspector",
			"19 Postings",
			"Awaiting Jev",
			"Potential matches",
			"Needs review",
			"Saved &amp; applied",
			"Location",
			"Work authorization",
			"Education fit",
			"Not yet",
		],
	},
	{
		hash: "#/inspector?status=hard-killed&rule=education_fit",
		label: "Filter inspector: everything the education-fit rule excluded, with its reasons",
		expected: [
			'aria-label="Stop filtering by Education fit"',
			"Senior Staff Scientist, Strain Engineering",
			"requires a completed PhD and 8+ years of industry experience",
			"Manufacturing Associate II, Drug Substance",
		],
		absent: ["Intern, Gene Writing Analytics"],
	},
	{
		hash: "#/inspector?status=hard-killed&rule=duplicate",
		label: "Filter inspector: the duplicate rule, naming the survivor in English",
		expected: [
			'aria-label="Stop filtering by Duplicate"',
			"the same role as 'Solutions Intern, Scientific Data (Remote US)' at Benchling",
			"already discovered from Ashby",
		],
	},
	{
		hash: "#/employers",
		label: "Employers: source health in words, with the last success as a relative time",
		expected: ["5 boards, 3 healthy", "Ginkgo Bioworks", "Greenhouse", "Healthy", "Failed", "The last check failed; Postings already found are kept"],
	},
	{
		hash: `#/postings/${DETAIL_KEY}`,
		label: "Posting: the employer's page stays in an opaque sandbox behind Read Full Description",
		press: "Read Full Description",
		expected: ['sandbox=""', "default-src", "The employer's page as they wrote it."],
	},
	{
		hash: `#/postings/${DETAIL_KEY}`,
		label: "Posting: What changed keeps every Filter Decision, replays included",
		press: "What changed 3 decisions",
		expected: ["Hard Filters: excluded", "Superseded by a later decision", "Hard Filters: passed", "Replayed under the summer-2027 housing clause"],
	},
	{
		hash: `#/postings/${DETAIL_KEY}`,
		label: "Posting: Prepare states its cost before the press and sends nothing to the employer",
		press: "Prepare Application…",
		expected: ["Prepare with", "Include a cover letter", "Preparing is one completion on that runtime.", "Nothing is sent to the employer."],
	},
	{
		hash: `#/postings/${TESSERA_KEY}`,
		label: "Posting: résumé evidence and an unavailable early review, with no score",
		expected: ["Intern, Gene Writing Analytics", "On your résumé", "QC reporting in Excel", "Jev unavailable", "An estimate, not a verdict"],
	},
	{
		hash: `#/postings/${WORKDAY_KEY}`,
		label: "Posting: a Workday Posting, captioned by employer and not by board token",
		expected: ["Process Development Intern, Drug Substance Technologies", ">Amgen"],
	},
	{
		hash: "#/profile",
		label: "Profile: an existing Install opens its saved facts as fields, not setup and not file text",
		expected: [
			PROFILE.contact, 'value="Avery Example"', PROFILE.history, "Backend Engineer", "Example University", PROFILE.skills, "TypeScript",
			TARGETING.jobs, "backend engineer", TARGETING.employers, "Cloudflare", PROFILE.filters, "Education fit", "Role target",
			"Mid level to Senior", "Bachelor’s degree", PROFILE.answers, "Not answered", PROFILE.install, CONNECT.keySaved, PROFILE.save,
		],
		absent: [
			"Connect an assistant", "Use Claude", "Use ChatGPT", "Set Up Venator", "<textarea", "name: Avery", "qualification_mode",
			"restriction_patterns", "primary: true", "experience_years", "requires_sponsorship", "Résumé facts",
		],
	},
	{
		hash: "#/queue?list=unscored",
		label: "first run: Awaiting Jev with no key keeps the list, selects nothing, and asks for the key",
		expected: [FIRST_RUN.keyTitle, "Add your TypeSafe key and Jev sorts", "into look first, review and skip. Until then they wait here.", FIRST_RUN.addKey, FIRST_RUN.getKey, 'role="listbox"'],
		absent: ['aria-selected="true"', "Prepare Application…"],
	},
	{
		hash: "#/queue?list=unscored",
		label: "first run: Add Key opens a sheet whose key goes to the Keychain",
		press: FIRST_RUN.addKey,
		expected: [FIRST_RUN.keySheetTitle, FIRST_RUN.keySheetDescription, "Paste your TypeSafe key", "Save Key"],
	},
];

/**
 * Setting up Venator: the three v6 steps.
 *
 * These render against whatever database is open, because none depends on one: setup has no
 * pipeline behind it yet, and the app drops the sidebar and toolbar for the duration.
 */
const ONBOARDING_CHECKS: readonly RouteCheck[] = [
	{
		hash: "#/onboarding",
		label: "setup: connect an assistant, with the key optional and nothing checked on render",
		expected: [
			"Set Up Venator",
			"Step 1 of 3",
			"Connect an assistant",
			"Venator uses it to read your résumé and help you apply.",
			// The two presses in the vendors' own words, and never as a login: Venator holds no
			// credential and signs nobody in.
			"Use Claude",
			"Use ChatGPT",
			"Claude Code, signed in with your Claude plan.",
			"Codex, signed in with your ChatGPT plan.",
			"Jev key",
			"Optional",
			"Paste your TypeSafe key",
			"Save Key",
			"The key stays in this Mac’s Keychain.",
			"Get a key",
			// About this starts closed.
			"About this",
			'aria-expanded="false"',
			"Skip for Now",
			"Continue",
		],
		// A check launches a real program and spends the person's own plan, so nothing a check
		// could have told the screen may be on it before a press.
		absent: ["Connected", "Not found on this Mac", "Not signed in", "Checking…", "How to install", "Log in", ...CONNECT.aboutLines],
	},
	{
		hash: "#/onboarding",
		label: "setup: About this opens to what Venator does and does not do",
		press: "About this",
		expected: ['aria-expanded="true"', ...CONNECT.aboutLines],
	},
	{
		hash: "#/onboarding?step=resume",
		label: "setup: add a résumé, with the reading and its cost said before a file is chosen",
		expected: [
			"Step 2 of 3",
			"Add your résumé",
			"Drop your résumé PDF here",
			"Choose File…",
			"Paste Text…",
			"Read it with",
			RESUME.thisMac,
			THIS_MAC_CAPTION,
			// Continue is held, and the reason is beside it.
			"Add a résumé first",
			"Back",
			"disabled",
		],
		/*
		 * Reached with no assistant connected, so the paid reading is not offered at all; and
		 * nothing may claim to have read a résumé nobody chose.
		 */
		absent: ["whole résumé", "one request of your plan", "Looks right", "Reading your résumé", "Replace…"],
	},
	{
		hash: "#/onboarding?step=resume",
		label: "setup: Paste Text opens a sheet that reads for free, on this Mac",
		press: "Paste Text…",
		expected: ["Paste your résumé", "Venator reads your contact details from the text, on this Mac, for free.", "Read Text", "Cancel"],
	},
	{
		hash: "#/onboarding?step=targeting",
		label: "setup: what jobs to find, where, and whose boards to check",
		expected: [
			"Step 3 of 3",
			"What jobs should Venator find?",
			"It checks these employers’ job boards each time you refresh.",
			"Job titles",
			"Add a title",
			"Level",
			// The band is the ladder's own rungs, one contiguous slice.
			"Mid level to Senior",
			"Places",
			"Add a place",
			"Remote jobs",
			"Include remote jobs",
			"Employers",
			"0 added",
			"Paste an employer’s careers page",
			"Find Jobs",
			"Add an employer first",
			"Skip for Now",
		],
	},
];

/** A Profile, and a pipeline nobody has run yet. Not an error, and it must not read as one. */
const EMPTY_CHECKS: readonly RouteCheck[] = [
	{
		hash: "#/queue",
		label: "first run: no jobs yet, and Refresh names how many employers it will check",
		expected: [
			FIRST_RUN.noJobs,
			refreshBody(FIXTURE_BOARDS.length),
			">Refresh<",
			// The window around it is the real one: sidebar, toolbar and the fetch footer.
			"For you",
			"Awaiting Jev",
			"Not checked yet",
			'aria-label="Refresh jobs"',
		],
		// Nothing has been discovered, so there is nothing to diagnose and nothing for Jev it.
		absent: ["Jev diagnostics", ">Jev it<", "could not be read"],
	},
];

/**
 * A shipped Install: the fixture is on disk, and nobody asked for it.
 *
 * This is the state the app used to fail in — an Install with no view of its own was handed
 * the sample database, so the first screen after setup was a review queue of somebody else's
 * job search under a badge reading "Sample Postings — not yours". The queue's own empty screen
 * could not help: it renders on `funnel.discovered === 0`, and nineteen is not zero.
 *
 * So the two halves are asserted separately, and both are the point. The screen written for
 * this state has to be the one on screen — otherwise a blank page would pass — and **not one
 * word of the fixture may appear anywhere in the markup**, which is stricter than the visible
 * text the other checks are held to: an employer in an `href` or a `title` is still an
 * employer this person never searched for.
 *
 * The absent list is derived from the fixture rather than typed out, for the same reason
 * `SCANNED_BOARD_TOKENS` is: a hand-picked list can only catch the Postings somebody thought
 * of, and this property is about every one of them.
 */
const SHIPPED_CHECKS: readonly RouteCheck[] = [
	{
		hash: "#/queue",
		label: "shipped: a fixture on disk that nobody asked for reaches no part of the queue",
		expected: [FIRST_RUN.noJobs, ">Refresh<", 'aria-label="Refresh jobs"'],
		absent: [...FIXTURE_ON_SCREEN_TEXT, SAMPLE_DATA_STAMP],
	},
	{
		hash: "#/inspector",
		label: "shipped: and no part of the filter inspector either",
		// The inspector has no first-run screen of its own; what it must do is count what is
		// there, which is nothing, rather than count a stranger's Postings.
		expected: ["Filter inspector", "0 Postings", "No Posting matches these filters"],
		absent: [...FIXTURE_ON_SCREEN_TEXT, SAMPLE_DATA_STAMP],
	},
	{
		hash: `#/postings/${DETAIL_KEY}`,
		// Deep-linking is the way around a routing rule: a person who lands on a Posting by
		// URL never passes the list. Nothing is served to open, so this is the 404 path.
		label: "shipped: a Posting of the fixture's cannot be opened by URL either",
		expected: ["This Posting could not be read"],
		absent: [...FIXTURE_ON_SCREEN_TEXT, SAMPLE_DATA_STAMP],
		// The app quotes the key it was asked to open, and that key is in the URL above.
		echoed: ["generatebiomedicines"],
	},
];

// The fixture is generated, not committed, and a view named explicitly is opened exactly as
// given — the server only builds one when it falls back to it on its own. So a fresh clone
// has to build the pinned fixture here, before the API tries to open it. Smoke pins it, so it
// rebuilds unconditionally rather than keeping whatever is already on disk: a fixture left by
// an older builder would otherwise be reused and these assertions would pass against stale
// data. The rebuild costs milliseconds.
buildFixtureDatabase(FIXTURE_DATABASE_PATH);
buildEmptyViewDatabase(EMPTY_DATABASE_PATH);

const HELD_WHOLE_NUMBERS = wholeNumbersOf(readHeldNumbers(FIXTURE_DATABASE_PATH));

/**
 * Every Posting the fixture holds, opened as a page: the margin is built per Posting from its
 * own evidence, so "no number anywhere" is only proved by opening every one of them.
 */
function readPostingPages(path: string): readonly RouteCheck[] {
	const database = new DatabaseSync(path, { readOnly: true });
	try {
		return database
			.prepare("SELECT key, title FROM postings ORDER BY key")
			.all()
			.map((row) => ({
				hash: `#/postings/${encodeURIComponent(String(row.key))}`,
				label: `every Posting: ${String(row.title)}, with no number in its margin`,
				expected: [asMarkup(String(row.title))],
			}));
	} finally {
		database.close();
	}
}

const POSTING_PAGES = readPostingPages(FIXTURE_DATABASE_PATH);

let failed = 0;
let checked = 0;

// Copy is prose and prose has no type, so the labels that describe the Track schema are
// pinned against it before any route is rendered. See tools/label-contract.ts.
const labelFailures = checkLabelContract();
checked += 1;
if (labelFailures.length === 0) {
	process.stdout.write("  ok    labels match the application and evidence contracts\n");
} else {
	failed += 1;
	for (const failure of labelFailures) {
		process.stdout.write(`  FAIL  label contract: ${failure}\n`);
	}
}

/**
 * The assertions themselves, against markup somebody else produced.
 *
 * Lifted out of `runChecks` unchanged, so a check whose screen only exists after a press is
 * held to exactly the same six conditions as one that renders on its own: the text that must
 * be there, the text that must not, the order, the machine vocabulary, the board tokens, and
 * the numbers no screen may show.
 */
function judge(check: RouteCheck, markup: string): void {
	const missing = check.expected.filter((needle) => !markup.includes(needle));
	const present = (check.absent ?? []).filter((needle) => markup.includes(needle));
	const text = visibleText(markup);
	const disordered = outOfOrder(text, check.ordered ?? []);
	const leaked = FORBIDDEN_ON_SCREEN.filter((identifier) =>
		identifier === "hist" ? /\bhist\b/u.test(text) : text.includes(identifier));
	const boards = leakedBoardTokens(text).filter((token) => !(check.echoed ?? []).includes(token));
	const numbers = numbersOnScreen(text, HELD_WHOLE_NUMBERS);
	if (
		missing.length === 0 &&
		present.length === 0 &&
		disordered.length === 0 &&
		leaked.length === 0 &&
		boards.length === 0 &&
		numbers.length === 0
	) {
		process.stdout.write(`  ok    ${check.label}\n`);
		return;
	}
	failed += 1;
	if (missing.length > 0) {
		process.stdout.write(`  FAIL  ${check.label}: missing ${missing.join(" | ")}\n`);
	}
	if (present.length > 0) {
		process.stdout.write(`  FAIL  ${check.label}: should not be on screen — ${present.join(" | ")}\n`);
	}
	if (disordered.length > 0) {
		process.stdout.write(`  FAIL  ${check.label}: out of order or missing — ${disordered.join(" | ")}\n`);
	}
	if (leaked.length > 0) {
		process.stdout.write(`  FAIL  ${check.label}: raw identifiers on screen — ${leaked.join(" | ")}\n`);
	}
	if (boards.length > 0) {
		process.stdout.write(`  FAIL  ${check.label}: board tokens on screen — ${boards.join(" | ")}\n`);
	}
	if (numbers.length > 0) {
		process.stdout.write(`  FAIL  ${check.label}: a Jev number or a score on screen — ${numbers.join(" | ")}\n`);
	}
}

let smokeSetup = false;
let smokeProfile = false;
async function runChecks(renderer: RouteRenderer, checks: readonly RouteCheck[]): Promise<void> {
	for (const check of checks) {
		checked += 1;
		smokeSetup = check.hash.startsWith("#/onboarding");
		smokeProfile = check.hash === "#/profile";
		try {
			judge(check, check.press === undefined ? await renderer.render(check.hash) : await renderer.press(check.hash, check.press));
		} finally { smokeSetup = false; smokeProfile = false; }
	}
}

/**
 * The setup checks, with every write on the onboarding surface refused and counted.
 *
 * **Rendering a setup screen must cost nothing.** Two of that surface's five routes spend the
 * Owner's own subscription when they are called — the runtime probe launches a runtime per
 * lane, and a resume parse asks for one completion — and two of the other three write to disk.
 * Every one of them is supposed to happen only from a press. The screens assert half of that by
 * absence, in `absent` above: nothing a probe or a parse could have told them may be on screen
 * before anybody has pressed anything. This asserts the other half directly, at the wire, for
 * all four screens at once: a `POST` to that surface during a render is recorded and fails the
 * pass, whatever ends up in the markup.
 *
 * `GET /api/onboarding/state` is not a write and is deliberately still allowed through — it
 * lists directories, and the app reads it on load on purpose.
 */
async function runSetupChecks(renderer: RouteRenderer, checks: readonly RouteCheck[]): Promise<void> {
	const attempted: string[] = [];
	const previous = globalThis.fetch;
	globalThis.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
		const method = (init?.method ?? "GET").toUpperCase();
		const target = String(input);
		if (method === "POST" && target.includes("/onboarding/")) {
			attempted.push(`${method} ${target}`);
			return Promise.reject(new Error("smoke: a setup screen posted on render"));
		}
		return previous(input, init);
	};
	try {
		await runChecks(renderer, checks);
	} finally {
		globalThis.fetch = previous;
	}
	checked += 1;
	if (attempted.length === 0) {
		process.stdout.write("  ok    setup: rendering a setup screen writes nothing and spends nothing\n");
		return;
	}
	failed += 1;
	process.stdout.write(`  FAIL  setup: a screen posted on render — ${attempted.join(" | ")}\n`);
}

/**
 * The one check that has to spend a press before there is anything to read.
 *
 * The runtime step shows nothing a check could have told it until a check is asked for, which
 * is the property the unprobed check above pins. That leaves the other half — what it says
 * once it *has* been told something — with no coverage at all, and it is the half this whole
 * change is about: a Windows machine must be shown the Windows install route and not a Mac's.
 */
type ProbedCheck = RouteCheck & {
	/** The visible label of the button to press. */
	readonly press: string;
	/** The body the probe answers with, verbatim, as text off the wire. */
	readonly response: string;
};

function fixtureJevCount(path: string): number {
	const database = new DatabaseSync(path, { readOnly: true });
	try {
		const row = database.prepare(`SELECT COUNT(*) AS count FROM postings p
			WHERE COALESCE(p.description_html, '') != ''
			AND (SELECT d.verdict FROM decisions d WHERE d.posting_key = p.key AND d.stage = 'hard_filter'
				ORDER BY d.decided_at DESC, d.rowid DESC LIMIT 1) = 'pass'
			AND NOT EXISTS (SELECT 1 FROM jev_triage j WHERE j.posting_key = p.key AND j.state = 'current')`).get();
		return Number(row?.count);
	} finally {
		database.close();
	}
}

const JEV_POSTING_COUNT = fixtureJevCount(FIXTURE_DATABASE_PATH);
const JEV_PLAN_SUMMARY =
	`${JEV_POSTING_COUNT} Postings passed the Hard Filters, have description text, and have no Jev result under the current release. Jev will pause until an API key is available.`;
const JEV_KEY_MISSING_NOTE = "No TypeSafe key is saved, so these Postings wait for Jev until you add one.";

/**
 * The footer's Jev row, with the key the pass answers with: none.
 *
 * The line beside the press is the plan's count in words and the plan's full sentence is that
 * line's tooltip, so both are pinned — the count as visible text, the sentence as the `title`
 * it now travels in. The press is the large capsule every prominent press in the app is, in
 * grey: with no key, Add Key… on the Awaiting Jev screen is the one ember press, and the
 * plan's own note under the row says why (ui/DESIGN.md, principle 2). It stays pressable.
 */
const JEV_PLAN_CHECK: RouteCheck = {
	hash: "#/queue",
	label: "runs: Awaiting Jev keeps the button ready, in grey, without showing a Jev number or cost preview",
	expected: [
		`${JEV_POSTING_COUNT} awaiting Jev`,
		`title="${JEV_PLAN_SUMMARY}"`,
		JEV_KEY_MISSING_NOTE,
		'<button type="button" class="button" data-size="large">Jev it</button>',
	],
	absent: ["spend allowance", "next refresh", 'data-tone="prominent">Jev it'],
};

/** The same row once a key is saved: the plan carries no note, and the press is ember. */
const JEV_READY_CHECK: RouteCheck = {
	hash: "#/queue",
	label: "runs: with a key saved, Jev it is the footer's ember press",
	expected: [`${JEV_POSTING_COUNT} awaiting Jev`, '<button type="button" class="button" data-size="large" data-tone="prominent">Jev it</button>'],
	absent: [JEV_KEY_MISSING_NOTE],
};

/**
 * Answers the next Jev plans as a plan with a key behind it, and hands back the undo. Only the
 * note changes: the count, the summary and the token are the pass's own.
 */
function answerJevPlanWithKey(): () => void {
	const previous = globalThis.fetch;
	globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
		const response = await previous(input, init);
		const path = new URL(String(input), "http://127.0.0.1").pathname;
		if (path !== "/api/runs/plan" || JSON.parse(String(init?.body)).kind !== "jev-it") return response;
		const body: PlanResponse = await response.json();
		const plan: RunPlan = { ...body.plan, notes: body.plan.notes.filter((note) => note.code !== "jev-key-missing") };
		return new Response(JSON.stringify({ plan }), { status: 200, headers: { "content-type": "application/json" } });
	};
	return () => {
		globalThis.fetch = previous;
	};
}

async function checkJevReady(renderer: RouteRenderer): Promise<void> {
	checked += 1;
	const restore = answerJevPlanWithKey();
	try {
		judge(JEV_READY_CHECK, await renderer.render(JEV_READY_CHECK.hash));
	} finally {
		restore();
	}
}

/**
 * Answers the next runtime probe with a fixed body, and hands back the undo.
 *
 * The stub sits at the wire and nowhere else: the real screen, the real button, the real
 * client and the real render, with only the server's answer supplied. Nothing is launched and
 * no subscription is spent, which is why this can run in the same pass as everything else.
 *
 * It is a string rather than a typed object on purpose — the case that matters most is a
 * response this build did not construct, from a host with no platform field in it at all, and
 * a typed literal cannot express one.
 */
function answerProbeWith(body: string): () => void {
	const previous = globalThis.fetch;
	globalThis.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
		const target = String(input);
		// A ready answer saves the choice. The save is answered here too, so no pass ever
		// writes a settings file into the Install this smoke happens to run beside.
		if (target.includes("/onboarding/settings")) {
			return Promise.resolve(new Response(SETTINGS, { status: 200, headers: { "content-type": "application/json" } }));
		}
		if (!target.includes("/onboarding/runtime/probe")) return previous(input, init);
		return Promise.resolve(
			new Response(body, { status: 200, headers: { "content-type": "application/json" } }),
		);
	};
	return () => {
		globalThis.fetch = previous;
	};
}

async function runProbedChecks(renderer: RouteRenderer, checks: readonly ProbedCheck[]): Promise<void> {
	for (const check of checks) {
		checked += 1;
		const restore = answerProbeWith(check.response);
		try {
			smokeSetup = true;
			judge(check, await renderer.press(check.hash, check.press));
		} finally {
			smokeSetup = false;
			restore();
		}
	}
}

/** What the settings route answers in smoke: Claude chosen, and no key in any Keychain. */
const SETTINGS = JSON.stringify({ runtime: "claude", jevKeyPresent: false });

/** The fixture answers every request that would otherwise consult the checkout's stores or Profile. */
/** The scaffold Profile the repository ships, as the Profile screen's fixture: every group has something in it. */
const EXAMPLE_PROFILE_DIRECTORY = fileURLToPath(new URL("../../profiles/example/", import.meta.url));
const EXAMPLE_PROFILE = {
	"resume.yaml": readFileSync(join(EXAMPLE_PROFILE_DIRECTORY, "resume.yaml"), "utf8"),
	"constraints.yaml": readFileSync(join(EXAMPLE_PROFILE_DIRECTORY, "constraints.yaml"), "utf8"),
	"targeting.yaml": readFileSync(join(EXAMPLE_PROFILE_DIRECTORY, "targeting.yaml"), "utf8"),
};

function fixtureAnswer(view: "fixture" | "empty"): FixtureResponse {
	return (input, init) => {
		const path = new URL(String(input), "http://127.0.0.1").pathname;
		const method = init?.method ?? "GET";
		const json = (body: string) => new Response(body, {
			status: 200, headers: { "content-type": "application/json" },
		});
		if (method === "GET" && path === "/api/onboarding/state") {
			return json(JSON.stringify({
				configured: !smokeSetup, implied: smokeSetup ? null : "smoke", ambiguous: false,
				profiles: smokeSetup ? [] : [{ name: "smoke", directory: "/smoke/profile", kind: "install", scaffold: false,
					files: ["targeting.yaml", "constraints.yaml", "resume.yaml"] }],
				roots: { search: ["/smoke/profiles"], write: "/smoke/profiles" },
				view: { kind: view, path: "/smoke/view.db" },
			}));
		}
		// Never the real Keychain: the settings read answers "no key" for every pass.
		if (method === "GET" && path === "/api/onboarding/settings") return json(smokeProfile ? '{"runtime":"codex","jevKeyPresent":true}' : SETTINGS);
		if (method === "GET" && path === "/api/onboarding/existing-profile") return json(JSON.stringify({ documents: EXAMPLE_PROFILE, form: formOf(EXAMPLE_PROFILE) }));
		if (method === "GET" && /^\/api\/applications\/[^/]+$/u.test(path)) return json('{"prepared":false}');
		if (method === "GET" && path === "/api/runs/current") return json('{"run":null}');
		if (method === "GET" && path === "/api/runs/recent") return json('{"runs":[]}');
		if (method === "POST" && path === "/api/runs/plan") {
			const kind = JSON.parse(String(init?.body)).kind;
			if (kind !== "jev-it" && kind !== "fetch-and-filter") throw new Error("smoke: unexpected run plan");
			const plan = {
				token: "smoke-plan", kind, profile: "smoke", profileDirectory: "/smoke/profile",
				stages: kind === "jev-it" ? ["view"] : ["discover", "filters", "view"],
				storeRoot: "/smoke/data", viewPath: "/smoke/view.db", dashboardViewPath: "/smoke/view.db",
				// A new Install can Refresh; the sample one cannot. Jev it is startable with waiting Postings.
				boards: FIXTURE_BOARDS.length,
				startable: kind === "jev-it" ? JEV_POSTING_COUNT > 0 : view === "empty",
				notes: kind === "jev-it"
					? [{ code: "jev-key-missing", message: "No TypeSafe key is saved, so these Postings wait for Jev until you add one." }]
					: view === "empty" ? [] : [{ code: "preview-only", message: "This example cannot start a run." }],
			};
			if (kind === "jev-it") {
				return json(JSON.stringify({ plan: { ...plan, jev: {
					asOf: "2026-09-23", mode: "shadow", postingCount: JEV_POSTING_COUNT, maximumUsd: 10,
					selectionHash: "a".repeat(64), summary: JEV_PLAN_SUMMARY,
				} } }));
			}
			return json(JSON.stringify({ plan }));
		}
		if (path.startsWith("/api/runs") || path.startsWith("/api/applications")) {
			throw new Error(`smoke: unexpected store-backed request: ${method} ${path}`);
		}
		return null;
	};
}

/**
 * Updating your job list: the view is being rebuilt, so every read of it is held open.
 *
 * The server does that on its own when the view is stale (`server/view-recovery.ts`); here the
 * two reads the window makes first are simply never answered, and the screen is read once it
 * has been waiting longer than a quick reload ever does.
 */
const UPDATING_CHECK: RouteCheck = {
	hash: "#/queue",
	label: "first run: a job list being rebuilt says so in the window, the toolbar and the sidebar",
	expected: [FIRST_RUN.updatingTitle, FIRST_RUN.updatingBody, FIRST_RUN.updatingToolbar, FIRST_RUN.updatingSidebar, "data-indeterminate"],
	absent: ['aria-label="Refresh jobs"', ">Jev it<", 'role="listbox"'],
};

async function checkUpdating(renderer: RouteRenderer): Promise<void> {
	checked += 1;
	const previous = globalThis.fetch;
	globalThis.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
		const path = new URL(String(input), "http://127.0.0.1").pathname;
		if (path === "/api/summary" || path === "/api/postings") return new Promise<Response>(() => undefined);
		return previous(input, init);
	};
	try {
		judge(UPDATING_CHECK, await renderer.render(UPDATING_CHECK.hash, 2 * SLOW_MS));
	} finally {
		globalThis.fetch = previous;
	}
}

async function checkJevPlanWords(renderer: RouteRenderer): Promise<void> {
	checked += 1;
	judge(JEV_PLAN_CHECK, await renderer.render(JEV_PLAN_CHECK.hash));
	await checkJevReady(renderer);
}

/** One runtime, in the shape `venator.llm.probe` reports it. */
function probeAnswer(lane: string, state: string, ready: boolean, remedy: string | null, caveat: string | null): string {
	return JSON.stringify({
		probe: { available: true, reason: null, message: null },
		scope: "lane",
		lane,
		lanes: [{ lane, state, ready, detail: ready ? `${lane} ran and answered` : "nothing answered on this machine", remedy, caveat, default: lane === "claude" }],
		platform: "macos",
	});
}

const RUNTIME_HASH = "#/onboarding?step=runtime";
const CLAUDE_REMEDY = "install Claude Code on this machine and sign in to it yourself.";
const CODEX_REMEDY = "sign in to the Codex CLI yourself: open a terminal and run `codex login`.";
const CODEX_CAVEAT = "No Venator run has gone all the way through Codex yet.";

const PROBED_CHECKS: readonly ProbedCheck[] = [
	{
		hash: RUNTIME_HASH,
		press: CONNECT.claudeButton,
		response: probeAnswer("claude", "not_installed", false, CLAUDE_REMEDY, null),
		label: "setup: a missing assistant says so, with the probe's own remedy and the installer",
		// The remedy is rendered as it arrives; only its first letter is set by the stylesheet.
		expected: ["Not found on this Mac", CLAUDE_REMEDY, "How to install", "code.claude.com/docs/en/setup"],
		absent: ["Connected"],
	},
	{
		hash: RUNTIME_HASH,
		press: CONNECT.claudeButton,
		response: probeAnswer("claude", "available", true, null, null),
		label: "setup: an assistant that answered is saved as the choice and says Connected",
		expected: ["Connected"],
		absent: ["Not found on this Mac", "How to install"],
	},
	{
		hash: RUNTIME_HASH,
		press: CONNECT.chatgptButton,
		response: probeAnswer("codex", "not_signed_in", false, CODEX_REMEDY, CODEX_CAVEAT),
		label: "setup: a signed-out assistant gets its sign-in step, and its caveat is never dropped",
		expected: ["Not signed in", CODEX_REMEDY, CODEX_CAVEAT],
		absent: ["Connected", "How to install"],
	},
];

/**
 * One pass: an API on its own database, a renderer pointed at it, and the checks that belong
 * to it. The DOM harness installs itself over the process globals, so the passes run one after
 * another rather than side by side — the second overwrites the first's window on purpose.
 *
 * `options.host` is what makes a pass the desktop app rather than a tab. The app reads its
 * host once, at import, so it cannot be switched inside a pass — which is why the two desktop
 * hosts get a pass each, with a renderer and a module graph of their own. `options.sampleData`
 * is the other half: whether this pass is allowed to be served the fixture at all.
 */
type PassOptions = {
	/** Whether this pass may be served the fixture. Withheld is what a shipped Install is. */
	readonly sampleData?: boolean;
	readonly host?: Host;
	/** True for the pass that renders the setup flow: see `runSetupChecks`. */
	readonly setup?: boolean;
	/** True for the pass that also reads the API directly: see `checkWireCarriesNoNumber`. */
	readonly wire?: boolean;
	/** True for the pass that checks the Jev plan words against a fixed wire answer. */
	readonly jevPlan?: boolean;
	/** True for the pass that holds the job list's reads open: see `checkUpdating`. */
	readonly updating?: boolean;
};

/**
 * The other half of "no Jev number and no score": neither leaves the server.
 *
 * A screen that shows no number can still be one render away from showing one if the number
 * is on the wire, so every read route a screen uses is asked for everything it has — every
 * list, every Posting's detail, and the early review — and no key in any answer may name a
 * probability or a score.
 */
async function checkWireCarriesNoNumber(origin: string): Promise<void> {
	checked += 1;
	const paths = [
		"/api/summary",
		"/api/postings?limit=1000",
		"/api/jev-triage?limit=1000",
		...POSTING_PAGES.map((page) => `/api/postings/${page.hash.slice("#/postings/".length)}`),
	];
	const found: string[] = [];
	for (const path of paths) {
		const response = await fetch(`${origin}${path}`);
		if (!response.ok) {
			found.push(`${path} answered ${response.status}`);
			continue;
		}
		for (const match of (await response.text()).matchAll(NUMBER_FIELD)) found.push(`${path} ${match[0]}`);
	}
	if (found.length === 0) {
		process.stdout.write("  ok    wire: no Jev number and no score leaves the API\n");
		return;
	}
	failed += 1;
	process.stdout.write(`  FAIL  wire: a Jev number or a score crossed the wire — ${found.join(" | ")}\n`);
}

async function pass(
	databasePath: string,
	port: number,
	checks: readonly RouteCheck[],
	probed: readonly ProbedCheck[],
	options: PassOptions = {},
): Promise<void> {
	const api = await startApi({ databasePath, port, sampleData: options.sampleData === true });
	const renderer = await startRenderer(
		api.origin, options.host, fixtureAnswer(options.sampleData === true ? "fixture" : "empty"),
	);
	try {
		// The setup screens are held to one extra condition the rest are not: rendering one may
		// not call a route that spends or writes. `runSetupChecks` is `runChecks` with that
		// watch around it, so the checks themselves read the same either way.
		if (options.setup === true) await runSetupChecks(renderer, checks);
		else await runChecks(renderer, checks);
		await runProbedChecks(renderer, probed);
		if (options.jevPlan === true) await checkJevPlanWords(renderer);
		if (options.updating === true) await checkUpdating(renderer);
		if (options.wire === true) await checkWireCarriesNoNumber(api.origin);
	} finally {
		await renderer.close();
		api.stop();
	}
}

/**
 * The same app in the desktop host, on each of the two operating systems it is packaged for.
 *
 * The window is opaque on both: the app draws its own grounds exactly as it does in a tab, and
 * only the frame differs. Every desktop host wears `app-native` and drops the drawn window
 * edge, because the system draws one. A Mac's title bar is overlaid, so its traffic lights sit
 * over the top of the sidebar — and, when the sidebar is hidden, over the list's toolbar,
 * which then keeps that gap clear. Windows draws its own title bar above the webview, so the
 * same gap there would be padding in front of nothing.
 *
 * So both hosts are rendered with the sidebar hidden, and each is asserted against the other's
 * treatment as well as its own. Markup rather than visible text, because the frame is the one
 * property of the app that is not words on a screen.
 */
const WINDOWS_WEBVIEW =
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0";
const MACOS_WEBVIEW =
	"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15";

const WINDOWS_DESKTOP_CHECKS: readonly RouteCheck[] = [
	{
		hash: "#/queue",
		label: "desktop on Windows: the system's frame, and no traffic-light gap with the sidebar hidden",
		press: "Hide the sidebar",
		expected: ['class="window app-native"', 'aria-label="Show the sidebar"'],
		absent: ['data-lights="reserved"'],
	},
	{
		hash: "#/onboarding",
		label: "desktop on Windows: setting up a Profile owns the native window",
		expected: ['class="window-bleed app-native"'],
	},
];

const MACOS_DESKTOP_CHECKS: readonly RouteCheck[] = [
	{
		hash: "#/queue",
		label: "desktop on a Mac: the toolbar keeps the traffic lights' gap with the sidebar hidden",
		press: "Hide the sidebar",
		expected: ['class="window app-native"', 'data-lights="reserved"'],
	},
	{
		hash: "#/onboarding",
		label: "desktop on a Mac: setting up a Profile owns the native window",
		expected: ['class="window-bleed app-native"'],
	},
];

await pass(FIXTURE_DATABASE_PATH, SMOKE_PORT, [...CHECKS, ...POSTING_PAGES, ...ONBOARDING_CHECKS], PROBED_CHECKS, {
	sampleData: true,
	// The setup screens ride in this pass, so the no-writes-on-render watch goes on it. It
	// forbids only a `POST` to the onboarding surface, so the dashboard checks sharing the pass
	// are unaffected — and the probed checks below it, which press on purpose, run outside it.
	setup: true,
	wire: true,
	jevPlan: true,
	updating: true,
});
await pass(EMPTY_DATABASE_PATH, EMPTY_SMOKE_PORT, EMPTY_CHECKS, []);
// The same fixture as the first pass, on the same disk, with the ask withheld.
await pass(FIXTURE_DATABASE_PATH, SHIPPED_SMOKE_PORT, SHIPPED_CHECKS, []);
await pass(FIXTURE_DATABASE_PATH, WINDOWS_DESKTOP_PORT, WINDOWS_DESKTOP_CHECKS, [], {
	sampleData: true,
	host: { tauri: true, userAgent: WINDOWS_WEBVIEW },
});
await pass(FIXTURE_DATABASE_PATH, MACOS_DESKTOP_PORT, MACOS_DESKTOP_CHECKS, [], {
	sampleData: true,
	host: { tauri: true, userAgent: MACOS_WEBVIEW },
});

if (failed > 0) {
	process.stderr.write(`smoke: ${failed} of ${checked} checks failed\n`);
	process.exitCode = 1;
} else {
	const databases = [FIXTURE_DATABASE_PATH, EMPTY_DATABASE_PATH].map((path) => basename(path));
	process.stdout.write(`smoke: ${checked} checks passed against ${databases.join(", ")}\n`);
}
