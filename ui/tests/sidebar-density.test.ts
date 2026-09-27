import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const tokens = readFileSync(resolve(import.meta.dirname, "../src/styles/tokens.css"), "utf8");
const windowCss = readFileSync(resolve(import.meta.dirname, "../src/styles/window.css"), "utf8");
const status = readFileSync(resolve(import.meta.dirname, "../src/runs/status.tsx"), "utf8");

function rule(selector: string): string {
	const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
	const match = windowCss.match(new RegExp(`${escaped} \\{([^}]+)\\}`));
	assert.ok(match, `missing ${selector}`);
	return match[1] ?? "";
}

test("Mail sidebar uses Medium rows on bare ground", () => {
	assert.match(tokens, /--sidebar-width: 224px;/);
	assert.match(tokens, /--sidebar-inset: 14px;/);
	assert.match(tokens, /--row-height: 32px;/);
	assert.match(tokens, /--sidebar-heading-height: 39px;/);
	assert.match(tokens, /--icon-size-large: 13px;/);
	assert.doesNotMatch(rule(".sidebar-card"), /background|padding|border-radius|gap/);
	assert.match(rule(".sidebar-row"), /font-size: var\(--size-body\)/);
	assert.match(rule(".sidebar-row::before"), /inset: 0 calc\(-1 \* var\(--space-1\)\)/);
	assert.match(rule(".sidebar-heading"), /font-size: var\(--size-subheadline\)/);
	assert.match(rule(".sidebar-count"), /color: var\(--label-tertiary\)/);
	assert.match(rule(".sidebar-symbol"), /width: var\(--space-6\)/);
	assert.match(rule(".sidebar-footer"), /gap: var\(--space-3\)/);
});

test("Jev it has no idle line or plan tooltip, but keeps the facts below the capsule", () => {
	assert.match(status, /idle=\{\[\]\}/);
	assert.doesNotMatch(status, /awaitingLabel|idleTitle|plan\?\.jev\?\.summary/);
	assert.match(status, /jevPauseLabel\(jevPause\.reason\)/);
	assert.match(status, /`Sorted \$\{formatMoment\(sorted\)\}`/);
	assert.match(status, /planError\.message/);
});

test("missing key or no startable plan leaves Jev it off without showing the key note", () => {
	assert.match(status, /const off = plan === null \|\| !plan.startable \|\| keyMissing !== null;/);
	assert.doesNotMatch(status, /keyMissing\.message/);
	assert.match(status, /jevPause\.reason !== "no-key" && keyMissing === null/);
});
