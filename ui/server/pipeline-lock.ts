import { closeSync, mkdirSync, openSync, readFileSync, unlinkSync, writeSync } from "node:fs";
import { join } from "node:path";
import { applicationDataDirectory, systemContext, type LocationContext } from "./locations.ts";
import { asMapping, asNumber, parseJson } from "./onboarding/json.ts";

/** Shared with venator.schedule.lock. Keep the file until the child tree settles. */
export function takePipelineLock(context: LocationContext = systemContext()): (() => void) | null {
	const path = join(applicationDataDirectory(context), "build", ".pipeline.lock");
	mkdirSync(join(applicationDataDirectory(context), "build"), { recursive: true });
	const token = JSON.stringify({ pid: process.pid, at: Date.now() });
	for (let attempt = 0; attempt < 2; attempt += 1) {
		try {
			const fd = openSync(path, "wx", 0o600);
			try { writeSync(fd, token); } finally { closeSync(fd); }
			return () => {
				try { if (readFileSync(path, "utf8") === token) unlinkSync(path); } catch { /* already removed */ }
			};
		} catch (error) {
			// SAFETY: Node's filesystem error has `code`; an absent code cannot be EEXIST.
			if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
			try {
				const record = asMapping(parseJson(readFileSync(path, "utf8")));
				const pid = asNumber(record?.pid);
				if (pid === null || !Number.isSafeInteger(pid) || pid <= 0) return null;
				try { process.kill(pid, 0); return null; }
				catch (probe) {
					// SAFETY: process.kill throws a Node system error for a missing PID.
					if ((probe as NodeJS.ErrnoException).code !== "ESRCH") return null;
				}
				unlinkSync(path);
			} catch { return null; }
		}
	}
	return null;
}
