/**
 * What the extractor reads, and — mostly — what it refuses to guess.
 *
 * Every assertion about a value is an assertion that it came back exactly as written. The
 * tests about what is *not* found matter more than the ones about what is: this endpoint
 * writes nothing, and a field it reports as missing is a field a person fills in, while a
 * field it invents is one they will scroll past.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { extractResume, readResumeText, ResumeReadError } from "../server/onboarding/resume.ts";

const RESUME = [
	"Alex Q. Rivera",
	"Boston, MA • (617) 555-0142 • alex.rivera@example.com • www.linkedin.com/in/alexqrivera",
	"",
	"EDUCATION",
	"Northeastern University - Boston, MA",
	"Bachelor of Science in Chemical Engineering, May 2027",
	"",
	"RELEVANT EXPERIENCE",
	"Some Employer - Cambridge, MA",
	"Laboratory Assistant, June 2025 - Present",
	"  - Ran 40 assays a week on a liquid handler",
].join("\n");

test("the five contact facts come back exactly as they were written", () => {
	const found = extractResume(RESUME);
	assert.equal(found.resume.name, "Alex Q. Rivera");
	assert.equal(found.resume.contact.location, "Boston, MA");
	assert.equal(found.resume.contact.phone, "(617) 555-0142");
	assert.equal(found.resume.contact.email, "alex.rivera@example.com");
	assert.equal(found.resume.contact.linkedin, "www.linkedin.com/in/alexqrivera");
	assert.deepEqual([...found.missing], []);
});

test("a location further down the page is not mistaken for the applicant's own", () => {
	// Cambridge, MA appears on an employer line; the contact line says Boston.
	assert.equal(extractResume(RESUME).resume.contact.location, "Boston, MA");
});

test("a missing fact is reported missing rather than filled in", () => {
	const sparse = extractResume("Alex Q. Rivera\nalex@example.com\n\nEDUCATION\nSomewhere");
	assert.equal(sparse.resume.contact.phone, null);
	assert.equal(sparse.resume.contact.linkedin, null);
	assert.ok(sparse.missing.includes("phone"));
	assert.ok(sparse.missing.includes("linkedin"));
	assert.ok(sparse.found.includes("email"));
});

test("a heading is never read as a name", () => {
	assert.equal(extractResume("RESUME\n\nEDUCATION\nSomewhere").resume.name, null);
	assert.equal(extractResume("CURRICULUM VITAE\nalex@example.com").resume.name, null);
});

test("a line that is plainly not a name is not one", () => {
	assert.equal(extractResume("alex@example.com\n617-555-0142").resume.name, null);
	assert.equal(extractResume("A B C D E F G\n").resume.name, null);
});

test("nothing is inferred from anything else", () => {
	// An email domain does not become an employer; a country code does not become a location.
	const found = extractResume("Alex Rivera\n+44 20 7946 0958 alex@bigco.co.uk");
	assert.equal(found.resume.contact.location, null);
	assert.equal(found.resume.name, "Alex Rivera");
});

test("a PDF is refused with something to do about it, not decoded approximately", () => {
	const pdf = new TextEncoder().encode("%PDF-1.7\n%âãÏÓ\n1 0 obj");
	assert.throws(
		() => readResumeText(pdf),
		(error: Error) => error instanceof ResumeReadError && /PDF/u.test(error.message),
	);
});

test("a Word document is refused the same way", () => {
	assert.throws(
		() => readResumeText(new TextEncoder().encode("PKrest of a docx")),
		(error: Error) => error instanceof ResumeReadError && /Word document/u.test(error.message),
	);
});

test("an empty file and a file that is not text are both refused", () => {
	assert.throws(() => readResumeText(new Uint8Array(0)), ResumeReadError);
	assert.throws(() => readResumeText(new Uint8Array([0x41, 0x00, 0x42])), ResumeReadError);
});

test("what the extractor did not attempt is said out loud", () => {
	const found = extractResume(RESUME);
	assert.ok(found.notAttempted.some((line) => line.includes("Education, experience")));
	assert.ok(found.notAttempted.some((line) => line.includes("plain text")));
});
