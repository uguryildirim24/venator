/**
 * One resume reading at a time, and what happens to the second.
 *
 * `POST /api/onboarding/resume/import` spawns a real process for every attached PDF, and on
 * `parse=model` that process spends one request on the Owner's own subscription. Nothing
 * bounded how many of those ran at once: ten requests arriving together were ten interpreters
 * and ten completions billed for the one press somebody made. `oneReadingAtATime` in
 * `server/onboarding/resume-pdf.ts` refuses the second rather than queueing it, on
 * `runtime.ts`'s posture for the runtime probe.
 *
 * Two properties are worth a test and both are asserted by looking at the child rather than at
 * the response alone:
 *
 * 1. **A refused reading spawns nothing.** The stand-in records the argument list it was given
 *    (`tests/resume-reader-stand-in.ts`), so "was a second completion asked for?" is answered
 *    by the absence of a second recording, not by a status code.
 * 2. **The guard never sticks.** A guard that survived a failure would lock somebody out of
 *    their own setup for the life of the server process — worse than the defect it closes — so
 *    every way a reading can end is followed by a reading that is admitted.
 *
 * **No test here reaches a real runtime or spends anything.** The reader is stood in for by
 * this process's own `node`, armed to hold for a moment so a reading can be in flight while a
 * second request arrives.
 */

import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import {
	oneReadingAtATime,
	readingInFlight,
	ResumeReaderBusyError,
} from "../server/onboarding/resume-pdf.ts";
import { createOnboardingRoutes } from "../server/onboarding/routes.ts";
import { ONBOARDING_REQUEST_HEADER, ONBOARDING_REQUEST_HEADER_VALUE } from "../shared/onboarding.ts";
import {
	armReaderStandIn,
	disarmReaderStandIn,
	forgetReaderCall,
	lastReaderCall,
	withReaderAnswer,
	withReaderDelay,
	withReaderExit,
} from "./resume-reader-stand-in.ts";

before(() => armReaderStandIn());
after(() => disarmReaderStandIn());

/** How long a held reading stays in flight. Long enough to lose a race, short enough to wait. */
const HELD_MS = 1_500;

/** Bytes that begin as a PDF does. Nothing here parses one; the stand-in answers for it. */
function pdfBytes(): Uint8Array<ArrayBuffer> {
	const bytes = new Uint8Array(64);
	bytes.set(new TextEncoder().encode("%PDF-1.7\n"), 0);
	return bytes;
}

type RequestHeaders = { readonly [ONBOARDING_REQUEST_HEADER]: string };

function headers(): RequestHeaders {
	return { [ONBOARDING_REQUEST_HEADER]: ONBOARDING_REQUEST_HEADER_VALUE };
}

/**
 * One attached PDF, asking for the reading named.
 *
 * Unlike `resume-pdf.test.ts`'s helper this does **not** forget the last call: which recording
 * is cleared and when is the whole of what these tests measure, so it is done in the test.
 */
async function post(parse: "model" | "text"): Promise<Response> {
	const form = new FormData();
	form.append("file", new File([pdfBytes()], "resume.pdf", { type: "application/pdf" }));
	form.append("parse", parse);
	return await createOnboardingRoutes().request("/resume/import", { method: "POST", headers: headers(), body: form });
}

/** A proposal the model path answers with, so a held reading finishes as a real answer. */
const PARSED = JSON.stringify({
	outcome: "parsed",
	mode: "model",
	resume: { name: "Alex Q. Rivera", contact: { email: "alex.rivera@example.com" } },
	sections: [{ section: "name", entries: 1, fields: 1, filled: 1, dropped: 0, confirmed: false }],
	pages: 1,
	characters: 120,
	truncated: false,
	dropped: 0,
	schemaEnforced: true,
});

/** The document's text, for the free reading. */
const EXTRACTED = JSON.stringify({
	outcome: "read",
	mode: "text",
	text: "Alex Q. Rivera\nBoston, MA • alex.rivera@example.com",
	pages: 1,
	characters: 50,
	truncated: false,
});

/** Waits until the held child has recorded itself, so the reading is genuinely in flight. */
async function waitForSpawn(): Promise<void> {
	for (let attempt = 0; attempt < 400; attempt += 1) {
		if (lastReaderCall() !== null) return;
		await new Promise((wake) => setTimeout(wake, 10));
	}
	assert.fail("the reader was never spawned");
}

// --- the refusal -------------------------------------------------------------

test("a second parse=model while one is in flight is refused, and spawns nothing", async () => {
	await withReaderAnswer(PARSED, async () => {
		await withReaderDelay(HELD_MS, async () => {
			forgetReaderCall();
			const held = post("model");
			await waitForSpawn();
			// Cleared while the first child is still alive, so anything recorded from here is a
			// second child. The stand-in records before it holds, which is what makes this safe.
			forgetReaderCall();

			const second = await post("model");
			assert.equal(second.status, 409);
			const body = await second.json();
			assert.equal(body.error.code, "reader_busy");
			assert.match(body.error.message, /already being read/u);
			assert.equal(
				lastReaderCall(),
				null,
				"a refused reading must not spawn a second reader, which is a second request spent",
			);
			assert.equal(readingInFlight(), true, "refusing the second upload must not release the first");

			const first = await held;
			assert.equal(first.status, 200, "the reading that was in flight still answers");
			assert.equal((await first.json()).kind, "parsed");
			assert.equal(readingInFlight(), false);
		});
	});
});

test("the free reading is guarded too, in both directions", async () => {
	// `--mode text` spends no money, so this is about the box rather than the bill — and about
	// the guard being a property of reading a resume rather than of one mode, so that the two
	// cannot interleave and a mode added later is covered without anybody remembering to.
	await withReaderAnswer(EXTRACTED, async () => {
		await withReaderDelay(HELD_MS, async () => {
			forgetReaderCall();
			const held = post("text");
			await waitForSpawn();
			forgetReaderCall();

			const free = await post("text");
			assert.equal(free.status, 409);
			assert.equal((await free.json()).error.code, "reader_busy");

			// And the paid one is refused behind a free one, which is the direction that matters:
			// a second press while a free reading runs must not become a request spent.
			const paid = await post("model");
			assert.equal(paid.status, 409);
			assert.equal((await paid.json()).error.code, "reader_busy");
			assert.equal(lastReaderCall(), null, "neither refusal spawned anything");

			assert.equal((await held).status, 200);
		});
	});
});

// --- the guard never sticks --------------------------------------------------

test("the guard clears after a reading finishes", async () => {
	await withReaderAnswer(PARSED, async () => {
		const first = await post("model");
		assert.equal(first.status, 200);
		assert.equal(readingInFlight(), false);

		forgetReaderCall();
		const second = await post("model");
		assert.equal(second.status, 200, "the next reading is admitted");
		assert.notEqual(lastReaderCall(), null, "and it really spawned a reader");
	});
});

test("the guard clears after a reading fails", async () => {
	await withReaderExit(1, async () => {
		const failed = await post("model");
		assert.equal(failed.status, 422);
		assert.equal((await failed.json()).error.code, "parse_failed");
		assert.equal(readingInFlight(), false);
	});
	await withReaderAnswer(PARSED, async () => {
		forgetReaderCall();
		const next = await post("model");
		assert.equal(next.status, 200, "a failed reading must not lock the step for good");
		assert.notEqual(lastReaderCall(), null);
	});
});

test("the guard clears when the reader could not be launched at all", async () => {
	const interpreter = process.env["VENATOR_PYTHON"];
	process.env["VENATOR_PYTHON"] = "/venator/no/such/interpreter";
	try {
		const absent = await post("model");
		assert.equal(absent.status, 503);
		assert.equal((await absent.json()).error.code, "reader_unavailable");
		assert.equal(readingInFlight(), false);
	} finally {
		if (interpreter === undefined) delete process.env["VENATOR_PYTHON"];
		else process.env["VENATOR_PYTHON"] = interpreter;
	}
	await withReaderAnswer(PARSED, async () => {
		forgetReaderCall();
		const next = await post("model");
		assert.equal(next.status, 200, "an Install that could not launch one is not locked out of the next");
		assert.notEqual(lastReaderCall(), null);
	});
});

/**
 * The two settle paths a test cannot reach through a real child.
 *
 * The 330-second timeout would have to be waited out, and a `spawn` that throws where it stands
 * needs an argument list this module will not build. Both end at the same `finally`, and that
 * is what is asserted here — directly on the guard, which is exported for exactly this.
 */
test("the guard clears however a reading ends, including the ways a child cannot show", async () => {
	// What a timeout looks like from the guard: a reading that resolved, carrying a refusal.
	const timedOut = await oneReadingAtATime(async () => ({ outcome: "timed-out" }) as const);
	assert.equal(timedOut.outcome, "timed-out");
	assert.equal(readingInFlight(), false);

	// What a spawn that throws where it stands looks like: a reading that rejected.
	await assert.rejects(
		oneReadingAtATime(async () => {
			throw new Error("the interpreter refused its arguments");
		}),
		/refused its arguments/u,
	);
	assert.equal(readingInFlight(), false);

	// And the next reading is admitted after both.
	assert.equal(await oneReadingAtATime(async () => "admitted"), "admitted");
});

test("a refused reading never runs the work behind the guard", async () => {
	let started = 0;
	let release = () => {};
	const gate = new Promise<void>((wake) => {
		release = () => wake();
	});
	const held = oneReadingAtATime(async () => {
		started += 1;
		await gate;
		return "first";
	});
	await assert.rejects(
		oneReadingAtATime(async () => {
			started += 1;
			return "second";
		}),
		ResumeReaderBusyError,
	);
	assert.equal(started, 1, "the refused reading's work never ran");
	release();
	assert.equal(await held, "first");
	assert.equal(readingInFlight(), false);
});
