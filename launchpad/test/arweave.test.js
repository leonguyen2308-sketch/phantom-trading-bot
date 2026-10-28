// Our ANS-104 data items, checked by arbundles (the reference implementation Turbo uses).
import { test } from "node:test";
import assert from "node:assert/strict";
import { DataItem } from "@dha-team/arbundles";
import { createDataItem, encodeTags, uploadToArweave, buildMetadata, GATEWAY_URL } from "../src/upload/arweave.js";

const tags = [
  { name: "Content-Type", value: "image/webp" },
  { name: "App-Name", value: "kimlaunch" },
];

test("data items verify with arbundles", async () => {
  for (const size of [0, 1, 300, 50_000, 99_000]) {
    const data = new Uint8Array(size).map((_, i) => (i * 31 + size) & 0xff);
    const item = await createDataItem(data, tags);
    const parsed = new DataItem(Buffer.from(item.bytes));
    assert.equal(await parsed.isValid(), true, `structure, ${size} bytes`);
    assert.equal(await DataItem.verify(Buffer.from(item.bytes)), true, `signature, ${size} bytes`);
    assert.equal(parsed.signatureType, 2);
    assert.equal(parsed.id, item.id);
    assert.deepEqual(parsed.tags, tags);
    assert.equal(Buffer.from(parsed.rawData).equals(Buffer.from(data)), true);
  }
});

test("tags encode like arbundles, including unicode and empty values", async () => {
  const odd = [
    { name: "Title", value: "Supreme 🚀 Leader" },
    { name: "Empty", value: "" },
    { name: "Long", value: "x".repeat(300) },
  ];
  const item = await createDataItem(new Uint8Array([1, 2, 3]), odd);
  const parsed = new DataItem(Buffer.from(item.bytes));
  assert.deepEqual(parsed.tags, odd);
  assert.equal(await DataItem.verify(Buffer.from(item.bytes)), true);
  assert.equal(encodeTags([]).length, 0);
});

test("upload posts a signed item, then falls back to the unsigned route", async () => {
  const calls = [];
  const fake = async (url, init) => {
    calls.push({ url, init });
    if (url.endsWith("/tx/solana")) return new Response("Insufficient balance", { status: 402 });
    return Response.json({ id: "unsignedId123" });
  };
  const out = await uploadToArweave(new Uint8Array([7, 7, 7]), "application/json", { fetchImpl: fake });
  assert.equal(out.id, "unsignedId123");
  assert.equal(out.url, `${GATEWAY_URL}/unsignedId123`);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, "https://upload.ardrive.io/v1/tx/solana");
  assert.equal(await DataItem.verify(Buffer.from(calls[0].init.body)), true);
  assert.equal(calls[1].url, "https://upload.ardrive.io/v1/x402/upload/unsigned");
  assert.deepEqual(JSON.parse(calls[1].init.headers["x-data-item-tags"])[0], { name: "Content-Type", value: "application/json" });

  const ok = await uploadToArweave(new Uint8Array([1]), "image/png", {
    fetchImpl: async (url, init) => Response.json({ id: new DataItem(Buffer.from(init.body)).id }),
  });
  assert.match(ok.url, /^https:\/\/arweave\.net\/[A-Za-z0-9_-]{43}$/);

  await assert.rejects(
    uploadToArweave(new Uint8Array(120 * 1024), "image/png", { fetchImpl: fake }),
    /free limit/,
  );
  await assert.rejects(
    uploadToArweave(new Uint8Array(10), "image/png", { fetchImpl: async () => new Response("down", { status: 503 }) }),
    /could not store/,
  );
});

test("metadata keeps only the socials that were given", () => {
  const m = buildMetadata({
    name: "Kim",
    symbol: "KIM",
    description: "",
    image: "https://arweave.net/abc",
    imageType: "image/webp",
    twitter: "https://x.com/kim",
    telegram: " ",
    createdOn: "https://kimlaunch.fun",
  });
  assert.equal(m.twitter, "https://x.com/kim");
  assert.equal("telegram" in m, false);
  assert.deepEqual(m.extensions, { twitter: "https://x.com/kim" });
  assert.equal(m.properties.files[0].uri, "https://arweave.net/abc");
});
