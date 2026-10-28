// Builds the launch: one transaction that pays the site's launch fee, creates the coin and its
// LaunchLab bonding curve, and (optionally) makes the creator's first buy. When the first buy does
// not fit in one transaction it goes in a second one, signed in the same wallet prompt.
import {
  AddressLookupTableAccount,
  ComputeBudgetProgram,
  PublicKey,
  SystemProgram,
  TransactionMessage,
  VersionedTransaction,
} from "@solana/web3.js";
import * as ll from "./launchlab.js";

export const MAX_TX_BYTES = 1232;
export const MAX_COMPUTE_UNITS = 1_400_000;
// Used when simulation is unavailable; launches use roughly half of these.
const FALLBACK_UNITS = { single: 600_000, create: 400_000, buy: 300_000 };

export const LIMITS = { name: 32, symbol: 10, uri: 200 };

export class LaunchError extends Error {
  constructor(message, { logs, cause } = {}) {
    super(message, { cause });
    this.logs = logs;
  }
}

// Everything a launch reads from chain: the curve config, the platform and Raydium's lookup table.
export async function loadLaunchContext(connection, { network = "mainnet", platform, config } = {}) {
  const net = ll.NETWORKS[network];
  if (!net) throw new LaunchError(`unknown network ${network}`);
  const platformId = platform ? new PublicKey(platform) : net.platform;
  const configId = config ? new PublicKey(config) : net.config;
  const [configInfo, platformInfo, lutInfo] = await connection.getMultipleAccountsInfo(
    [configId, platformId, net.lookupTable],
    "confirmed",
  );
  if (!configInfo || !configInfo.owner.equals(net.program))
    throw new LaunchError(`LaunchLab config ${configId.toBase58()} was not found on ${network}`);
  if (!platformInfo || !platformInfo.owner.equals(net.program))
    throw new LaunchError(`LaunchLab platform ${platformId.toBase58()} was not found on ${network}`);
  const cfg = ll.decodeConfig(configInfo.data);
  if (!cfg.mintB.equals(ll.WSOL_MINT)) throw new LaunchError("the LaunchLab config is not a SOL config");
  if (cfg.curveType !== 0) throw new LaunchError("only constant-product LaunchLab configs are supported");
  const plat = ll.decodePlatform(platformInfo.data);
  const lookupTables = [];
  if (lutInfo) {
    try {
      lookupTables.push(
        new AddressLookupTableAccount({ key: net.lookupTable, state: AddressLookupTableAccount.deserialize(lutInfo.data) }),
      );
    } catch {
      // A missing or malformed table only makes transactions bigger.
    }
  }
  return {
    network,
    program: net.program,
    configId,
    platformId,
    config: cfg,
    platform: plat,
    fees: ll.feesFrom(cfg, plat),
    lookupTables,
  };
}

export function validateCoin({ name, symbol, uri }) {
  const bytes = (s) => new TextEncoder().encode(s).length;
  if (!name || !name.trim()) throw new LaunchError("give your coin a name");
  if (bytes(name) > LIMITS.name) throw new LaunchError(`name is too long (max ${LIMITS.name} bytes)`);
  if (!symbol || !symbol.trim()) throw new LaunchError("give your coin a ticker");
  if (bytes(symbol) > LIMITS.symbol) throw new LaunchError(`ticker is too long (max ${LIMITS.symbol} characters)`);
  if (uri !== undefined && bytes(uri) > LIMITS.uri) throw new LaunchError("metadata link is too long");
}

// The instructions of a launch, split into the create part and the optional first buy.
export function launchInstructions(
  ctx,
  { owner, mint, name, symbol, uri, devBuyLamports = 0n, slippageBps = 500, fee, migrateType = "cpmm" },
) {
  validateCoin({ name, symbol, uri });
  const create = [];
  if (fee && fee.lamports > 0n) {
    create.push(SystemProgram.transfer({ fromPubkey: owner, toPubkey: fee.wallet, lamports: fee.lamports }));
  }
  create.push(
    ll.initializeV2Ix({
      program: ctx.program,
      payer: owner,
      creator: owner,
      config: ctx.configId,
      platform: ctx.platformId,
      mintA: mint,
      name,
      symbol,
      uri,
      migrateType,
      platformAllowConfig: ctx.platform.restrictGlobalConfig
        ? ll.platformAllowConfigPda(ctx.program, ctx.platformId, ctx.configId)
        : undefined,
      platformCurveRule: ctx.platform.restrictCurveParam
        ? ll.platformCurveRulePda(ctx.program, ctx.platformId, ctx.configId)
        : undefined,
    }),
  );

  const buy = [];
  let quote = null;
  if (devBuyLamports > 0n) {
    quote = ll.quoteBuy(ll.newPoolState(ctx.config), devBuyLamports, ctx.fees);
    buy.push(
      ...buyInstructions(ctx, {
        owner,
        mint,
        creator: owner,
        lamports: quote.amountB,
        minTokens: ll.withSlippage(quote.amountA, slippageBps),
      }),
    );
  }
  return { create, buy, quote };
}

// Wrap SOL, buy on the curve, unwrap what is left (and get the wrapped-SOL account's rent back).
export function buyInstructions(ctx, { owner, mint, creator, lamports, minTokens }) {
  const wsol = ll.ataAddress(owner, ll.WSOL_MINT);
  return [
    ll.createAtaIdempotentIx(owner, owner, mint),
    ll.createAtaIdempotentIx(owner, owner, ll.WSOL_MINT),
    SystemProgram.transfer({ fromPubkey: owner, toPubkey: wsol, lamports }),
    ll.syncNativeIx(wsol),
    ll.buyExactInIx({
      program: ctx.program,
      owner,
      config: ctx.configId,
      platform: ctx.platformId,
      mintA: mint,
      creator,
      amountB: lamports,
      minAmountA: minTokens,
    }),
    ll.closeAccountIx(wsol, owner, owner),
  ];
}

export function sellInstructions(ctx, { owner, mint, creator, tokens, minLamports }) {
  const wsol = ll.ataAddress(owner, ll.WSOL_MINT);
  return [
    ll.createAtaIdempotentIx(owner, owner, ll.WSOL_MINT),
    ll.sellExactInIx({
      program: ctx.program,
      owner,
      config: ctx.configId,
      platform: ctx.platformId,
      mintA: mint,
      creator,
      amountA: tokens,
      minAmountB: minLamports,
    }),
    ll.closeAccountIx(wsol, owner, owner),
  ];
}

export function compileTx({ payer, blockhash, instructions, lookupTables = [], units, microLamports = 0 }) {
  const budget = [ComputeBudgetProgram.setComputeUnitLimit({ units })];
  if (microLamports > 0) budget.push(ComputeBudgetProgram.setComputeUnitPrice({ microLamports }));
  const message = new TransactionMessage({
    payerKey: payer,
    recentBlockhash: blockhash,
    instructions: [...budget, ...instructions],
  }).compileToV0Message(lookupTables);
  return new VersionedTransaction(message);
}

export function txBytes(tx) {
  try {
    return tx.serialize().length;
  } catch {
    // web3.js refuses to serialize anything past the packet size.
    return Infinity;
  }
}

// One transaction when it fits, else create + buy as two.
export function packLaunch(ctx, { payer, blockhash, create, buy, microLamports }) {
  const compile = (instructions, units) =>
    compileTx({ payer, blockhash, instructions, lookupTables: ctx.lookupTables, units, microLamports });
  if (buy.length) {
    const single = compile([...create, ...buy], FALLBACK_UNITS.single);
    if (txBytes(single) <= MAX_TX_BYTES) return [{ tx: single, instructions: [...create, ...buy], units: FALLBACK_UNITS.single }];
  }
  const parts = [{ tx: compile(create, FALLBACK_UNITS.create), instructions: create, units: FALLBACK_UNITS.create }];
  if (buy.length) parts.push({ tx: compile(buy, FALLBACK_UNITS.buy), instructions: buy, units: FALLBACK_UNITS.buy });
  for (const p of parts) {
    if (txBytes(p.tx) > MAX_TX_BYTES) throw new LaunchError("the launch transaction is too large; shorten the name or ticker");
  }
  return parts;
}

// Runs the transaction against current chain state, without signatures. Returns the compute units
// used and what it costs the payer, or throws with the program logs when it would fail.
export async function simulate(connection, tx, payer) {
  const [balance, sim] = await Promise.all([
    connection.getBalance(payer, "confirmed"),
    connection.simulateTransaction(tx, {
      sigVerify: false,
      replaceRecentBlockhash: true,
      commitment: "confirmed",
      accounts: { encoding: "base64", addresses: [payer.toBase58()] },
    }),
  ]);
  const { err, logs, unitsConsumed, accounts } = sim.value;
  if (err) throw new LaunchError(explainFailure(err, logs, balance), { logs });
  const after = accounts?.[0]?.lamports;
  return { unitsConsumed: unitsConsumed ?? null, cost: typeof after === "number" ? balance - after : null, balance };
}

export function explainFailure(err, logs = [], balance) {
  const text = (logs || []).join("\n");
  if (/insufficient lamports|insufficient funds/i.test(text) || JSON.stringify(err).includes("InsufficientFunds"))
    return `not enough SOL in your wallet${balance !== undefined ? ` (you have ${(balance / 1e9).toFixed(4)} SOL)` : ""}`;
  if (/AccountNotFound|no record of a prior credit/i.test(text + JSON.stringify(err)))
    return "your wallet has no SOL on this network yet";
  if (/slippage|ExceededSlippage|TooLittleOutputReceived/i.test(text)) return "the price moved too much; try again or raise slippage";
  const programError = [...(logs || [])].reverse().find((l) => /Error Message:|failed:/.test(l));
  return programError ? programError.replace(/^Program log: /, "") : `transaction would fail: ${JSON.stringify(err)}`;
}

export const withUnits = (units) => Math.min(MAX_COMPUTE_UNITS, Math.ceil(units * 1.2) + 10_000);
