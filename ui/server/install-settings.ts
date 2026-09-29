import { mkdir, rename, rm, writeFile } from "node:fs/promises";
import { existsSync, readFileSync } from "node:fs";
import { asMapping, at, parseJson } from "./onboarding/json.ts";
import { join } from "node:path";

import { applicationDataDirectory, systemContext, type LocationContext } from "./locations.ts";

export type DocumentRuntime = "claude" | "codex";

function settingsPath(context: LocationContext): string {
	return join(applicationDataDirectory(context), "settings.json");
}

export function documentRuntime(context: LocationContext = systemContext()): DocumentRuntime {
	if (!existsSync(settingsPath(context))) return "claude";
	const data = parseJson(readFileSync(settingsPath(context), "utf8"));
	const settings = asMapping(data);
	return settings !== null && at(settings, "runtime") === "codex" ? "codex" : "claude";
}

export async function saveDocumentRuntime(runtime: DocumentRuntime, context: LocationContext = systemContext()): Promise<void> {
	if (runtime !== "claude" && runtime !== "codex") throw new Error("Choose Claude or ChatGPT.");
	const directory = applicationDataDirectory(context);
	await mkdir(directory, { recursive: true, mode: 0o700 });
	const temporary = join(directory, `settings-${process.pid}-${Date.now()}.tmp`);
	try {
		await writeFile(temporary, JSON.stringify({ runtime }), { mode: 0o600, flag: "wx" });
		await rename(temporary, settingsPath(context));
	} finally {
		await rm(temporary, { force: true });
	}
}
