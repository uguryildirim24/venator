/**
 * The one place SQLite output becomes domain values.
 *
 * SQLite is dynamically typed, so a column's declared affinity is a convention rather
 * than a guarantee: every value is checked here and a mismatch names the column it came
 * from, which is how a drift between build/venator.db and coordination/CONTRACTS.md
 * surfaces as a readable error instead of a wrong render. Prototype identity stands in
 * for `typeof` (banned by the anti-slop rules) and narrows just as precisely.
 */

export type SqlValue = null | number | bigint | string | Uint8Array;

export type SqlRow = Record<string, SqlValue>;

/** Thrown when the view's data does not match the contract the dashboard reads against. */
export class ViewDataError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "ViewDataError";
	}
}

function isText(value: SqlValue): value is string {
	return value !== null && Object.getPrototypeOf(value) === String.prototype;
}

function isNumeric(value: SqlValue): value is number {
	return value !== null && Object.getPrototypeOf(value) === Number.prototype;
}

function column(row: SqlRow, name: string): SqlValue {
	const value = row[name];
	if (value === undefined) {
		throw new ViewDataError(`Column \`${name}\` is missing from the result row.`);
	}
	return value;
}

export function textColumn(row: SqlRow, name: string): string {
	const value = column(row, name);
	if (!isText(value)) {
		throw new ViewDataError(`Column \`${name}\` holds a non-text value; TEXT expected.`);
	}
	return value;
}

export function optionalTextColumn(row: SqlRow, name: string): string | null {
	const value = column(row, name);
	if (value === null) return null;
	if (!isText(value)) {
		throw new ViewDataError(`Column \`${name}\` holds a non-text value; TEXT or NULL expected.`);
	}
	return value;
}

export function integerColumn(row: SqlRow, name: string): number {
	const value = column(row, name);
	if (!isNumeric(value)) {
		throw new ViewDataError(`Column \`${name}\` holds a non-numeric value; INTEGER expected.`);
	}
	return value;
}

export function optionalIntegerColumn(row: SqlRow, name: string): number | null {
	const value = column(row, name);
	if (value === null) return null;
	if (!isNumeric(value)) {
		throw new ViewDataError(
			`Column \`${name}\` holds a non-numeric value; INTEGER or NULL expected.`,
		);
	}
	return value;
}

export function booleanColumn(row: SqlRow, name: string): boolean {
	return integerColumn(row, name) !== 0;
}

/** Reads a column that the contract restricts to a fixed set of words. */
export function memberColumn<Member extends string>(
	row: SqlRow,
	name: string,
	allowed: readonly Member[],
): Member {
	const value = textColumn(row, name);
	const member = allowed.find((candidate) => candidate === value);
	if (member === undefined) {
		throw new ViewDataError(
			`Column \`${name}\` holds "${value}"; expected one of ${allowed.join(", ")}.`,
		);
	}
	return member;
}
