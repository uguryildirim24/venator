/**
 * Which host the app thinks it is in, and what it puts on its outermost element because of it.
 *
 * The bug this pins: `isDesktop` was once the whole signal, so every Tauri host got the
 * treatment that only makes sense on a Mac — a toolbar gap kept clear for traffic lights drawn
 * over it. On Windows and Linux there are no lights, so the app padded a gap in front of
 * nothing.
 *
 * `platformFromUserAgent` is a pure mapping and is exercised over every host below. The
 * module-level constants are read once at import, from whatever window is installed, so this
 * file states one host — a desktop on Windows, the machine the bug is about — before it loads
 * the module and then asserts what that host is given.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

/** Real user agents: WKWebView, WebView2 and WebKitGTK, which are the three desktop webviews. */
const MACOS =
	"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15";
const WINDOWS =
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0";
const LINUX =
	"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15";

/*
 * Both of these exist on the Node global as getters, so they are defined rather than assigned
 * — the same reason `tools/dom-harness.ts` gives for doing it that way. This has to happen
 * before the import below: the module reads the host once, at load, exactly as it does in a
 * browser where the host cannot change under a running page.
 */
Object.defineProperty(globalThis, "window", { configurable: true, value: { isTauri: true } });
Object.defineProperty(globalThis, "navigator", { configurable: true, value: { userAgent: WINDOWS } });

const { desktopPlatform, isDesktop, nativeClasses, platformFromUserAgent, reservesTrafficLights } =
	await import("../src/platform.ts");

test("each desktop webview is read as the operating system it is running on", () => {
	assert.equal(platformFromUserAgent(MACOS), "macos");
	assert.equal(platformFromUserAgent(WINDOWS), "windows");
	assert.equal(platformFromUserAgent(LINUX), "linux");
});

test("a user agent this app cannot place is unknown, never the nearest guess", () => {
	assert.equal(platformFromUserAgent(""), "unknown");
	assert.equal(platformFromUserAgent("Venator/1.0"), "unknown");
});

test("the desktop host on Windows is native, is not a Mac, and reserves no traffic lights", () => {
	assert.equal(isDesktop, true);
	assert.equal(desktopPlatform, "windows");
	assert.equal(nativeClasses, "app-native");
	assert.equal(reservesTrafficLights, false);
});
