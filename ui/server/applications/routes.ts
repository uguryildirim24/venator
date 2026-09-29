import { spawn, type ChildProcess } from "node:child_process";
import { Hono } from "hono";
import { cors } from "hono/cors";
import { checkLocalAction, LOCAL_ACTION_ORIGINS } from "../http/local-action-guard.ts";
import { pipelineWorkingDirectory, pythonInterpreter, systemContext } from "../locations.ts";
import { asBoolean, asMapping, asText, parseJson, type JsonValue } from "../onboarding/json.ts";
import { readOnboardingState } from "../onboarding/state.ts";
import { readProfileDocuments } from "../onboarding/profile-edit.ts";
import { formOf, saveProfileForm } from "../onboarding/profile-form.ts";
import { queueProfileReplay } from "../runs/runner.ts";
import { runEnvironment } from "../runs/environment.ts";
import { currentRun, signalTree } from "../runs/runner.ts";
import { applicationIsActive, setApplicationActive } from "./activity.ts";
import { takePipelineLock } from "../pipeline-lock.ts";

const HEADER = "X-Venator-Application";
const ACTIONS = ["apply", "edit", "save", "dismiss", "restore", "applied", "fill", "answer", "retract", "replace", "promote", "authorization"];
const children = new Set<ChildProcess>();

export function shutdownApplications(): void {
	for (const child of children) signalTree(child, "SIGKILL", systemContext());
}

export type ApplicationCommand = (argv: readonly string[], options?: { readonly signal?: AbortSignal; readonly timeoutMs?: number }) => Promise<JsonValue>;

/** Fixed module and argv, never a shell command or executable chosen by a request. */
export function applicationCommand(argv: readonly string[], options?: { readonly signal?: AbortSignal; readonly timeoutMs?: number }): Promise<JsonValue> {
	const context = systemContext();
	const environment = runEnvironment(context);
	// Forward custom endpoint configuration only; the request explicitly chooses
	// whether the API provider runs. An unrelated vendor key cannot activate it.
	for (const name of ["VENATOR_LLM_API_URL", "VENATOR_LLM_API_MODEL", "VENATOR_LLM_API_KEY_VAR", "VENATOR_LLM_API_KEY_HEADER", "VENATOR_LLM_API_KEY_PREFIX", "VENATOR_LLM_CODEX_MODEL"]) {
		const value = context.environment(name);
		if (value !== undefined) environment[name] = value;
	}
	const keyName = environment.VENATOR_LLM_API_KEY_VAR;
	if (keyName !== undefined && /^[A-Za-z_][A-Za-z0-9_]*$/.test(keyName)) {
		const key = context.environment(keyName);
		if (key !== undefined) environment[keyName] = key;
	}
	if (options?.signal?.aborted) return Promise.reject(new Error("Application status was cancelled."));
	return new Promise((resolve, reject) => {
		const child = spawn(pythonInterpreter(context), ["-m", "venator.applications", ...argv], {
			cwd: pipelineWorkingDirectory(context), env: environment, stdio: ["ignore", "pipe", "pipe"],
			detached: context.platform !== "win32",
		});
		children.add(child);
		let output = "";
		const abort = () => signalTree(child, "SIGKILL", context);
		options?.signal?.addEventListener("abort", abort, { once: true });
		if (options?.signal?.aborted) abort();
		const timeout = setTimeout(abort, options?.timeoutMs ?? 10 * 60_000);
		const cleanup = () => { children.delete(child); clearTimeout(timeout); options?.signal?.removeEventListener("abort", abort); };
		child.stdout.setEncoding("utf8");
		child.stdout.on("data", (chunk: string) => {
			output += chunk;
			if (output.length > 8 * 1024 * 1024) signalTree(child, "SIGKILL", context);
		});
		// The CLI returns a redacted structured error. Do not echo provider stderr.
		child.stderr.resume();
		child.on("error", () => { cleanup(); reject(new Error("The application service could not start.")); });
		child.on("close", (code) => {
			cleanup();
			try {
				if (options?.signal?.aborted) throw new Error("Application status was cancelled.");
				const result = parseJson(output);
				if (code !== 0) {
					const error = asMapping(result);
					reject(new Error(error === null ? "Application action failed." : asText(error.error) ?? "Application action failed."));
				} else resolve(result);
			} catch { reject(new Error("The application service did not return a result.")); }
		});
	});
}

function profileArgs(): readonly string[] {
	const profile = readOnboardingState().implied;
	if (profile === null) throw new Error("Choose a Profile before applying.");
	return ["--profile", profile];
}

function jsonResponse(value: JsonValue): Response {
	return new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
}

export function createApplicationRoutes(command: ApplicationCommand = applicationCommand, selectedProfile: () => readonly string[] = profileArgs): Hono {
	const routes = new Hono();
	const flights = new Map<string, { controller: AbortController; promise: Promise<JsonValue>; readers: number }>();
	let running = 0;
	const queue: Array<() => void> = [];
	async function slot(signal: AbortSignal): Promise<() => void> {
		if (signal.aborted) throw new Error("Application status was cancelled.");
		if (running >= 1) {
			if (queue.length >= 16) throw new Error("Too many application status checks. Try again shortly.");
			await new Promise<void>((resolve, reject) => {
				const wake = () => { signal.removeEventListener("abort", cancel); resolve(); };
				const cancel = () => { queue.splice(queue.indexOf(wake), 1); reject(new Error("Application status was cancelled.")); };
				queue.push(wake);
				signal.addEventListener("abort", cancel, { once: true });
			});
		} else {
			running += 1;
		}
		if (signal.aborted) {
			const next = queue.shift();
			if (next) next(); else running -= 1;
			throw new Error("Application status was cancelled.");
		}
		return () => { const next = queue.shift(); if (next) next(); else running -= 1; };
	}
	function status(args: readonly string[], signal: AbortSignal): Promise<JsonValue> {
		const identity = JSON.stringify(args);
		let flight = flights.get(identity);
		if (flight === undefined || flight.controller.signal.aborted) {
			const controller = new AbortController();
			const promise = (async () => {
				const release = await slot(controller.signal);
				try {
					// Status may record an employer confirmation, so it is a writer too.
					while (applicationIsActive() || currentRun() !== null) {
						if (controller.signal.aborted) throw new Error("Application status was cancelled.");
						await new Promise((done) => setTimeout(done, 50));
					}
					setApplicationActive(true);
					try { return await command(args, { signal: controller.signal, timeoutMs: 4_000 }); }
					finally { setApplicationActive(false); }
				} finally { release(); }
			})();
			flight = { controller, promise, readers: 0 };
			flights.set(identity, flight);
			void promise.finally(() => { if (flights.get(identity) === flight) flights.delete(identity); }).catch(() => {});
		}
		const active = flight;
		active.readers += 1;
		return new Promise<JsonValue>((resolve, reject) => {
			let finished = false;
			const finish = () => {
				if (finished) return false;
				finished = true;
				signal.removeEventListener("abort", cancel);
				active.readers -= 1;
				if (active.readers === 0) active.controller.abort();
				return true;
			};
			const cancel = () => { if (finish()) reject(new Error("Application status was cancelled.")); };
			signal.addEventListener("abort", cancel, { once: true });
			if (signal.aborted) cancel();
			void active.promise.then((value) => { if (finish()) resolve(value); }, (failure: Error) => { if (finish()) reject(failure instanceof Error ? failure : new Error("Application status could not be checked.")); });
		});
	}
	routes.use("*", cors({ origin: [...LOCAL_ACTION_ORIGINS], allowMethods: ["GET", "POST", "OPTIONS"], allowHeaders: ["Content-Type", HEADER] }));
	routes.post("/", async (context) => {
		const refusal = checkLocalAction(HEADER, "1", context.req.header(HEADER), context.req.header("origin"));
		if (refusal !== null) return context.json({ error: refusal.message }, 403);
		if (applicationIsActive() || currentRun() !== null) return context.json({ error: "Wait for the current operation to finish." }, 409);
		const raw = await context.req.text();
		// DRAFT_SCHEMA: 250 bullets + 5 paragraphs, 1800 text + 200 ID each.
		if (raw.length > 255 * (1800 + 200 + 48) + 1000) return context.json({ error: "Request too large." }, 413);
		const body = asMapping(parseJson(raw));
		const action = body === null ? null : asText(body.action);
		const key = body === null ? null : asText(body.key);
		if (action === null || !ACTIONS.includes(action) || !key || key.length > 1000) {
			return context.json({ error: "Choose a job and an application action." }, 400);
		}
		if (action !== "edit" && raw.length > 4096) return context.json({ error: "Request too large." }, 413);
		if (action === "authorization") {
			const value = asBoolean(body?.authorized);
			const name = readOnboardingState().implied;
			if (value === null || name === null) return context.json({ error: "Choose Yes or No for work authorization." }, 400);
			setApplicationActive(true);
			try {
				const documents = readProfileDocuments(name);
				const form = formOf(documents);
				const previous = form.constraints.work_authorization.authorized_to_work;
				const answer = value ? "yes" : "no";
				if (previous !== "unanswered" && previous !== answer) return context.json({ error: "Work authorization has already been answered in the Profile." }, 409);
				if (previous === "unanswered") {
					await saveProfileForm(name, { ...form, constraints: { ...form.constraints,
						work_authorization: { ...form.constraints.work_authorization, authorized_to_work: answer } } }, documents);
					try { queueProfileReplay(name); } catch (error) { process.stderr.write(`Profile replay could not be queued: ${String(error)}\n`); }
				}
				return jsonResponse({ saved: true });
			} finally { setApplicationActive(false); }
		}
		const args = [action, key, ...selectedProfile()];
		if (action === "answer") {
			const text = asText(body?.text);
			const kind = asText(body?.kind);
			const answer = asText(body?.answer) ?? asBoolean(body?.answer);
			const universal = asBoolean(body?.universal);
			const eeo = asBoolean(body?.eeo) ?? false;
			if (text === null || kind === null || answer === null || universal === null) return context.json({ error: "Choose a question and its answer." }, 400);
			args.push("--answer", JSON.stringify({ text, kind, answer, universal, eeo }));
		}
		if (action === "retract" || action === "replace" || action === "promote") {
			const id = asText(body?.id);
			const answer = asText(body?.answer) ?? asBoolean(body?.answer);
			if (id === null || action === "replace" && answer === null) return context.json({ error: "Choose an answer to change." }, 400);
			args.push("--answer", JSON.stringify({ id, answer: action === "retract" ? null : answer }));
		}
		if (action === "edit") {
			const version = body === null ? null : asText(body.version);
			const edits = body?.edits;
			if (version === null || !/^[a-zA-Z0-9]+$/u.test(version) || !Array.isArray(edits) || edits.length < 1 || edits.length > 255 ||
				edits.some((row) => { const entry = asMapping(row); const id = entry === null ? null : asText(entry.draft_id); const text = entry === null ? null : asText(entry.text); return entry === null || Object.keys(entry).length !== 2 || id === null || id.length > 200 || text === null || text.length > 1800 || !text.trim() || /[\r\n]/u.test(text); })) {
				return context.json({ error: "Choose bounded reviewed passages to edit." }, 400);
			}
			args.push("--version", version, "--edits", JSON.stringify(edits));
		}
		if (applicationIsActive() || currentRun() !== null) return context.json({ error: "Wait for the current operation to finish." }, 409);
		const releaseLock = takePipelineLock();
		if (releaseLock === null) return context.json({ error: "Wait for the current operation to finish." }, 409);
		setApplicationActive(true);
		try { return jsonResponse(await command(args)); }
		finally { setApplicationActive(false); releaseLock(); }
	});
	routes.get("/answers", async (context) => jsonResponse(await status(["answers", "_", ...selectedProfile()], context.req.raw.signal)));
	routes.get("/:key", async (context) => jsonResponse(await status(["status", context.req.param("key"), ...selectedProfile()], context.req.raw.signal)));
	routes.get("/:key/files/:name", async (context) => {
		const name = context.req.param("name");
		if (!["resume.pdf", "resume.txt", "letter.txt", "letter.pdf"].includes(name)) return context.json({ error: "Unknown document." }, 404);
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
