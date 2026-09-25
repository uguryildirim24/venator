import { Ban, CircleCheck, Info } from "lucide-react";
import { useEffect, useRef } from "react";

export type CellStatus = {
	readonly text: string;
	readonly glyph: "ready" | "info" | "excluded" | null;
};

export type Cell = {
	readonly key: string;
	readonly title: string;
	readonly meta: string;
	readonly time: string;
	/** A full date for the time's tooltip. */
	readonly stamp: string;
	readonly isNew: boolean;
	readonly status: CellStatus | null;
};

export type CellGroup = {
	readonly key: string;
	readonly header: string;
	readonly cells: readonly Cell[];
};

function StatusGlyph({ glyph }: { readonly glyph: CellStatus["glyph"] }) {
	if (glyph === "ready") return <CircleCheck aria-hidden="true" className="glyph" data-tone="green" />;
	if (glyph === "info") return <Info aria-hidden="true" className="glyph" />;
	if (glyph === "excluded") return <Ban aria-hidden="true" className="glyph" />;
	return null;
}

type CellListProps = {
	readonly label: string;
	readonly groups: readonly CellGroup[];
	readonly selectedKey: string | null;
	readonly onSelect: (key: string) => void;
	/** Advances when the keyboard moved the selection, so the selected cell takes focus. */
	readonly keyboardMoves: number;
};

/**
 * The list column: day headers and cells, in the order the list was read.
 *
 * A cell is a button, so selecting it with the pointer gives the list focus, and focus is what
 * turns the selection ember (`.list-scroll:focus-within`). While focus is in the Posting the
 * selection is the inactive grey, as a Mac source list is. Moving with the keyboard focuses the
 * selected cell for the same reason.
 */
export function CellList({ label, groups, selectedKey, onSelect, keyboardMoves }: CellListProps) {
	const selected = useRef<HTMLButtonElement | null>(null);
	useEffect(() => {
		if (keyboardMoves === 0) return;
		selected.current?.focus({ preventScroll: true });
		selected.current?.scrollIntoView({ block: "nearest" });
	}, [keyboardMoves]);

	return (
		<div role="listbox" aria-label={label}>
			{groups.map((group) => (
				<div key={group.key} className="day" role="group" aria-label={group.header}>
					<h3 className="day-header">{group.header}</h3>
					{group.cells.map((cell) => {
						const isSelected = cell.key === selectedKey;
						return (
							<button
								key={cell.key}
								ref={isSelected ? selected : undefined}
								type="button"
								role="option"
								aria-selected={isSelected}
								className="cell"
								onClick={() => onSelect(cell.key)}
							>
								{cell.isNew ? <span className="cell-dot" aria-label="New" role="img" /> : null}
								<span className="cell-title">{cell.title}</span>
								{cell.time === "" ? null : (
									<time className="cell-time" title={cell.stamp}>
										{cell.time}
									</time>
								)}
								<span className="cell-meta">{cell.meta}</span>
								{cell.status === null ? null : (
									<span className="cell-status">
										<StatusGlyph glyph={cell.status.glyph} />
										{cell.status.text}
									</span>
								)}
							</button>
						);
					})}
				</div>
			))}
		</div>
	);
}
