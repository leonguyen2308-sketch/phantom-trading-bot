// The assembled launch: fee transfer, create, first buy, and how it is packed into transactions.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { AddressLookupTableAccount, Keypair, PublicKey, SystemInstruction, SystemProgram } from "@solana/web3.js";
import * as ll from "../src/chain/launchlab.js";
import { launchInstructions, packLaunch, compileTx, txBytes, MAX_TX_BYTES, LaunchError, explainFailure } from "../src/chain/launch.js";

const FEE_WALLET = new PublicKey("DFAcvisLammoF7ZDLJWyC47EqKzXqWRWgwdNS34qa9Lj");
const FEE = { wallet: FEE_WALLET, lamports: 10_000_000n };

// Raydium's main lookup table as the SDK snapshots it (the live one only has more entries).
function raydiumLut() {
  const src = readFileSync(
    new URL("../node_modules/@raydium-io/raydium-sdk-v2/src/common/txTool/lookupTable.ts", import.meta.url),
    "utf8",
  );
  const b64 = src.match(/\/\/\s+"([A-Za-z0-9+/=]{200,})"/)[1];
  return new AddressLookupTableAccount({
    key: ll.NETWORKS.mainnet.lookupTable,
    state: AddressLookupTableAccount.deserialize(Buffer.from(b64, "base64")),
  });
}

function ctx({ lut = true, restrict = false } = {}) {
  const net = ll.NETWORKS.mainnet;
  const config = { curveType: 0, migrateFee: 0n, tradeFeeRate: 10_000n, mintB: ll.WSOL_MINT };
  const platform = { feeRate: 0n, creatorFeeRate: 0n, restrictGlobalConfig: restrict ? 1 : 0, restrictCurveParam: restrict ? 1 : 0 };
  return {
    network: "mainnet",
    program: net.program,
    configId: net.config,
    platformId: net.platform,
    config,
    platform,
    fees: ll.feesFrom(config, platform),
    lookupTables: lut ? [raydiumLut()] : [],
  };
}

const coin = {
  name: "Supreme Leader Rocket Coin! 🚀", // 32 bytes, the maximum
  symbol: "KIMROCKETS",
  uri: "https://arweave.net/" + "A".repeat(43),
};
const blockhash = "11111111111111111111111111111111";

test("the fee transfer pays 0.01 SOL to the fee wallet from the launcher", () => {
  const owner = Keypair.generate().publicKey;
  const mint = Keypair.generate().publicKey;
  const { create, buy, quote } = launchInstructions(ctx(), { owner, mint, ...coin, fee: FEE });
  assert.equal(buy.length, 0);
  assert.equal(quote, null);
  assert.equal(create.length, 2);
  assert.ok(create[0].programId.equals(SystemProgram.programId));
  const transfer = SystemInstruction.decodeTransfer(create[0]);
  assert.ok(transfer.fromPubkey.equals(owner));
  assert.ok(transfer.toPubkey.equals(FEE_WALLET));
  assert.equal(transfer.lamports, 10_000_000n);
  assert.ok(create[1].programId.equals(ll.NETWORKS.mainnet.program));
  assert.ok(create[1].keys[6].pubkey.equals(mint) && create[1].keys[6].isSigner);
});

test("a create-only launch is one transaction signed by the launcher and the new mint", () => {
  for (const lut of [true, false]) {
    const owner = Keypair.generate().publicKey;
    const mint = Keypair.generate().publicKey;
    const { create, buy } = launchInstructions(ctx({ lut }), { owner, mint, ...coin, fee: FEE });
    const parts = packLaunch(ctx({ lut }), { payer: owner, blockhash, create, buy, microLamports: 100_000 });
    assert.equal(parts.length, 1);
    const { tx } = parts[0];
    assert.ok(txBytes(tx) <= MAX_TX_BYTES, `${txBytes(tx)} bytes`);
    const signers = tx.message.staticAccountKeys.slice(0, tx.message.header.numRequiredSignatures);
    assert.deepEqual(signers.map(String).sort(), [owner, mint].map(String).sort());
    assert.ok(signers[0].equals(owner), "the launcher pays the fees");
  }
});

test("a launch with a first buy fits in one transaction or splits into two that each fit", () => {
  for (const lut of [true, false]) {
    for (const restrict of [false, true]) {
      const owner = Keypair.generate().publicKey;
      const mint = Keypair.generate().publicKey;
      const c = ctx({ lut, restrict });
      const { create, buy, quote } = launchInstructions(c, { owner, mint, ...coin, fee: FEE, devBuyLamports: 1_000_000_000n });
      assert.ok(quote.amountA > 0n);
      assert.equal(quote.amountB, 1_000_000_000n);
      const parts = packLaunch(c, { payer: owner, blockhash, create, buy, microLamports: 100_000 });
      for (const p of parts) assert.ok(txBytes(p.tx) <= MAX_TX_BYTES, `${txBytes(p.tx)} bytes`);
      // The fee always travels with the create, so a failed create charges nothing.
      const first = parts[0].instructions;
      assert.ok(first[0].programId.equals(SystemProgram.programId));
      assert.ok(first.some((ix) => ix.programId.equals(ll.NETWORKS.mainnet.program) && ix.keys.length >= 18));
      if (parts.length === 2) {
        const second = parts[1].tx.message;
        assert.equal(second.header.numRequiredSignatures, 1, "the buy needs only the launcher");
      }
      console.log(`lut=${lut} restrict=${restrict}: ${parts.map((p) => txBytes(p.tx)).join(" + ")} bytes`);
    }
  }
});

test("first buy math: 1 SOL buys a few percent of supply, a huge buy is capped at the curve", () => {
  const c = ctx();
  const owner = Keypair.generate().publicKey;
  const mint = Keypair.generate().publicKey;
  const small = launchInstructions(c, { owner, mint, ...coin, fee: FEE, devBuyLamports: 1_000_000_000n }).quote;
  const share = Number(small.amountA) / Number(ll.DEFAULT_CURVE.supply);
  assert.ok(share > 0.02 && share < 0.05, `1 SOL buys ${(share * 100).toFixed(2)}%`);
  const huge = launchInstructions(c, { owner, mint, ...coin, fee: FEE, devBuyLamports: 500_000_000_000n }).quote;
  assert.equal(huge.amountA, ll.DEFAULT_CURVE.totalSellA);
  assert.ok(huge.amountB < 500_000_000_000n && huge.amountB > 85_000_000_000n);
});

test("bad coin details are refused before anything is built", () => {
  const owner = Keypair.generate().publicKey;
  const mint = Keypair.generate().publicKey;
  const base = { owner, mint, fee: FEE, uri: coin.uri };
  assert.throws(() => launchInstructions(ctx(), { ...base, name: "", symbol: "KIM" }), LaunchError);
  assert.throws(() => launchInstructions(ctx(), { ...base, name: "x".repeat(33), symbol: "KIM" }), /too long/);
  assert.throws(() => launchInstructions(ctx(), { ...base, name: "Kim", symbol: "TOOLONGTICKER" }), /too long/);
});

test("failures read like advice", () => {
  assert.match(explainFailure({ InstructionError: [2, { Custom: 1 }] }, ["Transfer: insufficient lamports 5, need 10"], 5e7), /not enough SOL.*0\.0500/);
  assert.match(explainFailure("AccountNotFound", []), /no SOL on this network/);
  assert.match(
    explainFailure({ InstructionError: [3, { Custom: 6001 }] }, ["Program log: AnchorError occurred. Error Code: X. Error Message: Invalid input."]),
    /Invalid input/,
  );
});

test("compileTx puts the compute budget first", () => {
  const payer = Keypair.generate().publicKey;
  const tx = compileTx({ payer, blockhash, instructions: [], units: 200_000, microLamports: 5 });
  const programs = tx.message.compiledInstructions.map((ix) => tx.message.staticAccountKeys[ix.programIdIndex].toBase58());
  assert.deepEqual(programs, ["ComputeBudget111111111111111111111111111111", "ComputeBudget111111111111111111111111111111"]);
});
