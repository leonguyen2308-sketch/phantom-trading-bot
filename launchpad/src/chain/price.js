// SOL in US dollars, from Jupiter with CoinGecko as a backup. Null when neither answers: the site
// then shows SOL amounts only.
const WSOL = "So11111111111111111111111111111111111111112";

const SOURCES = [
  async (fetchImpl) => {
    const res = await fetchImpl(`https://lite-api.jup.ag/price/v3?ids=${WSOL}`, { signal: AbortSignal.timeout(6000) });
    const json = await res.json();
    return Number(json?.[WSOL]?.usdPrice);
  },
  async (fetchImpl) => {
    const res = await fetchImpl("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", {
      signal: AbortSignal.timeout(6000),
    });
    const json = await res.json();
    return Number(json?.solana?.usd);
  },
];

let cached = { value: null, at: 0 };

export async function solUsd({ fetchImpl = fetch, maxAgeMs = 60_000 } = {}) {
  if (cached.value && Date.now() - cached.at < maxAgeMs) return cached.value;
  for (const source of SOURCES) {
    try {
      const v = await source(fetchImpl);
      if (Number.isFinite(v) && v > 0) {
        cached = { value: v, at: Date.now() };
        return v;
      }
    } catch {
      // try the next source
    }
  }
  return cached.value;
}
