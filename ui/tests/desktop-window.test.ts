/**
 * The desktop window's configuration, and the one thing that may differ between platforms.
 *
 * The window is opaque everywhere: the app draws its own grounds on every platform, and light
 * and dark follow the system. What macOS adds is the frame — `titleBarStyle: "Overlay"` and
 * `hiddenTitle` put the traffic lights over the top of the sidebar, with no title bar above
 * the window. Both keys are macOS-only (the schema pinned in this repo says so in as many
 * words), so they live in `tauri.macos.conf.json`, which Tauri merges over the base
 * configuration when it builds for macOS and reads on no other platform.
 *
 * **The merge replaces the window array rather than merging into it**, so the override has to
 * restate the whole window — and that is the trap this file exists to catch: a width changed
 * in the base config and not in the override would silently give macOS a different window
 * from every other platform, and nothing that compiles or renders would notice. The assertion
 * below is therefore "the override is the base window and exactly two added keys".
 *
 * Nothing here compiles Rust or opens a window. What it pins is that the two files agree.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

function config(name: string) {
	return JSON.parse(readFileSync(fileURLToPath(new URL(`../src-tauri/${name}`, import.meta.url)), "utf8"));
}

/** The two keys that are macOS's alone: the traffic lights over the sidebar, and no title. */
const MACOS_ONLY = { titleBarStyle: "Overlay", hiddenTitle: true };

test("the base window asks for nothing macOS-only, and no window is transparent", () => {
	const window = config("tauri.conf.json").app.windows[0];
	assert.equal(window.transparent, undefined);
	assert.equal(config("tauri.macos.conf.json").app.windows[0].transparent, undefined);
	for (const key of Object.keys(MACOS_ONLY)) {
		assert.equal(window[key], undefined, `${key} is macOS-only and must not be in the base configuration`);
	}
});

test("the macOS override is the base window and exactly the two macOS-only keys", () => {
	const base = config("tauri.conf.json").app.windows[0];
	const macos = config("tauri.macos.conf.json").app.windows[0];
	assert.deepEqual(macos, { ...base, ...MACOS_ONLY });
});

test("both windows are labelled main, which is what the host looks the window up by", () => {
	// `lib.rs` calls `get_webview_window("main")` to show it once the API answers; an
	// override that replaced the array without the label would leave both reaching for nothing.
	assert.equal(config("tauri.conf.json").app.windows[0].label, "main");
	assert.equal(config("tauri.macos.conf.json").app.windows[0].label, "main");
});

test("the macOS override deviates in the window and in nothing else", () => {
	const macos = config("tauri.macos.conf.json");
	assert.deepEqual(Object.keys(macos).filter((key) => key !== "$schema"), ["app"]);
	assert.deepEqual(Object.keys(macos.app), ["windows"]);
	assert.equal(macos.app.windows.length, 1);
});
