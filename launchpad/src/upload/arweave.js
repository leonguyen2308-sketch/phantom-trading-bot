// Permanent, free storage for coin images and metadata: Arweave through ar.io's Turbo upload
// service, which stores items under ~100 KiB for free. Each upload is an ANS-104 data item signed
// with a throwaway ed25519 key, so the launcher's wallet never signs anything for it. If Turbo
// refuses signed items, its unsigned free route is tried next.
import { ed25519 } from "@noble/curves/ed25519";

export const TURBO_URL = "https://upload.ardrive.io/v1";
export const GATEWAY_URL = "https://arweave.net";
export const FREE_UPLOAD_BYTES = 100 * 1024;

const SIGNATURE_TYPE_ED25519 = 2;
const utf8 = new TextEncoder();

function concat(...parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) {
    out.set(p, o);
    o += p.length;
  }
  return out;
}

async function sha(algorithm, data) {
  return new Uint8Array(await crypto.subtle.digest(algorithm, data));
}

function base64url(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function u16(v) {
  return Uint8Array.of(v & 0xff, v >> 8);
}
function u64(v) {
  const b = new Uint8Array(8);
  new DataView(b.buffer).setBigUint64(0, BigInt(v), true);
  return b;
}

// Arweave's deep hash (SHA-384), the message an ANS-104 data item signs.
export async function deepHash(data) {
  if (Array.isArray(data)) {
    let acc = await sha("SHA-384", concat(utf8.encode("list"), utf8.encode(String(data.length))));
    for (const chunk of data) acc = await sha("SHA-384", concat(acc, await deepHash(chunk)));
    return acc;
  }
  const tag = concat(utf8.encode("blob"), utf8.encode(String(data.byteLength)));
  return sha("SHA-384", concat(await sha("SHA-384", tag), await sha("SHA-384", data)));
}

// Avro long (zig-zag varint) for the non-negative counts and lengths tags use.
function avroLong(n) {
  let v = BigInt(n) * 2n;
  const out = [];
  while (v >= 0x80n) {
    out.push(Number((v & 0x7fn) | 0x80n));
    v >>= 7n;
  }
  out.push(Number(v));
  return Uint8Array.from(out);
}

// Tags as an Avro array of {name: bytes, value: bytes} records.
export function encodeTags(tags) {
  if (tags.length === 0) return new Uint8Array(0);
  const parts = [avroLong(tags.length)];
  for (const { name, value } of tags) {
    const n = utf8.encode(name);
    const v = utf8.encode(value);
    parts.push(avroLong(n.length), n, avroLong(v.length), v);
  }
  parts.push(Uint8Array.of(0));
  return concat(...parts);
}

// A signed ANS-104 data item. `secret` is a 32-byte ed25519 seed.
export async function createDataItem(data, tags, secret = ed25519.utils.randomPrivateKey()) {
  const owner = ed25519.getPublicKey(secret);
  const rawTags = encodeTags(tags);
  const empty = new Uint8Array(0);
  const message = await deepHash([
    utf8.encode("dataitem"),
    utf8.encode("1"),
    utf8.encode(String(SIGNATURE_TYPE_ED25519)),
    owner,
    empty, // no target
    empty, // no anchor
    rawTags,
    data,
  ]);
  const signature = ed25519.sign(message, secret);
  const bytes = concat(
    u16(SIGNATURE_TYPE_ED25519),
    signature,
    owner,
    Uint8Array.of(0), // target absent
    Uint8Array.of(0), // anchor absent
    u64(tags.length),
    u64(rawTags.length),
    rawTags,
    data,
  );
  return { id: base64url(await sha("SHA-256", signature)), bytes };
}

export class UploadError extends Error {}

async function postJson(url, init, fetchImpl) {
  const res = await fetchImpl(url, init);
  const text = await res.text();
  if (!res.ok) throw new UploadError(`upload refused (${res.status}): ${text.slice(0, 200)}`);
  try {
    return JSON.parse(text);
  } catch {
    throw new UploadError(`upload gave an unreadable answer: ${text.slice(0, 200)}`);
  }
}

// Stores `data` on Arweave and returns its id and gateway URL.
export async function uploadToArweave(data, contentType, { appName = "kimlaunch", fetchImpl = fetch, signal } = {}) {
  if (data.byteLength > FREE_UPLOAD_BYTES - 1024)
    throw new UploadError(`file is ${Math.ceil(data.byteLength / 1024)} KB; the free limit is about 99 KB`);
  const tags = [
    { name: "Content-Type", value: contentType },
    { name: "App-Name", value: appName },
  ];
  const failures = [];
  try {
    const item = await createDataItem(data, tags);
    const body = await postJson(
      `${TURBO_URL}/tx/solana`,
      { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: item.bytes, signal },
      fetchImpl,
    );
    const id = body.id || item.id;
    return { id, url: `${GATEWAY_URL}/${id}` };
  } catch (e) {
    if (signal?.aborted) throw e;
    failures.push(e.message);
  }
  try {
    const body = await postJson(
      `${TURBO_URL}/x402/upload/unsigned`,
      {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream", "x-data-item-tags": JSON.stringify(tags) },
        body: data,
        signal,
      },
      fetchImpl,
    );
    const id = body.id || body.receipt?.id;
    if (!id) throw new UploadError("upload answer had no id");
    return { id, url: `${GATEWAY_URL}/${id}` };
  } catch (e) {
    if (signal?.aborted) throw e;
    failures.push(e.message);
  }
  throw new UploadError(`could not store the file on Arweave: ${failures.join("; ")}`);
}

// The token metadata JSON wallets, Raydium and DEX screeners read.
export function buildMetadata({ name, symbol, description, image, imageType, twitter, telegram, website, createdOn }) {
  const socials = Object.fromEntries(
    Object.entries({ twitter, telegram, website }).filter(([, v]) => typeof v === "string" && v.trim()),
  );
  return {
    name,
    symbol,
    description: description || "",
    image,
    showName: true,
    createdOn,
    ...socials,
    ...(Object.keys(socials).length ? { extensions: socials } : {}),
    properties: { files: [{ uri: image, type: imageType }], category: "image" },
  };
}
