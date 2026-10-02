import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { createServer } from "vite";
import type { PostingDetail } from "../shared/contracts.ts";
import type { ApplicationControl } from "../src/application.ts";
import { buildMargin } from "../src/margin.ts";
import { installDom } from "../tools/dom-harness.ts";

const detail: PostingDetail = {
	posting: { key: "quest:lab", source: "other", board: "quest", company: "Quest", title: "Lab", location: "Boston", url: "https://example.org/jobs", postedAt: null, discoveredAt: "2026-09-26" },
	status: "unscored", keepProbability: null, keepScoreCarried: false, descriptionHtml: "<p>Original</p>",
	page: [
		{ kind: "heading", text: "Overview" },
		{ kind: "paragraph", text: "Work in the lab." },
		{ kind: "heading", text: "Requirements" },
		{ kind: "item", text: "Bachelor's degree" },
		{ kind: "item", text: "MLS certification" },
		{ kind: "item", text: "Standing and lifting 25 lb" },
		{ kind: "item", text: "Specimen handling" },
	],
	decisions: [], trackEvents: [], application: null,
	assessment: {
		status: "needs_review", summary: "", assessedBy: "deterministic", assessedAsOf: null,
		evidence: [{ requirement: "Specimen handling", candidateEvidence: "Lab Technician (Feb – May 2026)", source: "profile.resume.experience[0].role" }],
		conflicts: ["Qualification not established: Bachelor's degree", "Qualification not established: MLS certification"],
		unknowns: ["No confirmed resume evidence for physical demands: Standing and lifting 25 lb"],
		requirementsRead: 4, listingStatus: "open", lastVerifiedAt: null, descriptionKind: "full", applyUrl: "", opportunityType: "",
	},
};

const control: ApplicationControl = {
	manifest: { status: "loading" }, prepared: false, busy: null, error: null, failedAction: null, notice: null, warnings: [], run: () => {},
};
const settle = () => new Promise((resolve) => setTimeout(resolve, 40));

test("ledger groups each status once and anchored notes use one line without dates", () => {
	const margin = buildMargin({ page: detail.page, decisions: [], assessment: detail.assessment });
	assert.deepEqual(margin.fit?.groups.map((group) => [group.title, group.clauses.length, group.at]), [
		["Required · not on your résumé", 2, 3], ["Not on your résumé", 1, 5], ["On your résumé", 1, 6],
	]);
	assert.deepEqual(margin.blocks.slice(3).map((block) => block.notes.map((note) => [note.title, note.subtitle])), [
		[["Required · not on your résumé", null]], [["Required · not on your résumé", null]],
		[["Not on your résumé", null]], [["Lab Technician", null]],
	]);
});

test("the collapsed reader is eight lines with a fade to the page ground", () => {
	const styles = readFileSync(new URL("../src/styles/window.css", import.meta.url), "utf8");
	assert.match(styles, /height: calc\(8 \* var\(--leading-page-text\)\)/u);
	assert.match(styles, /background: linear-gradient\(to bottom, transparent, var\(--window\)\)/u);
});

test("eight-line faded fold, ledger jump, Read Less and keyed switch", async () => {
	installDom("http://localhost");
	let scrolledTo: string | null = null;
	HTMLElement.prototype.scrollIntoView = function () { scrolledTo = this.id; };
	const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), server: { middlewareMode: true }, appType: "custom", logLevel: "silent" });
	const { createRoot } = await import("react-dom/client");
	const container = document.createElement("div");
	document.body.append(container);
	const root = createRoot(container);
	try {
		const { PostingPage } = await server.ssrLoadModule("/src/components/posting-page.tsx");
		const render = async (key: string) => { root.render(createElement(PostingPage, { key, detail: { ...detail, posting: { ...detail.posting, key } }, control, arriving: false })); await settle(); };
		await render("first");
		const fold = () => container.querySelector<HTMLElement>(".description-blocks");
		assert.equal(fold()?.dataset.expanded, "false");
		assert.ok(container.querySelector('.description-blocks:not([data-expanded="true"])'));
		assert.equal(container.querySelectorAll(".fit-group").length, 3);
		assert.ok(container.textContent?.includes("Read More"));
		assert.ok(!container.textContent?.includes("Read Full Description"));
		assert.ok(!container.textContent?.includes("Requirements ↓"));
		container.querySelector<HTMLButtonElement>(".fit-group")?.click();
		await settle();
		assert.equal(fold()?.dataset.expanded, "true");
		assert.ok(container.textContent?.includes("Read Less"));
		assert.equal(scrolledTo, "posting-line-3");
		await render("second");
		assert.equal(fold()?.dataset.expanded, "false");
		container.querySelector<HTMLButtonElement>(".page-foot button")?.click();
		await settle();
		assert.equal(fold()?.dataset.expanded, "true");
		container.querySelector<HTMLButtonElement>(".page-foot button")?.click();
		await settle();
		assert.equal(fold()?.dataset.expanded, "false");
	} finally {
		root.unmount();
		container.remove();
		await server.close();
	}
});
