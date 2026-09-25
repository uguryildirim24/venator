import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
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

// The account is scoped to this Install, including a portable VENATOR_HOME. No key bytes
// appear in a file, command argument, URL, log, or response. macOS security prompts for -w
// on stdin; passing a value after -w instead would expose it in the process list.
function account(context: LocationContext): string {
	return createHash("sha256").update(applicationDataDirectory(context)).digest("hex");
}

function security(args: string[], input?: string): Promise<{ code: number | null; output: string }> {
	return new Promise((resolve, reject) => {
		const child = spawn("/usr/bin/security", args, { stdio: ["pipe", "pipe", "ignore"] });
		let output = "";
		child.stdout.setEncoding("utf8");
		child.stdout.on("data", (chunk: string) => { output += chunk; });
		child.on("error", reject);
		child.on("close", (code) => resolve({ code, output }));
		child.stdin.end(input);
	});
}

const SERVICE = "org.venator.install.typesafe";

export async function saveJevKey(key: string, context: LocationContext = systemContext()): Promise<void> {
	if (context.platform !== "darwin") throw new Error("Keychain entry is available on macOS only.");
	if (!key.trim() || key.includes("\n") || key.includes("\r") || key.length > 4096) throw new Error("Enter one TypeSafe key.");
	const result = await security(["add-generic-password", "-U", "-a", account(context), "-s", SERVICE, "-w"], `${key}\n${key}\n`);
	if (result.code !== 0) throw new Error("The macOS Keychain did not save the key.");
}

export async function jevKey(context: LocationContext = systemContext()): Promise<string | null> {
	if (context.platform !== "darwin") return null;
	const result = await security(["find-generic-password", "-a", account(context), "-s", SERVICE, "-w"]);
	return result.code === 0 && result.output.trim() ? result.output.trim() : null;
}
