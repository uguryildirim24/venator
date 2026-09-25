/**
 * Where this Install keeps things, on each platform, with and without an override.
 *
 * These are the tests that stop the shipped product assuming a checkout. Every case states a
 * machine rather than being one, so the macOS and Windows answers are checked from Linux.
 *
 * ## What "stating a machine" can and cannot check
 *
 * `server/locations.ts` joins with host-native `node:path`, and that is correct: `systemContext()`
 * always passes `process.platform`, and no caller anywhere passes a foreign one. So a case that
 * names `win32` while running on Linux is a configuration no Install ever has — the *branch* is
 * the Windows branch, and the *separator* is the host's.
 *
 * That distinction used to be invisible here, because every expectation was assembled with the
 * same `join` the function under test calls. An expectation built that way agrees with `join`
 * whatever `join` does, so this file passed on Linux with forward slashes throughout and asserted
 * precisely nothing about the shape a Windows Install actually gets. It was recorded as verified.
 * It was not.
 *
 * So the shape is stated instead of asked for: `SEPARATOR` and `ROOT` below are written down from
 * `process.platform`, `posix()` translates a POSIX-shaped literal into what this host writes, and
 * one case at the bottom pins the fully separated string per host as a literal with nothing
 * computed in it at all.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
	applicationDataDirectory,
	CHECKOUT_ROOT,
	profileSearchRoots,
	namedPythonInterpreter,
	pythonInterpreter,
	viewDatabaseCandidates,
	viewDatabaseWasConfigured,
	writableProfilesRoot,
	type LocationContext,
} from "../server/locations.ts";

/** True where `node:path` is `path.win32`. */
const WINDOWS = process.platform === "win32";

/** The separator this host's `node:path` writes. Stated, never asked of `join`. */
const SEPARATOR = WINDOWS ? "\\" : "/";

/**
 * What `resolve()` puts in front of a rooted path here: nothing on POSIX, and the current drive
 * on Windows, where a rooted path with no drive letter is not yet absolute. Read off the process
 * rather than hardcoded, because a runner's drive letter is not ours to choose — and it is an
 * environment fact rather than the behaviour under test.
 */
const ROOT = WINDOWS ? process.cwd().slice(0, 2) : "";

/** A POSIX-shaped literal written the way this host writes it: `/a/b` → `\a\b` on Windows. */
function posix(path: string): string {
	return path.replaceAll("/", SEPARATOR);
}

/** The same, for a path `resolve()` produced, which on Windows also carries the drive. */
function resolved(path: string): string {
	return ROOT + posix(path);
}

function machine(
	platform: NodeJS.Platform,
	home: string,
	environment: ReadonlyMap<string, string>,
	workingDirectory = "/work",
	checkoutRoot: string | null = CHECKOUT_ROOT,
): LocationContext {
	return {
		platform,
		home,
		workingDirectory,
		checkoutRoot,
		environment: (name) => environment.get(name),
	};
}

const nothing: ReadonlyMap<string, string> = new Map();

test("macOS keeps an Install under Application Support", () => {
	const context = machine("darwin", "/mock-macos-home/someone", nothing);
	assert.equal(applicationDataDirectory(context), posix("/mock-macos-home/someone/Library/Application Support/Venator"));
});

test("Linux follows XDG_DATA_HOME when it is set", () => {
	const context = machine("linux", "/home/someone", new Map([["XDG_DATA_HOME", "/home/someone/.data"]]));
	assert.equal(applicationDataDirectory(context), posix("/home/someone/.data/venator"));
});

test("Linux falls back to ~/.local/share when XDG_DATA_HOME is unset or blank", () => {
	assert.equal(
		applicationDataDirectory(machine("linux", "/home/someone", nothing)),
		posix("/home/someone/.local/share/venator"),
	);
	assert.equal(
		applicationDataDirectory(machine("linux", "/home/someone", new Map([["XDG_DATA_HOME", "   "]]))),
		posix("/home/someone/.local/share/venator"),
	);
});

test("Windows follows APPDATA, and has an answer without it", () => {
	// A Windows-shaped %APPDATA%, because that is the only kind there is. The separator between it
	// and Venator is this host's, and it is written out rather than obtained from `join`.
	const roaming = "C:\\Users\\someone\\AppData\\Roaming";
	assert.equal(
		applicationDataDirectory(machine("win32", "C:\\Users\\someone", new Map([["APPDATA", roaming]]))),
		`${roaming}${SEPARATOR}Venator`,
	);
	assert.equal(
		applicationDataDirectory(machine("win32", "C:\\Users\\someone", nothing)),
		["C:\\Users\\someone", "AppData", "Roaming", "Venator"].join(SEPARATOR),
	);
});

test("the Windows answer is the shape a Windows Install actually gets", () => {
	// The one case with nothing computed in it. `node:path` answers for the host, not for the
	// platform a case names, so on Linux the Windows branch joins with a forward slash — a
	// configuration no Install has, since `systemContext()` only ever passes `process.platform`.
	// Both literals are spelled out so that on a Windows runner this asserts the real backslashed
	// path, and on a Linux runner it asserts the only other thing the code can produce.
	const context = machine(
		"win32",
		"C:\\Users\\someone",
		new Map([["APPDATA", "C:\\Users\\someone\\AppData\\Roaming"]]),
	);
	assert.equal(
		applicationDataDirectory(context),
		WINDOWS
			? "C:\\Users\\someone\\AppData\\Roaming\\Venator"
			: "C:\\Users\\someone\\AppData\\Roaming/Venator",
	);
});

test("VENATOR_HOME overrides the platform answer everywhere", () => {
	for (const platform of ["darwin", "linux", "win32"] as const) {
		const context = machine(platform, "/home/someone", new Map([["VENATOR_HOME", "/srv/venator"]]));
		assert.equal(applicationDataDirectory(context), resolved("/srv/venator"));
	}
});

test("a relative VENATOR_HOME resolves against the working directory, never against nothing", () => {
	const context = machine("linux", "/home/someone", new Map([["VENATOR_HOME", "state"]]), "/work");
	assert.equal(applicationDataDirectory(context), resolved("/work/state"));
});

test("Profiles are searched Install-first", () => {
	const context = machine("linux", "/home/someone", new Map([["VENATOR_HOME", "/srv/venator"]]), "/work");
	const roots = profileSearchRoots(context);
	assert.deepEqual(roots, [
		resolved("/srv/venator/profiles"),
		resolved("/work/profiles"),
		`${CHECKOUT_ROOT}${SEPARATOR}profiles`,
	]);
});

test("a repeated root is listed once", () => {
	const context = machine("linux", "/home/someone", new Map([["VENATOR_HOME", CHECKOUT_ROOT]]), CHECKOUT_ROOT);
	assert.deepEqual(profileSearchRoots(context), [`${CHECKOUT_ROOT}${SEPARATOR}profiles`]);
});

test("onboarding only ever writes inside the application data directory", () => {
	const context = machine("linux", "/home/someone", new Map([["VENATOR_HOME", "/srv/venator"]]));
	assert.equal(writableProfilesRoot(context), resolved("/srv/venator/profiles"));
	// Never the checkout, whatever the working directory says.
	assert.notEqual(writableProfilesRoot(context), `${CHECKOUT_ROOT}${SEPARATOR}profiles`);
});

test("the view database never falls back to checkout data", () => {
	const context = machine("linux", "/home/someone", new Map([["VENATOR_HOME", "/srv/venator"]]));
	assert.deepEqual(viewDatabaseCandidates(context), [resolved("/srv/venator/build/venator.db")]);
	assert.equal(viewDatabaseWasConfigured(context), false);
});

test("VENATOR_VIEW_DB still names the database outright", () => {
	const context = machine("linux", "/home/someone", new Map([["VENATOR_VIEW_DB", "/tmp/one.db"]]));
	assert.deepEqual(viewDatabaseCandidates(context), [resolved("/tmp/one.db")]);
	assert.equal(viewDatabaseWasConfigured(context), true);
});

test("a shipped Install with no clone anywhere still resolves every location", () => {
	const context = machine("linux", "/home/someone", new Map([["VENATOR_HOME", "/srv/venator"]]), "/", null);
	assert.deepEqual(profileSearchRoots(context), [resolved("/srv/venator/profiles"), resolved("/profiles")]);
	assert.deepEqual(viewDatabaseCandidates(context), [resolved("/srv/venator/build/venator.db")]);
	assert.equal(writableProfilesRoot(context), resolved("/srv/venator/profiles"));
	// `python3`, on every host: unlike a path separator, the interpreter name is decided by the
	// platform this case *states* — `pythonInterpreter` reads `context.platform`, not the host —
	// so a Linux machine gets the Linux answer wherever this runs.
	assert.equal(pythonInterpreter(context), "python3");
});

test("VENATOR_PYTHON names the interpreter the probe runs under", () => {
	const context = machine("linux", "/home/someone", new Map([["VENATOR_PYTHON", "/opt/py/bin/python"]]));
	assert.equal(pythonInterpreter(context), resolved("/opt/py/bin/python"));
	assert.equal(namedPythonInterpreter(context), resolved("/opt/py/bin/python"));
});

/**
 * The desktop bundle's own interpreter, named by the Tauri host that started this server.
 *
 * A Windows Install with no system Python is the machine the whole staging story is for: the
 * bundle carries an embeddable CPython, the wheel set and `src/venator` under
 * `resources/python/<triple>/`, and before this leg existed the server resolved past all of it
 * to a bare `python` that machine does not have.
 *
 * `checkoutRoot` is null here so nothing on the filesystem can decide the answer — this is the
 * shipped Install, which has no clone anywhere.
 */
test("the interpreter the bundle shipped with is used when nobody named one", () => {
	const bundled = "/opt/Venator/resources/python/x86_64-pc-windows-msvc/python.exe";
	const context = machine("linux", "/home/someone", new Map([["VENATOR_BUNDLED_PYTHON", bundled]]), "/work", null);
	assert.equal(pythonInterpreter(context), resolved(bundled));
	assert.equal(namedPythonInterpreter(context), resolved(bundled));
});

/**
 * The bundle's value is resolved against the working directory, exactly as `$VENATOR_PYTHON`
 * and `$VENATOR_HOME` are.
 *
 * A relative value rather than an absolute one because it is the only input that separates the
 * two on *every* host: an absolute POSIX path is left alone here and rewritten with a drive
 * letter on Windows, so a case built on one asserts the resolution only on the Windows leg. The
 * host writes an absolute path, and this is what says the server does not depend on that.
 */
test("a relative bundled interpreter resolves against the working directory", () => {
	const context = machine("linux", "/home/someone", new Map([["VENATOR_BUNDLED_PYTHON", "runtime/python"]]), "/work", null);
	assert.equal(pythonInterpreter(context), resolved("/work/runtime/python"));
});

/**
 * The precedence the wiring is not allowed to cost anybody. The host announces what it shipped
 * with under a name of its own precisely so that an Owner who named an interpreter keeps it.
 */
test("an interpreter somebody named deliberately still outranks the bundle's own", () => {
	const context = machine(
		"linux",
		"/home/someone",
		new Map([
			["VENATOR_PYTHON", "/opt/py/bin/python"],
			["VENATOR_BUNDLED_PYTHON", "/opt/Venator/resources/python/x86_64-pc-windows-msvc/python.exe"],
		]),
		"/work",
		null,
	);
	assert.equal(pythonInterpreter(context), resolved("/opt/py/bin/python"));
});

/**
 * Every development build and every macOS bundle carries no runtime, so the host names none —
 * and the answer has to be exactly the answer it was before this variable existed. A blank
 * value is the same absence: `configured` treats whitespace as unset on every other variable
 * here, and a host that wrote an empty string would otherwise hand the probe `/work`.
 */
test("a bundle with no interpreter in it changes nothing", () => {
	const absent = machine("linux", "/home/someone", nothing, "/work", null);
	const blank = machine("linux", "/home/someone", new Map([["VENATOR_BUNDLED_PYTHON", "   "]]), "/work", null);
	assert.equal(pythonInterpreter(absent), "python3");
	assert.equal(pythonInterpreter(blank), "python3");
	assert.equal(namedPythonInterpreter(absent), null);
	assert.equal(namedPythonInterpreter(blank), null);
	// The Windows answer, on the platform this case states rather than on the host running it.
	assert.equal(pythonInterpreter(machine("win32", "C:\\Users\\someone", nothing, "/work", null)), "python");
});
