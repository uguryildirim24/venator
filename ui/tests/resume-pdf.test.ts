/**
 * Reading an uploaded resume PDF: what is asked of the pipeline, and what comes back.
 *
 * The two things worth testing here are not the parsing — that is
 * `venator.resume.parse`'s, in Python, with its own tests — but the two properties this side
 * owns and can get wrong:
 *
 * 1. **Which reading was asked for.** One of the two spends a request on the Owner's own
 *    subscription. So the argument list the adapter is actually spawned with is asserted, and
 *    the case that matters most is the negative one: a multipart PDF with no `parse` field must
 *    never reach `--mode model`. There is no default into it, no fallback into it, and no
 *    retry that escalates into it.
 * 2. **What each answer becomes.** Every outcome the adapter can give maps to one refusal
 *    written by hand, and nothing the adapter, the runtime, the model or the PDF library said
 *    is in any of them. An answer this side does not recognise is a mismatch and is never
 *    guessed at.
 *
 * **No test here reaches a real runtime or spends anything.** The adapter is stood in for by
 * this process's own `node` (`tests/resume-reader-stand-in.ts`), which records what it was
 * given and prints what the case armed it with.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { after, before, test } from "node:test";

import { CHECKOUT_ROOT } from "../server/locations.ts";
import { readResumePdfDocument, readResumePdfResult } from "../server/onboarding/resume-pdf.ts";
import { createOnboardingRoutes } from "../server/onboarding/routes.ts";
import {
	MAXIMUM_PDF_BYTES,
	ONBOARDING_REQUEST_HEADER,
	ONBOARDING_REQUEST_HEADER_VALUE,
	PDF_UNREADABLE_REASONS,
} from "../shared/onboarding.ts";
import {
	armReaderStandIn,
	disarmReaderStandIn,
	forgetReaderCall,
	lastReaderCall,
	withReaderAnswer,
	withReaderExit,
} from "./resume-reader-stand-in.ts";

before(() => armReaderStandIn());
after(() => disarmReaderStandIn());

/** A resume as text. The same one `resume.test.ts` uses, so the two agree about what is read. */
const RESUME_TEXT = [
	"Alex Q. Rivera",
	"Boston, MA • (617) 555-0142 • alex.rivera@example.com • www.linkedin.com/in/alexqrivera",
	"",
	"EDUCATION",
	"Northeastern University - Boston, MA",
].join("\n");

/** Bytes that begin as a PDF does. Nothing here parses one; the stand-in answers for it. */
function pdfBytes(size = 64): Uint8Array<ArrayBuffer> {
	const bytes = new Uint8Array(size);
	bytes.set(new TextEncoder().encode("%PDF-1.7\n"), 0);
	return bytes;
}

/** The header every onboarding write must carry, and nothing else. */
type RequestHeaders = { readonly [ONBOARDING_REQUEST_HEADER]: string };

function headers(): RequestHeaders {
	return { [ONBOARDING_REQUEST_HEADER]: ONBOARDING_REQUEST_HEADER_VALUE };
}

async function postForm(form: FormData): Promise<Response> {
	forgetReaderCall();
	return await createOnboardingRoutes().request("/resume/import", {
		method: "POST",
		headers: headers(),
		body: form,
	});
}

function attach(bytes: Uint8Array<ArrayBuffer>, fields: ReadonlyMap<string, string> = new Map()): FormData {
	const form = new FormData();
	form.append("file", new File([bytes], "resume.pdf", { type: "application/pdf" }));
	for (const [key, value] of fields) form.append(key, value);
	return form;
}

// --- the free paths, exactly as they were ------------------------------------

test("a pasted resume is still read here, with nothing spawned", async () => {
	forgetReaderCall();
	const response = await createOnboardingRoutes().request("/resume/import", {
		method: "POST",
		headers: { ...headers(), "content-type": "application/json" },
		body: JSON.stringify({ text: RESUME_TEXT }),
	});
	assert.equal(response.status, 200);
	const body = await response.json();
	assert.equal(body.kind, "read");
	assert.equal(body.resume.name, "Alex Q. Rivera");
	assert.equal(body.resume.contact.email, "alex.rivera@example.com");
	assert.equal(lastReaderCall(), null, "the pipeline was never spawned for a pasted resume");
});

test("a text/plain body is still read here, with nothing spawned", async () => {
	forgetReaderCall();
	const response = await createOnboardingRoutes().request("/resume/import", {
		method: "POST",
		headers: { ...headers(), "content-type": "text/plain" },
		body: RESUME_TEXT,
	});
	assert.equal(response.status, 200);
	assert.equal((await response.json()).resume.contact.phone, "(617) 555-0142");
	assert.equal(lastReaderCall(), null);
});

test("a PDF pasted as a body is refused, and the refusal points at attaching it", async () => {
	forgetReaderCall();
	const response = await createOnboardingRoutes().request("/resume/import", {
		method: "POST",
		headers: { ...headers(), "content-type": "text/plain" },
		body: "%PDF-1.7 and the rest of somebody's document",
	});
	assert.equal(response.status, 422);
	const body = await response.json();
	assert.equal(body.error.code, "extraction_failed");
	assert.match(body.error.message, /PDF/u);
	assert.match(body.error.remedy, /Attach the PDF as a file/u);
	assert.equal(lastReaderCall(), null, "a refusal never costs a subprocess");
});

test("a Word document and bytes that are not text keep their own refusals", async () => {
	// The zip magic written as the four bytes it is. Spelling it as a string literal here would
	// put two invisible control characters in this file, which is exactly how the first version
	// of this test came to assert something else while looking correct.
	const docx = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x14, 0x00, 0x00, 0x00]);
	const word = await postForm(attach(docx));
	assert.equal(word.status, 422);
	assert.match((await word.json()).error.message, /Word document/u);
	assert.equal(lastReaderCall(), null, "a packaged file is refused without a subprocess");

	const binary = await postForm(attach(new Uint8Array([0x41, 0x00, 0x42])));
	assert.equal(binary.status, 422);
	assert.match((await binary.json()).error.message, /not text/u);
	assert.equal(lastReaderCall(), null);
});

// --- which reading was asked for --------------------------------------------

test("a multipart PDF with no parse field never asks for a completion", async () => {
	await withReaderAnswer(
		JSON.stringify({ outcome: "read", mode: "text", text: RESUME_TEXT, pages: 1, characters: 120, truncated: false }),
		async () => {
			const response = await postForm(attach(pdfBytes()));
			assert.equal(response.status, 200);
			const body = await response.json();
			// It came back through the deterministic extractor, which is the free reading.
			assert.equal(body.kind, "read");
			assert.equal(body.resume.name, "Alex Q. Rivera");

			const call = lastReaderCall();
			assert.notEqual(call, null, "the extractor still runs as a subprocess");
			assert.deepEqual([...(call?.argv ?? [])].slice(1), ["--mode", "text"]);
			assert.ok(!(call?.argv ?? []).includes("model"), "no completion was asked for");
			assert.ok(!(call?.argv ?? []).includes("--lane"));
		},
	);
});

test("a parse field holding anything but the one word is the free reading", async () => {
	const answer = JSON.stringify({
		outcome: "read",
		mode: "text",
		text: RESUME_TEXT,
		pages: 1,
		characters: 120,
		truncated: false,
	});
	for (const asked of ["", "text", "Model", "MODEL", "true", "1", "yes", "model "]) {
		await withReaderAnswer(answer, async () => {
			const response = await postForm(attach(pdfBytes(), new Map([["parse", asked]])));
			assert.equal(response.status, 200, `parse=${asked}`);
			assert.deepEqual(
				[...(lastReaderCall()?.argv ?? [])].slice(1),
				["--mode", "text"],
				`parse=${asked} must not spend`,
			);
		});
	}
});

test("parse=model spawns the reader in model mode, with the PDF on its stdin", async () => {
	const parsed = {
		outcome: "parsed",
		mode: "model",
		resume: {
			name: "Alex Q. Rivera",
			contact: { location: "Boston, MA", phone: "(617) 555-0142", email: "alex.rivera@example.com" },
			education: [{ org: "Northeastern University", degree: "Bachelor of Science" }],
			experience: [{ org: "Some Employer", role: "Laboratory Assistant", bullets: ["Ran 40 assays a week"] }],
		},
		sections: [
			{ section: "education", entries: 1, fields: 6, filled: 2, dropped: 1, confirmed: false },
			{ section: "experience", entries: 1, fields: 5, filled: 3, dropped: 0, confirmed: false },
		],
		pages: 2,
		characters: 2400,
		truncated: false,
		dropped: 1,
		schemaEnforced: true,
	};
	await withReaderAnswer(JSON.stringify(parsed), async () => {
		const bytes = pdfBytes(512);
		const response = await postForm(attach(bytes, new Map([["parse", "model"], ["lane", "claude"]])));
		assert.equal(response.status, 200);
		const body = await response.json();
		assert.equal(body.kind, "parsed");
		assert.equal(body.resume.name, "Alex Q. Rivera");
		assert.equal(body.resume.education[0].org, "Northeastern University");
		assert.equal(body.resume.experience[0].bullets[0], "Ran 40 assays a week");
		assert.equal(body.dropped, 1);
		assert.equal(body.schemaEnforced, true);
		// The five the renderer needs, worked out on this side from what came back.
		assert.deepEqual([...body.found], ["name", "location", "phone", "email"]);
		assert.deepEqual([...body.missing], ["linkedin"]);
		// Nothing is confirmed, and the screen renders provenance from exactly this.
		assert.ok(body.sections.every((section: { confirmed: boolean }) => section.confirmed === false));

		const call = lastReaderCall();
		assert.deepEqual([...(call?.argv ?? [])].slice(1), ["--mode", "model", "--lane", "claude"]);
		assert.deepEqual([...(call?.stdin ?? [])], [...bytes], "the PDF reached the adapter unaltered");
	});
});

test("a lane that is not a lane name is refused before it becomes an argument", async () => {
	const response = await postForm(attach(pdfBytes(), new Map([["parse", "model"], ["lane", "../../etc"]])));
	assert.equal(response.status, 400);
	const body = await response.json();
	assert.equal(body.error.code, "bad_request");
	assert.equal(body.error.field, "lane");
	assert.equal(lastReaderCall(), null, "nothing was spawned");
});

// --- what each answer becomes ------------------------------------------------

test("every unreadable reason has its own sentence and its own remedy", async () => {
	const seen = new Set<string>();
	for (const reason of PDF_UNREADABLE_REASONS) {
		await withReaderAnswer(JSON.stringify({ outcome: "unreadable", reason }), async () => {
			const response = await postForm(attach(pdfBytes()));
			assert.equal(response.status, 422, reason);
			const body = await response.json();
			assert.equal(body.error.code, "extraction_failed");
			assert.notEqual(body.error.remedy, null, `${reason} says what to do about it`);
			assert.ok(String(body.error.remedy).length > 0);
			// The reason itself is machine vocabulary and never reaches the person.
			assert.ok(!body.error.message.includes(reason));
			seen.add(body.error.message);
		});
	}
	assert.equal(seen.size, PDF_UNREADABLE_REASONS.length, "no two reasons share a sentence");
});

test("no runtime is its own answer, with the free reading named as the way on", async () => {
	await withReaderAnswer(JSON.stringify({ outcome: "no_runtime" }), async () => {
		const response = await postForm(attach(pdfBytes(), new Map([["parse", "model"]])));
		assert.equal(response.status, 503);
		const body = await response.json();
		assert.equal(body.error.code, "runtime_unavailable");
		assert.match(body.error.remedy, /read the PDF on this machine/u);
	});
});

test("a reading that gave up names its stage and quotes nothing", async () => {
	for (const stage of ["extract", "complete", "decode"]) {
		await withReaderAnswer(JSON.stringify({ outcome: "failed", stage }), async () => {
			const response = await postForm(attach(pdfBytes(), new Map([["parse", "model"]])));
			assert.equal(response.status, 422, stage);
			const body = await response.json();
			assert.equal(body.error.code, "parse_failed");
			assert.ok(!body.error.message.includes(stage), "the stage is described, not echoed");
		});
	}
});

test("an Install with no pipeline in it says so rather than looking broken", async () => {
	await withReaderExit(3, async () => {
		const response = await postForm(attach(pdfBytes()));
		assert.equal(response.status, 503);
		const body = await response.json();
		assert.equal(body.error.code, "reader_unavailable");
		assert.match(body.error.message, /cannot read a PDF/u);
	});
});

test("an adapter that exits non-zero is a failure, and its stderr is not the answer", async () => {
	await withReaderExit(1, async () => {
		const response = await postForm(attach(pdfBytes()));
		assert.equal(response.status, 422);
		assert.equal((await response.json()).error.code, "parse_failed");
	});
});

test("an answer this dashboard does not know is reported, never guessed at", async () => {
	for (const answer of ['{"outcome":"triumph"}', "{}", "not json at all", '{"outcome":"read","pages":1}']) {
		await withReaderAnswer(answer, async () => {
			const response = await postForm(attach(pdfBytes()));
			assert.equal(response.status, 503, answer);
			const body = await response.json();
			assert.equal(body.error.code, "reader_unavailable");
			assert.match(body.error.message, /shape this dashboard does not know/u);
		});
	}
});

test("an outcome that does not match the mode that was asked for is a mismatch", async () => {
	// A `parsed` document off a `--mode text` run is drift, and drift is not a proposal.
	const parsed = JSON.stringify({
		outcome: "parsed",
		resume: {},
		sections: [],
		pages: 1,
		characters: 10,
		truncated: false,
		dropped: 0,
		schemaEnforced: false,
	});
	await withReaderAnswer(parsed, async () => {
		const response = await postForm(attach(pdfBytes()));
		assert.equal(response.status, 503);
		assert.equal((await response.json()).error.code, "reader_unavailable");
	});
});

test("a section claiming to be confirmed makes the whole document a mismatch", () => {
	// Nothing the adapter emits has been seen by the person it is about. A `true` here would be
	// the screen rendering a stranger's sentences as the Owner's own confirmed facts.
	const document = readResumePdfDocument(
		JSON.stringify({
			outcome: "parsed",
			resume: { name: "Sinclair Vale" },
			sections: [{ section: "name", entries: 1, fields: 1, filled: 1, dropped: 0, confirmed: true }],
			pages: 1,
			characters: 10,
			truncated: false,
			dropped: 0,
			schemaEnforced: false,
		}),
	);
	assert.equal(document.outcome, "mismatch");
});

test("a timeout and a launch failure are answers rather than crashes", () => {
	assert.deepEqual(readResumePdfResult({ code: null, stdout: "", stderr: "", timedOut: true, launchFailed: false }), {
		outcome: "timed-out",
	});
	assert.deepEqual(readResumePdfResult({ code: null, stdout: "", stderr: "", timedOut: false, launchFailed: true }), {
		outcome: "absent",
	});
});

test("keys the adapter omitted stay omitted rather than becoming null", () => {
	const document = readResumePdfDocument(
		JSON.stringify({
			outcome: "parsed",
			resume: { name: "Alex Q. Rivera", education: [{ org: "Northeastern University" }] },
			sections: [],
			pages: 1,
			characters: 10,
			truncated: false,
			dropped: 0,
			schemaEnforced: false,
		}),
	);
	assert.equal(document.outcome, "parsed");
	if (document.outcome !== "parsed") return;
	assert.equal(document.resume.contact, undefined, "an absent contact block is absent, not null");
	assert.equal(document.resume.education?.[0]?.degree, undefined);
	assert.equal(document.resume.experience, undefined);
	// And what is on the wire has no key there at all, which is what the Profile write needs.
	assert.ok(!JSON.stringify(document.resume).includes("degree"));
});

// --- the bound ---------------------------------------------------------------

test("the PDF bound this side declares is the one the pipeline enforces", () => {
	// Stated twice because the two toolchains never import from each other, so this is the only
	// thing keeping them equal. `src/venator/resume/parse.py` is the authority.
	const parse = readFileSync(resolve(CHECKOUT_ROOT ?? ".", "src/venator/resume/parse.py"), "utf8");
	const declared = /MAXIMUM_PDF_BYTES = (\d+) \* 1024 \* 1024/u.exec(parse);
	assert.notEqual(declared, null, "the pipeline still declares the bound in megabytes");
	assert.equal(Number(declared?.[1]) * 1024 * 1024, MAXIMUM_PDF_BYTES);
});

test("a declared length over the bound is refused before the body is read", async () => {
	forgetReaderCall();
	const response = await createOnboardingRoutes().request("/resume/import", {
		method: "POST",
		headers: {
			...headers(),
			"content-type": "multipart/form-data; boundary=x",
			"content-length": String(MAXIMUM_PDF_BYTES + 1),
		},
		body: "--x--\r\n",
	});
	assert.equal(response.status, 413);
	assert.equal((await response.json()).error.code, "body_too_large");
	assert.equal(lastReaderCall(), null, "nothing was spawned and nothing was buffered");
});

test("an attachment over the bound is refused from its size, not from its contents", async () => {
	const response = await postForm(attach(pdfBytes(MAXIMUM_PDF_BYTES + 1)));
	assert.equal(response.status, 413);
	assert.equal((await response.json()).error.code, "body_too_large");
	assert.equal(lastReaderCall(), null, "the pipeline was never handed four megabytes");
});

test("the text bound is untouched: an attached text file over 512 KiB is still refused", async () => {
	const long = new TextEncoder().encode("a".repeat(600 * 1024));
	const response = await postForm(attach(long));
	assert.equal(response.status, 413);
	assert.equal((await response.json()).error.code, "body_too_large");
	assert.equal(lastReaderCall(), null);
});
