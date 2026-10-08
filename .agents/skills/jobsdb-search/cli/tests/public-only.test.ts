import { afterEach, expect, test } from "bun:test";
import { searchGet } from "../src/helpers";

const originalFetch = globalThis.fetch;
const originalPublic = process.env.JOBSFLOW_PUBLIC_ONLY;
const originalCookie = process.env.JOBSDB_COOKIE;
afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalPublic === undefined) delete process.env.JOBSFLOW_PUBLIC_ONLY;
  else process.env.JOBSFLOW_PUBLIC_ONLY = originalPublic;
  if (originalCookie === undefined) delete process.env.JOBSDB_COOKIE;
  else process.env.JOBSDB_COOKIE = originalCookie;
});

test("public-only search never inherits a personal cookie bridge", async () => {
  process.env.JOBSFLOW_PUBLIC_ONLY = "1";
  process.env.JOBSDB_COOKIE = "synthetic-personal-cookie-must-not-be-used";
  let observed: HeadersInit | undefined;
  globalThis.fetch = (async (_url, init) => {
    observed = init?.headers;
    return new Response('{"data":[]}', { status: 200 });
  }) as typeof fetch;
  await searchGet({ keywords: "graduate" }, { includeSearchCookie: true });
  expect(new Headers(observed).has("Cookie")).toBe(false);
});
