import { spawn } from "node:child_process";
import { join } from "node:path";
import { Hono } from "hono";
import { cors } from "hono/cors";

import { checkLocalAction, LOCAL_ACTION_ORIGINS } from "../http/local-action-guard.ts";
import { applicationDataDirectory, pipelineWorkingDirectory, pythonInterpreter, systemContext } from "../locations.ts";
import { readOnboardingState } from "../onboarding/state.ts";
import { asMapping, asText, parseJson, type JsonValue } from "../onboarding/json.ts";
import { takePipelineLock } from "../pipeline-lock.ts";
import { runEnvironment } from "../runs/environment.ts";

const HEADER = "X-Venator-Training";
const MAX_CSV = 2_000_000;

type Request = { action: "preview" | "train" | "pick"; csv: string | null; sha256: string | null; choice: "old" | "new" | null };

function parseRequest(text: string): Request | null {
	const value = asMapping(parseJson(text));
	if (value === null) return null;
	const action = asText(value.action);
	if (action !== "preview" && action !== "train" && action !== "pick") return null;
	const choice = asText(value.choice);
	return { action, csv: asText(value.csv), sha256: asText(value.sha256), choice: choice === "old" || choice === "new" ? choice : null };
}

function command(args: string[], csv: string): Promise<JsonValue> {
	const context = systemContext();
	return new Promise((resolve, reject) => {
		const child = spawn(pythonInterpreter(context), ["-m", "venator.score.train", ...args], {
			cwd: pipelineWorkingDirectory(context), env: runEnvironment(context), stdio: ["pipe", "pipe", "ignore"],
		});
		let output = "";
		const timeout = setTimeout(() => child.kill("SIGKILL"), 30 * 60_000);
		child.stdout.setEncoding("utf8");
		child.stdout.on("data", (part: string) => { output += part; if (output.length > 100_000) child.kill("SIGKILL"); });
		child.stdin.on("error", () => { /* a failed child may close stdin first */ });
		child.stdin.end(csv);
		child.on("error", () => { clearTimeout(timeout); reject(new Error("Training service could not start.")); });
		child.on("close", (code) => {
			clearTimeout(timeout);
			if (code !== 0) { reject(new Error("Training could not complete. The active keep model was not changed.")); return; }
			try { resolve(parseJson(output)); } catch { reject(new Error("Training returned no comparison.")); }
		});
	});
}

/** Local only: preview first, explicit upload press second, explicit swap press third. */
export function createTrainRoutes(): Hono {
	const routes = new Hono();
	let active = false;
	routes.use("*", cors({ origin: [...LOCAL_ACTION_ORIGINS], allowMethods: ["POST", "OPTIONS"], allowHeaders: ["Content-Type", HEADER] }));
	routes.post("/", async (context) => {
		const refused = checkLocalAction(HEADER, "1", context.req.header(HEADER), context.req.header("origin"));
		if (refused !== null) return context.json({ error: refused.message }, 403);
		const text = await context.req.text();
		if (text.length > MAX_CSV + 200) return context.json({ error: "Label file is too large." }, 413);
		let request: Request | null;
		try { request = parseRequest(text); } catch { request = null; }
		if (request === null) return context.json({ error: "Invalid training request." }, 400);
		if (active) return context.json({ error: "Training is already in progress." }, 409);
		const profile = readOnboardingState().implied;
		if (profile === null) return context.json({ error: "Choose a Profile first." }, 400);
		if (request.action !== "pick" && (request.csv === null || request.csv.length > MAX_CSV || request.csv.length === 0)) return context.json({ error: "Choose a private label CSV." }, 400);
		if (request.action === "train" && (request.sha256 === null || !/^[0-9a-f]{64}$/.test(request.sha256))) return context.json({ error: "Preview the upload first." }, 400);
		if (request.action === "pick" && request.choice !== "old" && request.choice !== "new") return context.json({ error: "Pick old or new." }, 400);
		if (!["preview", "train", "pick"].includes(request.action)) return context.json({ error: "Unknown training action." }, 400);
		const release = request.action === "preview" ? null : takePipelineLock();
		if (request.action !== "preview" && release === null) return context.json({ error: "Wait for the current operation to finish." }, 409);
		active = true;
		try {
			const args = ["--profile", profile];
			if (request.action === "pick") {
				args.push("--comparison", join(applicationDataDirectory(), "build", "keep-training-comparison.json"), "--pick", request.choice!);
			} else {
				args.push("--labels", "-");
				if (request.action === "train") args.push("--approve-sha256", request.sha256!);
			}
			const result = await command(args, request.csv ?? "");
			return new Response(JSON.stringify(result), { headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
		} catch (error) {
			return context.json({ error: error instanceof Error ? error.message : "Training failed." }, 500);
		} finally { active = false; release?.(); }
	});
	return routes;
}
