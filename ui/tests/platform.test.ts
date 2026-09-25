/**
 * The two platform rules the desktop build and `pnpm dev` depend on.
 *
 * Neither has been run on Windows — there is no Windows machine here — so what these assert
 * is the decision, taken from documented behaviour, rather than an observed outcome. Both
 * rules are one line each and both were previously buried inside scripts that do their work
 * at module scope, where nothing could reach them.
 *
 * What they are guarding, concretely:
 *
 * * Tauri looks for `binaries/node-<triple><suffix>`. `prepare-sidecar.ts` wrote the file
 *   with no suffix, so on Windows `tauri build` reported "binary not found" — and even if it
 *   had been found, `CreateProcess` cannot start an extension-less file, so the packaged app
 *   would have opened onto a dashboard with no API behind it.
 * * `tools/dev.ts` spawned `pnpm` with no shell. There is no extension-less `pnpm` on
 *   Windows, so the `app` child died at spawn and took the API down with it — which is the
 *   whole browser fallback, the path a Windows tester would actually be pointed at.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { executableSuffix, needsShell } from "../tools/platform.ts";

test("a sidecar needs an .exe on Windows and nothing anywhere else", () => {
	assert.equal(executableSuffix("win32"), ".exe");
	assert.equal(executableSuffix("darwin"), "");
	assert.equal(executableSuffix("linux"), "");
});

test("the sidecar filename Tauri looks for carries the suffix, not just the triple", () => {
	assert.equal(
		`node-x86_64-pc-windows-msvc${executableSuffix("win32")}`,
		"node-x86_64-pc-windows-msvc.exe",
	);
	assert.equal(`node-aarch64-apple-darwin${executableSuffix("darwin")}`, "node-aarch64-apple-darwin");
});

test("pnpm goes through a shell on Windows, because the PATH entry is pnpm.cmd", () => {
	assert.equal(needsShell("pnpm", "win32", "C:\\Program Files\\nodejs\\node.exe"), true);
});

test("an absolute node executable never does — it needs no PATHEXT lookup", () => {
	const node = "C:\\Program Files\\nodejs\\node.exe";
	assert.equal(needsShell(node, "win32", node), false);
});

test("nothing goes through a shell anywhere else, so the POSIX spawn is unchanged", () => {
	assert.equal(needsShell("pnpm", "linux", "/usr/bin/node"), false);
	assert.equal(needsShell("pnpm", "darwin", "/usr/local/bin/node"), false);
});
