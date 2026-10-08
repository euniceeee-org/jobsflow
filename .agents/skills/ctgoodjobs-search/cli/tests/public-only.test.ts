import { afterEach, expect, test } from "bun:test";
import { resolveHeaders } from "../src/helpers";

const originalFetch = globalThis.fetch;
const names = ["JOBSFLOW_PUBLIC_ONLY", "CTGOOD_SID", "CTGOOD_VISITOR_ID", "CTGOOD_CHANNEL_ID"];
const previous = Object.fromEntries(names.map(name => [name, process.env[name]]));
afterEach(() => {
  globalThis.fetch = originalFetch;
  for (const name of names) {
    if (previous[name] === undefined) delete process.env[name];
    else process.env[name] = previous[name];
  }
});

test("public-only CT search ignores personal session header bindings", async () => {
  process.env.JOBSFLOW_PUBLIC_ONLY = "1";
  process.env.CTGOOD_SID = "synthetic-personal-sid";
  process.env.CTGOOD_VISITOR_ID = "synthetic-personal-visitor";
  process.env.CTGOOD_CHANNEL_ID = "99";
  let requests = 0;
  globalThis.fetch = (async () => {
    requests++;
    return new Response("", { headers: { "set-cookie": "sid=public-sid; Path=/, visitor_id=public-visitor; Path=/" } });
  }) as typeof fetch;
  const headers = await resolveHeaders();
  expect(requests).toBe(1);
  expect(headers.sid).toBe("public-sid");
  expect(headers["channel-id"]).toBe("1");
  expect(JSON.stringify(headers)).not.toContain("synthetic-personal");
});
