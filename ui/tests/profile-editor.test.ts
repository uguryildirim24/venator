import assert from "node:assert/strict";
import { mkdtempSync, realpathSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";

import { documentRuntime, saveDocumentRuntime } from "../server/install-settings.ts";
import { ONBOARDING_REQUEST_HEADER } from "../shared/onboarding.ts";
import { createOnboardingRoutes } from "../server/onboarding/routes.ts";
import { currentRun, recentRuns, resetRunsForTest } from "../server/runs/runner.ts";
import type { LocationContext } from "../server/locations.ts";
import { editProfileDocuments, readProfileDocuments } from "../server/onboarding/profile-edit.ts";
import { formOf, saveProfileForm } from "../server/onboarding/profile-form.ts";
import { writeProfile } from "../server/onboarding/profile.ts";
import { profileHash, parseRoute } from "../src/router.ts";
import { armVerifierStandIn, disarmVerifierStandIn } from "./verifier-stand-in.ts";

let standIn: ReadonlyMap<string, string> = new Map();
before(() => { standIn = armVerifierStandIn(); });
after(() => { disarmVerifierStandIn(); });

test("Profile opens the existing Install's three files, edits them and preserves runtime, Jev policy and assets", async () => {
	const home = realpathSync(mkdtempSync(join(tmpdir(), "venator-profile-editor-")));
	const context: LocationContext = {
		platform: "linux", home, workingDirectory: home, checkoutRoot: null,
		environment: (key) => key === "VENATOR_HOME" ? home : standIn.get(key),
	};
	try {
		// Use the setup writer rather than hand-making a Profile.
		const created = await writeProfile({ name: "main", overwrite: false,
			resume: { name: "A Person", contact: { location: "Boston", phone: "555-0100", email: "a@b.co", linkedin: "linkedin.com/in/a" } },
			constraints: { screening: { how_heard: null } }, targeting: { search: { queries: ["biologist"] } },
		}, context);
		await saveDocumentRuntime("codex", context);
		writeFileSync(join(created.directory, "reference.pdf"), "prepared document");
		assert.equal(parseRoute(profileHash()).name, "profile");
		const original = readProfileDocuments("main", context);
		assert.match(original["targeting.yaml"], /qualification_mode: "jev"/u);
		const updated = { ...original, "resume.yaml": original["resume.yaml"].replace("A Person", "Edited Person"), "constraints.yaml": original["constraints.yaml"] + "\n# confirmed by person\n" };
		await editProfileDocuments("main", updated, original, context);
		assert.deepEqual(readProfileDocuments("main", context), updated);
		await assert.rejects(editProfileDocuments("main", original, original, context), /changed since you opened it/u);
		assert.deepEqual(readProfileDocuments("main", context), updated);
		assert.equal(readFileSync(join(created.directory, "reference.pdf"), "utf8"), "prepared document");
		assert.equal(documentRuntime(context), "codex");
	} finally { rmSync(home, { recursive: true, force: true }); }
});

test("the Profile save route starts a replay on a version change but not on an unchanged save", async () => {
	const home = realpathSync(mkdtempSync(join(tmpdir(), "venator-profile-save-run-")));
	const previousHome = process.env["VENATOR_HOME"];
	const previousPython = process.env["VENATOR_PYTHON"];
	process.env["VENATOR_HOME"] = home;
	process.env["VENATOR_PYTHON"] = process.execPath;
	try {
		const context: LocationContext = {
			platform: "linux", home, workingDirectory: home, checkoutRoot: null,
			environment: (key) => key === "VENATOR_HOME" ? home : standIn.get(key),
		};
		await writeProfile({ name: "main", overwrite: false,
			resume: { name: "A Person", contact: { location: "Boston", phone: "555-0100", email: "a@b.co", linkedin: "linkedin.com/in/a" } },
			constraints: {}, targeting: { search: { queries: ["biologist"] } },
		}, context);
		const routes = createOnboardingRoutes();
		async function save(original: ReturnType<typeof readProfileDocuments>, name: string) {
			const form = formOf(original);
			return routes.request("/existing-profile", {
				method: "POST", headers: { "content-type": "application/json", [ONBOARDING_REQUEST_HEADER]: "1" },
				body: JSON.stringify({ name: "main", original, form: { ...form, resume: { ...form.resume, name } } }),
			});
		}
		const opened = readProfileDocuments("main", context);
		assert.equal((await save(opened, "New Person")).status, 200);
		for (let i = 0; i < 100 && recentRuns(1).length === 0; i++) await new Promise((done) => setTimeout(done, 20));
		assert.equal(recentRuns(1)[0]?.kind, "profile-replay");
		assert.equal(currentRun(), null);
		const current = readProfileDocuments("main", context);
		assert.equal((await save(current, "New Person")).status, 200);
		assert.equal(recentRuns(5).length, 1);
	} finally {
		resetRunsForTest();
		if (previousHome === undefined) delete process.env["VENATOR_HOME"]; else process.env["VENATOR_HOME"] = previousHome;
		if (previousPython === undefined) delete process.env["VENATOR_PYTHON"]; else process.env["VENATOR_PYTHON"] = previousPython;
		rmSync(home, { recursive: true, force: true });
	}
});

test("the Profile screen's save writes the form's differences and keeps the stale check, the Jev policy and the runtime", async () => {
	const home = realpathSync(mkdtempSync(join(tmpdir(), "venator-profile-form-")));
	const context: LocationContext = {
		platform: "linux", home, workingDirectory: home, checkoutRoot: null,
		environment: (key) => key === "VENATOR_HOME" ? home : standIn.get(key),
	};
	try {
		const created = await writeProfile({ name: "main", overwrite: false,
			resume: { name: "A Person", contact: { location: "Boston", phone: "555-0100", email: "a@b.co", linkedin: "linkedin.com/in/a" } },
			constraints: { screening: { how_heard: null } },
			targeting: { search: { queries: ["biologist"] }, filters: { qualification_mode: "jev", jev: { policy_version: "mine-1", restricted_roles: "review", temporary_student_authorization_exclusion: "review", no_sponsorship_student: "review", no_sponsorship_nonstudent: "review", unmet_completed_degree: "review", domains: ["pharmacy"] } } },
		}, context);
		await saveDocumentRuntime("codex", context);
		writeFileSync(join(created.directory, "reference.pdf"), "prepared document");
		const original = readProfileDocuments("main", context);
		const form = formOf(original);
		assert.equal(form.resume.name, "A Person");
		const edited = { ...form,
			resume: { ...form.resume, name: "Edited Person" },
			constraints: { ...form.constraints, screening: { ...form.constraints.screening, how_heard: "A friend" } },
			targeting: { ...form.targeting, search: { ...form.targeting.search, queries: ["biologist", "chemist"] } },
		};
		assert.equal(await saveProfileForm("main", edited, original, context), true);
		const saved = readProfileDocuments("main", context);
		// The setup writer quotes every scalar; an edit in place keeps the spelling it found, and
		// an answer that was null is written plain.
		assert.match(saved["resume.yaml"], /name: "Edited Person"/u);
		assert.match(saved["constraints.yaml"], /how_heard: A friend/u);
		assert.match(saved["targeting.yaml"], /- chemist/u);
		assert.match(saved["targeting.yaml"], /policy_version: "mine-1"/u, "the Jev policy the form never shows is still there");
		assert.match(saved["targeting.yaml"], /qualification_mode: "jev"/u);
		assert.deepEqual(formOf(saved).targeting.search.queries, ["biologist", "chemist"]);
		assert.equal(await saveProfileForm("main", formOf(saved), saved, context), false);
		// The files the person opened are no longer what is on disk: refused, and nothing changes.
		await assert.rejects(saveProfileForm("main", form, original, context), /changed since you opened it/u);
		assert.deepEqual(readProfileDocuments("main", context), saved);
		assert.equal(readFileSync(join(created.directory, "reference.pdf"), "utf8"), "prepared document");
		assert.equal(documentRuntime(context), "codex");
	} finally { rmSync(home, { recursive: true, force: true }); }
});
