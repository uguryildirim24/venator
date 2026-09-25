/**
 * `pnpm dev` — the API and the app under one Ctrl-C, with prefixed output.
 *
 * A supervisor script rather than a concurrently-style dependency: two child processes and
 * a shared exit are cheap to write and keep the toolchain to what ships with Node, which is
 * the same reason the server uses node:sqlite.
 */

import { spawn, type ChildProcess } from "node:child_process";
import type { Readable } from "node:stream";

import { SAMPLE_DATA_ENVIRONMENT, UI_ROOT } from "../server/locations.ts";
import { API_SERVER_PORT, APP_DEV_PORT } from "../shared/ports.ts";
import { needsShell } from "./platform.ts";

const children: ChildProcess[] = [];
let shuttingDown = false;

function relay(name: string, stream: Readable | null): void {
	if (stream === null) return;
	stream.setEncoding("utf8");
	let pending = "";
	stream.on("data", (chunk: string) => {
		pending += chunk;
		const lines = pending.split("\n");
		pending = lines.pop() ?? "";
		for (const line of lines) {
			process.stdout.write(`${name.padEnd(4)} | ${line}\n`);
		}
	});
}

function stopAll(): void {
	if (shuttingDown) return;
	shuttingDown = true;
	for (const child of children) child.kill("SIGTERM");
}

function start(name: string, command: string, args: readonly string[], environment: NodeJS.ProcessEnv = {}): void {
	const child = spawn(command, [...args], {
		cwd: UI_ROOT,
		stdio: ["ignore", "pipe", "pipe"],
		shell: needsShell(command),
		env: { ...process.env, ...environment },
	});
	relay(name, child.stdout);
	relay(name, child.stderr);
	child.on("exit", (code) => {
		if (shuttingDown) return;
		process.stdout.write(`${name.padEnd(4)} | exited (${code ?? 0}); stopping the other process\n`);
		process.exitCode = code ?? 0;
		stopAll();
	});
	children.push(child);
}

process.on("SIGINT", stopAll);
process.on("SIGTERM", stopAll);

process.stdout.write(
	`venator dashboard\n  app  http://127.0.0.1:${APP_DEV_PORT}\n  api  http://127.0.0.1:${API_SERVER_PORT}/api\n\n`,
);

/*
 * The dev loop asks for the sample Postings, and that is the whole of what changed here.
 *
 * The server used to fall back to the fixture for anybody with no `build/venator.db`, which
 * meant a person who had just installed the app opened it onto nineteen Postings from a
 * stranger's job search. It now serves an empty view unless the run asks (`server/db.ts`), so
 * the ask moved to the one place where a populated dashboard is the point. A real view still
 * wins: this decides what happens when there is none, and nothing else.
 */
start("api", process.execPath, ["server/main.ts"], { [SAMPLE_DATA_ENVIRONMENT]: "1" });
start("app", "pnpm", ["exec", "vite"]);
