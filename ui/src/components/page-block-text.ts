import { createElement, Fragment } from "react";

import { segments, type MarkedBlock } from "../margin.ts";

/** One grid child for a list item, regardless of how many underlined runs it holds. */
export function PageBlockText({ block }: { readonly block: MarkedBlock }) {
	const text = segments(block).map((run, index) => run.tone === null
		? createElement(Fragment, { key: index }, run.text)
		: createElement("span", { key: index, className: "mark", "data-mark": run.tone }, run.text));
	return block.kind === "item" ? createElement("span", null, ...text) : createElement(Fragment, null, ...text);
}
