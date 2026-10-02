import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createElement, useState } from "react";
import { createServer } from "vite";
import type { PostingDetail } from "../shared/contracts.ts";
import { installDom } from "../tools/dom-harness.ts";

const settle = () => new Promise((resolve) => setTimeout(resolve, 100));

for (const source of ["greenhouse", "ashby", "workday", "lever"]) {
	for (const predrafted of [false, true]) {
		test(`one Apply opens ${source}, pre-drafted=${String(predrafted)}, never on render or edit`, async () => {
			installDom("http://localhost");
			const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), server: { middlewareMode: true }, appType: "custom", logLevel: "silent" });
			const { createRoot } = await import("react-dom/client");
			const container = document.createElement("div");
			document.body.append(container);
			const root = createRoot(container);
			const originalFetch = globalThis.fetch;
			const actions: string[] = [];
			let prepared = predrafted;
			let opened = false;
			globalThis.fetch = async (input, options) => {
				if (String(input).endsWith("/contacts")) return Response.json({ contacts: [] });
				if (options?.method === "POST") {
					const body: { action: string } = JSON.parse(String(options.body));
					actions.push(body.action);
					await settle();
					if (body.action === "apply") prepared = true;
					if (body.action === "fill") opened = true;
				}
				return Response.json({ prepared, version: "fixture", handoff: opened ? "ready" : null, savedAnswers: opened ? [{ id: "answer1" }, { id: "answer2" }] : [], resumeText: "Résumé", letterText: "Letter", draftProvenance: [] });
			};
			try {
				const { PostingPage } = await server.ssrLoadModule("/src/components/posting-page.tsx");
				const { PostingActions } = await server.ssrLoadModule("/src/components/toolbar.tsx");
				const { useApplicationControl } = await server.ssrLoadModule("/src/application.ts");
				const detail: PostingDetail = {
					posting: { key: `${source}:fixture:1`, source, board: "fixture", company: "Fixture", title: "Lab", location: "Boston", url: "https://example.test/job", postedAt: null, discoveredAt: "2026-09-26" },
					status: "queued", keepProbability: 0.8, keepScoreCarried: false, descriptionHtml: "Fixture", page: [], decisions: [], trackEvents: [], application: null,
				};
				function Harness() {
					const [reload, setReload] = useState(0);
					const control = useApplicationControl(detail.posting.key, reload, () => setReload((previous) => previous + 1));
					return createElement("div", {}, createElement(PostingActions, { detail, control }), createElement(PostingPage, { detail, control, arriving: false }), createElement("button", { onClick: () => control.run("edit", { version: "fixture", edits: [] }) }, "Fixture edit"));
				}
				root.render(createElement(Harness));
				await settle();
				assert.deepEqual(actions, []);
				const button = (text: string) => [...container.querySelectorAll<HTMLButtonElement>("button")].find((item) => item.textContent === text);
				button("Apply")?.click();
				await new Promise((resolve) => setTimeout(resolve, 40));
				assert.ok(container.textContent?.includes(predrafted ? "Opening the form…" : "Drafting…"));
				await settle();
				assert.ok(container.textContent?.includes("Opening the form…"));
				await settle();
				await settle();
				assert.deepEqual(actions, ["apply", "fill"]);
				assert.equal(document.querySelector('[role="dialog"]'), null);
				assert.ok(container.textContent?.includes(source === "lever" ? "Listing open. Documents ready." : "Filled. Press Submit on the employer's page."));
				assert.ok(container.textContent?.includes("Saved 2 answers"));
				button("Undo")?.click();
				await settle();
				await settle();
				assert.deepEqual(actions, ["apply", "fill", "retract", "retract"]);
				assert.ok(!container.textContent?.includes("Saved 2 answers"));
				button("Fixture edit")?.click();
				await settle();
				await settle();
				assert.deepEqual(actions, ["apply", "fill", "retract", "retract", "edit"]);
				button("Quick Look")?.click();
				await settle();
				assert.ok(document.querySelector('[role="dialog"]'));
				assert.equal(actions.filter((action) => action === "fill").length, 1);
			} finally {
				root.unmount();
				container.remove();
				globalThis.fetch = originalFetch;
				await server.close();
			}
		});
	}
}
