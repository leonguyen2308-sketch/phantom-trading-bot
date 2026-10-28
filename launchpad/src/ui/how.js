// How it works, fees, and the fine print.
import { html, icon, mount, short } from "./dom.js";

export function renderHow(root, app) {
  const fee = app.cfg.feeSol;
  mount(
    root,
    html`<section class="how-page">
      <header class="page-head">
        <span class="kicker">${icon("help", { size: 15 })} how it works</span>
        <h1>from supermarket to <span class="hl">orbit</span> in four steps.</h1>
      </header>
      <div class="how-steps">
        <div class="panel how-step"><span class="how-n">1</span><h3>paint it</h3>
          <p>pick a picture, a name and a ticker. no picture? the leader makes you a badge. it's all stored on Arweave, for good.</p></div>
        <div class="panel how-step"><span class="how-n">2</span><h3>fuel it</h3>
          <p>optional: buy the first bag in the same transaction as the launch, so nobody gets in before you.</p></div>
        <div class="panel how-step"><span class="how-n">3</span><h3>launch it</h3>
          <p>connect Phantom, approve one transaction. your coin and its bonding curve go live on Raydium LaunchLab. the launch fee is ${fee} SOL.</p></div>
        <div class="panel how-step"><span class="how-n">4</span><h3>fly to orbit</h3>
          <p>anyone can buy and sell on the curve. when 85 SOL is in the tank it graduates to a Raydium pool on its own.</p></div>
      </div>

      <div class="how-grid">
        <div class="panel">
          <h3>${icon("flame", { size: 18 })} what it costs</h3>
          <div class="cost-rows">
            <div class="cost"><span>launch fee, to ${app.cfg.name}</span><b class="mono">${fee} SOL</b></div>
            <div class="cost"><span>Raydium accounts (rent, paid to the network)</span><b class="mono">≈ 0.015 SOL</b></div>
            <div class="cost"><span>network fee</span><b class="mono">≈ 0.0002 SOL</b></div>
            <div class="cost"><span>trading on the curve</span><b class="mono">Raydium's fee, ~1%</b></div>
          </div>
          <p class="muted small">the launch fee and the launch are one transaction, so if the launch fails nothing is charged.
            fees go to <span class="mono">${short(app.cfg.feeWallet.toBase58(), 6)}</span>.</p>
        </div>
        <div class="panel">
          <h3>${icon("rocket", { size: 18 })} every coin gets</h3>
          <ul class="ticks">
            <li>${icon("check", { size: 16 })} 1,000,000,000 supply, 6 decimals</li>
            <li>${icon("check", { size: 16 })} 793,100,000 sold on the bonding curve</li>
            <li>${icon("check", { size: 16 })} graduation to Raydium at 85 SOL</li>
            <li>${icon("check", { size: 16 })} no presale and no team bag: 79% sells on the curve, the rest seeds the Raydium pool</li>
            <li>${icon("check", { size: 16 })} trades on Raydium right away; Jupiter and DEX screeners pick it up</li>
            <li>${icon("check", { size: 16 })} one ticker, one coin: copies of a ticker or name already on the pad are refused</li>
          </ul>
        </div>
      </div>

      <div class="panel faq">
        <h3>${icon("help", { size: 18 })} questions</h3>
        <details><summary>is this the real supreme leader?${icon("down", { size: 16, cls: "chev" })}</summary>
          <p>no. this is a parody launchpad with a meme mascot. it has nothing to do with any government, supreme or otherwise.</p></details>
        <details><summary>who controls my coin?${icon("down", { size: 16, cls: "chev" })}</summary>
          <p>Raydium LaunchLab's program does: the supply is minted once at launch and sold from the curve, not from anyone's wallet. you are recorded as the creator; where Raydium pays creator fees, you claim them on raydium.io.</p></details>
        <details><summary>which wallets work?${icon("down", { size: 16, cls: "chev" })}</summary>
          <p>Phantom first, and any Solana wallet that supports the Wallet Standard (Solflare, Backpack and others). on a phone, open this site inside the Phantom app.</p></details>
        <details><summary>can I change the name or picture later?${icon("down", { size: 16, cls: "chev" })}</summary>
          <p>no. pick carefully: name, ticker and picture are permanent.</p></details>
        <details><summary>is this financial advice?${icon("down", { size: 16, cls: "chev" })}</summary>
          <p>no. memecoins are a gamble. most go to zero. only spend what you can lose.</p></details>
      </div>
    </section>`,
  );
  return () => {};
}
