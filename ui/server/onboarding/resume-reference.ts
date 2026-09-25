import { execFile } from "node:child_process";
import { cpSync, existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { MAXIMUM_PDF_BYTES } from "../../shared/onboarding.ts";
import { pipelineWorkingDirectory, pythonInterpreter, type LocationContext } from "../locations.ts";
import { nonJevInheritedEnvironment } from "../runs/environment.ts";
import { OnboardingError } from "./errors.ts";
import { asBoolean, asMapping, asText, at, parseJson } from "./json.ts";

export const UNSUPPORTED_LAYOUT_WARNING = "Your résumé was saved, but its layout cannot be reproduced yet.";
export type ReferenceCapture = (source: string, directory: string, context: LocationContext) => Promise<
	{ readonly supported: true } | { readonly supported: false; readonly reason: string }
>;

function captureFailure(): OnboardingError {
	return new OnboardingError("write_failed", "The résumé layout could not be saved. Your previous profile is unchanged.", "Try saving again.");
}

export const captureReference: ReferenceCapture = (source, directory, context) => new Promise((resolve, reject) => {
	execFile(pythonInterpreter(context), ["-m", "venator.resume.reference", source, directory], {
		cwd: pipelineWorkingDirectory(context), timeout: 30_000, maxBuffer: 64 * 1024,
		env: nonJevInheritedEnvironment(context),
	}, (error, stdout) => {
		if (error !== null) { reject(captureFailure()); return; }
		try {
			const result = asMapping(parseJson(stdout));
			if (result === null) throw captureFailure();
			const supported = asBoolean(at(result, "supported"));
			if (supported === true) { resolve({ supported: true }); return; }
			const reason = asText(at(result, "reason"));
			if (supported !== false || reason === null || reason.trim() === "" || reason.length > 2000) throw captureFailure();
			resolve({ supported: false, reason });
		} catch { reject(captureFailure()); }
	});
});

export function validateReferencePdf(bytes: Uint8Array): void {
	if (bytes.byteLength > MAXIMUM_PDF_BYTES) throw new OnboardingError("body_too_large", "Attach a PDF no larger than 4 MB.");
	if (bytes.byteLength < 5 || new TextDecoder().decode(bytes.subarray(0, 5)) !== "%PDF-") {
		throw new OnboardingError("bad_request", "The attached résumé must be a PDF.");
	}
}

/** Runs only inside the final profile staging transaction. */
export async function stageResumeReference(staging: string, previous: string | null, pdf: Uint8Array | undefined, context: LocationContext, capture: ReferenceCapture): Promise<readonly string[]> {
	const directory = join(staging, "resume-reference");
	if (pdf === undefined) {
		if (previous === null) return [];
		const old = join(previous, "resume-reference");
		if (lstatSync(old, { throwIfNoEntry: false }) === undefined) return [];
		cpSync(old, directory, { recursive: true, filter: (source) => {
			const stat = lstatSync(source);
			if (!stat.isFile() && !stat.isDirectory()) throw captureFailure();
			return true;
		} });
		const statusPath = join(directory, "status.json");
		if (existsSync(statusPath)) {
			if (lstatSync(statusPath).size > 64 * 1024) throw captureFailure();
			const status = asMapping(parseJson(readFileSync(statusPath, "utf8")));
			if (status !== null && asBoolean(at(status, "supported")) === false) return [UNSUPPORTED_LAYOUT_WARNING];
		}
		return [];
	}
	validateReferencePdf(pdf);
	mkdirSync(directory, { mode: 0o700 });
	const source = join(directory, "source.pdf");
	writeFileSync(source, pdf, { mode: 0o600, flush: true });
	const result = await capture(source, directory, context);
	if (!result.supported) {
		for (const name of readdirSync(directory)) rmSync(join(directory, name), { recursive: true, force: true });
		writeFileSync(source, pdf, { mode: 0o600, flush: true });
		writeFileSync(join(directory, "status.json"), JSON.stringify(result), { mode: 0o600, flush: true });
		return [UNSUPPORTED_LAYOUT_WARNING];
	}
	for (const name of ["source.pdf", "layout.json", "regular.ttf", "bold.ttf", "italic.ttf"]) {
		const path = join(directory, name);
		if (!existsSync(path) || !lstatSync(path).isFile() || lstatSync(path).size === 0) throw captureFailure();
	}
	return [];
}
