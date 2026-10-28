// Browser test of the whole site against a mock chain, mock wallet and mock Arweave:
// board, connect, launch with a first buy (fee checked on the wire), buy, sell, screenshots.
// Run: npm run build && npm run e2e   (screenshots land in $SHOTS or ./shots)
import { mkdir, readdir } from "node:fs/promises";
import { join } from "node:path";
import { createRequire } from "node:module";
import assert from "node:assert/strict";
import { SystemInstruction, SystemProgram } from "@solana/web3.js";
import { createWorld, FEE_WALLET, FIXTURES } from "./world.mjs";

const { chromium } = createRequire(import.meta.url)("playwright");
const SHOTS = process.env.SHOTS || "shots";

await mkdir(SHOTS, { recursive: true });
const world = await createWorld();
const { chain, arweave, user, siteUrl } = world;
const browser = await chromium.launch();
const problems = [];
const newPage = (viewport) => world.newPage(browser, viewport, { problems });

async function shot(page, name, full = true) {
  if (full) await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage: full });
}
const step = (s) => console.log(`• ${s}`);

try {
  // Board
  const page = await newPage({ width: 1440, height: 900 });
  await page.goto(siteUrl);
  await page.waitForSelector(".card .card-name", { timeout: 15000 });
  await page.waitForFunction(() => document.querySelectorAll(".card").length >= 10);
  await page.waitForFunction(() => document.querySelectorAll(".card img.coin-img").length >= 10, null, { timeout: 15000 });
  await page.waitForTimeout(600);
  const names = await page.$$eval(".card .card-name b", (els) => els.map((e) => e.textContent));
  assert.equal(names.length, 10, "every seeded launch is listed, and the bare transfer is not");
  assert.equal(names[0], "Hermit Hamster", "newest first");
  assert.ok(await page.isVisible(".tcard"), "trending rail shows");
  assert.match(await page.textContent(".king-card"), /Orbit Program/, "the supreme rocket is the biggest coin still on its curve");
  await shot(page, "board-desktop", false);
  await shot(page, "board-desktop-full");
  step("board lists 10 launches, newest first, noise ignored");

  // Tabs and search
  await page.click('[data-tab="orbit"]');
  assert.deepEqual((await page.$$eval(".card .card-name b", (els) => els.map((e) => e.textContent))).sort(), ["Glorious Medal", "Juche Coin"]);
  await page.click('[data-tab="new"]');
  await page.fill("#search", "frog");
  await page.waitForFunction(() => document.querySelectorAll(".card").length === 1);
  await page.fill("#search", "");
  step("tabs and search filter the board");

  // Connect
  await page.click(".side [data-wallet-button]");
  await page.waitForFunction(() => document.querySelector(".side [data-wallet-button]")?.textContent.includes("SOL"));
  step("wallet connects and shows its balance");

  // Create with a first buy
  await page.goto(`${siteUrl}/#/create`);
  await page.waitForSelector("#name");
  await page.fill("#symbol", "juche");
  await page.waitForFunction(() => /already on the pad/.test(document.querySelector("#taken")?.textContent ?? ""));
  await page.fill("#symbol", "");
  step("a ticker already on the pad is refused");
  await page.setInputFiles("#file", join(FIXTURES, "supreme.webp"));
  await page.waitForSelector(".drop.has-image");
  await page.fill("#name", "Supreme Leader Coin");
  await page.fill("#symbol", "kimlead");
  assert.equal(await page.inputValue("#symbol"), "KIMLEAD", "ticker is uppercased");
  await page.fill("#description", "the official coin of the launchpad.");
  await page.click("details.box summary");
  await page.fill("#twitter", "https://x.com/kimlaunch");
  await page.click('[data-preset="0.5"]');
  await page.waitForFunction(() => /of supply/.test(document.querySelector("#devbuy-hint")?.textContent ?? ""));
  await page.waitForTimeout(300);
  await shot(page, "create-desktop");
  const before = chain.balance(user.publicKey);
  await page.click("#launch");
  await page.waitForSelector(".liftoff", { timeout: 20000 });
  await page.waitForTimeout(900);
  await shot(page, "launch-success", false);

  const launches = chain.sent.filter((s) => s.effects.created);
  assert.equal(launches.length, 1, "one create transaction reached the chain");
  const created = launches[0];
  assert.equal(created.effects.feePaid, 10_000_000n, "0.01 SOL launch fee paid to the fee wallet");
  const feeIx = created.instructions.find((ix) => ix.programId.equals(SystemProgram.programId) && SystemInstruction.decodeTransfer(ix).toPubkey.equals(FEE_WALLET));
  assert.ok(SystemInstruction.decodeTransfer(feeIx).fromPubkey.equals(user.publicKey), "the launcher pays the fee");
  assert.equal(created.effects.created.name, "Supreme Leader Coin");
  assert.equal(created.effects.created.symbol, "KIMLEAD");
  assert.ok(created.size <= 1232);
  const meta = JSON.parse(arweave.get(created.effects.created.uri.split("/").pop()).body.toString());
  assert.equal(meta.name, "Supreme Leader Coin");
  assert.equal(meta.twitter, "https://x.com/kimlaunch");
  assert.ok(arweave.get(meta.image.split("/").pop()).type.startsWith("image/"), "image stored on arweave");
  const bought = chain.sent.find((s) => s.effects.bought);
  assert.ok(bought && bought.effects.bought.mint === created.effects.created.mint, "first buy landed on the new coin");
  const spent = (before - chain.balance(user.publicKey)) / 1e9;
  assert.ok(spent > 0.5 && spent < 0.54, `launch + 0.5 SOL first buy cost ${spent} SOL`);
  step(`launch: fee paid, coin created, first buy landed (${chain.sent.length} tx, ${created.size} bytes, ${spent.toFixed(4)} SOL)`);

  // Coin page: buy then sell
  await page.click(".liftoff a.btn-primary");
  await page.waitForSelector(".coin-page .kpi");
  await page.waitForSelector("#go");
  assert.match(await page.textContent(".coin-title h1"), /Supreme Leader Coin/);
  await page.waitForTimeout(500);
  await shot(page, "coin-desktop");
  await page.click('[data-amount="0.1"]');
  await page.waitForFunction(() => /you get/.test(document.querySelector("#quote")?.textContent ?? ""));
  await page.click("#go");
  await page.waitForSelector(".toast-good", { timeout: 15000 });
  assert.equal(chain.sent.filter((s) => s.effects.bought).length, 2, "the buy reached the chain");
  await page.click('[data-side="sell"]');
  await page.waitForFunction(() => /max/.test(document.querySelector(".presets")?.textContent ?? ""));
  await page.click('[data-amount="0.5"]');
  await page.click("#go");
  await page.waitForFunction(() => [...document.querySelectorAll(".toast-good")].some((t) => /sold/.test(t.textContent)), null, { timeout: 15000 });
  assert.ok(chain.sent.find((s) => s.effects.sold), "the sell reached the chain");
  step("coin page: buy and sell landed");

  // Without the lookup table, a long name and ticker push create + first buy past one
  // transaction: they go as two, signed in one wallet prompt.
  chain.dropLookupTable();
  const sentBefore = chain.sent.length;
  await page.goto(`${siteUrl}/#/create`);
  await page.waitForSelector("#name");
  await page.setInputFiles("#file", join(FIXTURES, "juche.webp"));
  await page.waitForSelector(".drop.has-image");
  await page.fill("#name", "Supreme Leader Rocket Coin! 🚀");
  await page.fill("#symbol", "KIMROCKETS");
  await page.click('[data-preset="1"]');
  await page.waitForFunction(() => /of supply/.test(document.querySelector("#devbuy-hint")?.textContent ?? ""));
  await page.click("#launch");
  await page.waitForSelector(".liftoff", { timeout: 20000 });
  const [createTx, buyTx] = chain.sent.slice(sentBefore);
  assert.ok(createTx?.effects.created && buyTx?.effects.bought, "create, then buy");
  assert.equal(createTx.effects.feePaid, 10_000_000n, "fee rides with the create");
  assert.equal(buyTx.effects.bought.mint, createTx.effects.created.mint);
  assert.equal(createTx.tx.message.header.numRequiredSignatures, 2);
  assert.equal(buyTx.tx.message.header.numRequiredSignatures, 1);
  step(`split launch: create (${createTx.size} bytes) then first buy (${buyTx.size} bytes), one prompt`);
  await page.click(".liftoff [data-close]");

  // Board shows the new coin
  await page.goto(`${siteUrl}/#/`);
  await page.waitForFunction(() => [...document.querySelectorAll(".card .card-name b")].some((b) => b.textContent === "Supreme Leader Coin"));
  step("the new coin is on the board");

  await page.goto(`${siteUrl}/#/how`);
  await page.waitForSelector(".how-steps");
  await shot(page, "how-desktop");

  await page.goto(`${siteUrl}/#/live`);
  await page.waitForSelector(".card");
  await shot(page, "live-desktop", false);

  // Ideas
  await page.goto(`${siteUrl}/#/`);
  await page.click(".mascot .bubble");
  await page.waitForSelector(".idea");
  await shot(page, "ideas-modal", false);
  await page.click(".idea >> nth=0");
  await page.waitForSelector("#name");
  assert.ok((await page.inputValue("#name")).length > 0, "an idea fills the form");
  step("the leader pitches ideas that fill the form");

  // Launching from the fee wallet itself: the page says the fee comes straight back.
  world.config.feeWallet = user.publicKey.toBase58();
  const own = await newPage({ width: 1440, height: 900 });
  await own.goto(`${siteUrl}/#/create`);
  await own.waitForSelector("#name");
  await own.click(".side [data-wallet-button]");
  await own.waitForFunction(() => /comes back to you/.test(document.querySelector("#costs")?.textContent ?? ""));
  world.config.feeWallet = FEE_WALLET.toBase58();
  await own.close();
  step("launching from the fee wallet shows the fee coming back");

  // The owner can take a coin off the board.
  world.config.hide = ["$CFROG"];
  const hidden = await newPage({ width: 1440, height: 900 });
  await hidden.goto(siteUrl);
  await hidden.waitForFunction(() => document.querySelectorAll(".card").length >= 9);
  const listed = await hidden.$$eval(".card .card-name b", (els) => els.map((e) => e.textContent));
  assert.ok(!listed.includes("Comrade Frog") && listed.includes("Kimchi Fund"), "a hidden ticker is off the board");
  world.config.hide = [];
  await hidden.close();
  step("a hidden coin is off the board");

  // Mobile
  const phone = await newPage({ width: 390, height: 844 });
  await phone.goto(siteUrl);
  await phone.waitForFunction(() => document.querySelectorAll(".card img.coin-img").length >= 10, null, { timeout: 15000 });
  await phone.waitForTimeout(500);
  await shot(phone, "board-mobile", false);
  await shot(phone, "board-mobile-full");
  const overflow = await phone.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow <= 1, `no sideways scroll on a phone (${overflow}px)`);
  await phone.goto(`${siteUrl}/#/create`);
  await phone.waitForSelector("#name");
  await shot(phone, "create-mobile");
  const overflow2 = await phone.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow2 <= 1, `create page fits a phone (${overflow2}px)`);
  const coinMint = created.effects.created.mint;
  await phone.goto(`${siteUrl}/#/coin/${coinMint}`);
  await phone.waitForSelector(".coin-page .kpi");
  await phone.waitForTimeout(400);
  await shot(phone, "coin-mobile");
  step("phone layout fits without sideways scroll");

  const real = problems.filter((p) => !/Failed to load resource/.test(p));
  if (real.length) throw new Error(`browser errors:\n${real.join("\n")}`);
  console.log(`\nall good. screenshots in ${SHOTS}/: ${(await readdir(SHOTS)).join(", ")}`);
} finally {
  await browser.close();
  world.close();
}
