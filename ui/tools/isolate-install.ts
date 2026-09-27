/* Test/smoke bootstrap: no process or child may inherit a person's Install. */
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { isAbsolute, join, relative, resolve, sep } from "node:path";

import { applicationDataDirectory, defaultViewDatabase, systemContext } from "../server/locations.ts";

const inherited = { ...process.env };
const home = inherited["HOME"] ?? inherited["USERPROFILE"] ?? homedir();
const original = systemContext();
const platforms: readonly NodeJS.Platform[] = ["darwin", "linux", "win32"];
const protectedRoots = platforms.map((platform) =>
	applicationDataDirectory({
		...original,
		platform,
		home,
		environment: (name) => name === "VENATOR_HOME" ? undefined : inherited[name],
	}),
);
if (inherited["VENATOR_HOME"]?.trim()) protectedRoots.push(resolve(inherited["VENATOR_HOME"]));

const scratch = mkdtempSync(join(tmpdir(), "venator-ui-install-"));
process.env["VENATOR_HOME"] = join(scratch, "Install");
process.env["HOME"] = join(scratch, "home");
process.env["USERPROFILE"] = join(scratch, "home");
process.env["XDG_DATA_HOME"] = join(scratch, "xdg");
process.env["APPDATA"] = join(scratch, "appdata");
delete process.env["VENATOR_VIEW_DB"];

function assertIsolated(path: string): void {
	for (const root of protectedRoots) {
		const part = relative(root, path);
		assert.ok(part === ".." || part.startsWith(`..${sep}`) || isAbsolute(part),
			`a test resolved the real Install: ${path}`);
	}
}

// Check the actual resolver, including the platform fallback when VENATOR_HOME is absent.
assertIsolated(applicationDataDirectory());
assertIsolated(defaultViewDatabase());
for (const platform of platforms) {
	const context = {
		...systemContext(),
		platform,
		environment: (name: string) => name === "VENATOR_HOME" ? undefined : process.env[name],
	};
	assertIsolated(applicationDataDirectory(context));
}
process.on("exit", () => rmSync(scratch, { recursive: true, force: true }));
