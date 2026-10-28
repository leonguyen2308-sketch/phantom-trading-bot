// End-to-end actions the UI calls: launch a coin, load a coin, buy and sell on its curve.
import { Keypair, PublicKey } from "@solana/web3.js";
import * as ll from "./launchlab.js";
import {
  LaunchError,
  buyInstructions,
  compileTx,
  launchInstructions,
  loadLaunchContext,
  packLaunch,
  sellInstructions,
  simulate,
  withUnits,
} from "./launch.js";
import { completeSignatures, sendAndConfirm } from "./send.js";
import { buildMetadata, uploadToArweave } from "../upload/arweave.js";

// Upload the image, then the metadata JSON that points at it.
export async function uploadCoinFiles({ image, coin, site }, onStep = () => {}) {
  onStep("image");
  const pic = await uploadToArweave(new Uint8Array(await image.blob.arrayBuffer()), image.type, { appName: site.name });
  onStep("metadata");
  const meta = buildMetadata({ ...coin, image: pic.url, imageType: image.type, createdOn: site.url });
  const json = await uploadToArweave(new TextEncoder().encode(JSON.stringify(meta)), "application/json", {
    appName: site.name,
  });
  return { imageUrl: pic.url, uri: json.url };
}

// Simulate first: a launch that would fail stops before the wallet opens, and the compute budget
// is sized from what the simulation used. If simulation itself is unavailable, carry on.
async function sized(connection, part, { payer, blockhash, lookupTables, microLamports }) {
  try {
    const sim = await simulate(connection, part.tx, payer);
    const tx = sim.unitsConsumed
      ? compileTx({ payer, blockhash, instructions: part.instructions, lookupTables, units: withUnits(sim.unitsConsumed), microLamports })
      : part.tx;
    return { ...part, tx, cost: sim.cost };
  } catch (e) {
    if (e instanceof LaunchError) throw e;
    return { ...part, cost: null };
  }
}

export async function launchCoin({ connection, wallet, cfg, coin, uri, devBuyLamports = 0n, slippageBps = 500, microLamports = 0, onStep = () => {}, onSent }) {
  onStep("build");
  const owner = wallet.publicKey;
  if (!owner) throw new LaunchError("connect your wallet first");
  const ctx = await loadLaunchContext(connection, {
    network: cfg.network,
    platform: cfg.platformId || undefined,
    config: cfg.configId || undefined,
  });
  const mint = Keypair.generate();
  const { create, buy, quote } = launchInstructions(ctx, {
    owner,
    mint: mint.publicKey,
    name: coin.name,
    symbol: coin.symbol,
    uri,
    devBuyLamports,
    slippageBps,
    fee: { wallet: cfg.feeWallet, lamports: cfg.feeLamports },
    migrateType: cfg.migrateType,
  });
  const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash("confirmed");
  const parts = packLaunch(ctx, { payer: owner, blockhash, create, buy, microLamports });
  parts[0] = await sized(connection, parts[0], { payer: owner, blockhash, lookupTables: ctx.lookupTables, microLamports });

  onStep("sign", { cost: parts[0].cost, transactions: parts.length });
  const signed = await wallet.signTransactions(
    parts.map((p) => p.tx),
    cfg.network,
  );
  // The wallet signs first; the new mint's key signs whatever the wallet approved.
  completeSignatures(signed[0], [mint]);
  for (const tx of signed.slice(1)) completeSignatures(tx);

  onStep("launch");
  const createSignature = await sendAndConfirm(connection, signed[0], { lastValidBlockHeight, onSent });
  let buySignature = null;
  let buyError = null;
  if (signed[1]) {
    onStep("buy");
    try {
      buySignature = await sendAndConfirm(connection, signed[1], { lastValidBlockHeight });
    } catch (e) {
      buyError = e;
    }
  }
  return { mint: mint.publicKey.toBase58(), createSignature, buySignature, buyError, quote };
}

// Name, symbol and uri from a Metaplex metadata account.
export function decodeMetadataAccount(data) {
  try {
    const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
    let o = 1 + 32 + 32;
    const text = () => {
      const n = view.getUint32(o, true);
      o += 4;
      const s = new TextDecoder().decode(data.subarray(o, o + n));
      o += n;
      return s.replace(/\0+$/g, "").trim();
    };
    const name = text();
    const symbol = text();
    const uri = text();
    return { name, symbol, uri };
  } catch {
    return null;
  }
}

// Everything the coin page needs from chain. `pool` is null for coins not on LaunchLab.
export async function loadCoin(connection, network, mint) {
  const program = ll.NETWORKS[network].program;
  const mintKey = new PublicKey(mint);
  const [poolInfo, metaInfo] = await connection.getMultipleAccountsInfo(
    [ll.poolPda(program, mintKey), ll.metadataPda(mintKey)],
    "confirmed",
  );
  const onchain = metaInfo ? decodeMetadataAccount(metaInfo.data) : null;
  if (!poolInfo || !poolInfo.owner.equals(program)) return { pool: null, ctx: null, onchain };
  const pool = ll.decodePool(poolInfo.data);
  const [configInfo, platformInfo] = await connection.getMultipleAccountsInfo([pool.configId, pool.platformId], "confirmed");
  if (!configInfo || !platformInfo) return { pool, ctx: null, onchain };
  const config = ll.decodeConfig(configInfo.data);
  const platform = ll.decodePlatform(platformInfo.data);
  return {
    pool,
    onchain,
    ctx: {
      network,
      program,
      configId: pool.configId,
      platformId: pool.platformId,
      config,
      platform,
      fees: ll.feesFrom(config, platform),
      lookupTables: [],
    },
  };
}

export async function tokenBalance(connection, owner, mint) {
  try {
    const res = await connection.getTokenAccountBalance(ll.ataAddress(owner, new PublicKey(mint)), "confirmed");
    return BigInt(res.value.amount);
  } catch {
    return 0n;
  }
}

// Buy (`amount` in lamports) or sell (`amount` in raw token units) on the bonding curve.
export async function trade({ connection, wallet, network, mint, coin, side, amount, slippageBps = 500, microLamports = 0, onStep = () => {} }) {
  const owner = wallet.publicKey;
  if (!owner) throw new LaunchError("connect your wallet first");
  const { pool, ctx } = coin;
  if (!pool || !ctx) throw new LaunchError("this coin has no bonding curve to trade on");
  if (pool.status !== ll.POOL_STATUS.trading) throw new LaunchError("this coin graduated; trade it on Raydium");
  if (amount <= 0n) throw new LaunchError("enter an amount");
  const mintKey = new PublicKey(mint);
  let instructions;
  let quote;
  if (side === "buy") {
    quote = ll.quoteBuy(pool, amount, ctx.fees);
    if (quote.amountA <= 0n) throw new LaunchError("that buys nothing; enter a bigger amount");
    instructions = buyInstructions(ctx, {
      owner,
      mint: mintKey,
      creator: pool.creator,
      lamports: quote.amountB,
      minTokens: ll.withSlippage(quote.amountA, slippageBps),
    });
  } else {
    quote = ll.quoteSell(pool, amount, ctx.fees);
    if (quote.amountB <= 0n) throw new LaunchError("that sells for nothing; enter a bigger amount");
    instructions = sellInstructions(ctx, {
      owner,
      mint: mintKey,
      creator: pool.creator,
      tokens: amount,
      minLamports: ll.withSlippage(quote.amountB, slippageBps),
    });
  }
  onStep("build");
  const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash("confirmed");
  const part = await sized(
    connection,
    { tx: compileTx({ payer: owner, blockhash, instructions, units: 300_000, microLamports }), instructions },
    { payer: owner, blockhash, lookupTables: [], microLamports },
  );
  onStep("sign");
  const [signed] = await wallet.signTransactions([part.tx], network);
  completeSignatures(signed);
  onStep("send");
  const signature = await sendAndConfirm(connection, signed, { lastValidBlockHeight });
  return { signature, quote };
}
