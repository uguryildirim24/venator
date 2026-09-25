/**
 * Reading an uploaded resume PDF, by running the pipeline rather than reimplementing it.
 *
 * **Why this exists.** Onboarding's resume step read plain text and refused a PDF, which is
 * what a person actually has. Turning a PDF into text is not string work — it is a parser with
 * a decade of edge cases in it — and reading facts out of that text is a completion. Neither
 * belongs in Node: `src/venator/resume/parse.py` owns both, this module runs it, and the rules
 * it applies stay in one language.
 *
 * It is the fourth adapter of exactly the shape `verify.ts`, `boards.ts` and `runs/plan.ts`
 * already use — one subprocess, one JSON document off stdout, one normalisation here — and the
 * shape is followed rather than varied on purpose. What is different is only that the input is
 * megabytes of binary, so it arrives on the child's stdin instead of as an argument.
 *
 * Five properties are load-bearing and are not this module's to soften:
 *
 * - **`text` spends nothing and `model` spends one completion.** Which one runs is decided by
 *   the caller, from something a person pressed, and there is no path from one to the other:
 *   no default into `model`, no fallback into it when the deterministic reading finds little,
 *   and no retry. `routes.ts` is where that decision is made and it is made once.
 * - **It writes nothing and starts nothing.** No Profile is created or edited, no store is
 *   stamped, no Posting's lifecycle state is touched, and no request is made of any employer.
 *   What the pipeline eventually writes, `POST /profile` writes, after a person has confirmed
 *   what is on screen and after the load-back.
 * - **Nothing the adapter, the runtime or the model said reaches the caller.** stderr is
 *   logged here and returned nowhere: it carries paths, environment contents, a PDF parser's
 *   byte offsets, and — on the model path — text a stranger wrote. Every outcome below is a
 *   word from a closed set, and `routes.ts` turns it into a sentence written by hand.
 * - **An Install that cannot run this says so.** Exit 3 is the pipeline not being importable,
 *   and a launch failure is an interpreter that would not start. Both are answers, not
 *   crashes, and both leave the person the free reading of a pasted resume.
 * - **One reading at a time in this server process, and a second is refused rather than
 *   queued.** See `oneReadingAtATime` below for why refusing is the honest answer here, why it
 *   covers the free reading too, and what it does not cover.
 */

import { spawn } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import type {
	ParsedContact,
	ParsedEducation,
	ParsedExperience,
	ParsedProficiency,
	ParsedResumeDocument,
	ParsedResumeSection,
	PdfUnreadableReason,
	ResumeParseMode,
} from "../../shared/onboarding.ts";
import { PDF_UNREADABLE_REASONS } from "../../shared/onboarding.ts";
import { nonJevInheritedEnvironment } from "../runs/environment.ts";
import { pipelineWorkingDirectory, pythonInterpreter, systemContext, type LocationContext } from "../locations.ts";
import { asBoolean, asList, asMapping, asNumber, asText, at, parseJson } from "./json.ts";
import type { JsonMapping, JsonValue } from "./json.ts";

/** The adapter that reads the PDF and answers in JSON. Beside this file, and only here. */
const READER_SCRIPT = join(fileURLToPath(new URL(".", import.meta.url)), "parse_resume.py");

/**
 * Extracting text: a PDF parser on this machine, and nothing else.
 *
 * Bounded like the load-back's own timeout and for the same reason — there is no network and
 * no runtime in it, so this bounds a machine that is thrashing rather than work that
 * legitimately takes time. It is longer than `VERIFY_TIMEOUT_MS` because the work is not the
 * same size: the load-back reads three small YAML files, and this parses up to twenty pages of
 * a container format somebody else produced.
 */
const EXTRACT_TIMEOUT_MS = 60_000;

/**
 * Asking a runtime to read the document: a completion, and a person watching a spinner.
 *
 * **This is deliberately far longer than `VERIFY_TIMEOUT_MS`, and the difference is the whole
 * point of stating it here.** The load-back is local file reading and is bounded tightly
 * because a person is waiting on a button with a half-written staging directory behind it.
 * This one starts a coding assistant and waits for a model to answer, which `TIMEOUT` in
 * `src/venator/resume/parse.py` allows 300 seconds for. Killing the child before the pipeline
 * gives up would spend the Owner's subscription and throw the answer away — the one outcome
 * worth more than the wait — so this is that 300 seconds plus the room the interpreter, the
 * import and the PDF extraction take before the completion is even asked for.
 */
const PARSE_TIMEOUT_MS = 330_000;

/** Exit 3 from the adapter: the pipeline is not importable in this Install. */
const PIPELINE_MISSING_EXIT = 3;

/** Which stage of the reading gave up. The adapter's own closed set. */
export type ResumeParseStage = "extract" | "complete" | "decode";

const PARSE_STAGES: readonly ResumeParseStage[] = ["extract", "complete", "decode"];

/**
 * What the adapter said about one uploaded PDF.
 *
 * The first five are the adapter's own outcomes; the last three are this side's reading of a
 * process rather than of a document, exactly as `VerifyOutcome` splits them.
 */
export type ResumePdfOutcome =
	/** `--mode text`: the document's text, extracted on this machine, spending nothing. */
	| {
			readonly outcome: "read";
			readonly text: string;
			readonly pages: number;
			readonly characters: number;
			readonly truncated: boolean;
	  }
	/** `--mode model`: a proposal, every value grounded in the document and none confirmed. */
	| {
			readonly outcome: "parsed";
			readonly resume: ParsedResumeDocument;
			readonly sections: readonly ParsedResumeSection[];
			readonly pages: number;
			readonly characters: number;
			readonly truncated: boolean;
			readonly dropped: number;
			readonly schemaEnforced: boolean;
	  }
	/** The file could not be turned into text, named by a reason from the closed set. */
	| { readonly outcome: "unreadable"; readonly reason: PdfUnreadableReason }
	/** No runtime is connected on this machine. A state of this product, not a failure. */
	| { readonly outcome: "no_runtime" }
	/** The reading gave up at a named stage. Nothing of what was said travels with it. */
	| { readonly outcome: "failed"; readonly stage: ResumeParseStage }
	/** The adapter answered in a shape this dashboard does not know. Never guessed at. */
	| { readonly outcome: "mismatch" }
	/** The pipeline is not part of this Install, or the interpreter would not start. */
	| { readonly outcome: "absent" }
	| { readonly outcome: "timed-out" };

function readReason(value: string | null): PdfUnreadableReason | null {
	return PDF_UNREADABLE_REASONS.find((candidate) => candidate === value) ?? null;
}

function readStage(value: string | null): ResumeParseStage | null {
	return PARSE_STAGES.find((candidate) => candidate === value) ?? null;
}

/** A whole number that is not negative, or null. Counts on this wire are never anything else. */
function readCount(value: JsonValue | undefined): number | null {
	const count = asNumber(value);
	if (count === null || !Number.isInteger(count) || count < 0) return null;
	return count;
}

/**
 * One optional string field of the proposal.
 *
 * `undefined` for an absent key and for a key holding anything but a string. That second half
 * is deliberate: the adapter omits what it found nothing for and never writes null, so a
 * non-string here is drift rather than a value, and dropping the field is the same answer as
 * the document not having said. Nothing is coerced — a number is not turned into its digits.
 */
function optionalText(entry: JsonMapping, key: string): string | undefined {
	const value = asText(at(entry, key));
	return value === null || value === "" ? undefined : value;
}

function readContact(value: JsonValue | undefined): ParsedContact | undefined {
	const mapping = asMapping(value);
	if (mapping === null) return undefined;
	return {
		location: optionalText(mapping, "location"),
		phone: optionalText(mapping, "phone"),
		email: optionalText(mapping, "email"),
		linkedin: optionalText(mapping, "linkedin"),
	};
}

/** Each entry of a section, in the order the reply had them. A non-mapping entry is dropped. */
function entriesOf(value: JsonValue | undefined): readonly JsonMapping[] {
	const list = asList(value);
	if (list === null) return [];
	const entries: JsonMapping[] = [];
	for (const item of list) {
		const mapping = asMapping(item);
		if (mapping !== null) entries.push(mapping);
	}
	return entries;
}

function readEducation(value: JsonValue | undefined): readonly ParsedEducation[] {
	return entriesOf(value).map((entry) => ({
		org: optionalText(entry, "org"),
		location: optionalText(entry, "location"),
		date: optionalText(entry, "date"),
		degree: optionalText(entry, "degree"),
		gpa: optionalText(entry, "gpa"),
		coursework: optionalText(entry, "coursework"),
	}));
}

function readProficiencies(value: JsonValue | undefined): readonly ParsedProficiency[] {
	return entriesOf(value).map((entry) => ({
		label: optionalText(entry, "label"),
		items: optionalText(entry, "items"),
	}));
}

function readBullets(value: JsonValue | undefined): readonly string[] | undefined {
	const list = asList(value);
	if (list === null) return undefined;
	const bullets: string[] = [];
	for (const item of list) {
		const bullet = asText(item);
		if (bullet !== null && bullet !== "") bullets.push(bullet);
	}
	return bullets.length === 0 ? undefined : bullets;
}

function readExperience(value: JsonValue | undefined): readonly ParsedExperience[] {
	return entriesOf(value).map((entry) => ({
		org: optionalText(entry, "org"),
		role: optionalText(entry, "role"),
		location: optionalText(entry, "location"),
		dates: optionalText(entry, "dates"),
		bullets: readBullets(at(entry, "bullets")),
	}));
}

/**
 * The proposal, read key by key.
 *
 * Only the five sections `resume.yaml` defines are read. A key this side does not know is left
 * where it is rather than carried through blind — the screen would have nothing to draw for it
 * and the Profile write has no field to put it in, so passing it along would only move the
 * question further from the place that can answer it.
 */
function readResumeDocument(value: JsonValue | undefined): ParsedResumeDocument {
	const mapping = asMapping(value);
	if (mapping === null) return {};
	const education = readEducation(at(mapping, "education"));
	const proficiencies = readProficiencies(at(mapping, "technical_proficiencies"));
	const experience = readExperience(at(mapping, "experience"));
	return {
		name: optionalText(mapping, "name"),
		contact: readContact(at(mapping, "contact")),
		education: education.length === 0 ? undefined : education,
		technical_proficiencies: proficiencies.length === 0 ? undefined : proficiencies,
		experience: experience.length === 0 ? undefined : experience,
	};
}

/**
 * One section report, or null.
 *
 * `confirmed` is read rather than assumed, and a report that claims `true` is drift: nothing
 * the adapter emits has been seen by the person it is about, and a screen that rendered a
 * parsed value as confirmed would be the one failure this whole step exists to prevent. So a
 * `true` here makes the whole document a mismatch rather than being quietly corrected.
 */
function readSection(entry: JsonMapping): ParsedResumeSection | null {
	const section = asText(at(entry, "section"));
	const entries = readCount(at(entry, "entries"));
	const fields = readCount(at(entry, "fields"));
	const filled = readCount(at(entry, "filled"));
	const dropped = readCount(at(entry, "dropped"));
	const confirmed = asBoolean(at(entry, "confirmed"));
	if (section === null || section === "") return null;
	if (entries === null || fields === null || filled === null || dropped === null) return null;
	if (confirmed !== false) return null;
	return { section, entries, fields, filled, dropped, confirmed: false };
}

function readSections(value: JsonValue | undefined): readonly ParsedResumeSection[] | null {
	const list = asList(value);
	if (list === null) return null;
	const sections: ParsedResumeSection[] = [];
	for (const item of list) {
		const mapping = asMapping(item);
		const section = mapping === null ? null : readSection(mapping);
		if (section === null) return null;
		sections.push(section);
	}
	return sections;
}

/**
 * Reads the adapter's document.
 *
 * Exported for its own tests. A document this does not recognise is a `mismatch` rather than a
 * guess, on `readProbeDocument`'s and `readVerifyDocument`'s own principle: naming the drift
 * beats rendering something wrong, and here "something wrong" would be somebody's own resume
 * facts assembled out of a shape nobody agreed on.
 */
export function readResumePdfDocument(text: string): ResumePdfOutcome {
	let document;
	try {
		document = asMapping(parseJson(text));
	} catch {
		return { outcome: "mismatch" };
	}
	if (document === null) return { outcome: "mismatch" };
	const outcome = asText(at(document, "outcome"));

	if (outcome === "unreadable") {
		const reason = readReason(asText(at(document, "reason")));
		return reason === null ? { outcome: "mismatch" } : { outcome: "unreadable", reason };
	}
	if (outcome === "no_runtime") return { outcome: "no_runtime" };
	if (outcome === "failed") {
		const stage = readStage(asText(at(document, "stage")));
		return stage === null ? { outcome: "mismatch" } : { outcome: "failed", stage };
	}

	const pages = readCount(at(document, "pages"));
	const characters = readCount(at(document, "characters"));
	const truncated = asBoolean(at(document, "truncated"));
	if (pages === null || characters === null || truncated === null) return { outcome: "mismatch" };

	if (outcome === "read") {
		const body = asText(at(document, "text"));
		if (body === null) return { outcome: "mismatch" };
		return { outcome: "read", text: body, pages, characters, truncated };
	}
	if (outcome === "parsed") {
		const sections = readSections(at(document, "sections"));
		const dropped = readCount(at(document, "dropped"));
		const schemaEnforced = asBoolean(at(document, "schemaEnforced"));
		if (sections === null || dropped === null || schemaEnforced === null) return { outcome: "mismatch" };
		return {
			outcome: "parsed",
			resume: readResumeDocument(at(document, "resume")),
			sections,
			pages,
			characters,
			truncated,
			dropped,
			schemaEnforced,
		};
	}
	return { outcome: "mismatch" };
}

/** What the adapter's process did, as the four facts the outcome is decided from. */
export type ResumePdfProcess = {
	readonly code: number | null;
	readonly stdout: string;
	readonly stderr: string;
	readonly timedOut: boolean;
	readonly launchFailed: boolean;
};

/**
 * What a finished adapter process means.
 *
 * Separated from the spawn so every branch is a pure function of four values and can be
 * asserted without a child process — `verify.ts` and `runs/plan.ts` split the same way, for
 * the same reason: the branches are then testable identically on every host.
 */
export function readResumePdfResult(finished: ResumePdfProcess): ResumePdfOutcome {
	if (finished.launchFailed) return { outcome: "absent" };
	if (finished.timedOut) return { outcome: "timed-out" };
	if (finished.code === PIPELINE_MISSING_EXIT) return { outcome: "absent" };
	if (finished.code !== 0) {
		// Logged rather than returned. An adapter that exited non-zero prints paths, environment
		// contents and a PDF parser's own words about somebody's document, and none of that is
		// the caller's to see.
		process.stderr.write(
			`onboarding: the resume reader exited ${String(finished.code)}: ${finished.stderr.slice(0, 400)}\n`,
		);
		return { outcome: "failed", stage: "extract" };
	}
	return readResumePdfDocument(finished.stdout);
}

/** What one reading asks for: which mode, and the runtime to name when there is one. */
export type ResumePdfRequest = {
	readonly mode: ResumeParseMode;
	/**
	 * The runtime to run the completion on, validated by the caller before it gets here.
	 *
	 * Null is not "choose one" — it is "say nothing", which leaves the choice exactly where
	 * `venator.llm.adapter` already makes it. Nothing in this module selects a lane, and there
	 * is no ladder from one to another.
	 */
	readonly lane: string | null;
};

function argumentsFor(request: ResumePdfRequest): readonly string[] {
	const named = request.lane === null ? [] : ["--lane", request.lane];
	return [READER_SCRIPT, "--mode", request.mode, ...named];
}

function runReader(
	pdf: Uint8Array,
	request: ResumePdfRequest,
	context: LocationContext,
): Promise<ResumePdfProcess> {
	const timeout = request.mode === "model" ? PARSE_TIMEOUT_MS : EXTRACT_TIMEOUT_MS;
	return new Promise((settle) => {
		const child = spawn(pythonInterpreter(context), [...argumentsFor(request)], {
			cwd: pipelineWorkingDirectory(context),
			stdio: ["pipe", "pipe", "pipe"],
			env: nonJevInheritedEnvironment(context),
		});
		let stdout = "";
		let stderr = "";
		let timedOut = false;
		let launchFailed = false;
		const timer = setTimeout(() => {
			timedOut = true;
			child.kill("SIGKILL");
		}, timeout);
		child.stdout?.setEncoding("utf8");
		child.stderr?.setEncoding("utf8");
		child.stdout?.on("data", (chunk: string) => {
			stdout += chunk;
		});
		child.stderr?.on("data", (chunk: string) => {
			stderr += chunk;
		});
		child.on("error", () => {
			launchFailed = true;
		});
		// A child that refused the bytes — because it exited first, or never started — breaks the
		// pipe, and an unhandled `EPIPE` on a stdin stream takes this process down with it. The
		// answer is the exit code and the document, which the two handlers above are already
		// collecting; the write failing is how those say what they say.
		child.stdin?.on("error", () => {});
		child.stdin?.end(pdf);
		child.on("close", (code) => {
			clearTimeout(timer);
			settle({ code, stdout, stderr, timedOut, launchFailed });
		});
	});
}

/**
 * One reading at a time in this server process — and a second is refused, not queued.
 *
 * **The defect this closes.** Nothing bounded how many readings ran at once. Ten requests
 * arriving together were ten interpreters, ten PDF parsers, and — on the model path — ten
 * completions billed against the Owner's own subscription. A person pressed one button.
 *
 * **Why refused rather than queued.** `runtime.ts` refuses a second probe while one is running
 * and this follows it, because the reasoning is the same and it is stronger here. Somebody is
 * watching a spinner, so the honest reading of a second press is that the first appeared to do
 * nothing — and a queue answers that by spending a second completion on a document they already
 * asked about once, arriving after they have stopped looking. A refusal costs nothing, leaves
 * the first reading's answer on its way, and is a sentence the screen can render. The write
 * path queues instead (`one-writer.ts`) and the difference is not inconsistency: a dropped
 * write is a fact nobody can get back, while a dropped read is a button somebody presses again.
 *
 * **Why it covers the free reading too.** `--mode text` spends no money, so a limit on it is
 * about this machine rather than about the bill — though a PDF parser over twenty pages, once
 * per press, is a real interpreter each time and ten of them are ten. The reason it is in here
 * is that the guard is then a property of *reading a resume* rather than of one mode: a mode
 * added later is covered without anybody remembering to cover it, and two readings cannot
 * interleave into a screen showing one document's text beside another's proposal. A person has
 * one resume and one file chooser. There is no reading anybody wants two of at once.
 *
 * **What it does not cover.** It is per server process, exactly as `one-writer.ts`'s
 * serialisation is per Profile per process: two server processes against one application data
 * directory still race, and nothing here prevents it. The run surface's port is fixed and an
 * existing listener is adopted rather than a second one started, so that is unusual rather than
 * impossible; closing it needs a lock on the filesystem and a decision about what a stale one
 * means after a crash. It is also not the first line of defence:
 * `views/onboarding/resume.tsx` disables both read buttons while
 * a reading is in flight and says why. This is the backstop for everything that is not that
 * screen.
 */
export class ResumeReaderBusyError extends Error {
	constructor() {
		super("A resume is already being read.");
		this.name = "ResumeReaderBusyError";
	}
}

let reading = false;

/**
 * Runs `work` as the one reading in flight, or throws `ResumeReaderBusyError` at the caller.
 *
 * **The release is a `finally` around a single `await`**, so every way a reading can end goes
 * through one line: a document, a non-zero exit, a child killed where it stood, an interpreter
 * that would not start, the 330-second timeout, and a `spawn` that throws before there is a
 * process at all. A guard that stuck would lock somebody out of their own setup for the life of
 * the server process, which is a worse failure than the one it exists to prevent, so there is
 * exactly one place it is cleared and no early return before it.
 *
 * Exported for its own tests, which is the only way to assert that release on the paths a test
 * cannot reach through a real child.
 */
export async function oneReadingAtATime<T>(work: () => Promise<T>): Promise<T> {
	if (reading) throw new ResumeReaderBusyError();
	reading = true;
	try {
		return await work();
	} finally {
		reading = false;
	}
}

/** Whether a reading is in flight. For the tests that assert the guard does not stick. */
export function readingInFlight(): boolean {
	return reading;
}

/**
 * Reads one uploaded PDF, in the mode the caller asked for.
 *
 * The bytes never touch the filesystem: they go to the child on stdin and the answer comes
 * back on stdout. That is not only tidiness — a resume written to a temporary file is a copy of
 * somebody's own document left on disk for a process crash to keep.
 *
 * Throws `ResumeReaderBusyError` — and spawns nothing at all — when a reading is already in
 * flight in this process. That is the one thing this function does that is not an answer about
 * the document, and it is deliberate: a refusal a caller has to handle cannot be mistaken for a
 * reading that happened.
 */
export async function readResumePdf(
	pdf: Uint8Array,
	request: ResumePdfRequest,
	context: LocationContext = systemContext(),
): Promise<ResumePdfOutcome> {
	return await oneReadingAtATime(async () => readResumePdfResult(await runReader(pdf, request, context)));
}
