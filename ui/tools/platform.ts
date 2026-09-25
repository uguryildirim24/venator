/**
 * The two places a build or dev script has to know which platform it is on.
 *
 * Both are one line of logic and neither was testable while it lived inside a script that
 * does its work at module scope. They are here so a test can call them, and so the reasoning
 * sits in one place instead of in two comments that drift.
 *
 * Nothing here has been run on Windows. Both rules are taken from documented behaviour:
 * `CreateProcess` appends `.exe` and cannot start an extension-less file or a batch file, and
 * a pnpm bin on Windows is `pnpm.cmd`, which Node will not spawn directly.
 */

/**
 * The extension an executable needs on `platform`.
 *
 * Tauri looks for `binaries/node-<triple><suffix>` and hands what it finds to the OS. On
 * Windows the `.exe` is not decoration: `CreateProcess` cannot start an extension-less file
 * at all, so a sidecar copied to a bare name is both invisible to the bundler and unrunnable
 * if it were found.
 */
export function executableSuffix(platform: NodeJS.Platform = process.platform): string {
	return platform === "win32" ? ".exe" : "";
}

/**
 * True when `command` has to go through a shell to be found and started.
 *
 * There is no extension-less `pnpm` on Windows — the PATH entry is `pnpm.cmd` — and Node will
 * not start a `.cmd` directly: it either fails to resolve it, or resolves it and then refuses
 * it under the post-CVE-2024-27980 guard. A shell is what makes `cmd.exe` do the PATHEXT
 * lookup and run the shim, which is the supported route for a pnpm bin.
 *
 * An absolute `node.exe` needs none of that, so this is decided per command rather than set
 * once for a whole script: turning the shell on for something that does not need it moves that
 * argv into cmd.exe's grammar for no reason.
 */
export function needsShell(
	command: string,
	platform: NodeJS.Platform = process.platform,
	nodePath: string = process.execPath,
): boolean {
	return platform === "win32" && command !== nodePath;
}
