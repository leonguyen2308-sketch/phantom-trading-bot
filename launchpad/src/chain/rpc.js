// RPC with fallbacks: the site's own RPC first, then free public ones. An endpoint that refuses
// the browser (CORS, 403), is rate limited (429) or is down (5xx) is skipped; the last one that
// worked is tried first next time.
export const PUBLIC_RPCS = {
  mainnet: ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com"],
  devnet: ["https://api.devnet.solana.com"],
};

const skippable = (status) => status === 403 || status === 429 || status >= 500;

export function failoverFetch(endpoints, fetchImpl = (...a) => fetch(...a)) {
  const urls = [...new Set(endpoints.filter(Boolean))];
  let preferred = 0;
  return async (_input, init) => {
    let lastResponse = null;
    let lastError = null;
    for (let i = 0; i < urls.length; i++) {
      const at = (preferred + i) % urls.length;
      try {
        const res = await fetchImpl(urls[at], init);
        if (skippable(res.status) && i < urls.length - 1) {
          lastResponse = res;
          continue;
        }
        preferred = at;
        return res;
      } catch (e) {
        lastError = e;
      }
    }
    if (lastResponse) return lastResponse;
    throw lastError ?? new Error("no RPC endpoint answered");
  };
}

export function rpcEndpoints(network, configured) {
  return [configured, ...(PUBLIC_RPCS[network] ?? [])].filter(Boolean);
}
