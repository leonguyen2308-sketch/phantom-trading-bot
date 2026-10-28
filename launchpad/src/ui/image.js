// Coin images: center-cropped to a square and squeezed under the free upload limit, plus a
// badge generator for launchers who have no picture.
const MAX_BYTES = 95 * 1024;
const SIDES = [512, 448, 384, 320, 256];
const QUALITIES = [0.9, 0.82, 0.74, 0.66, 0.58];

function canvasBlob(canvas, type, quality) {
  return new Promise((resolve) => canvas.toBlob(resolve, type, quality));
}

async function decode(file) {
  if ("createImageBitmap" in window) return createImageBitmap(file);
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    return img;
  } finally {
    URL.revokeObjectURL(url);
  }
}

// Returns { blob, type, width } ready for upload.
export async function prepareImage(file) {
  if (!file || !/^image\//.test(file.type)) throw new Error("pick an image file (png, jpg, webp or gif)");
  if (file.size > 15 * 1024 * 1024) throw new Error("that image is over 15 MB; pick a smaller one");
  const bitmap = await decode(file);
  const w = bitmap.width;
  const h = bitmap.height;
  if (!w || !h) throw new Error("that image could not be read");
  // Small square files (animated GIFs included) go up untouched.
  if (file.size <= MAX_BYTES && Math.abs(w - h) <= 2 && /^(image\/(png|jpeg|webp|gif))$/.test(file.type)) {
    return { blob: file, type: file.type, width: w };
  }
  const crop = Math.min(w, h);
  const sx = (w - crop) / 2;
  const sy = (h - crop) / 2;
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  for (const side of SIDES) {
    const size = Math.min(side, crop);
    canvas.width = size;
    canvas.height = size;
    ctx.clearRect(0, 0, size, size);
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(bitmap, sx, sy, crop, crop, 0, 0, size, size);
    for (const q of QUALITIES) {
      const webp = await canvasBlob(canvas, "image/webp", q);
      if (webp && webp.type === "image/webp" && webp.size <= MAX_BYTES) return { blob: webp, type: "image/webp", width: size };
      // Browsers without WebP encoding fall back to JPEG on a dark background.
      if (!webp || webp.type !== "image/webp") {
        const flat = document.createElement("canvas");
        flat.width = size;
        flat.height = size;
        const f = flat.getContext("2d");
        f.fillStyle = "#0a0d12";
        f.fillRect(0, 0, size, size);
        f.drawImage(canvas, 0, 0);
        const jpeg = await canvasBlob(flat, "image/jpeg", q);
        if (jpeg && jpeg.size <= MAX_BYTES) return { blob: jpeg, type: "image/jpeg", width: size };
      }
    }
  }
  throw new Error("could not shrink that image under 95 KB; try a simpler picture");
}

const PALETTES = [
  ["#ff4436", "#7a0f0a", "#ffcb45"],
  ["#ffcb45", "#a86a00", "#17110c"],
  ["#2f6bff", "#0b1d5c", "#fff2d8"],
  ["#19c37d", "#06452b", "#fff2d8"],
  ["#b04bff", "#3a0b66", "#ffcb45"],
];

function star(ctx, cx, cy, r) {
  ctx.beginPath();
  for (let i = 0; i < 10; i++) {
    const a = -Math.PI / 2 + (i * Math.PI) / 5;
    const rr = i % 2 ? r * 0.42 : r;
    ctx.lineTo(cx + Math.cos(a) * rr, cy + Math.sin(a) * rr);
  }
  ctx.closePath();
  ctx.fill();
}

// A propaganda-style badge with the ticker on it, as a PNG file.
export async function makeBadge(symbol, seed = Math.random()) {
  const [bg, deep, accent] = PALETTES[Math.floor(seed * PALETTES.length) % PALETTES.length];
  const size = 512;
  const c = document.createElement("canvas");
  c.width = size;
  c.height = size;
  const ctx = c.getContext("2d");
  const g = ctx.createRadialGradient(size / 2, size * 0.42, 40, size / 2, size / 2, size * 0.72);
  g.addColorStop(0, bg);
  g.addColorStop(1, deep);
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  // Sun rays.
  ctx.save();
  ctx.translate(size / 2, size * 0.42);
  ctx.fillStyle = "rgba(255,255,255,0.07)";
  for (let i = 0; i < 18; i++) {
    ctx.rotate((Math.PI * 2) / 18);
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(-26, -420);
    ctx.lineTo(26, -420);
    ctx.closePath();
    ctx.fill();
  }
  ctx.restore();
  ctx.fillStyle = accent;
  star(ctx, size / 2, size * 0.4, 118);
  const text = (symbol || "KIM").toUpperCase().slice(0, 10);
  await document.fonts?.load?.('800 80px "Bricolage Grotesque"').catch(() => {});
  let fontSize = 108;
  ctx.font = `800 ${fontSize}px "Bricolage Grotesque", system-ui, sans-serif`;
  while (ctx.measureText(`$${text}`).width > size - 70 && fontSize > 40) {
    fontSize -= 6;
    ctx.font = `800 ${fontSize}px "Bricolage Grotesque", system-ui, sans-serif`;
  }
  ctx.textAlign = "center";
  ctx.textBaseline = "alphabetic";
  ctx.lineJoin = "round";
  ctx.lineWidth = 14;
  ctx.strokeStyle = deep;
  ctx.strokeText(`$${text}`, size / 2, size * 0.86);
  ctx.fillStyle = "#fff8ea";
  ctx.fillText(`$${text}`, size / 2, size * 0.86);
  const blob = await canvasBlob(c, "image/png");
  return new File([blob], `${text.toLowerCase()}.png`, { type: "image/png" });
}
