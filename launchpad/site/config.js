// Site settings. Edit, save, and redeploy: no rebuild needed.
window.KIMLAUNCH_CONFIG = {
  // Name shown in the header, and your site's address (written into every coin's metadata).
  siteName: "kimlaunch",
  siteUrl: "",

  // "mainnet" for real coins, "devnet" to test with free SOL first.
  network: "mainnet",

  // Your Solana RPC address. Left empty, the site falls back to free public RPCs, which are slow
  // and sometimes refuse browsers. Get a free key at helius.dev and paste
  // https://mainnet.helius-rpc.com/?api-key=YOUR_KEY (for devnet: https://devnet.helius-rpc.com/?api-key=YOUR_KEY).
  rpcUrl: "",

  // Every launch pays this much SOL to this wallet, in the same transaction as the launch.
  feeWallet: "DFAcvisLammoF7ZDLJWyC47EqKzXqWRWgwdNS34qa9Lj",
  feeSol: 0.01,

  // The board lists coins whose launch paid at least this much to feeWallet.
  // If you ever raise feeSol, keep this at the lowest fee you ever charged.
  listMinFeeSol: 0.01,

  // Optional: your own Raydium LaunchLab platform id (leave empty to use Raydium's default platform).
  platformId: "",

  // Where coins graduate: "cpmm" (Raydium CPMM, default) or "amm" (Raydium AMM v4).
  migrateType: "cpmm",

  // Default network priority: "normal", "fast" or "turbo".
  priorityFee: "fast",

  // Your socials (leave empty to hide).
  links: { x: "", telegram: "" },

  // Coins to take off the board (scams, copies, offensive names): add a coin's mint address or
  // its ticker, e.g. hide: ["9xYz...pump", "SCAM"]. Hidden coins can't be traded here either.
  hide: [],
};
