import { useState } from "react";
import { trainAction, type TrainPreview, type TrainComparison } from "../api.ts";
import { Sheet } from "../components/sheet.tsx";

/** Every training upload and every adapter swap is a separate Owner press. */
export function TrainStatus({ onSettled }: { readonly onSettled: () => void }) {
	const [open, setOpen] = useState(false);
	const [csv, setCsv] = useState<string | null>(null);
	const [preview, setPreview] = useState<TrainPreview | null>(null);
	const [comparison, setComparison] = useState<TrainComparison | null>(null);
	const [busy, setBusy] = useState(false);
	const [error, setError] = useState<string | null>(null);
	const [choice, setChoice] = useState<string | null>(null);
	async function act(action: "preview" | "train" | "pick", pick?: "old" | "new") {
		if (busy) return;
		setBusy(true);
		setError(null);
		try {
			const result = await trainAction({ action, csv, sha256: preview?.sha256, choice: pick });
			if (action === "preview") { setPreview(result); setComparison(null); }
			if (action === "train") setComparison(result);
			if (action === "pick") { setChoice(pick ?? null); onSettled(); }
		} catch (failure) { setError(failure instanceof Error ? failure.message : "Training failed."); }
		finally { setBusy(false); }
	}
	return (
		<>
			<button type="button" className="link-button" onClick={() => setOpen(true)}>Retrain</button>
			<Sheet open={open} onOpenChange={setOpen} title="Retrain keep model">
				<label>Private label CSV <input type="file" accept=".csv,text/csv" onChange={(event) => {
					setPreview(null); setComparison(null); setChoice(null); setCsv(null);
					const file = event.currentTarget.files?.[0];
					if (file) void file.text().then(setCsv).catch(() => setError("Could not read the label file."));
				}} /></label>
				<button type="button" className="button" disabled={busy || csv === null} onClick={() => void act("preview")}>Show upload</button>
				{preview === null ? null : <div className="sheet-notes">
					<p>{preview.training} training Postings · {preview.holdout} holdout Postings · {preview.training_bytes} training bytes · {preview.holdout_bytes} holdout bytes</p>
					<p>Leaves the Mac: {preview.leaves_mac.join("; ")}</p>
					<p>Stays on the Mac: {preview.stays_mac.join("; ")}. Not uploaded: {preview.not_uploaded.join("; ")}.</p>
					<button type="button" className="button" disabled={busy || comparison !== null} onClick={() => void act("train")}>Upload and train this payload</button>
				</div>}
				{comparison === null ? null : <div className="sheet-notes">
					<p>Current: {comparison.old.correct} of {comparison.old.total} holdout labels right</p>
					<p>New: {comparison.new.correct} of {comparison.new.total} holdout labels right</p>
					<button type="button" className="button" disabled={busy || choice !== null} onClick={() => void act("pick", "old")}>Keep current</button>
					<button type="button" className="button" disabled={busy || choice !== null} onClick={() => void act("pick", "new")}>Use new</button>
				</div>}
				{choice === null ? null : <p>{choice === "new" ? "New adapter selected. Score will process the current Hard Filter passes in batches." : "Current adapter kept."}</p>}
				{error === null ? null : <p role="alert">{error}</p>}
			</Sheet>
		</>
	);
}
