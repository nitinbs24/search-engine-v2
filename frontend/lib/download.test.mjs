import assert from "node:assert/strict";
import { test } from "node:test";

import { dispositionType, encodeKey, isSafeKey } from "./download.mjs";

test("keys with . or .. segments are rejected: they would resolve into another bucket", () => {
  for (const key of ["../other/secret.txt", "a/../../b", "a/./b", "..", "."]) {
    assert.equal(isSafeKey(key), false, key);
  }
});

test("ordinary keys are accepted, including dots inside a segment", () => {
  for (const key of ["a.txt", "reports/q1.pdf", "v1..2/notes.txt", "..hidden/file"]) {
    assert.equal(isSafeKey(key), true, key);
  }
});

test("keys are encoded the way AWS signature V4 requires", () => {
  assert.equal(encodeKey("reports/report (1)!*'.pdf"), "reports/report%20%281%29%21%2A%27.pdf");
  assert.equal(encodeKey("a/b c/d+e.txt"), "a/b%20c/d%2Be.txt");
});

test("only types that cannot run script are shown inline", () => {
  for (const type of ["image/png", "image/jpeg", "application/pdf", "text/plain; charset=utf-8"]) {
    assert.equal(dispositionType(type, false), "inline", type);
  }
  for (const type of ["text/html", "image/svg+xml", "application/xhtml+xml", "text/xml", "application/octet-stream", ""]) {
    assert.equal(dispositionType(type, false), "attachment", type);
  }
});

test("a requested download is always an attachment", () => {
  assert.equal(dispositionType("image/png", true), "attachment");
});
