import assert from "node:assert/strict";
import test from "node:test";

import { nextPostingArrival } from "../src/views/posting-arrival.ts";

test("Posting entry is one-shot and fast selection changes skip rather than queue", () => {
	const initial = { key: "first", at: 0, animate: false };
	const second = nextPostingArrival(initial, "second", 200);
	assert.equal(second.animate, true);
	assert.equal(nextPostingArrival(second, "second", 205), second);
	const third = nextPostingArrival(second, "third", 230);
	assert.equal(third.animate, false);
	assert.equal(nextPostingArrival(third, "fourth", 250).animate, false);
	assert.equal(nextPostingArrival(third, "fourth", 420).animate, true);
});
