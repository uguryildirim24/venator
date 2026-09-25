import { spawn, type ChildProcess } from "node:child_process";
import { Hono } from "hono";
import { cors } from "hono/cors";
import { checkLocalAction, LOCAL_ACTION_ORIGINS } from "../http/local-action-guard.ts";
import { pipelineWorkingDirectory, pythonInterpreter, systemContext } from "../locations.ts";
import { asBoolean, asMapping, asText, parseJson, type JsonValue } from "../onboarding/json.ts";
import { readOnboardingState } from "../onboarding/state.ts";
import { runEnvironment, TYPESAFE_API_KEY_ENVIRONMENT } from "../runs/environment.ts";
import { currentRun, signalTree } from "../runs/runner.ts";
import { applicationIsActive, setApplicationActive } from "./activity.ts";

const HEADER = "X-Venator-Application";
const ACTIONS = ["prepare", "save", "dismiss", "restore", "applied", "handoff"];
const PROVIDERS = ["claude", "codex", "api"];
const children = new Set<ChildProcess>();

export function shutdownApplications(): void {
	for (const child of children) signalTree(child, "SIGKILL", systemContext());
}

export type ApplicationCommand = (argv: readonly string[]) => Promise<JsonValue>;

/** Fixed module and argv, never a shell command or executable chosen by a request. */
export function applicationCommand(argv: readonly string[]): Promise<JsonValue> {
	const context = systemContext();
	const environment = runEnvironment(context);
	// Forward custom endpoint configuration only; the request explicitly chooses
	// whether the API provider runs. An unrelated vendor key cannot activate it.
	for (const name of ["VENATOR_LLM_API_URL", "VENATOR_LLM_API_MODEL", "VENATOR_LLM_API_KEY_VAR", "VENATOR_LLM_API_KEY_HEADER", "VENATOR_LLM_API_KEY_PREFIX", "VENATOR_LLM_CODEX_MODEL"]) {
		const value = context.environment(name);
		if (value !== undefined) environment[name] = value;
	}
	const keyName = environment.VENATOR_LLM_API_KEY_VAR;
	if (keyName !== undefined && keyName !== TYPESAFE_API_KEY_ENVIRONMENT && /^[A-Za-z_][A-Za-z0-9_]*$/.test(keyName)) {
		const key = context.environment(keyName);
		if (key !== undefined) environment[keyName] = key;
	}
	return new Promise((resolve, reject) => {
		const child = spawn(pythonInterpreter(context), ["-m", "venator.applications", ...argv], {
			cwd: pipelineWorkingDirectory(context), env: environment, stdio: ["ignore", "pipe", "pipe"],
			detached: context.platform !== "win32",
		});
		children.add(child);
		let output = "";
		const timeout = setTimeout(() => signalTree(child, "SIGKILL", context), 10 * 60_000);
		child.stdout.setEncoding("utf8");
		child.stdout.on("data", (chunk: string) => {
			output += chunk;
			if (output.length > 8 * 1024 * 1024) signalTree(child, "SIGKILL", context);
		});
		// The CLI returns a redacted structured error. Do not echo provider stderr.
		child.stderr.resume();
		child.on("error", () => { children.delete(child); clearTimeout(timeout); reject(new Error("The application service could not start.")); });
		child.on("close", (code) => {
			children.delete(child);
			clearTimeout(timeout);
			try {
				const result = parseJson(output);
				if (code !== 0) {
					const error = asMapping(result);
					reject(new Error(error === null ? "Application preparation failed." : asText(error.error) ?? "Application preparation failed."));
				} else resolve(result);
			} catch { reject(new Error("The application service did not return a result.")); }
		});
	});
}

function profileArgs(): readonly string[] {
	const profile = readOnboardingState().implied;
	if (profile === null) throw new Error("Choose a profile before preparing an application.");
	return ["--profile", profile];
}

function jsonResponse(value: JsonValue): Response {
	return new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
}

export function createApplicationRoutes(command: ApplicationCommand = applicationCommand, selectedProfile: () => readonly string[] = profileArgs): Hono {
	const routes = new Hono();
	routes.use("*", cors({ origin: [...LOCAL_ACTION_ORIGINS], allowMethods: ["GET", "POST", "OPTIONS"], allowHeaders: ["Content-Type", HEADER] }));
	routes.post("/", async (context) => {
		const refusal = checkLocalAction(HEADER, "1", context.req.header(HEADER), context.req.header("origin"));
		if (refusal !== null) return context.json({ error: refusal.message }, 403);
		if (applicationIsActive() || currentRun() !== null) return context.json({ error: "Wait for the current operation to finish." }, 409);
		const raw = await context.req.text();
		if (raw.length > 4096) return context.json({ error: "Request too large." }, 413);
		const body = asMapping(parseJson(raw));
		const action = body === null ? null : asText(body.action);
		const key = body === null ? null : asText(body.key);
		const provider = body === null ? null : asText(body.provider);
		if (action === null || !ACTIONS.includes(action) || !key || key.length > 1000) {
			return context.json({ error: "Choose a job and an application action." }, 400);
		}
		if (action === "prepare" && (provider === null || !PROVIDERS.includes(provider))) {
			return context.json({ error: "Choose which AI connection will prepare the documents." }, 400);
		}
		const args = [action, key, ...selectedProfile()];
		if (action === "prepare" && provider !== null) args.push("--provider", provider);
		if (body !== null && asBoolean(body.coverLetter) === true) args.push("--cover-letter");
		if (applicationIsActive() || currentRun() !== null) return context.json({ error: "Wait for the current operation to finish." }, 409);
		setApplicationActive(true);
		try { return jsonResponse(await command(args)); }
		finally { setApplicationActive(false); }
	});
	routes.get("/:key", async (context) => jsonResponse(await command(["status", context.req.param("key"), ...selectedProfile()])));
	routes.get("/:key/files/:name", async (context) => {
		const name = context.req.param("name");
		if (!["resume.pdf", "resume.txt", "letter.txt"].includes(name)) return context.json({ error: "Unknown document." }, 404);
		const result = asMapping(await command(["file", context.req.param("key"), "--file", name, ...selectedProfile()]));
		const content = result === null ? null : asText(result.base64);
		if (content === null) return context.json({ error: "Document unavailable." }, 404);
		context.header("Content-Type", name.endsWith(".pdf") ? "application/pdf" : "text/plain; charset=utf-8");
		context.header("Content-Disposition", `inline; filename="${name}"`);
		context.header("Cache-Control", "no-store");
		return context.body(Buffer.from(content, "base64"));
	});
	routes.onError((error, context) => context.json({ error: error.message }, 400));
	return routes;
}
