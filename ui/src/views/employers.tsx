import type { SourceHealth, SummaryResponse } from "../../shared/contracts.ts";
import type { RequestState } from "../api.ts";
import { Toolbar } from "../components/toolbar.tsx";
import { formatAgo, formatMoment } from "../format.ts";
import { sourceHealthStatusLabel, sourceLabel } from "../labels.ts";

/** The employer a board belongs to, or where it was found when the Profile names none. */
function employerOf(source: SourceHealth): string {
	const named = source.employer?.trim() ?? "";
	return named === "" ? `via ${sourceLabel(source.key.split(":", 1)[0] ?? source.key)}` : named;
}

const HEALTHY = new Set(["ok", "healthy", "success"]);

/** What the last check left known, when the view measured it. */
function discoveryNote(source: SourceHealth): string {
	const status = source.status.toLowerCase();
	if (status === "partial") return "Incomplete; these are the Postings known so far";
	if (status === "failed") return "The last check failed; Postings already found are kept";
	if (status === "skipped") return "Skipped; these are the Postings known so far";
	if (status === "stale") return "Needs a refresh";
	return "";
}

type EmployersProps = {
	readonly summary: RequestState<SummaryResponse>;
	readonly sidebarHidden: boolean;
	readonly onShowSidebar: () => void;
};

/**
 * Source health, one row per board the Profile checks: whether the last check worked, when
 * one last did, and how many of the Postings it found have a full, verified description.
 * Status is words; the last success is a relative time with the moment in its tooltip.
 */
export function EmployersView({ summary, sidebarHidden, onShowSidebar }: EmployersProps) {
	const sources = summary.status === "ready" ? (summary.value.sources ?? []) : [];
	const healthy = sources.filter((source) => HEALTHY.has(source.status)).length;
	return (
		<>
			<Toolbar
				title="Employers"
				subtitle={summary.status === "ready" ? `${sources.length.toLocaleString("en-US")} boards, ${healthy.toLocaleString("en-US")} healthy` : null}
				search={null}
				sidebarHidden={sidebarHidden}
				onShowSidebar={onShowSidebar}
				layout="single"
			/>
			<section className="table-view" aria-label="Employers">
				<div className="table-scroll">
					{summary.status === "error" ? (
						<div className="empty">
							<p className="empty-title">Source health could not be read</p>
							<p>{summary.message}</p>
						</div>
					) : null}
					{summary.status === "ready" && sources.length === 0 ? (
						<div className="empty">
							<p className="empty-title">No board has been checked yet</p>
							<p>A board appears here after the first fetch that reaches it. Refresh in the sidebar starts one.</p>
						</div>
					) : null}
					{sources.length === 0 ? null : (
						<table className="table" aria-label="Boards and their last check">
							<thead>
								<tr>
									<th scope="col">Employer</th>
									<th scope="col">Board</th>
									<th scope="col">Status</th>
									<th scope="col">Last success</th>
									<th scope="col">Postings known</th>
									<th scope="col">Need a detail check</th>
									<th scope="col" className="table-fill">Note</th>
								</tr>
							</thead>
							<tbody>
								{sources.map((source) => (
									<tr key={source.key}>
										<td>{employerOf(source)}</td>
										<td className="secondary">{sourceLabel(source.key.split(":", 1)[0] ?? source.key)}</td>
										<td>{sourceHealthStatusLabel(source.status)}</td>
										<td className="secondary" title={source.lastSuccessAt === null ? undefined : formatMoment(source.lastSuccessAt)}>
											{formatAgo(source.lastSuccessAt)}
										</td>
										<td className="tabular">{source.coverage === null ? "" : source.coverage.knownJobs.toLocaleString("en-US")}</td>
										<td className="tabular">{source.coverage === null ? "" : source.coverage.needsDetailCheck.toLocaleString("en-US")}</td>
										<td className="secondary table-fill" title={source.message ?? undefined}>
											{discoveryNote(source)}
										</td>
									</tr>
								))}
							</tbody>
						</table>
					)}
				</div>
			</section>
		</>
	);
}
