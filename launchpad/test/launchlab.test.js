// Golden tests: our LaunchLab code against the official Raydium SDK.
import { test } from "node:test";
import assert from "node:assert/strict";
import { Keypair, PublicKey } from "@solana/web3.js";
import BN from "bn.js";
import * as sdk from "@raydium-io/raydium-sdk-v2";
import * as spl from "@solana/spl-token";
import * as ll from "../src/chain/launchlab.js";

const bn = (v) => new BN(v.toString());
const big = (v) => BigInt(v.toString());
const key = () => Keypair.generate().publicKey;

// Deterministic PRNG so failures reproduce.
let state = 0x2545f491;
function rand() {
  state ^= state << 13;
  state ^= state >>> 17;
  state ^= state << 5;
  return (state >>> 0) / 2 ** 32;
}
const randBig = (max) => BigInt(Math.floor(rand() * Number(max)));

function sameIx(ours, theirs) {
  assert.equal(ours.programId.toBase58(), theirs.programId.toBase58());
  assert.deepEqual(
    ours.keys.map((k) => [k.pubkey.toBase58(), k.isSigner, k.isWritable]),
    theirs.keys.map((k) => [k.pubkey.toBase58(), k.isSigner, k.isWritable]),
  );
  assert.equal(Buffer.from(ours.data).toString("hex"), Buffer.from(theirs.data).toString("hex"));
}

test("network constants match the SDK and the PDAs they come from", () => {
  const main = ll.NETWORKS.mainnet;
  assert.equal(main.program.toBase58(), sdk.LAUNCHPAD_PROGRAM.toBase58());
  assert.equal(main.platform.toBase58(), sdk.LAUNCHPAD_PLATFORM.toBase58());
  assert.equal(main.config.toBase58(), sdk.LAUNCHPAD_CONFIG.toBase58());
  assert.equal(
    main.config.toBase58(),
    sdk.getPdaLaunchpadConfigId(main.program, spl.NATIVE_MINT, 0, 0).publicKey.toBase58(),
  );
  assert.equal(ll.authPda(main.program).toBase58(), sdk.LAUNCHPAD_AUTH.toBase58());

  const dev = ll.NETWORKS.devnet;
  assert.equal(dev.program.toBase58(), sdk.DEV_LAUNCHPAD_PROGRAM.toBase58());
  assert.equal(ll.authPda(dev.program).toBase58(), sdk.DEV_LAUNCHPAD_AUTH.toBase58());
  assert.equal(dev.platform.toBase58(), sdk.DEVNET_PROGRAM_ID.LAUNCHPAD_PLATFORM.toBase58());
  assert.equal(dev.config.toBase58(), sdk.DEVNET_PROGRAM_ID.LAUNCHPAD_CONFIG.toBase58());
  assert.equal(
    dev.config.toBase58(),
    sdk.getPdaLaunchpadConfigId(dev.program, spl.NATIVE_MINT, 0, 0).publicKey.toBase58(),
  );
});

test("PDAs match the SDK", () => {
  const program = ll.NETWORKS.mainnet.program;
  for (let i = 0; i < 20; i++) {
    const mint = key();
    const platform = key();
    const config = key();
    const creator = key();
    const pool = sdk.getPdaLaunchpadPoolId(program, mint, spl.NATIVE_MINT).publicKey;
    assert.equal(ll.poolPda(program, mint).toBase58(), pool.toBase58());
    assert.equal(
      ll.vaultPda(program, pool, mint).toBase58(),
      sdk.getPdaLaunchpadVaultId(program, pool, mint).publicKey.toBase58(),
    );
    assert.equal(
      ll.platformVaultPda(program, platform).toBase58(),
      sdk.getPdaPlatformVault(program, platform, spl.NATIVE_MINT).publicKey.toBase58(),
    );
    assert.equal(
      ll.creatorVaultPda(program, creator).toBase58(),
      sdk.getPdaCreatorVault(program, creator, spl.NATIVE_MINT).publicKey.toBase58(),
    );
    assert.equal(
      ll.platformAllowConfigPda(program, platform, config).toBase58(),
      sdk.getPdaPlatformAllowConfig(program, platform, config).publicKey.toBase58(),
    );
    assert.equal(
      ll.platformCurveRulePda(program, platform, config).toBase58(),
      sdk.getPdaPlatformCurveRule(program, platform, config).publicKey.toBase58(),
    );
    assert.equal(ll.metadataPda(mint).toBase58(), sdk.getPdaMetadataKey(mint).publicKey.toBase58());
    assert.equal(
      ll.ataAddress(creator, mint).toBase58(),
      spl.getAssociatedTokenAddressSync(mint, creator, true).toBase58(),
    );
  }
});

test("initializeV2 matches the SDK byte for byte", () => {
  const names = ["Kim Rocket", "Supreme 🚀 Leader", "x", "A".repeat(32), "Ракета"];
  const symbols = ["KIM", "ROCKET", "$", "ABCDEFGHIJ"];
  for (const network of ["mainnet", "devnet"]) {
    const { program } = ll.NETWORKS[network];
    for (let i = 0; i < 24; i++) {
      const payer = key();
      const config = key();
      const platform = key();
      const mintA = key();
      const name = names[i % names.length];
      const symbol = symbols[i % symbols.length];
      const uri = "https://arweave.net/" + "x".repeat(i * 3);
      const migrateType = i % 2 ? "amm" : "cpmm";
      const allow = i % 3 === 0;
      const rule = i % 4 === 0;
      const curve = {
        supply: ll.DEFAULT_CURVE.supply + BigInt(i),
        totalSellA: ll.DEFAULT_CURVE.totalSellA - BigInt(i),
        totalFundRaisingB: ll.DEFAULT_CURVE.totalFundRaisingB + BigInt(i * 7),
      };
      const pool = sdk.getPdaLaunchpadPoolId(program, mintA, spl.NATIVE_MINT).publicKey;
      const theirs = sdk.initializeV2(
        program,
        payer,
        payer,
        config,
        platform,
        sdk.getPdaLaunchpadAuth(program).publicKey,
        pool,
        mintA,
        spl.NATIVE_MINT,
        sdk.getPdaLaunchpadVaultId(program, pool, mintA).publicKey,
        sdk.getPdaLaunchpadVaultId(program, pool, spl.NATIVE_MINT).publicKey,
        sdk.getPdaMetadataKey(mintA).publicKey,
        spl.TOKEN_PROGRAM_ID,
        6,
        name,
        symbol,
        uri,
        {
          type: "ConstantCurve",
          totalSellA: bn(curve.totalSellA),
          migrateType,
          supply: bn(curve.supply),
          totalFundRaisingB: bn(curve.totalFundRaisingB),
        },
        new BN(0),
        new BN(0),
        new BN(0),
        sdk.CpmmCreatorFeeOn.OnlyTokenB,
        allow ? sdk.getPdaPlatformAllowConfig(program, platform, config).publicKey : undefined,
        rule ? sdk.getPdaPlatformCurveRule(program, platform, config).publicKey : undefined,
      );
      const ours = ll.initializeV2Ix({
        program,
        payer,
        creator: payer,
        config,
        platform,
        mintA,
        name,
        symbol,
        uri,
        curve,
        migrateType,
        platformAllowConfig: allow ? ll.platformAllowConfigPda(program, platform, config) : undefined,
        platformCurveRule: rule ? ll.platformCurveRulePda(program, platform, config) : undefined,
      });
      sameIx(ours, theirs);
      const decoded = ll.decodeInitializeV2Data(ours.data);
      assert.deepEqual(decoded, { decimals: 6, name, symbol, uri });
    }
  }
});

test("buyExactIn and sellExactIn match the SDK byte for byte", () => {
  const program = ll.NETWORKS.mainnet.program;
  for (let i = 0; i < 20; i++) {
    const owner = key();
    const config = key();
    const platform = key();
    const mintA = key();
    const creator = key();
    const amountIn = randBig(1e15);
    const minOut = randBig(1e15);
    const pool = sdk.getPdaLaunchpadPoolId(program, mintA, spl.NATIVE_MINT).publicKey;
    const args = [
      program,
      owner,
      sdk.getPdaLaunchpadAuth(program).publicKey,
      config,
      platform,
      pool,
      spl.getAssociatedTokenAddressSync(mintA, owner),
      spl.getAssociatedTokenAddressSync(spl.NATIVE_MINT, owner),
      sdk.getPdaLaunchpadVaultId(program, pool, mintA).publicKey,
      sdk.getPdaLaunchpadVaultId(program, pool, spl.NATIVE_MINT).publicKey,
      mintA,
      spl.NATIVE_MINT,
      spl.TOKEN_PROGRAM_ID,
      spl.TOKEN_PROGRAM_ID,
      sdk.getPdaPlatformVault(program, platform, spl.NATIVE_MINT).publicKey,
      sdk.getPdaCreatorVault(program, creator, spl.NATIVE_MINT).publicKey,
      bn(amountIn),
      bn(minOut),
      new BN(0),
    ];
    const common = { program, owner, config, platform, mintA, creator };
    sameIx(ll.buyExactInIx({ ...common, amountB: amountIn, minAmountA: minOut }), sdk.buyExactInInstruction(...args));
    sameIx(ll.sellExactInIx({ ...common, amountA: amountIn, minAmountB: minOut }), sdk.sellExactInInstruction(...args));
  }
});

test("token helpers match @solana/spl-token", () => {
  const payer = key();
  const owner = key();
  const mint = key();
  const ata = spl.getAssociatedTokenAddressSync(mint, owner, true);
  sameIx(
    ll.createAtaIdempotentIx(payer, owner, mint),
    spl.createAssociatedTokenAccountIdempotentInstruction(payer, ata, owner, mint),
  );
  sameIx(ll.syncNativeIx(ata), spl.createSyncNativeInstruction(ata));
  sameIx(ll.closeAccountIx(ata, owner, owner), spl.createCloseAccountInstruction(ata, owner, owner));
});

function randomize(layout) {
  // Fill every field of an SDK layout with random values, then encode it.
  const value = {};
  for (const field of layout.fields) {
    if (!field.property) continue;
    value[field.property] = randomField(field);
  }
  return value;
}
function randomField(field) {
  const name = field.constructor.name;
  if (field.fields) return randomize(field);
  if (field.span === 32 && !field.elementLayout) return key();
  if (field.elementLayout) return Array.from({ length: field.count }, () => Math.floor(rand() * 256));
  if (field.span === 1) return Math.floor(rand() * 256);
  if (field.span === 2) return Math.floor(rand() * 65536);
  if (field.span === 8) return new BN(randBig(2n ** 53n).toString()).mul(new BN(Math.floor(rand() * 1000) + 1));
  throw new Error(`unhandled layout field ${name} ${field.property}`);
}
function encode(layout, value) {
  const buf = Buffer.alloc(layout.span);
  layout.encode(value, buf);
  return buf;
}

test("account decoders match the SDK layouts", () => {
  for (let i = 0; i < 10; i++) {
    const cfg = randomize(sdk.LaunchpadConfig);
    const cfgBuf = encode(sdk.LaunchpadConfig, cfg);
    const sdkCfg = sdk.LaunchpadConfig.decode(cfgBuf);
    const ourCfg = ll.decodeConfig(cfgBuf);
    for (const k of ["curveType", "index"]) assert.equal(ourCfg[k], sdkCfg[k], k);
    for (const k of ["migrateFee", "tradeFeeRate", "maxShareFeeRate", "minSupplyA", "maxLockRate", "minSellRateA", "minMigrateRateA", "minFundRaisingB"])
      assert.equal(ourCfg[k], big(sdkCfg[k]), k);
    assert.equal(ourCfg.mintB.toBase58(), sdkCfg.mintB.toBase58());

    const plat = randomize(sdk.PlatformConfig);
    plat.name = [...Buffer.from("Kim Launch".padEnd(64, "\0"))];
    plat.web = [...Buffer.from("https://example.com".padEnd(256, "\0"))];
    plat.img = [...Buffer.alloc(256)];
    const platBuf = encode(sdk.PlatformConfig, plat);
    const sdkPlat = sdk.PlatformConfig.decode(platBuf);
    const ourPlat = ll.decodePlatform(platBuf);
    assert.equal(ourPlat.feeRate, big(sdkPlat.feeRate));
    assert.equal(ourPlat.creatorFeeRate, big(sdkPlat.creatorFeeRate));
    assert.equal(ourPlat.restrictGlobalConfig, sdkPlat.restrictGlobalConfig);
    assert.equal(ourPlat.restrictCurveParam, sdkPlat.restrictCurveParam);
    assert.equal(ourPlat.platformClaimFeeWallet.toBase58(), sdkPlat.platformClaimFeeWallet.toBase58());
    assert.equal(ourPlat.name, "Kim Launch");
    assert.equal(ourPlat.web, "https://example.com");
    assert.equal(ourPlat.img, "");

    const pool = randomize(sdk.LaunchpadPool);
    const poolBuf = encode(sdk.LaunchpadPool, pool);
    const sdkPool = sdk.LaunchpadPool.decode(poolBuf);
    const ourPool = ll.decodePool(poolBuf);
    for (const k of ["status", "mintDecimalsA", "mintDecimalsB", "migrateType"]) assert.equal(ourPool[k], sdkPool[k], k);
    for (const k of ["supply", "totalSellA", "virtualA", "virtualB", "realA", "realB", "totalFundRaisingB", "migrateFee"])
      assert.equal(ourPool[k], big(sdkPool[k]), k);
    for (const k of ["configId", "platformId", "mintA", "mintB", "vaultA", "vaultB", "creator"])
      assert.equal(ourPool[k].toBase58(), sdkPool[k].toBase58(), k);
  }
});

test("curve math matches the SDK", () => {
  const curve = sdk.Curve.getCurve(0);
  for (let i = 0; i < 200; i++) {
    const migrateFee = i % 5 === 0 ? 0n : randBig(5_000_000_000n);
    const params = {
      supply: ll.DEFAULT_CURVE.supply,
      totalSellA: ll.DEFAULT_CURVE.totalSellA - randBig(100_000_000_000_000n),
      totalFundRaisingB: ll.DEFAULT_CURVE.totalFundRaisingB + randBig(50_000_000_000n),
    };
    const theirs = curve.getInitParam({
      supply: bn(params.supply),
      totalSell: bn(params.totalSellA),
      totalLockedAmount: new BN(0),
      totalFundRaising: bn(params.totalFundRaisingB),
      migrateFee: bn(migrateFee),
    });
    const ours = ll.initialReserves({ ...params, migrateFee });
    assert.equal(ours.virtualA, big(theirs.a));
    assert.equal(ours.virtualB, big(theirs.b));

    const fees = {
      protocol: randBig(20_000n),
      platform: randBig(20_000n),
      creator: i % 3 ? randBig(10_000n) : 0n,
    };
    const realB = i % 4 === 0 ? 0n : randBig(params.totalFundRaisingB);
    const pool = {
      ...params,
      virtualA: ours.virtualA,
      virtualB: ours.virtualB,
      realA: realB === 0n ? 0n : ll.quoteBuy({ ...params, ...ours, realA: 0n, realB: 0n }, realB, { protocol: 0n, platform: 0n, creator: 0n }).amountA,
      realB,
    };
    // Mostly ordinary buys, some that overshoot what is left on the curve.
    const amountB = i % 7 === 0 ? 200_000_000_000n : randBig(10_000_000_000n) + 1n;
    const sdkBuy = sdk.Curve.buyExactIn({
      poolInfo: {
        virtualA: bn(pool.virtualA),
        virtualB: bn(pool.virtualB),
        realA: bn(pool.realA),
        realB: bn(pool.realB),
        totalSellA: bn(pool.totalSellA),
        totalFundRaisingB: bn(pool.totalFundRaisingB),
      },
      amountB: bn(amountB),
      protocolFeeRate: bn(fees.protocol),
      platformFeeRate: bn(fees.platform),
      creatorFeeRate: bn(fees.creator),
      shareFeeRate: new BN(0),
      curveType: 0,
      transferFeeConfigA: undefined,
      transferFeeConfigB: undefined,
      slot: 0,
    });
    const ourBuy = ll.quoteBuy(pool, amountB, fees);
    assert.equal(ourBuy.amountA, big(sdkBuy.amountA.amount), `buy amountA #${i}`);
    assert.equal(ourBuy.amountB, big(sdkBuy.amountB), `buy amountB #${i}`);

    if (pool.realA > 0n) {
      const amountA = randBig(pool.realA) + 1n;
      const sdkSell = sdk.Curve.sellExactIn({
        poolInfo: { virtualA: bn(pool.virtualA), virtualB: bn(pool.virtualB), realA: bn(pool.realA), realB: bn(pool.realB) },
        amountA: bn(amountA),
        protocolFeeRate: bn(fees.protocol),
        platformFeeRate: bn(fees.platform),
        creatorFeeRate: bn(fees.creator),
        shareFeeRate: new BN(0),
        curveType: 0,
        transferFeeConfigA: undefined,
        transferFeeConfigB: undefined,
        slot: 0,
      });
      assert.equal(ll.quoteSell(pool, amountA, fees).amountB, big(sdkSell.amountB), `sell #${i}`);
    }
  }
});

test("default curve passes the SDK's own parameter check", () => {
  const config = {
    maxLockRate: new BN(300_000),
    minSupplyA: new BN(10_000_000),
    minSellRateA: new BN(200_000),
    minFundRaisingB: new BN(30_000_000_000),
    minMigrateRateA: new BN(200_000),
  };
  for (const migrateType of ["amm", "cpmm"]) {
    sdk.Curve.checkParam({
      supply: bn(ll.DEFAULT_CURVE.supply),
      totalSell: bn(ll.DEFAULT_CURVE.totalSellA),
      totalLockedAmount: new BN(0),
      totalFundRaising: bn(ll.DEFAULT_CURVE.totalFundRaisingB),
      decimals: 6,
      config,
      migrateType,
    });
  }
});

test("price and progress", () => {
  const pool = { ...ll.newPoolState({ migrateFee: 0n }), status: 0 };
  const { price, marketCap } = ll.poolPrice(pool);
  assert.ok(price > 0 && marketCap > 20 && marketCap < 40, `start market cap ${marketCap} SOL`);
  assert.equal(ll.poolProgress(pool), 0);
  assert.equal(ll.poolProgress({ ...pool, realB: pool.totalFundRaisingB / 2n }), 0.5);
  assert.equal(ll.poolProgress({ ...pool, status: 2 }), 1);
  assert.equal(ll.withSlippage(10_000n, 100), 9_900n);
  assert.ok(new PublicKey(ll.WSOL_MINT).equals(spl.NATIVE_MINT));
});
