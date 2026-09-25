/** Shares the store-writing guard with scheduled refreshes. */
let active = false;

export function applicationIsActive(): boolean { return active; }
export function setApplicationActive(value: boolean): void { active = value; }
