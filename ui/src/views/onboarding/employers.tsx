import { CircleCheck, CircleX, LoaderCircle, Minus, Search } from "lucide-react";
import { useCallback, useRef, useState } from "react";

import type { BoardEntry, ResolvedBoard } from "../../../shared/onboarding.ts";
import { sourceLabel } from "../../labels.ts";
import { OnboardingRequestError, resolveBoards } from "../../onboarding/api.ts";
import { addedCount, boardLine, removeEmployer, TARGETING, titleHintNote } from "./copy.ts";

export function boardKey(board: { readonly source: string; readonly board: string }): string {
	return `${board.source}:${board.board}`;
}

type Lookup =
	| { readonly kind: "idle" }
	| { readonly kind: "looking" }
	| { readonly kind: "refused"; readonly error: OnboardingRequestError }
	| { readonly kind: "found"; readonly titleHint: string | null; readonly boards: readonly ResolvedBoard[] };

type EmployerPickerProps = {
	readonly boards: readonly BoardEntry[];
	readonly onChange: (boards: readonly BoardEntry[]) => void;
};

/**
 * Employers to watch: paste a careers page, name what it finds, Add; remove with the minus.
 *
 * There is no directory of employers to search, so the search field takes the employer's own
 * careers page and `venator.discover.register` works out which job board it names and asks
 * that board whether it answers. **The name is always typed by the person.** Dedup reads it
 * from `sources.names`, and a page title is never a name — it is shown only as a hint. A board
 * that did not answer is listed, never dropped, and is added only if the person adds it.
 *
 * One component for two callers: step three holds the list in the draft, and the first-run
 * rescue commits it to an existing Profile. What happens to the list is the caller's.
 */
export function EmployerPicker({ boards, onChange }: EmployerPickerProps) {
	const [query, setQuery] = useState("");
	const [lookup, setLookup] = useState<Lookup>({ kind: "idle" });
	const [names, setNames] = useState<Readonly<Record<string, string>>>({});
	const request = useRef(0);

	const lookUp = useCallback(() => {
		if (query.trim() === "" || lookup.kind === "looking") return;
		const current = ++request.current;
		setLookup({ kind: "looking" });
		resolveBoards(query.trim())
			.then((response) => {
				if (request.current === current) setLookup({ kind: "found", titleHint: response.titleHint, boards: response.boards });
			})
			.catch((error: Error) => {
				if (request.current === current) setLookup({
					kind: "refused",
					error: error instanceof OnboardingRequestError ? error : new OnboardingRequestError(error.message, null, null, null),
				});
			});
	}, [lookup.kind, query]);

	const clear = useCallback(() => {
		request.current++;
		setQuery("");
		setLookup({ kind: "idle" });
		setNames({});
	}, []);

	const added = new Set(boards.map(boardKey));
	const found = lookup.kind === "found" ? lookup.boards.filter((board) => !added.has(boardKey(board))) : [];

	const add = (board: ResolvedBoard) => {
		const name = (names[boardKey(board)] ?? "").trim();
		if (name === "") return;
		onChange([...boards, { source: board.source, board: board.board, confirmed: board.confirmed, name }]);
	};

	return (
		<section className="setup-section" aria-label={TARGETING.employers}>
			<h2 className="group-header">
				{TARGETING.employers}
				<span className="group-count">{addedCount(boards.length)}</span>
			</h2>
			<div className="setup-group">
				<div className="search-row">
					<div className="search-field">
						<Search aria-hidden="true" className="icon" />
						<input
							type="url"
							aria-label={TARGETING.search}
							placeholder={TARGETING.search}
							value={query}
							onChange={(event) => {
								request.current++;
								setQuery(event.target.value);
								setLookup({ kind: "idle" });
								setNames({});
							}}
							onKeyDown={(event) => {
								if (event.key === "Enter") lookUp();
							}}
						/>
						{query === "" ? null : (
							<button type="button" className="icon-button" aria-label="Clear" title="Clear" onClick={clear}>
								<CircleX aria-hidden="true" className="icon" />
							</button>
						)}
					</div>
					{query.trim() === "" ? null : (
						<button type="button" className="button" data-size="large" disabled={lookup.kind === "looking"} onClick={lookUp}>
							{TARGETING.lookUp}
						</button>
					)}
				</div>
				{lookup.kind === "looking" ? (
					<p className="setup-row" role="status">
						<LoaderCircle aria-hidden="true" className="icon spinner" />
						{TARGETING.lookingUp}
					</p>
				) : null}
				{lookup.kind === "refused" ? (
					<div className="setup-row" role="alert">
						<span className="setup-row-lines">
							<span>{lookup.error.message}</span>
							{lookup.error.remedy === null ? null : <span className="setup-row-subtitle">{lookup.error.remedy}</span>}
						</span>
					</div>
				) : null}
				{lookup.kind === "found" && lookup.boards.length === 0 ? <p className="setup-row">{TARGETING.nothingFound}</p> : null}
				{found.map((board) => {
					const key = boardKey(board);
					const name = names[key] ?? "";
					const source = sourceLabel(board.source);
					return (
						<div key={key} className="setup-row">
							<span className="setup-row-lines">
								<input
									className="text-field name-field"
									aria-label={`${TARGETING.namePlaceholder}, ${source}`}
									placeholder={TARGETING.namePlaceholder}
									value={name}
									onChange={(event) => setNames((current) => ({ ...current, [key]: event.target.value }))}
									onKeyDown={(event) => {
										if (event.key === "Enter") add(board);
									}}
								/>
								<span className="setup-row-subtitle">{boardLine(source, board.confirmed, board.postingCount, board.complete !== false)}</span>
							</span>
							<button type="button" className="button" disabled={name.trim() === ""} onClick={() => add(board)}>
								{TARGETING.add}
							</button>
						</div>
					);
				})}
				{lookup.kind === "found" && lookup.titleHint !== null && found.length > 0 ? (
					<p className="setup-row setup-row-subtitle">{titleHintNote(lookup.titleHint)}</p>
				) : null}
			</div>
			{boards.length === 0 ? null : (
				<div className="setup-group">
					{boards.map((board) => (
						<div key={boardKey(board)} className="setup-row">
							<span className="setup-row-lines">
								<span className="setup-row-title">{board.name}</span>
								<span className="setup-row-subtitle">{`${sourceLabel(board.source)} job board`}</span>
							</span>
							<span className="setup-row-trail">
								{board.confirmed ? (
									<span className="ready">
										<CircleCheck aria-hidden="true" className="icon" />
										{TARGETING.ready}
									</span>
								) : (
									<span className="ready">{TARGETING.notChecked}</span>
								)}
								<button
									type="button"
									className="icon-button"
									aria-label={removeEmployer(board.name)}
									title={removeEmployer(board.name)}
									onClick={() => onChange(boards.filter((entry) => boardKey(entry) !== boardKey(board)))}
								>
									<Minus aria-hidden="true" className="icon" />
								</button>
							</span>
						</div>
					))}
				</div>
			)}
		</section>
	);
}
