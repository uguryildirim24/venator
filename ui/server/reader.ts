/**
 * The reader page: an employer's description, reduced to headings, paragraphs and list items.
 *
 * This is the sanitiser `ui/DESIGN.md` asks for. It never builds a DOM and never returns
 * markup: it walks the tags only to learn where one block ends and the next begins, keeps the
 * text between them, and throws the tags away. Script, style and every other element whose
 * content is not prose are dropped with their content. What leaves here is plain text, and the
 * app renders it as React text, so no employer markup, style or script can reach the page.
 *
 * The original HTML is untouched and stays behind Read Full Description, in the sandboxed
 * frame, for anything this reading loses.
 */

import type { ReaderBlock } from "../shared/contracts.ts";

/** Elements whose content is not reading text. Dropped whole, content and all. */
const DROPPED = ["script", "style", "noscript", "template", "iframe", "object", "embed", "svg", "math", "head", "title", "select", "textarea", "button", "form"];

/** Elements that end the block before them and start a new one. */
const BLOCK = new Set([
	"p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "blockquote", "pre",
	"ul", "ol", "dl", "dt", "dd", "table", "thead", "tbody", "tr", "td", "th", "figure", "figcaption",
	"hr", "address", "center",
]);

const HEADINGS = new Set(["h1", "h2", "h3", "h4", "h5", "h6"]);
const EMPHASIS = new Set(["strong", "b"]);

const NAMED_ENTITIES = new Map<string, string>([
	["amp", "&"], ["lt", "<"], ["gt", ">"], ["quot", '"'], ["apos", "'"], ["nbsp", " "],
	["ndash", "–"], ["mdash", "—"], ["lsquo", "‘"], ["rsquo", "’"], ["ldquo", "“"], ["rdquo", "”"],
	["hellip", "…"], ["bull", "•"], ["middot", "·"], ["trade", "™"], ["reg", "®"], ["copy", "©"],
	["eacute", "é"], ["egrave", "è"], ["aacute", "á"], ["iacute", "í"], ["oacute", "ó"], ["uacute", "ú"],
	["ntilde", "ñ"], ["uuml", "ü"], ["ouml", "ö"], ["auml", "ä"], ["ccedil", "ç"], ["deg", "°"],
	["plusmn", "±"], ["times", "×"], ["zwnj", ""], ["zwj", ""], ["shy", ""], ["ensp", " "], ["emsp", " "],
	["thinsp", " "],
]);

/** Characters a line of plain text uses as a bullet when the employer typed the list by hand. */
const TYPED_BULLET = /^[•·●▪◦‣∙*–-]\s+/u;

function decodeEntities(text: string): string {
	return text.replaceAll(/&(#x[0-9a-f]+|#\d+|[a-z]+);/giu, (whole, name: string) => {
		if (name.startsWith("#x") || name.startsWith("#X")) {
			const code = Number.parseInt(name.slice(2), 16);
			return Number.isSafeInteger(code) && code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : "";
		}
		if (name.startsWith("#")) {
			const code = Number.parseInt(name.slice(1), 10);
			return Number.isSafeInteger(code) && code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : "";
		}
		return NAMED_ENTITIES.get(name.toLowerCase()) ?? whole;
	});
}

function collapse(text: string): string {
	return text.replaceAll(/\s+/gu, " ").trim();
}

function withoutDropped(html: string): string {
	let text = html.replaceAll(/<!--[\s\S]*?(?:-->|$)/gu, " ");
	for (const name of DROPPED) {
		text = text.replaceAll(new RegExp(`<${name}\\b[\\s\\S]*?(?:</${name}\\s*>|$)`, "giu"), " ");
	}
	return text;
}

type Draft = {
	kind: ReaderBlock["kind"];
	text: string;
	/** Characters of text that sat inside strong or b. */
	emphasised: number;
};

/**
 * A short paragraph that is only bold, or that ends in a colon, is a heading in all but tag.
 * Employers write "<p><strong>What you'll do</strong></p>" far more often than "<h3>".
 */
function promoted(draft: Draft, text: string): ReaderBlock["kind"] {
	if (draft.kind !== "paragraph" || text.length > 90) return draft.kind;
	const letters = text.replaceAll(/\s/gu, "").length;
	if (letters > 0 && draft.emphasised >= letters && !/[.!?]$/u.test(text)) return "heading";
	if (text.endsWith(":") && text.length <= 60) return "heading";
	return draft.kind;
}

/** Reads `html` into blocks. Empty input, or input with no text, reads as no blocks. */
export function readerBlocks(html: string): readonly ReaderBlock[] {
	const blocks: ReaderBlock[] = [];
	let draft: Draft = { kind: "paragraph", text: "", emphasised: 0 };
	let emphasis = 0;

	const flush = (next: ReaderBlock["kind"]) => {
		const text = collapse(draft.text);
		if (text !== "") {
			const typed = TYPED_BULLET.exec(text);
			if (typed !== null && draft.kind === "paragraph") {
				blocks.push({ kind: "item", text: text.slice(typed[0].length) });
			} else {
				const kind = promoted(draft, text);
				blocks.push({ kind, text: kind === "heading" && text.endsWith(":") ? text.slice(0, -1).trimEnd() : text });
			}
		}
		draft = { kind: next, text: "", emphasised: 0 };
	};

	const source = withoutDropped(html);
	const token = /<\s*(\/?)\s*([a-z][a-z0-9-]*)\b[^>]*>|([^<]+)|</giu;
	for (const match of source.matchAll(token)) {
		const [, closing, rawName, rawText] = match;
		if (rawText !== undefined || rawName === undefined) {
			const text = decodeEntities(rawText ?? "<");
			draft.text += text;
			if (emphasis > 0) draft.emphasised += text.replaceAll(/\s/gu, "").length;
			continue;
		}
		const name = rawName.toLowerCase();
		const open = closing === "";
		if (EMPHASIS.has(name)) {
			emphasis = Math.max(0, emphasis + (open ? 1 : -1));
			continue;
		}
		if (name === "br") {
			// A line break inside a paragraph is where hand-typed bullets and short lines split.
			flush(draft.kind === "heading" ? "paragraph" : draft.kind);
			continue;
		}
		if (HEADINGS.has(name)) {
			flush(open ? "heading" : "paragraph");
			continue;
		}
		if (name === "li") {
			flush(open ? "item" : "paragraph");
			continue;
		}
		if (BLOCK.has(name)) {
			// A block opened inside a list item continues that item rather than leaving the list.
			flush(draft.kind === "item" && open ? "item" : "paragraph");
		}
	}
	flush("paragraph");
	return blocks;
}
