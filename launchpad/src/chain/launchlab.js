// Raydium LaunchLab: addresses, account decoding, instructions and bonding-curve math.
// Mirrors @raydium-io/raydium-sdk-v2 (src/raydium/launchpad); test/launchlab.test.js checks
// every builder byte for byte and the curve math number for number against the SDK.
import { Buffer } from "buffer";
import { PublicKey, SystemProgram, TransactionInstruction } from "@solana/web3.js";

export const TOKEN_PROGRAM = new PublicKey("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
export const ATA_PROGRAM = new PublicKey("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL");
export const METADATA_PROGRAM = new PublicKey("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s");
export const RENT_SYSVAR = new PublicKey("SysvarRent111111111111111111111111111111111");
export const WSOL_MINT = new PublicKey("So11111111111111111111111111111111111111112");

// Raydium's own deployments. `platform` is Raydium's default LaunchLab platform and `config`
// is the SOL constant-product curve config (curve type 0, index 0).
export const NETWORKS = {
  mainnet: {
    program: new PublicKey("LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"),
    platform: new PublicKey("4Bu96XjU84XjPDSpveTVf6LYGCkfW5FK7SNkREWcEfV4"),
    config: new PublicKey("6s1xP3hpbAfFoNtUNF8mfHsjr2Bd97JxFJRWLbL6aHuX"),
    lookupTable: new PublicKey("AcL1Vo8oy1ULiavEcjSUcwfBSForXMudcZvDZy5nzJkU"),
  },
  devnet: {
    program: new PublicKey("DRay6fNdQ5J82H7xV6uq2aV3mNrUZ1J4PgSKsWgptcm6"),
    platform: new PublicKey("2Jx4KTDrVSdWNazuGpcA8n3ZLTRGGBDxAWhuKe2Xcj2a"),
    config: new PublicKey("7ZR4zD7PYfY2XxoG1Gxcy2EgEeGYrpxrwzPuwdUBssEt"),
    lookupTable: new PublicKey("EFhMuDw1PKEuckuFRW9PavNfTH4LKP5uKHgyXDmWpFCq"),
  },
};

// The launch Raydium's own UI uses: 1B tokens (6 decimals), 79.31% sold on the curve,
// graduation at 85 SOL raised.
export const TOKEN_DECIMALS = 6;
export const SOL_DECIMALS = 9;
export const DEFAULT_CURVE = {
  supply: 1_000_000_000_000_000n,
  totalSellA: 793_100_000_000_000n,
  totalFundRaisingB: 85_000_000_000n,
};
export const FEE_DENOMINATOR = 1_000_000n;
export const MIGRATE_TYPES = { amm: 0, cpmm: 1 };
export const CPMM_CREATOR_FEE_ON_TOKEN_B = 0; // creator fees after graduation are paid in SOL
export const POOL_STATUS = { trading: 0, migrating: 1, migrated: 2 };

const DISCRIMINATOR = {
  initializeV2: Uint8Array.of(67, 153, 175, 39, 218, 16, 38, 32),
  buyExactIn: Uint8Array.of(250, 234, 13, 123, 213, 156, 19, 236),
  sellExactIn: Uint8Array.of(149, 39, 222, 155, 211, 124, 152, 26),
};

const utf8 = new TextEncoder();
const pda = (seeds, programId) => PublicKey.findProgramAddressSync(seeds, programId)[0];
const seed = (s) => Buffer.from(s, "utf8");

export const authPda = (program) => pda([seed("vault_auth_seed")], program);
export const eventAuthorityPda = (program) => pda([seed("__event_authority")], program);
export const poolPda = (program, mintA, mintB = WSOL_MINT) =>
  pda([seed("pool"), mintA.toBuffer(), mintB.toBuffer()], program);
export const vaultPda = (program, pool, mint) => pda([seed("pool_vault"), pool.toBuffer(), mint.toBuffer()], program);
export const platformVaultPda = (program, platform, mintB = WSOL_MINT) =>
  pda([platform.toBuffer(), mintB.toBuffer()], program);
export const creatorVaultPda = (program, creator, mintB = WSOL_MINT) =>
  pda([creator.toBuffer(), mintB.toBuffer()], program);
export const platformAllowConfigPda = (program, platform, config) =>
  pda([seed("platform_allow_config"), platform.toBuffer(), config.toBuffer()], program);
export const platformCurveRulePda = (program, platform, config) =>
  pda([seed("platform_curve_rule"), platform.toBuffer(), config.toBuffer()], program);
export const metadataPda = (mint) =>
  pda([seed("metadata"), METADATA_PROGRAM.toBuffer(), mint.toBuffer()], METADATA_PROGRAM);
export const ataAddress = (owner, mint, tokenProgram = TOKEN_PROGRAM) =>
  pda([owner.toBuffer(), tokenProgram.toBuffer(), mint.toBuffer()], ATA_PROGRAM);

// ---- encoding -------------------------------------------------------------------------------

function u8(v) {
  return Uint8Array.of(v);
}
function u32(v) {
  const b = new Uint8Array(4);
  new DataView(b.buffer).setUint32(0, v, true);
  return b;
}
function u64(v) {
  const b = new Uint8Array(8);
  new DataView(b.buffer).setBigUint64(0, BigInt(v), true);
  return b;
}
function str(s) {
  const bytes = utf8.encode(s);
  return [u32(bytes.length), bytes];
}
function bytes(...parts) {
  return Buffer.concat(parts.flat().map((p) => Buffer.from(p)));
}

const meta = (pubkey, isSigner, isWritable) => ({ pubkey, isSigner, isWritable });

// ---- decoding -------------------------------------------------------------------------------

function reader(data) {
  const dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
  return {
    u8: (o) => dv.getUint8(o),
    u16: (o) => dv.getUint16(o, true),
    u64: (o) => dv.getBigUint64(o, true),
    key: (o) => new PublicKey(data.subarray(o, o + 32)),
    text: (o, n) =>
      new TextDecoder()
        .decode(data.subarray(o, o + n))
        .replace(/\0+$/, "")
        .trim(),
  };
}

export function decodeConfig(data) {
  const r = reader(data);
  return {
    curveType: r.u8(16),
    index: r.u16(17),
    migrateFee: r.u64(19),
    tradeFeeRate: r.u64(27),
    maxShareFeeRate: r.u64(35),
    minSupplyA: r.u64(43),
    maxLockRate: r.u64(51),
    minSellRateA: r.u64(59),
    minMigrateRateA: r.u64(67),
    minFundRaisingB: r.u64(75),
    mintB: r.key(83),
  };
}

export function decodePlatform(data) {
  const r = reader(data);
  return {
    platformClaimFeeWallet: r.key(16),
    feeRate: r.u64(104),
    name: r.text(112, 64),
    web: r.text(176, 256),
    img: r.text(432, 256),
    creatorFeeRate: r.u64(720),
    // Older platform accounts end before these flags; missing means "not restricted".
    restrictGlobalConfig: data.length > 832 ? r.u8(832) : 0,
    restrictCurveParam: data.length > 833 ? r.u8(833) : 0,
  };
}

export function decodePool(data) {
  const r = reader(data);
  return {
    status: r.u8(17),
    mintDecimalsA: r.u8(18),
    mintDecimalsB: r.u8(19),
    migrateType: r.u8(20),
    supply: r.u64(21),
    totalSellA: r.u64(29),
    virtualA: r.u64(37),
    virtualB: r.u64(45),
    realA: r.u64(53),
    realB: r.u64(61),
    totalFundRaisingB: r.u64(69),
    migrateFee: r.u64(93),
    configId: r.key(141),
    platformId: r.key(173),
    mintA: r.key(205),
    mintB: r.key(237),
    vaultA: r.key(269),
    vaultB: r.key(301),
    creator: r.key(333),
  };
}

// The initializeV2 data: decimals, name, symbol, uri, curve params, vesting, creator-fee mode.
export function decodeInitializeV2Data(data) {
  const buf = Buffer.from(data);
  if (buf.length < 8 || !DISCRIMINATOR.initializeV2.every((b, i) => buf[i] === b)) return null;
  let o = 8;
  const decimals = buf[o++];
  const readStr = () => {
    const n = buf.readUInt32LE(o);
    o += 4;
    const s = buf.subarray(o, o + n).toString("utf8");
    o += n;
    return s;
  };
  const name = readStr();
  const symbol = readStr();
  const uri = readStr();
  return { decimals, name, symbol, uri };
}

// ---- instructions ---------------------------------------------------------------------------

export function initializeV2Ix({
  program,
  payer,
  creator,
  config,
  platform,
  mintA,
  name,
  symbol,
  uri,
  decimals = TOKEN_DECIMALS,
  curve = DEFAULT_CURVE,
  migrateType = "cpmm",
  cpmmCreatorFeeOn = CPMM_CREATOR_FEE_ON_TOKEN_B,
  platformAllowConfig,
  platformCurveRule,
}) {
  const pool = poolPda(program, mintA);
  const keys = [
    meta(payer, true, true),
    meta(creator, false, false),
    meta(config, false, false),
    meta(platform, false, false),
    meta(authPda(program), false, false),
    meta(pool, false, true),
    meta(mintA, true, true),
    meta(WSOL_MINT, false, false),
    meta(vaultPda(program, pool, mintA), false, true),
    meta(vaultPda(program, pool, WSOL_MINT), false, true),
    meta(metadataPda(mintA), false, true),
    meta(TOKEN_PROGRAM, false, false),
    meta(TOKEN_PROGRAM, false, false),
    meta(METADATA_PROGRAM, false, false),
    meta(SystemProgram.programId, false, false),
    meta(RENT_SYSVAR, false, false),
    meta(eventAuthorityPda(program), false, false),
    meta(program, false, false),
  ];
  if (platformAllowConfig) keys.push(meta(platformAllowConfig, false, false));
  if (platformCurveRule) keys.push(meta(platformCurveRule, false, false));

  const data = bytes(
    DISCRIMINATOR.initializeV2,
    u8(decimals),
    str(name),
    str(symbol),
    str(uri),
    // Constant-product curve (index 0).
    u8(0),
    u64(curve.supply),
    u64(curve.totalSellA),
    u64(curve.totalFundRaisingB),
    u8(MIGRATE_TYPES[migrateType]),
    // No vesting: total locked, cliff and unlock periods are zero.
    u64(0),
    u64(0),
    u64(0),
    u8(cpmmCreatorFeeOn),
  );
  return new TransactionInstruction({ programId: program, keys, data });
}

function tradeIx(kind, { program, owner, config, platform, mintA, creator, amountIn, minAmountOut }) {
  const pool = poolPda(program, mintA);
  const keys = [
    meta(owner, true, true),
    meta(authPda(program), false, false),
    meta(config, false, false),
    meta(platform, false, false),
    meta(pool, false, true),
    meta(ataAddress(owner, mintA), false, true),
    meta(ataAddress(owner, WSOL_MINT), false, true),
    meta(vaultPda(program, pool, mintA), false, true),
    meta(vaultPda(program, pool, WSOL_MINT), false, true),
    meta(mintA, false, false),
    meta(WSOL_MINT, false, false),
    meta(TOKEN_PROGRAM, false, false),
    meta(TOKEN_PROGRAM, false, false),
    meta(eventAuthorityPda(program), false, false),
    meta(program, false, false),
    meta(SystemProgram.programId, false, false),
    meta(platformVaultPda(program, platform), false, true),
    meta(creatorVaultPda(program, creator), false, true),
  ];
  // amount in, minimum out, share fee rate (no share fee).
  const data = bytes(DISCRIMINATOR[kind], u64(amountIn), u64(minAmountOut), u64(0));
  return new TransactionInstruction({ programId: program, keys, data });
}

// Spend `amountB` lamports of WSOL for at least `minAmountA` tokens.
export const buyExactInIx = ({ amountB, minAmountA, ...rest }) =>
  tradeIx("buyExactIn", { ...rest, amountIn: amountB, minAmountOut: minAmountA });

// Sell `amountA` tokens for at least `minAmountB` lamports of WSOL.
export const sellExactInIx = ({ amountA, minAmountB, ...rest }) =>
  tradeIx("sellExactIn", { ...rest, amountIn: amountA, minAmountOut: minAmountB });

export function createAtaIdempotentIx(payer, owner, mint, tokenProgram = TOKEN_PROGRAM) {
  return new TransactionInstruction({
    programId: ATA_PROGRAM,
    keys: [
      meta(payer, true, true),
      meta(ataAddress(owner, mint, tokenProgram), false, true),
      meta(owner, false, false),
      meta(mint, false, false),
      meta(SystemProgram.programId, false, false),
      meta(tokenProgram, false, false),
    ],
    data: Buffer.from([1]),
  });
}

export function syncNativeIx(account) {
  return new TransactionInstruction({ programId: TOKEN_PROGRAM, keys: [meta(account, false, true)], data: Buffer.from([17]) });
}

export function closeAccountIx(account, destination, owner) {
  return new TransactionInstruction({
    programId: TOKEN_PROGRAM,
    keys: [meta(account, false, true), meta(destination, false, true), meta(owner, true, false)],
    data: Buffer.from([9]),
  });
}

// ---- curve math (constant product, curve type 0) ---------------------------------------------

const ceilDiv = (a, b) => (a === 0n ? 0n : (a + b - 1n) / b);

// Virtual reserves a new pool starts with.
export function initialReserves({ supply, totalSellA, totalFundRaisingB, totalLockedAmount = 0n, migrateFee }) {
  if (supply <= totalSellA) throw new Error("supply must exceed the amount sold on the curve");
  const migrated = supply - totalSellA - totalLockedAmount;
  if (migrated <= 0n) throw new Error("nothing left to migrate");
  const raised = totalFundRaisingB - migrateFee;
  if (raised <= 0n) throw new Error("migrate fee exceeds the raise");
  const numerator = (raised * totalSellA * totalSellA) / migrated;
  const denominator = (raised * totalSellA) / migrated - totalFundRaisingB;
  const virtualA = numerator / denominator;
  const virtualB = (totalFundRaisingB * totalFundRaisingB) / denominator;
  if (virtualA < 0n || virtualB < 0n) throw new Error("invalid curve parameters");
  return { virtualA, virtualB };
}

// The state of a pool right after initializeV2.
export function newPoolState(config, curve = DEFAULT_CURVE) {
  const { virtualA, virtualB } = initialReserves({ ...curve, migrateFee: config.migrateFee });
  return { ...curve, virtualA, virtualB, realA: 0n, realB: 0n };
}

export function totalFeeRate(fees) {
  const rate = fees.protocol + fees.platform + fees.creator + (fees.share ?? 0n);
  if (rate > FEE_DENOMINATOR) throw new Error("total fee rate above 100%");
  return rate;
}

// Tokens out for `amountB` lamports in; caps at what is left on the curve and then also
// returns the smaller amount of lamports that buys exactly that.
export function quoteBuy(pool, amountB, fees) {
  const rate = totalFeeRate(fees);
  const inAfterFee = amountB - ceilDiv(amountB * rate, FEE_DENOMINATOR);
  const reserveA = pool.virtualA - pool.realA;
  const reserveB = pool.virtualB + pool.realB;
  const amountA = (inAfterFee * reserveA) / (reserveB + inAfterFee);
  const remaining = pool.totalSellA - pool.realA;
  if (amountA <= remaining) return { amountA, amountB };
  const neededAfterFee = ceilDiv(reserveB * remaining, reserveA - remaining);
  const neededB = rate === 0n ? neededAfterFee : ceilDiv(neededAfterFee * FEE_DENOMINATOR, FEE_DENOMINATOR - rate);
  return { amountA: remaining, amountB: neededB };
}

// Lamports out (after fees) for `amountA` tokens in.
export function quoteSell(pool, amountA, fees) {
  const rate = totalFeeRate(fees);
  const reserveA = pool.virtualA - pool.realA;
  const reserveB = pool.virtualB + pool.realB;
  const grossB = (amountA * reserveB) / (reserveA + amountA);
  return { amountA, amountB: grossB - ceilDiv(grossB * rate, FEE_DENOMINATOR) };
}

export const withSlippage = (amount, bps) => (amount * BigInt(10_000 - bps)) / 10_000n;

// Price in SOL per whole token, and market cap in SOL for the full supply.
export function poolPrice(pool) {
  const reserveA = Number(pool.virtualA - pool.realA) / 10 ** TOKEN_DECIMALS;
  const reserveB = Number(pool.virtualB + pool.realB) / 10 ** SOL_DECIMALS;
  const price = reserveB / reserveA;
  return { price, marketCap: price * (Number(pool.supply) / 10 ** TOKEN_DECIMALS) };
}

// Share of the graduation target raised so far, 0..1.
export function poolProgress(pool) {
  if (pool.status !== undefined && pool.status !== POOL_STATUS.trading) return 1;
  if (pool.totalFundRaisingB === 0n) return 0;
  const p = Number((pool.realB * 10_000n) / pool.totalFundRaisingB) / 10_000;
  return Math.min(1, Math.max(0, p));
}

export const feesFrom = (config, platform) => ({
  protocol: config.tradeFeeRate,
  platform: platform.feeRate,
  creator: platform.creatorFeeRate,
});
