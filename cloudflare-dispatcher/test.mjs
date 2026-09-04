import assert from "node:assert/strict";
import { run, vietnamDate } from "./src/index.js";

assert.equal(vietnamDate(new Date("2026-09-03T17:00:00Z")), "2026-09-04");

const originalFetch = globalThis.fetch;
let dispatchCalls = 0;

globalThis.fetch = async (url) => {
  if (url.includes("raw.githubusercontent.com")) {
    return Response.json({ last_success_run_date: "2026-09-04" });
  }
  dispatchCalls += 1;
  return new Response(null, { status: 204 });
};
await run({ GITHUB_OWNER: "o", GITHUB_REPO: "r", GITHUB_WORKFLOW: "w", GITHUB_TOKEN: "x" }, new Date("2026-09-04T01:00:00Z"));
assert.equal(dispatchCalls, 0);

globalThis.fetch = async (url) => {
  if (url.includes("raw.githubusercontent.com")) {
    return Response.json({ sent: {} });
  }
  dispatchCalls += 1;
  return new Response(null, { status: 204 });
};
await run({ GITHUB_OWNER: "o", GITHUB_REPO: "r", GITHUB_WORKFLOW: "w", GITHUB_TOKEN: "x" }, new Date("2026-09-04T01:00:00Z"));
assert.equal(dispatchCalls, 1);

globalThis.fetch = originalFetch;
console.log("tests passed");
