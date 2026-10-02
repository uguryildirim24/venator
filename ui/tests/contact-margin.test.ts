import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { createServer } from "vite";
import type { PostingDetail } from "../shared/contracts.ts";
import type { ApplicationControl } from "../src/application.ts";
import type { Contact } from "../src/api.ts";
import { installDom } from "../tools/dom-harness.ts";

const detail: PostingDetail = {
	posting: { key: "greenhouse:example:1", source: "greenhouse", board: "example", company: "Example Lab", title: "Lab", location: "Boston", url: "https://example.test/job", postedAt: null, discoveredAt: "2026-10-01" },
	status: "queued", keepProbability: 0.8, keepScoreCarried: false, descriptionHtml: "Fixture", page: [], decisions: [], trackEvents: [], application: null,
};

test("contacts in the Posting margin show people, routes and an undoable date; no match renders nothing", async (context) => {
	installDom("http://localhost");
	const contact: Contact = { id: "a".repeat(32), person: "Alex Example", title: "Research lead", conversationAngle: "Ask about assay development", contactRoute: "alex@example.test", href: "mailto:alex@example.test", reachedOutAt: null };
	let contacts = [contact, { ...contact, id: "b".repeat(32), person: "Jamie Example" }];
	context.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({ contacts }), { headers: { "Content-Type": "application/json" } }));
	const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), server: { middlewareMode: true }, appType: "custom", logLevel: "silent" });
	const { createRoot } = await import("react-dom/client");
	const container = document.createElement("div");
	document.body.append(container);
	const root = createRoot(container);
	try {
		const { ContactMargin } = await server.ssrLoadModule("/src/components/contact-margin.tsx");
		const actions: { action: string; id: string | undefined }[] = [];
		const control: ApplicationControl = { manifest: { status: "ready", value: {} }, prepared: false, busy: null, error: null, failedAction: null, notice: null, warnings: [], run: (action, options) => { actions.push({ action, id: options?.id }); } };
		const render = async (count: number) => {
			root.render(createElement(ContactMargin, { detail: { ...detail, trackEvents: Array.from({ length: count }, (_, id) => ({ id, event: "outreach", actor: "owner", detail: "Alex Example", at: "2026-10-01" })) }, control }));
			await new Promise((resolve) => setTimeout(resolve, 100));
		};
		await render(0);
		assert.ok(container.textContent?.includes("Alex Example"));
		assert.ok(container.textContent?.includes("Jamie Example"));
		assert.ok(container.textContent?.includes("Research lead"));
		assert.ok(container.textContent?.includes("Ask about assay development"));
		assert.equal(container.querySelector("a")?.getAttribute("href"), "mailto:alex@example.test");
		container.querySelector<HTMLButtonElement>("button")?.click();
		assert.deepEqual(actions, [{ action: "outreach", id: contact.id }]);
		contacts = [{ ...contact, reachedOutAt: "2026-10-01T12:00:00Z" }];
		await render(1);
		assert.match(container.textContent ?? "", /Reached out .*Oct/);
		container.querySelector<HTMLButtonElement>("button")?.click();
		assert.equal(actions.at(-1)?.action, "outreach_undo");
		contacts = [];
		await render(2);
		assert.equal(container.textContent, "");
	} finally { root.unmount(); container.remove(); await server.close(); }
});
