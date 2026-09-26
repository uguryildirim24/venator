import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

import { jevRunEnvironment, nonJevInheritedEnvironment, runEnvironment } from "../server/runs/environment.ts";
import type { LocationContext } from "../server/locations.ts";
import { removeBytecode } from "../tools/python-staging.ts";

const ROOT = resolve(import.meta.dirname, "../..");
const server = resolve(ROOT, "ui/server");
const read = (path: string): string => readFileSync(resolve(ROOT, path), "utf8");

const context: LocationContext = {
	platform: "darwin",
	home: "/tmp",
	workingDirectory: "/tmp",
	checkoutRoot: null,
	environment: (name) => name === "PYTHONDONTWRITEBYTECODE" ? "0" : undefined,
};

test("every Python child environment disables bytecode even when inherited as disabled", () => {
	assert.equal(runEnvironment(context).PYTHONDONTWRITEBYTECODE, "1");
	assert.equal(jevRunEnvironment(context).PYTHONDONTWRITEBYTECODE, "1");
	assert.equal(nonJevInheritedEnvironment(context).PYTHONDONTWRITEBYTECODE, "1");
});

// These are all server launches of Python: Refresh/Hard Filters/View, Jev it, view recovery,
// planning, applications/handoff, setup/load-back, board resolution and résumé adapters.
// Keep the list exhaustive: adding a new spawn path requires choosing its environment here.
const PYTHON_SITES = {
	"applications/routes.ts": ["env: environment"],
	"onboarding/boards.ts": ["env: nonJevInheritedEnvironment(context)"],
	"onboarding/resume-pdf.ts": ["env: nonJevInheritedEnvironment(context)"],
	"onboarding/resume-reference.ts": ["env: nonJevInheritedEnvironment(context)"],
	"onboarding/runtime.ts": ["env: nonJevInheritedEnvironment(context)"],
	"onboarding/verify.ts": ["env: nonJevInheritedEnvironment(context)"],
	"runs/plan.ts": ["env: runEnvironment(context)"],
	"runs/runner.ts": ["env: runEnvironment(context)", "env: environment"],
} satisfies Record<string, readonly string[]>;

function typescriptFiles(folder: string): string[] {
	return readdirSync(folder, { withFileTypes: true }).flatMap((entry) => {
		const path = join(folder, entry.name);
		return entry.isDirectory() ? typescriptFiles(path) : entry.name.endsWith(".ts") ? [path] : [];
	});
}

test("each server spawn of Python uses a no-bytecode child environment", () => {
	const found: string[] = [];
	for (const file of typescriptFiles(server)) {
		const source = readFileSync(file, "utf8");
		const relative = file.slice(server.length + 1);
		const launches = [...source.matchAll(/pythonInterpreter\(context\)/gu)];
		if (launches.length === 0) continue;
		found.push(relative);
		const expected = Object.entries(PYTHON_SITES).find(([name]) => name === relative)?.[1];
		assert.ok(expected, `new Python spawn path: ${relative}`);
		assert.equal(launches.length, expected.length, `${relative}: Python launch count changed`);
		for (const [index, match] of launches.entries()) {
			const options = source.slice(match.index, match.index + 320);
			assert.ok(options.includes(expected[index] ?? "<missing>"), `${relative}: Python launch ${index + 1} must use its no-bytecode environment`);
		}
	}
	assert.deepEqual(found.sort(), Object.keys(PYTHON_SITES).sort());
	const runner = read("ui/server/runs/runner.ts");
	assert.match(runner, /let environment = runEnvironment\(context\)/u);
	assert.match(runner, /environment = \{ \.\.\.jevRunEnvironment\(context\)/u);
});

test("staging discards archived and wheel bytecode, keeping source files", () => {
	const root = mkdtempSync(join(tmpdir(), "venator-staged-python-"));
	try {
		mkdirSync(join(root, "lib", "__pycache__"), { recursive: true });
		writeFileSync(join(root, "lib", "__pycache__", "module.pyc"), "cached");
		writeFileSync(join(root, "lib", "stray.pyc"), "cached");
		writeFileSync(join(root, "lib", "module.py"), "source");
		removeBytecode(root);
		assert.deepEqual(readdirSync(join(root, "lib")), ["module.py"]);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("the desktop host and release import checks never create bytecode in the bundle", () => {
	assert.match(read("ui/src-tauri/src/lib.rs"), /command\.arg\(server\)[\s\S]*?\.env\("PYTHONDONTWRITEBYTECODE", "1"\)/u);
	const staging = read("ui/tools/prepare-python.ts");
	assert.match(staging, /removeBytecode\(scratch\);\s*const interpreter/u);
	assert.match(staging, /writePathConfiguration\(scratch, target, say\);\s*removeBytecode\(scratch\)/u);
	assert.match(staging, /execFileSync\(interpreter, \["-B", "-c"/u);
	const staged = read(".github/scripts/run_staged_interpreter.ps1");
	assert.match(staged, /& \$pythonFull -B \$program/u);
	assert.match(staged, /& \$pythonFull -B -m venator\.llm\.probe/u);
});
