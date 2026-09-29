import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { createServer } from "vite";
import type { PostingDetail } from "../shared/contracts.ts";
import type { ApplicationControl } from "../src/application.ts";
import { installDom } from "../tools/dom-harness.ts";

const detail: PostingDetail = {
	posting: { key: "greenhouse:fixture:1", source: "greenhouse", board: "fixture", company: "Fixture", title: "Lab", location: "Boston", url: "https://example.test/job", postedAt: null, discoveredAt: "2026-09-26" },
	status: "unscored", descriptionHtml: "Fixture", page: [], decisions: [], trackEvents: [], application: null,
};
const settle = () => new Promise((resolve) => setTimeout(resolve, 50));

test("Apply on an unprepared Posting opens review, failure offers retry, Fill uses the reviewed version", async () => {
	installDom("http://localhost");
	const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), server: { middlewareMode: true }, appType: "custom", logLevel: "silent" });
	const { createRoot } = await import("react-dom/client");
	const container = document.createElement("div");
	document.body.append(container);
	const root = createRoot(container);
	try {
		const { PostingPage } = await server.ssrLoadModule("/src/components/posting-page.tsx");
		const { PostingActions } = await server.ssrLoadModule("/src/components/toolbar.tsx");
		const actions: string[] = [];
		let control: ApplicationControl = { manifest: { status: "ready", value: { prepared: false } }, prepared: false, busy: null, error: null, failedAction: null, reviewVersion: null, notice: null, warnings: [], run: (action) => { actions.push(action); } };
		const render = async () => { root.render(createElement("div", {}, createElement(PostingActions, { detail, control }), createElement(PostingPage, { detail, control, arriving: false }))); await settle(); };
		await render();
		const button = () => [...container.querySelectorAll<HTMLButtonElement>(".capsule")].find((item) => item.textContent === "Apply");
		assert.equal(button()?.disabled, false);
		button()?.click();
		assert.deepEqual(actions, ["apply"]);
		control = { ...control, busy: "apply" };
		await render();
		assert.ok(container.textContent?.includes("Applying…"));
		control = { ...control, busy: null, error: "Fixture refresh failed", failedAction: "apply" };
		await render();
		assert.ok(container.textContent?.includes("Fixture refresh failed"));
		[...container.querySelectorAll<HTMLButtonElement>("button")].find((item) => item.textContent === "Retry Apply")?.click();
		assert.deepEqual(actions, ["apply", "apply"]);
		control = { ...control, error: null, failedAction: null, busy: "apply" };
		await render();
		control = { ...control, busy: null, prepared: true, reviewVersion: "review2", manifest: { status: "ready", value: { prepared: true, version: "review1", resumeText: "Old résumé", letterText: "Old letter", draftProvenance: [] } } };
		await render();
		assert.equal(document.querySelector('[role="dialog"]'), null);
		control = { ...control, manifest: { status: "ready", value: { prepared: true, version: "review2", resumeText: "Résumé", letterText: "Letter", draftProvenance: [] } } };
		await render();
		assert.ok(document.querySelector('[role="dialog"]'));
		assert.ok(container.textContent?.includes("Fill"));
		[...container.querySelectorAll<HTMLButtonElement>("button")].find((item) => item.textContent === "Fill")?.click();
		assert.equal(actions.at(-1), "fill");
	} finally {
		root.unmount();
		container.remove();
		await server.close();
	}
});
