// Browser wallets through the Wallet Standard (Phantom, Solflare, Backpack, ...), with Phantom's
// injected provider as a fallback for old extensions.
import { getWallets } from "@wallet-standard/app";
import { PublicKey, VersionedTransaction } from "@solana/web3.js";

const CONNECT = "standard:connect";
const DISCONNECT = "standard:disconnect";
const EVENTS = "standard:events";
const SIGN_TX = "solana:signTransaction";
const SIGN_AND_SEND = "solana:signAndSendTransaction";

export const chainId = (network) => (network === "devnet" ? "solana:devnet" : "solana:mainnet");

export class WalletError extends Error {}

function isRejection(e) {
  const text = `${e?.name ?? ""} ${e?.message ?? ""} ${e?.code ?? ""}`;
  return /reject|denied|cancel|4001|declined|closed/i.test(text);
}

function wrapError(e, what) {
  if (isRejection(e)) return new WalletError(`you cancelled the ${what} in your wallet`);
  return new WalletError(`your wallet could not ${what}: ${e?.message ?? e}`);
}

class StandardWallet {
  constructor(wallet) {
    this.wallet = wallet;
    this.name = wallet.name;
    this.icon = wallet.icon;
    this.account = null;
  }
  get publicKey() {
    return this.account ? new PublicKey(this.account.address) : null;
  }
  async connect({ silent = false } = {}) {
    try {
      const { accounts } = await this.wallet.features[CONNECT].connect({ silent });
      const all = accounts?.length ? accounts : this.wallet.accounts;
      this.account = all.find((a) => a.chains?.some((c) => c.startsWith("solana:"))) ?? all[0] ?? null;
    } catch (e) {
      throw wrapError(e, "connect");
    }
    if (!this.account) throw new WalletError(silent ? "not authorized yet" : "your wallet shared no Solana account");
    return this.publicKey;
  }
  async disconnect() {
    this.account = null;
    await this.wallet.features[DISCONNECT]?.disconnect?.().catch(() => {});
  }
  onChange(listener) {
    const events = this.wallet.features[EVENTS];
    if (!events) return () => {};
    return events.on("change", ({ accounts }) => {
      if (!accounts) return;
      const next = accounts.find((a) => a.chains?.some((c) => c.startsWith("solana:"))) ?? accounts[0] ?? null;
      this.account = next;
      listener(this.publicKey);
    });
  }
  async signTransactions(txs, network) {
    const chain = chainId(network);
    try {
      const outputs = await this.wallet.features[SIGN_TX].signTransaction(
        ...txs.map((tx) => ({ account: this.account, transaction: tx.serialize(), chain })),
      );
      return outputs.map((o) => VersionedTransaction.deserialize(o.signedTransaction));
    } catch (e) {
      throw wrapError(e, "sign the transaction");
    }
  }
  get canSignAndSend() {
    return Boolean(this.wallet.features[SIGN_AND_SEND]);
  }
  // Single-signer transactions only: the wallet may add its own safety instructions.
  async signAndSend(tx, network) {
    try {
      const [out] = await this.wallet.features[SIGN_AND_SEND].signAndSendTransaction({
        account: this.account,
        transaction: tx.serialize(),
        chain: chainId(network),
        options: { preflightCommitment: "confirmed" },
      });
      return out.signature;
    } catch (e) {
      throw wrapError(e, "send the transaction");
    }
  }
}

class InjectedPhantom {
  constructor(provider) {
    this.provider = provider;
    this.name = "Phantom";
    this.icon = null;
  }
  get publicKey() {
    return this.provider.publicKey ? new PublicKey(this.provider.publicKey.toString()) : null;
  }
  async connect({ silent = false } = {}) {
    try {
      await this.provider.connect(silent ? { onlyIfTrusted: true } : undefined);
    } catch (e) {
      throw wrapError(e, "connect");
    }
    return this.publicKey;
  }
  async disconnect() {
    await this.provider.disconnect?.().catch?.(() => {});
  }
  onChange(listener) {
    const handler = (pk) => listener(pk ? new PublicKey(pk.toString()) : null);
    this.provider.on?.("accountChanged", handler);
    return () => this.provider.off?.("accountChanged", handler);
  }
  async signTransactions(txs) {
    try {
      return await this.provider.signAllTransactions(txs);
    } catch (e) {
      throw wrapError(e, "sign the transaction");
    }
  }
  get canSignAndSend() {
    return false;
  }
}

function supportsSolana(w) {
  return w.features?.[CONNECT] && w.features?.[SIGN_TX] && w.chains?.some((c) => c.startsWith("solana:"));
}

// Every wallet the browser offers, Phantom first.
export function availableWallets() {
  const found = getWallets().get().filter(supportsSolana).map((w) => new StandardWallet(w));
  if (!found.some((w) => /phantom/i.test(w.name)) && globalThis.phantom?.solana?.isPhantom) {
    found.push(new InjectedPhantom(globalThis.phantom.solana));
  }
  return found.sort((a, b) => Number(/phantom/i.test(b.name)) - Number(/phantom/i.test(a.name)));
}

export function onWalletsChanged(listener) {
  const api = getWallets();
  const off = [api.on("register", listener), api.on("unregister", listener)];
  return () => off.forEach((f) => f());
}

export function isMobile() {
  return /android|iphone|ipad|ipod/i.test(globalThis.navigator?.userAgent ?? "");
}

// Opens this page inside Phantom's in-app browser (for phones without an extension).
export function phantomBrowseLink(url = globalThis.location?.href) {
  return `https://phantom.app/ul/browse/${encodeURIComponent(url)}?ref=${encodeURIComponent(new URL(url).origin)}`;
}
