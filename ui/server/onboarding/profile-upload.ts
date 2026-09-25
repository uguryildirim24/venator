import { MAXIMUM_PDF_BYTES } from "../../shared/onboarding.ts";
import { OnboardingError } from "./errors.ts";
import { validateReferencePdf } from "./resume-reference.ts";

export const MAXIMUM_PROFILE_JSON_BYTES = 512 * 1024;
export const MAXIMUM_PROFILE_UPLOAD_BYTES = MAXIMUM_PDF_BYTES + MAXIMUM_PROFILE_JSON_BYTES + 64 * 1024;

/** Bound actual bytes before the multipart parser buffers the complete upload. */
export async function readProfileUpload(request: Request): Promise<{ readonly json: string; readonly pdf: Uint8Array }> {
	const declared = Number(request.headers.get("content-length"));
	if (declared > MAXIMUM_PROFILE_UPLOAD_BYTES) throw new OnboardingError("body_too_large", "That profile upload is too large.");
	const reader = request.body?.getReader();
	if (reader === undefined) throw new OnboardingError("bad_request", "Attach a résumé PDF and profile details.");
	const chunks: Uint8Array[] = [];
	let size = 0;
	try {
		while (true) {
			const chunk = await reader.read();
			if (chunk.done) break;
			size += chunk.value.byteLength;
			if (size > MAXIMUM_PROFILE_UPLOAD_BYTES) {
				await reader.cancel();
				throw new OnboardingError("body_too_large", "That profile upload is too large.");
			}
			chunks.push(chunk.value);
		}
	} finally { reader.releaseLock(); }
	let form: FormData;
	try {
		form = await new Response(Buffer.concat(chunks), { headers: { "content-type": request.headers.get("content-type") ?? "" } }).formData();
	} catch { throw new OnboardingError("bad_request", "The profile upload could not be read."); }
	if ([...form.keys()].some((key) => key !== "proposal" && key !== "file") || form.getAll("proposal").length !== 1 || form.getAll("file").length !== 1) {
		throw new OnboardingError("bad_request", "Attach one résumé PDF and one set of profile details.");
	}
	const json = form.get("proposal");
	const file = form.get("file");
	if (json === null || json instanceof File || !(file instanceof File)) throw new OnboardingError("bad_request", "Attach a résumé PDF and profile details.");
	if (Buffer.byteLength(json) > MAXIMUM_PROFILE_JSON_BYTES || file.size > MAXIMUM_PDF_BYTES) {
		throw new OnboardingError("body_too_large", "That profile upload is too large.");
	}
	const pdf = new Uint8Array(await file.arrayBuffer());
	validateReferencePdf(pdf);
	return { json, pdf };
}
