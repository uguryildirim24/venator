import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import type { PostingDetail } from "../shared/contracts.ts";
import { PageBlockText } from "../src/components/page-block-text.ts";
import { buildMargin, segments, type MarkedBlock } from "../src/margin.ts";

const sentence = "Proficiency in Microsoft Office Application (Word, Excel, Outlook)";

const detail: PostingDetail = {
	posting: {
		key: "quest:office", source: "greenhouse", board: "quest", company: "Quest Diagnostics",
		title: "Technician", location: null, url: "https://example.com/job", postedAt: null, discoveredAt: "2026-09-26",
	},
	status: "hard-killed", keepProbability: null, keepScoreCarried: false,
	descriptionHtml: `<li>${sentence}</li>`,
	page: [{ kind: "item", text: sentence }],
	decisions: [], trackEvents: [], application: null,
	assessment: {
		status: "suitable", summary: "", assessedBy: "deterministic", assessedAsOf: null,
		evidence: [{ requirement: "Microsoft Office Application (Word, Excel, Outlook)", candidateEvidence: "", source: "resume" }],
		conflicts: [], unknowns: [], listingStatus: "open", lastVerifiedAt: null,
		descriptionKind: "full", applyUrl: "", opportunityType: "",
	},
};
test("a marked list item keeps the Quest Office sentence in one grid cell, in source order", () => {
	const block = buildMargin({ page: detail.page, decisions: [], assessment: detail.assessment }).blocks[0];
	assert.ok(block);
	assert.equal(block.marks.length, 1);
	assert.equal(segments(block).map((run) => run.text).join(""), sentence);
	const html = renderToStaticMarkup(createElement(PageBlockText, { block }));
	assert.match(html, /^<span>Proficiency in <span class="mark" data-mark="green">Microsoft Office Application \(Word, Excel, Outlook\)<\/span><\/span>$/u);
});

test("overlapping and touching marks never duplicate text or split adjacent underlines", () => {
	const block: MarkedBlock = {
		kind: "item", text: sentence, count: null, notes: [],
		marks: [
			{ start: 15, end: 40, tone: "green" },
			{ start: 35, end: 52, tone: "green" },
			{ start: 52, end: sentence.length, tone: "green" },
		],
	};
	const runs = segments(block);
	assert.equal(runs.map((run) => run.text).join(""), sentence);
	assert.equal(runs.filter((run) => run.tone === "green").length, 1);
	assert.equal(runs[1]?.text, sentence.slice(15));

	const differentTones = segments({ ...block, marks: [
		{ start: 35, end: sentence.length, tone: "ember" },
		{ start: 15, end: 40, tone: "green" },
	] });
	assert.equal(differentTones.map((run) => run.text).join(""), sentence);
	assert.equal(differentTones[1]?.text, sentence.slice(15, 40));
});
