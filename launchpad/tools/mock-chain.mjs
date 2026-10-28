// A tiny in-memory Solana for browser tests: answers the JSON-RPC calls the site makes and
// "executes" the instructions it sends (fee transfer, LaunchLab create, buys and sells) with the
// same curve math as the site. Account data is encoded with the Raydium SDK's own layouts.
import { AddressLookupTableAccount, Keypair, PublicKey, SystemInstruction, SystemProgram, VersionedTransaction } from "@solana/web3.js";
import bs58 from "bs58";
import BN from "bn.js";
import { readFileSync } from "node:fs";
import { ed25519 } from "@noble/curves/ed25519";
import * as sdk from "@raydium-io/raydium-sdk-v2";
import * as ll from "../src/chain/launchlab.js";
import { launchInstructions } from "../src/chain/launch.js";

const LUT_PROGRAM = "AddressLookupTab1e1111111111111111111111111";
const bn = (v) => new BN(v.toString());

function encode(layout, value) {
  const buf = Buffer.alloc(layout.span);
  layout.encode(value, buf);
  return buf;
}

function metadataAccount(mint, name, symbol, uri) {
  const str = (s) => {
    const b = Buffer.from(s, "utf8");
    const len = Buffer.alloc(4);
    len.writeUInt32LE(b.length);
    return Buffer.concat([len, b]);
  };
  return Buffer.concat([Buffer.from([4]), Buffer.alloc(32), mint.toBuffer(), str(name), str(symbol), str(uri), Buffer.alloc(200)]);
}

function raydiumLutData() {
  const src = readFileSync(new URL("../node_modules/@raydium-io/raydium-sdk-v2/src/common/txTool/lookupTable.ts", import.meta.url), "utf8");
  return Buffer.from(src.match(/\/\/\s+"([A-Za-z0-9+/=]{200,})"/)[1], "base64");
}

export function createMockChain({ feeWallet, network = "mainnet", fees = { trade: 10_000n, platform: 2_500n, creator: 0n } } = {}) {
  const net = ll.NETWORKS[network];
  const accounts = new Map(); // address -> { lamports, owner, data }
  const history = new Map(); // address -> [{ signature, slot, blockTime }], newest first
  const txs = new Map(); // signature -> parsed transaction JSON
  const tokens = new Map(); // `${owner}:${mint}` -> raw amount (bigint)
  const sent = []; // every transaction the site sent, decoded
  let slot = 312_000_000;
  let height = 290_000_000;
  const clockStart = Math.floor(Date.now() / 1000);

  const setAccount = (key, owner, data, lamports = 2_000_000) =>
    accounts.set(key.toString(), { lamports, owner: owner.toString(), data: Buffer.from(data) });
  const balance = (key) => accounts.get(key.toString())?.lamports ?? 0;
  const credit = (key, lamports) => {
    const k = key.toString();
    const a = accounts.get(k) ?? { lamports: 0, owner: SystemProgram.programId.toBase58(), data: Buffer.alloc(0) };
    a.lamports += Number(lamports);
    accounts.set(k, a);
  };

  const configState = {
    epoch: new BN(0), curveType: 0, index: 0, migrateFee: new BN(0), tradeFeeRate: bn(fees.trade),
    maxShareFeeRate: new BN(10_000), minSupplyA: new BN(10_000_000), maxLockRate: new BN(300_000),
    minSellRateA: new BN(200_000), minMigrateRateA: new BN(200_000), minFundRaisingB: new BN(30_000_000_000),
    mintB: ll.WSOL_MINT, protocolFeeOwner: Keypair.generate().publicKey, migrateFeeOwner: Keypair.generate().publicKey,
    migrateToAmmWallet: Keypair.generate().publicKey, migrateToCpmmWallet: Keypair.generate().publicKey,
  };
  setAccount(net.config, net.program, encode(sdk.LaunchpadConfig, configState));
  const name = Buffer.alloc(64);
  name.write("Raydium");
  const web = Buffer.alloc(256);
  web.write("https://raydium.io");
  setAccount(
    net.platform,
    net.program,
    encode(sdk.PlatformConfig, {
      epoch: new BN(0), platformClaimFeeWallet: Keypair.generate().publicKey, platformLockNftWallet: Keypair.generate().publicKey,
      platformScale: new BN(0), creatorScale: new BN(0), burnScale: new BN(0), feeRate: bn(fees.platform),
      name: [...name], web: [...web], img: [...Buffer.alloc(256)], cpConfigId: Keypair.generate().publicKey,
      creatorFeeRate: bn(fees.creator), transferFeeExtensionAuth: Keypair.generate().publicKey,
      platformVestingWallet: Keypair.generate().publicKey, platformVestingScale: new BN(0),
      platformCpCreator: Keypair.generate().publicKey, restrictGlobalConfig: 0, restrictCurveParam: 0,
      curveRuleManager: Keypair.generate().publicKey,
    }),
  );
  setAccount(net.lookupTable, LUT_PROGRAM, raydiumLutData());
  const lut = new AddressLookupTableAccount({ key: net.lookupTable, state: AddressLookupTableAccount.deserialize(raydiumLutData()) });

  const config = ll.decodeConfig(accounts.get(net.config.toString()).data);
  const platform = ll.decodePlatform(accounts.get(net.platform.toString()).data);
  const curveFees = ll.feesFrom(config, platform);
  const ctx = { network, program: net.program, configId: net.config, platformId: net.platform, config, platform, fees: curveFees, lookupTables: [] };

  function writePool(mint, creator, pool) {
    const vaultPool = ll.poolPda(net.program, mint);
    setAccount(
      vaultPool,
      net.program,
      encode(sdk.LaunchpadPool, {
        epoch: new BN(0), bump: 255, status: pool.status ?? 0, mintDecimalsA: 6, mintDecimalsB: 9, migrateType: 1,
        supply: bn(pool.supply), totalSellA: bn(pool.totalSellA), virtualA: bn(pool.virtualA), virtualB: bn(pool.virtualB),
        realA: bn(pool.realA), realB: bn(pool.realB), totalFundRaisingB: bn(pool.totalFundRaisingB), protocolFee: new BN(0),
        platformFee: new BN(0), migrateFee: new BN(0),
        vestingSchedule: { totalLockedAmount: new BN(0), cliffPeriod: new BN(0), unlockPeriod: new BN(0), startTime: new BN(0), totalAllocatedShare: new BN(0) },
        configId: net.config, platformId: net.platform, mintA: mint, mintB: ll.WSOL_MINT,
        vaultA: ll.vaultPda(net.program, vaultPool, mint), vaultB: ll.vaultPda(net.program, vaultPool, ll.WSOL_MINT),
        creator, mintProgramFlag: 0, cpmmCreatorFeeOn: 0, platformVestingShare: new BN(0),
      }),
    );
  }
  const readPool = (mint) => {
    const a = accounts.get(ll.poolPda(net.program, mint).toString());
    return a ? ll.decodePool(a.data) : null;
  };

  function record(signature, keys, parsedInstructions, { blockTime, payer }) {
    slot += 3;
    height += 3;
    const parsed = {
      slot,
      blockTime,
      version: 0,
      transaction: {
        signatures: [signature],
        message: {
          accountKeys: keys.map((k, i) => ({ pubkey: k, signer: i === 0 || k === payer, writable: true, source: "transaction" })),
          instructions: parsedInstructions,
          recentBlockhash: bs58.encode(Buffer.alloc(32, 7)),
          addressTableLookups: [],
        },
      },
      meta: {
        err: null, fee: 5000, preBalances: keys.map(() => 0), postBalances: keys.map(() => 0), innerInstructions: [],
        logMessages: ["Program log: mock"], preTokenBalances: [], postTokenBalances: [],
        loadedAddresses: { writable: [], readonly: [] }, computeUnitsConsumed: 160_000,
      },
    };
    txs.set(signature, parsed);
    for (const k of new Set(keys)) {
      const list = history.get(k) ?? [];
      list.unshift({ signature, slot, blockTime, err: null, memo: null, confirmationStatus: "finalized" });
      history.set(k, list);
    }
  }

  const parsedIx = (ix) => {
    if (ix.programId.equals(SystemProgram.programId) && ix.data.length >= 12 && ix.data.readUInt32LE(0) === 2) {
      const t = SystemInstruction.decodeTransfer(ix);
      return { program: "system", programId: SystemProgram.programId.toBase58(), stackHeight: null,
        parsed: { type: "transfer", info: { source: t.fromPubkey.toBase58(), destination: t.toPubkey.toBase58(), lamports: Number(t.lamports) } } };
    }
    return { programId: ix.programId.toBase58(), accounts: ix.keys.map((k) => k.pubkey.toBase58()), data: bs58.encode(ix.data), stackHeight: null };
  };

  // ---- executing what the site sends --------------------------------------------------------

  function execute(instructions, payer) {
    const effects = { feePaid: 0n, created: null, bought: null, sold: null };
    for (const ix of instructions) {
      const program = ix.programId.toBase58();
      if (program === SystemProgram.programId.toBase58()) {
        if (ix.data.readUInt32LE(0) === 2) {
          const t = SystemInstruction.decodeTransfer(ix);
          if (balance(t.fromPubkey) < Number(t.lamports)) throw new Error("Transfer: insufficient lamports");
          credit(t.fromPubkey, -t.lamports);
          credit(t.toPubkey, t.lamports);
          if (t.toPubkey.equals(feeWallet)) effects.feePaid += t.lamports;
        }
      } else if (ix.programId.equals(net.program)) {
        const init = ll.decodeInitializeV2Data(ix.data);
        if (init) {
          const mint = ix.keys[6].pubkey;
          const creator = ix.keys[1].pubkey;
          writePool(mint, creator, ll.newPoolState(config));
          setAccount(ll.metadataPda(mint), ll.METADATA_PROGRAM, metadataAccount(mint, init.name, init.symbol, init.uri));
          credit(payer, -15_400_000);
          effects.created = { mint: mint.toBase58(), ...init };
          continue;
        }
        const disc = [...ix.data.subarray(0, 8)].join(",");
        const amountIn = ix.data.readBigUInt64LE(8);
        const minOut = ix.data.readBigUInt64LE(16);
        const mint = ix.keys[9].pubkey;
        const owner = ix.keys[0].pubkey;
        const pool = readPool(mint);
        if (!pool) throw new Error("pool not found");
        if (disc === "250,234,13,123,213,156,19,236") {
          const q = ll.quoteBuy(pool, amountIn, curveFees);
          if (q.amountA < minOut) throw new Error("ExceededSlippage");
          const net_ = q.amountB - (q.amountB * (curveFees.protocol + curveFees.platform + curveFees.creator) + 999_999n) / 1_000_000n;
          writePool(mint, pool.creator, { ...pool, realA: pool.realA + q.amountA, realB: pool.realB + net_, status: pool.realB + net_ >= pool.totalFundRaisingB ? 2 : 0 });
          const k = `${owner.toBase58()}:${mint.toBase58()}`;
          tokens.set(k, (tokens.get(k) ?? 0n) + q.amountA);
          effects.bought = { mint: mint.toBase58(), tokens: q.amountA, lamports: q.amountB };
        } else if (disc === "149,39,222,155,211,124,152,26") {
          const k = `${owner.toBase58()}:${mint.toBase58()}`;
          if ((tokens.get(k) ?? 0n) < amountIn) throw new Error("insufficient token balance");
          const q = ll.quoteSell(pool, amountIn, curveFees);
          if (q.amountB < minOut) throw new Error("ExceededSlippage");
          const grossB = (amountIn * (pool.virtualB + pool.realB)) / (pool.virtualA - pool.realA + amountIn);
          writePool(mint, pool.creator, { ...pool, realA: pool.realA - amountIn, realB: pool.realB - grossB });
          tokens.set(k, tokens.get(k) - amountIn);
          credit(owner, q.amountB);
          effects.sold = { mint: mint.toBase58(), tokens: amountIn, lamports: q.amountB };
        }
      }
      // ATA creation, sync-native and close are bookkeeping-free here.
    }
    return effects;
  }

  function decode(raw) {
    const tx = VersionedTransaction.deserialize(raw);
    const msg = tx.message;
    const lookups = msg.addressTableLookups.map((l) => {
      if (!l.accountKey.equals(lut.key)) throw new Error("unknown lookup table");
      return lut;
    });
    const keys = msg.getAccountKeys({ addressLookupTableAccounts: lookups });
    const instructions = msg.compiledInstructions.map((ci) => ({
      programId: keys.get(ci.programIdIndex),
      keys: ci.accountKeyIndexes.map((i) => ({ pubkey: keys.get(i), isSigner: msg.isAccountSigner(i), isWritable: msg.isAccountWritable(i) })),
      data: Buffer.from(ci.data),
    }));
    const signed = msg.serialize();
    for (let i = 0; i < msg.header.numRequiredSignatures; i++) {
      const sig = tx.signatures[i];
      const ok = sig && !sig.every((b) => b === 0) && ed25519.verify(sig, signed, msg.staticAccountKeys[i].toBytes());
      if (!ok) throw new Error(`signature verification failed for ${msg.staticAccountKeys[i].toBase58()}`);
    }
    return { tx, instructions, payer: msg.staticAccountKeys[0], signature: bs58.encode(tx.signatures[0]), size: raw.length,
      keyList: [...msg.staticAccountKeys, ...lookups.flatMap(() => [])] };
  }

  // ---- seeding launches for the board -------------------------------------------------------

  function seedLaunch({ name, symbol, uri, raisedSol = 0, graduated = false, agoSeconds = 3600, creator = Keypair.generate().publicKey }) {
    const mint = Keypair.generate().publicKey;
    const { create } = launchInstructions(ctx, { owner: creator, mint, name, symbol, uri, fee: { wallet: feeWallet, lamports: 10_000_000n } });
    let pool = ll.newPoolState(config);
    if (raisedSol > 0) {
      const q = ll.quoteBuy(pool, BigInt(Math.round(raisedSol * 1e9)), { protocol: 0n, platform: 0n, creator: 0n });
      pool = { ...pool, realA: q.amountA, realB: q.amountB };
    }
    writePool(mint, creator, { ...pool, status: graduated ? 2 : 0 });
    setAccount(ll.metadataPda(mint), ll.METADATA_PROGRAM, metadataAccount(mint, name, symbol, uri));
    const signature = bs58.encode(Keypair.generate().secretKey);
    const keys = [...new Set([creator, ...create.flatMap((ix) => ix.keys.map((k) => k.pubkey)), ...create.map((ix) => ix.programId)].map(String))];
    record(signature, keys, create.map(parsedIx), { blockTime: clockStart - agoSeconds, payer: creator.toBase58() });
    return mint.toBase58();
  }

  // Noise in the fee wallet's history that must not be listed: a bare transfer and a memo.
  function seedNoise() {
    const from = Keypair.generate().publicKey;
    const transfer = SystemProgram.transfer({ fromPubkey: from, toPubkey: feeWallet, lamports: 50_000_000 });
    record(bs58.encode(Keypair.generate().secretKey), [from.toBase58(), feeWallet.toBase58(), SystemProgram.programId.toBase58()], [parsedIx(transfer)], {
      blockTime: clockStart - 120,
      payer: from.toBase58(),
    });
  }

  // ---- JSON-RPC -------------------------------------------------------------------------------

  const ctxOf = () => ({ context: { slot, apiVersion: "2.2.0" } });
  const accountJson = (a) => (a ? { data: [a.data.toString("base64"), "base64"], executable: false, lamports: a.lamports, owner: a.owner, rentEpoch: 0, space: a.data.length } : null);

  function call(method, params = []) {
    switch (method) {
      case "getMultipleAccounts":
        return { ...ctxOf(), value: params[0].map((k) => accountJson(accounts.get(k))) };
      case "getAccountInfo":
        return { ...ctxOf(), value: accountJson(accounts.get(params[0])) };
      case "getBalance":
        return { ...ctxOf(), value: balance(params[0]) };
      case "getLatestBlockhash":
        return { ...ctxOf(), value: { blockhash: bs58.encode(Keypair.generate().publicKey.toBytes()), lastValidBlockHeight: height + 150 } };
      case "getBlockHeight":
        return height;
      case "getSlot":
        return slot;
      case "getSignaturesForAddress": {
        const list = history.get(params[0]) ?? [];
        const { limit = 1000, before, until } = params[1] ?? {};
        let start = 0;
        if (before) start = list.findIndex((s) => s.signature === before) + 1 || list.length;
        let end = list.length;
        if (until) {
          const u = list.findIndex((s) => s.signature === until);
          if (u >= 0) end = u;
        }
        return list.slice(start, end).slice(0, limit);
      }
      case "getTransaction":
        return txs.get(params[0]) ?? null;
      case "getSignatureStatuses":
        return { ...ctxOf(), value: params[0].map((s) => (txs.has(s) ? { slot, confirmations: null, err: null, confirmationStatus: "confirmed" } : null)) };
      case "getTokenAccountBalance": {
        for (const [k, amount] of tokens) {
          const [owner, mint] = k.split(":");
          if (ll.ataAddress(new PublicKey(owner), new PublicKey(mint)).toBase58() === params[0]) {
            return { ...ctxOf(), value: { amount: amount.toString(), decimals: 6, uiAmount: Number(amount) / 1e6, uiAmountString: String(Number(amount) / 1e6) } };
          }
        }
        throw Object.assign(new Error("could not find account"), { code: -32602 });
      }
      case "simulateTransaction": {
        const raw = Buffer.from(params[0], "base64");
        const tx = VersionedTransaction.deserialize(raw);
        const payer = tx.message.staticAccountKeys[0].toBase58();
        const keys = tx.message.getAccountKeys({ addressLookupTableAccounts: tx.message.addressTableLookups.length ? [lut] : [] });
        let cost = 5000;
        for (const ci of tx.message.compiledInstructions) {
          const program = keys.get(ci.programIdIndex);
          const data = Buffer.from(ci.data);
          if (program.equals(SystemProgram.programId) && data.readUInt32LE(0) === 2) cost += Number(data.readBigUInt64LE(4));
          if (program.equals(net.program) && ll.decodeInitializeV2Data(data)) cost += 15_400_000;
        }
        const after = balance(payer) - cost;
        if (after < 0) return { ...ctxOf(), value: { err: { InstructionError: [2, { Custom: 1 }] }, logs: ["Transfer: insufficient lamports"], accounts: null, unitsConsumed: 1500 } };
        return { ...ctxOf(), value: { err: null, logs: ["Program log: ok"], unitsConsumed: 182_000, accounts: [{ ...accountJson({ lamports: after, owner: SystemProgram.programId.toBase58(), data: Buffer.alloc(0) }) }], returnData: null } };
      }
      case "sendTransaction": {
        const raw = Buffer.from(params[0], "base64");
        const d = decode(raw);
        if (txs.has(d.signature)) return d.signature;
        const effects = execute(d.instructions, d.payer);
        const keys = [...new Set([d.payer, ...d.instructions.flatMap((ix) => ix.keys.map((k) => k.pubkey)), ...d.instructions.map((ix) => ix.programId)].map(String))];
        record(d.signature, keys, d.instructions.map(parsedIx), { blockTime: Math.floor(Date.now() / 1000), payer: d.payer.toBase58() });
        sent.push({ ...d, effects });
        return d.signature;
      }
      default:
        throw Object.assign(new Error(`mock RPC: ${method} not implemented`), { code: -32601 });
    }
  }

  function handle(body) {
    const one = (req) => {
      try {
        return { jsonrpc: "2.0", id: req.id, result: call(req.method, req.params) };
      } catch (e) {
        return { jsonrpc: "2.0", id: req.id, error: { code: e.code ?? -32002, message: e.message } };
      }
    };
    return Array.isArray(body) ? body.map(one) : one(body);
  }

  // Without Raydium's lookup table a launch with a first buy no longer fits one transaction.
  const dropLookupTable = () => accounts.delete(net.lookupTable.toString());

  return { handle, seedLaunch, seedNoise, credit, balance, sent, tokens, readPool, dropLookupTable, net, config, platform };
}
