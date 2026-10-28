// Sending signed transactions and waiting for the outcome. A transaction that has not confirmed
// is never reported as failed until its blockhash has expired.
import bs58 from "bs58";
import { explainFailure } from "./launch.js";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class TxError extends Error {
  constructor(message, { signature, logs, unknown = false } = {}) {
    super(message);
    this.signature = signature;
    this.logs = logs;
    // True when the outcome is not known yet: the transaction may still land.
    this.unknown = unknown;
  }
}

// Adds the signatures only the app holds (the new mint) to a wallet-signed transaction, then
// checks nobody is missing.
export function completeSignatures(tx, signers = []) {
  if (signers.length) tx.sign(signers);
  const required = tx.message.header.numRequiredSignatures;
  for (let i = 0; i < required; i++) {
    if (!tx.signatures[i] || tx.signatures[i].every((b) => b === 0)) {
      throw new TxError(`missing signature from ${tx.message.staticAccountKeys[i].toBase58()}`);
    }
  }
  return tx;
}

async function failureLogs(connection, signature) {
  try {
    const tx = await connection.getTransaction(signature, { maxSupportedTransactionVersion: 0, commitment: "confirmed" });
    return tx?.meta?.logMessages ?? undefined;
  } catch {
    return undefined;
  }
}

// Waits until `signature` is confirmed. Rebroadcasts `raw` (when given) while waiting.
export async function confirm(connection, signature, { raw, lastValidBlockHeight, pollMs = 1500, giveUpMs = 180_000 } = {}) {
  const started = Date.now();
  let resentAt = started;
  for (;;) {
    await sleep(pollMs);
    let status = null;
    try {
      ({
        value: [status],
      } = await connection.getSignatureStatuses([signature]));
    } catch {
      // RPC hiccup: keep waiting.
    }
    if (status?.err) {
      const logs = await failureLogs(connection, signature);
      throw new TxError(explainFailure(status.err, logs), { signature, logs });
    }
    if (status?.confirmationStatus === "confirmed" || status?.confirmationStatus === "finalized") return signature;

    if (raw && Date.now() - resentAt > 4000) {
      resentAt = Date.now();
      connection.sendRawTransaction(raw, { skipPreflight: true, maxRetries: 0 }).catch(() => {});
    }
    if (lastValidBlockHeight) {
      const height = await connection.getBlockHeight("confirmed").catch(() => 0);
      if (height > lastValidBlockHeight) {
        const {
          value: [final],
        } = await connection.getSignatureStatuses([signature], { searchTransactionHistory: true }).catch(() => ({ value: [null] }));
        if (final && !final.err && final.confirmationStatus) return signature;
        if (final?.err) throw new TxError(explainFailure(final.err, await failureLogs(connection, signature)), { signature });
        throw new TxError("the network dropped the transaction before it landed; nothing was charged, try again", { signature });
      }
    }
    if (Date.now() - started > giveUpMs) {
      throw new TxError("still waiting for the network to confirm; check the explorer before trying again", {
        signature,
        unknown: true,
      });
    }
  }
}

export async function sendAndConfirm(connection, tx, { lastValidBlockHeight, onSent } = {}) {
  const raw = tx.serialize();
  const signature = bs58.encode(tx.signatures[0]);
  try {
    await connection.sendRawTransaction(raw, { skipPreflight: false, preflightCommitment: "confirmed", maxRetries: 0 });
  } catch (e) {
    const message = e?.message ?? "";
    if (/blockhash not found/i.test(message)) {
      // The RPC node is behind; the transaction itself is fine.
      await connection.sendRawTransaction(raw, { skipPreflight: true, maxRetries: 0 }).catch(() => {});
    } else if (!/already (been )?processed/i.test(message)) {
      const logs = e?.logs ?? (await e?.getLogs?.(connection).catch(() => undefined));
      throw new TxError(explainFailure(message || "rejected", logs), { signature, logs });
    }
  }
  onSent?.(signature);
  return confirm(connection, signature, { raw, lastValidBlockHeight });
}
