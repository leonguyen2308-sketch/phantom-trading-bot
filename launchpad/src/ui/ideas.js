// "No idea what to launch? I'll pitch you three." Coin ideas from the Supreme Leader.
const FIRST = ["Supreme", "Comrade", "Glorious", "Rocket", "Orbit", "Juche", "Hermit", "Red Star", "Dear", "Eternal", "Parade", "Little", "Pyongyang", "Five-Year", "Launchpad"];
const SECOND = ["Pepe", "Doge", "Frog", "Hamster", "Penguin", "Shiba", "Panda", "Goat", "Cat", "Bonk", "Duck", "Ape", "Kimchi", "Noodle", "Burger"];
const THIRD = ["", "", " Rocket", " Inu", " Army", " Club", " Mission", " Moon", " Parade", " Plan", " Fund", " Program"];
const LINES = [
  "approved by the supreme leader. no questions taken.",
  "goes up, never comes down. that is the plan.",
  "five-year plan: 1000x by thursday.",
  "launched from a supermarket, aimed at the moon.",
  "no presale. no roadmap. only parades.",
  "built in the hermit kingdom of memes.",
  "the only rocket test the world is waiting for.",
  "every holder gets a medal. the medal is the coin.",
  "glorious chart. glorious holders. glorious leader.",
  "fueled by kimchi, steered by vibes.",
  "the leader pointed at the sky and said: there.",
  "officially the best coin in the republic.",
];

function pick(list, r) {
  return list[Math.floor(r() * list.length) % list.length];
}

function rng(seed) {
  let s = seed >>> 0 || 1;
  return () => {
    s ^= s << 13;
    s ^= s >>> 17;
    s ^= s << 5;
    return (s >>> 0) / 2 ** 32;
  };
}

function tickerFor(name) {
  const words = name.toUpperCase().replace(/[^A-Z0-9 ]/g, "").split(/\s+/).filter(Boolean);
  if (words.length === 1) return words[0].slice(0, 6);
  const last = words[words.length - 1];
  const t = words.length > 2 ? words.map((w) => w[0]).join("") + last.slice(1, 3) : words[0].slice(0, 2) + last.slice(0, 4);
  return t.slice(0, 8);
}

export function pitchThree(seed = Date.now()) {
  const r = rng(seed);
  const ideas = [];
  const seen = new Set();
  while (ideas.length < 3) {
    let name = `${pick(FIRST, r)} ${pick(SECOND, r)}${pick(THIRD, r)}`.trim();
    if (new TextEncoder().encode(name).length > 32) continue;
    if (seen.has(name)) continue;
    seen.add(name);
    ideas.push({ name, symbol: tickerFor(name), description: pick(LINES, r) });
  }
  return ideas;
}
