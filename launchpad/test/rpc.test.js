// RPC fallbacks: refused, rate-limited or dead endpoints are skipped.
import { test } from "node:test";
import assert from "node:assert/strict";
import { failoverFetch, rpcEndpoints } from "../src/chain/rpc.js";

test("the site's RPC comes first, public ones after", () => {
  assert.deepEqual(rpcEndpoints("mainnet", "https://mine.example"), [
    "https://mine.example",
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
  ]);
  assert.deepEqual(rpcEndpoints("devnet", ""), ["https://api.devnet.solana.com"]);
});

test("skips endpoints that refuse, then sticks with the one that works", async () => {
  const calls = [];
  const fake = async (url) => {
    calls.push(url);
    if (url === "a") throw new TypeError("Failed to fetch"); // CORS refusal looks like this
    if (url === "b") return new Response("", { status: 403 });
    return new Response('{"ok":1}', { status: 200 });
  };
  const f = failoverFetch(["a", "b", "c"], fake);
  assert.equal((await f("ignored", { body: "{}" })).status, 200);
  assert.deepEqual(calls, ["a", "b", "c"]);
  calls.length = 0;
  await f("ignored", { body: "{}" });
  assert.deepEqual(calls, ["c"], "remembers the endpoint that worked");
});

test("when every endpoint is rate limited, hands back the last answer for the client's own retry", async () => {
  const f = failoverFetch(["a", "b"], async () => new Response("", { status: 429 }));
  assert.equal((await f("x", {})).status, 429);
  const dead = failoverFetch(["a"], async () => {
    throw new TypeError("offline");
  });
  await assert.rejects(dead("x", {}), /offline/);
});
