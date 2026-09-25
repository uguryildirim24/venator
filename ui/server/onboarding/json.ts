/**
 * The value model every onboarding payload is read through.
 *
 * A request body arrives as text and becomes a `JsonValue` — never `unknown`, never `any`
 * leaking past this module. Every reader below returns a narrowed value or null, so the
 * validators downstream never guess at a shape: a caller asks for the mapping at a key and
 * gets a mapping or nothing.
 */

export type JsonValue = null | boolean | number | string | readonly JsonValue[] | JsonMapping;

export type JsonMapping = { readonly [key: string]: JsonValue };

/**
 * Keys that would reach an object's prototype rather than its contents. A payload naming one
 * is refused outright rather than sanitised, because a Profile that quietly lost a key is
 * worse than one that was never written.
 */
export const RESERVED_KEYS: readonly string[] = ["__proto__", "constructor", "prototype"];

/** Thrown when a body is not JSON at all. Callers turn it into a 400 with their own wording. */
export class JsonParseError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "JsonParseError";
	}
}

/**
 * Parses request text into the value model.
 *
 * `JSON.parse` produces exactly this shape by construction — JSON has no non-finite numbers,
 * no undefined, and no cycles — so the annotation is a statement of what the parser can
 * return, not a claim layered over an unchecked value.
 */
export function parseJson(text: string): JsonValue {
	try {
		const parsed: JsonValue = JSON.parse(text);
		return parsed;
	} catch (cause) {
		throw new JsonParseError(cause instanceof Error ? cause.message : "unparseable JSON");
	}
}

function isMapping(value: JsonValue): value is JsonMapping {
	return value !== null && Object.getPrototypeOf(value) === Object.prototype;
}

function isList(value: JsonValue): value is readonly JsonValue[] {
	return value !== null && Array.isArray(value);
}

function isText(value: JsonValue): value is string {
	return value !== null && Object.getPrototypeOf(value) === String.prototype;
}

function isNumber(value: JsonValue): value is number {
	return value !== null && Object.getPrototypeOf(value) === Number.prototype;
}

function isBoolean(value: JsonValue): value is boolean {
	return value !== null && Object.getPrototypeOf(value) === Boolean.prototype;
}

export function asMapping(value: JsonValue | undefined): JsonMapping | null {
	return value !== undefined && isMapping(value) ? value : null;
}

export function asList(value: JsonValue | undefined): readonly JsonValue[] | null {
	return value !== undefined && isList(value) ? value : null;
}

export function asText(value: JsonValue | undefined): string | null {
	return value !== undefined && isText(value) ? value : null;
}

export function asNumber(value: JsonValue | undefined): number | null {
	return value !== undefined && isNumber(value) ? value : null;
}

export function asBoolean(value: JsonValue | undefined): boolean | null {
	return value !== undefined && isBoolean(value) ? value : null;
}

/** Reads one own key. A key inherited from the prototype chain is not a value in the payload. */
export function at(mapping: JsonMapping, key: string): JsonValue | undefined {
	return Object.hasOwn(mapping, key) ? mapping[key] : undefined;
}

/** Own keys in the order the payload wrote them, which is the order the YAML file keeps. */
export function keysOf(mapping: JsonMapping): readonly string[] {
	return Object.keys(mapping);
}

/** True when the value is present and is not JSON null. */
export function isPresent(value: JsonValue | undefined): boolean {
	return value !== undefined && value !== null;
}
