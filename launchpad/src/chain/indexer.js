// The board: every coin launched through this site, read from the fee wallet's history. A coin is
// listed only when one successful transaction both paid the launch fee and created its LaunchLab
// pool, so a transfer or memo sent to the fee wallet cannot list anything.
import bs58 from "bs58";
import { PublicKey } from "@solana/web3.js";
import * as ll from "./launchlab.js";

export function parseLaunch(tx, { program, feeWallet, minFeeLamports }) {
  if (!tx || tx.meta?.err) return null;
  const { instructions, accountKeys } = tx.transaction.message;
  let found = null;
  for (const ix of instructions) {
    if (!("data" in ix) || !ix.programId.equals(program) || ix.accounts.length < 18) continue;
    let data;
    try {
      data = bs58.decode(ix.data);
    } catch {
      continue;
    }
    const coin = ll.decodeInitializeV2Data(data);
    if (coin) found = { coin, creator: ix.accounts[1], mint: ix.accounts[6] };
  }
  if (!found) return null;
  const fee = feeWallet.toBase58();
  const paid = instructions.some(
    (ix) =>
      ix.program === "system" &&
      ix.parsed?.type === "transfer" &&
      ix.parsed.info.destination === fee &&
      BigInt(ix.parsed.info.lamports) >= minFeeLamports,
  );
  if (!paid) return null;
  return {
    signature: tx.transaction.signatures[0],
    slot: tx.slot,
    time: tx.blockTime ?? null,
    mint: found.mint.toBase58(),
    creator: found.creator.toBase58(),
    payer: accountKeys[0].pubkey.toBase58(),
    name: found.coin.name,
    symbol: found.coin.symbol,
    uri: found.coin.uri,
  };
}

async function parsedTransactions(connection, signatures, chunk = 10) {
  const out = [];
  for (let i = 0; i < signatures.length; i += chunk) {
    const part = signatures.slice(i, i + chunk);
    try {
      out.push(
        ...(await connection.getParsedTransactions(part, { maxSupportedTransactionVersion: 0, commitment: "confirmed" })),
      );
    } catch {
      // Some RPCs refuse batches: fall back to one request per transaction.
      for (const sig of part) {
        out.push(
          await connection
            .getParsedTransaction(sig, { maxSupportedTransactionVersion: 0, commitment: "confirmed" })
            .catch(() => null),
        );
      }
    }
  }
  return out;
}

// One page of the fee wallet's history, newest first. `until` stops at a signature already seen,
// `before` continues further back.
export async function fetchLaunches(connection, opts, { before, until, limit = 100, skip = new Set() } = {}) {
  const sigs = await connection.getSignaturesForAddress(opts.feeWallet, { before, until, limit }, "confirmed");
  const wanted = sigs.filter((s) => !s.err && !skip.has(s.signature)).map((s) => s.signature);
  const txs = await parsedTransactions(connection, wanted);
  const launches = txs.map((tx) => parseLaunch(tx, opts)).filter(Boolean);
  return {
    launches,
    scanned: sigs.map((s) => s.signature),
    oldest: sigs.length ? sigs[sigs.length - 1].signature : null,
    done: sigs.length < limit,
  };
}

// Current curve state of each coin's pool, keyed by mint.
export async function fetchPools(connection, program, mints) {
  const out = new Map();
  for (let i = 0; i < mints.length; i += 100) {
    const part = mints.slice(i, i + 100);
    const infos = await connection.getMultipleAccountsInfo(
      part.map((m) => ll.poolPda(program, new PublicKey(m))),
      "confirmed",
    );
    infos.forEach((info, j) => {
      if (info && info.owner.equals(program)) {
        try {
          out.set(part[j], ll.decodePool(info.data));
        } catch {
          // Not a pool account.
        }
      }
    });
  }
  return out;
}

// Only http(s) links leave this function; ipfs:// and ar:// become gateway URLs.
export function safeUrl(value) {
  if (typeof value !== "string") return null;
  let v = value.trim();
  if (v.startsWith("ipfs://")) v = `https://ipfs.io/ipfs/${v.slice(7).replace(/^ipfs\//, "")}`;
  if (v.startsWith("ar://")) v = `https://arweave.net/${v.slice(5)}`;
  try {
    const u = new URL(v);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
}

// A social handle or link as a URL: "@kim" on X becomes https://x.com/kim.
export function socialUrl(kind, value) {
  if (typeof value !== "string" || !value.trim()) return null;
  const v = value.trim();
  if (/^https?:\/\//i.test(v)) return safeUrl(v);
  const handle = v.replace(/^@/, "");
  if (!/^[A-Za-z0-9_.-]{1,64}$/.test(handle)) return null;
  if (kind === "twitter") return `https://x.com/${handle}`;
  if (kind === "telegram") return `https://t.me/${handle}`;
  return safeUrl(`https://${v}`);
}

const metaCache = new Map();

// The coin's metadata JSON (image, description, socials), or null if it cannot be read.
export async function fetchCoinMeta(uri, { fetchImpl = fetch, timeoutMs = 8000 } = {}) {
  const url = safeUrl(uri);
  if (!url) return null;
  if (metaCache.has(url)) return metaCache.get(url);
  const pending = (async () => {
    try {
      const res = await fetchImpl(url, { signal: AbortSignal.timeout(timeoutMs) });
      if (!res.ok) return null;
      const json = await res.json();
      return {
        image: safeUrl(json.image),
        description: typeof json.description === "string" ? json.description.slice(0, 600) : "",
        twitter: socialUrl("twitter", json.twitter ?? json.extensions?.twitter),
        telegram: socialUrl("telegram", json.telegram ?? json.extensions?.telegram),
        website: socialUrl("website", json.website ?? json.extensions?.website),
      };
    } catch {
      return null;
    }
  })();
  metaCache.set(url, pending);
  const result = await pending;
  if (!result) metaCache.delete(url);
  return result;
}
