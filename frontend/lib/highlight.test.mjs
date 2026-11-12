import test from "node:test";
import assert from "node:assert/strict";
import { splitHighlight } from "./highlight.mjs";

test("splits the snippet around the highlighted sentence", () => {
  assert.deepEqual(splitHighlight("One. Two here. Three.", "Two here."), { before: "One. ", match: "Two here.", after: " Three." });
});

test("no highlight, or one that is not in the snippet, means no split", () => {
  assert.equal(splitHighlight("One. Two.", ""), null);
  assert.equal(splitHighlight("One. Two.", undefined), null);
  assert.equal(splitHighlight("One. Two.", "Other."), null);
  assert.equal(splitHighlight("", "x"), null);
});
