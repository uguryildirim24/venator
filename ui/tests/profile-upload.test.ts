import assert from "node:assert/strict";
import { test } from "node:test";
import { MAXIMUM_PDF_BYTES } from "../shared/onboarding.ts";
import { readProfileUpload, MAXIMUM_PROFILE_UPLOAD_BYTES } from "../server/onboarding/profile-upload.ts";

function request(pdf: Blob = new Blob(["%PDF-original"]), proposal = "{}"): Request {
	const form = new FormData();
	form.append("proposal", proposal);
	form.append("file", pdf, "resume.pdf");
	return new Request("http://localhost/profile", { method: "POST", body: form });
}

test("profile multipart retains exact PDF bytes and proposal without persisting the import", async () => {
	const result = await readProfileUpload(request());
	assert.equal(result.json, "{}");
	assert.equal(new TextDecoder().decode(result.pdf), "%PDF-original");
});

test("profile uploads enforce PDF and JSON bounds and reject duplicate attachments", async () => {
	await assert.rejects(readProfileUpload(request(new Blob([new Uint8Array(MAXIMUM_PDF_BYTES + 1)]))));
	await assert.rejects(readProfileUpload(request(new Blob(["%PDF-a"]), "é".repeat(300_000))));
	await assert.rejects(readProfileUpload(request(new Blob(["not PDF"]))));
	const form = new FormData();
	form.append("proposal", "{}");
	form.append("file", new Blob(["%PDF-one"]), "one.pdf");
	form.append("file", new Blob(["%PDF-two"]), "two.pdf");
	await assert.rejects(readProfileUpload(new Request("http://localhost/profile", { method: "POST", body: form })));
});

test("profile upload stops an oversized streamed body even without a Content-Length", async () => {
	let cancelled = false;
	const body = new ReadableStream<Uint8Array>({
		pull(controller) { controller.enqueue(new Uint8Array(1024 * 1024)); },
		cancel() { cancelled = true; },
	});
	const init = { method: "POST", body, duplex: "half" };
	const input = new Request("http://localhost/profile", init);
	await assert.rejects(readProfileUpload(input));
	assert.equal(cancelled, true);
	const declared = request();
	declared.headers.set("content-length", String(MAXIMUM_PROFILE_UPLOAD_BYTES + 1));
	await assert.rejects(readProfileUpload(declared));
});
