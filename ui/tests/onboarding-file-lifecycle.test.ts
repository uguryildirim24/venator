import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { createServer } from "vite";
import { installDom } from "../tools/dom-harness.ts";
import { EMPTY_DRAFT, profileProposal } from "../src/onboarding/draft.ts";

const settle = () => new Promise((resolve) => setTimeout(resolve, 30));

const READ = {
	kind: "read",
	resume: {
		name: "Alex Q. Rivera",
		contact: { location: "Boston, MA", phone: "617-555-0100", email: "alex@example.com", linkedin: "linkedin.com/in/alex" },
	},
	found: ["name", "location", "phone", "email", "linkedin"],
	missing: [],
	notAttempted: [],
};

test("choosing a PDF reads it once, the file survives leaving the step, and Save sends bytes only on request", async () => {
	installDom("http://localhost");
	const before = globalThis.fetch;
	const calls: { readonly url: string; readonly body: BodyInit | null | undefined }[] = [];
	globalThis.fetch = async (input, init) => {
		const url = String(input);
		if ((init?.method ?? "GET") === "GET") {
			return new Response(JSON.stringify({ runtime: "claude", jevKeyPresent: false }), { status: 200, headers: { "content-type": "application/json" } });
		}
		calls.push({ url, body: init?.body });
		const answer = url.endsWith("/resume/import") ? READ : { profile: { name: "sample", directory: "sample", files: [] }, replaced: false, warnings: [] };
		return new Response(JSON.stringify(answer), { status: 200, headers: { "content-type": "application/json" } });
	};
	const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), server: { middlewareMode: true }, appType: "custom", logLevel: "silent" });
	const { createRoot } = await import("react-dom/client");
	const container = document.createElement("div");
	document.body.append(container);
	const flowRoot = createRoot(container);
	try {
		const { OnboardingFlow } = await server.ssrLoadModule("/src/views/onboarding/flow.tsx");
		const render = async (step: string) => { flowRoot.render(createElement(OnboardingFlow, { step, implied: null, onWritten: () => {} })); await settle(); };
		await render("resume");
		const file = new File(["%PDF-original"], "original.pdf", { type: "application/pdf" });
		const input = container.querySelector('input[type="file"]');
		assert.ok(input);
		Object.defineProperty(input, "files", { configurable: true, value: [file] });
		input.dispatchEvent(new window.Event("change", { bubbles: true }));
		await settle();
		assert.equal(calls.length, 1, "choosing the file is the one press that reads it");
		const read = calls[0]?.body;
		assert.ok(read instanceof FormData);
		// No assistant was connected, so the free reading is the only one offered or sent.
		assert.equal(read.get("parse"), "text");
		assert.equal(read.get("lane"), null);
		assert.ok(container.textContent?.includes("original.pdf"));
		assert.ok(container.textContent?.includes("Read on this Mac"));
		await render("targeting");
		await render("resume");
		assert.ok(container.textContent?.includes("original.pdf"));
		assert.equal(calls.length, 1, "changing steps neither reads nor saves the PDF again");
		const api = await server.ssrLoadModule("/src/onboarding/api.ts");
		await api.writeProfile(profileProposal(EMPTY_DRAFT, false), file);
		const form = calls[1]?.body;
		assert.ok(form instanceof FormData);
		const attached = form.get("file");
		assert.ok(attached instanceof File);
		assert.equal(await attached.text(), "%PDF-original");
		await api.writeProfile(profileProposal(EMPTY_DRAFT, false));
		assert.equal(calls[2]?.body, JSON.stringify(profileProposal(EMPTY_DRAFT, false)), "JSON-only callers remain supported");
	} finally {
		flowRoot.unmount();
		container.remove();
		await server.close();
		globalThis.fetch = before;
	}
});
