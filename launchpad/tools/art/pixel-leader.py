"""Pixel-art coin logo: the supreme leader drawn by hand on a 40x40 grid over a red sunburst,
with a rocket, rendered at 25 px per cell (1000x1000 PNG, about 8 KB, so the launchpad uploads it
untouched). Left halves are drawn by hand and mirrored; one-sided details are painted on after.

    python3 tools/art/pixel-leader.py tools/art/kim-pixel.png
    python3 tools/art/pixel-leader.py tools/art/kim-pixel-shades.png shades
"""
import math, sys
from PIL import Image

N = 40
LEFT = [  # columns 0-19 of each row; the right half is the mirror image
    "....................",  # 0
    "....................",  # 1
    "....................",  # 2
    "....................",  # 3
    ".............ooooooo",  # 4  hair top outline
    "............oHHHHHHH",  # 5
    "...........oHHHHHHHH",  # 6
    "...........oHHHHHHHH",  # 7
    "..........oHHHHHHHHH",  # 8
    "..........oGHHHHHHHH",  # 9  shaved sides start
    ".........oGGHHHHHHHH",  # 10
    ".........oGGHHHHHHHH",  # 11
    ".........oGGFlllllll",  # 12 hairline
    ".........oGGFFFFFFFF",  # 13
    ".........oGFFFFFFFFF",  # 14
    ".........oFFFFbbbFFF",  # 15 brows
    ".......offFFFFFFFFFF",  # 16 ears
    ".......offFFFFeeeFFl",  # 17 eyes
    ".......offFFFFFFFFFl",  # 18
    ".......offFFFFFFFFFl",  # 19
    "........oFFccFFFFFff",  # 20 nose
    "........oFFccFFFFFFF",  # 21 cheeks
    "........oFFFFFFFFmmm",  # 22 mouth
    "........oFFFFFFFFFff",  # 23
    "........ofFFFFFFFFFF",  # 24 jowls
    ".........ofFFFFFFFFF",  # 25
    "..........offfffffff",  # 26 double chin
    "...........oKKKKKKKK",  # 27 collar
    "......oBBBBBkBBBBBBB",  # 28 shoulders
    "....oBBBBBBBBkBBBBBB",  # 29
    "...oBBBBBBBBBBBBBBBk",  # 30
    "..oBBBBBBBBBBBBBBBBk",  # 31
    "..oBBBBBBBBBBBBBBBBk",  # 32
    "..oBBBBBBBBBBBBBBBBk",  # 33
    "..oBBBBBBBBBBBBBBBBk",  # 34
    "..oBBBBBBBBBBBBBBBBk",  # 35
    "..oBBBBBBBBBBBBBBBBk",  # 36
    "..oBBBBBBBBBBBBBBBBk",  # 37
    "..oBBBBBBBBBBBBBBBBk",  # 38
    "..oBBBBBBBBBBBBBBBBk",  # 39
]
PAL = {
    "o": (24, 18, 28), "H": (30, 30, 40), "h": (62, 62, 80), "G": (150, 130, 128),
    "F": (240, 190, 150), "f": (212, 150, 112), "l": (255, 216, 182), "c": (238, 150, 128),
    "b": (40, 32, 36), "e": (24, 18, 28), "m": (150, 56, 44), "M": (190, 92, 76),
    "B": (44, 49, 62), "k": (30, 34, 44), "K": (78, 86, 104),
    "y": (255, 203, 69), "Y": (255, 236, 160), "r": (226, 52, 40), "R": (255, 110, 90),
    "S": (10, 10, 14), "s": (70, 74, 96), "w": (255, 255, 255),
}

def grid(shades=False):
    rows = [list(l + l[::-1]) for l in LEFT]
    put = lambda x, y, ch: rows[y].__setitem__(x, ch)
    # Hair: highlights swept back on top.
    for x, y in [(15, 6), (16, 6), (22, 6), (23, 6), (14, 8), (15, 8), (19, 7), (20, 7), (24, 8), (25, 8), (17, 10), (18, 10), (21, 10), (22, 10)]:
        put(x, y, "h")
    # Mouth: a smirk on one side.
    put(22, 22, "F"); put(22, 21, "m"); put(23, 21, "m")
    # Suit: centre buttons, chest pocket flaps and a red badge.
    for y in (31, 34, 37):
        put(19, y, "y"); put(20, y, "y")
    for x in list(range(6, 12)) + list(range(28, 34)):
        put(x, 33, "k")
    for x in (8, 9, 10):
        for y in (30, 31, 32):
            put(x, y, "r")
    put(9, 31, "y")
    if shades:  # pixel sunglasses over the brows and eyes
        for x in range(11, 29):
            put(x, 16, "S")
        for x in list(range(12, 18)) + list(range(22, 28)):
            put(x, 17, "S"); put(x, 18, "S")
        for x in (13, 14, 15, 16, 23, 24, 25, 26):
            put(x, 19, "S")
        for x, y in [(13, 17), (14, 17), (13, 18), (23, 17), (24, 17), (23, 18)]:
            put(x, y, "s")
        put(14, 18, "w"); put(24, 18, "w")
    return rows

def sunburst(x, y):
    a = math.atan2(y - 16, x - 19.5)
    ray = int(((a + math.pi) / (2 * math.pi)) * 18) % 2
    d = math.hypot(x - 19.5, y - 16) / 22
    base = (232, 60, 44) if ray else (200, 40, 30)
    k = max(0.7, 1 - 0.3 * d)
    return tuple(int(c * k) for c in base)

ROCKET = [  # 5x9, standing on its flame
    "..r..",
    ".rrr.",
    ".www.",
    ".wsw.",
    ".www.",
    "rwwwr",
    "r.w.r",
    "..y..",
    ".yYy.",
]
RPAL = {"r": (226, 52, 40), "w": (255, 242, 216), "s": (19, 33, 63), "y": (255, 203, 69), "Y": (255, 246, 200)}

def render(out, shades=False):
    rows = grid(shades)
    im = Image.new("RGB", (N, N))
    for y in range(N):
        for x in range(N):
            ch = rows[y][x]
            im.putpixel((x, y), sunburst(x, y) if ch == "." else PAL[ch])
    for dy, row in enumerate(ROCKET):  # rocket, top right of the field
        for dx, ch in enumerate(row):
            if ch != ".":
                im.putpixel((33 + dx, 1 + dy), RPAL[ch])
    for cx, cy in [(5, 7), (34, 15), (4, 19), (35, 24)]:  # twinkles
        for x, y in [(cx, cy), (cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)]:
            im.putpixel((x, y), (255, 214, 102) if (x, y) == (cx, cy) else (255, 180, 70))
    im.resize((N * 25, N * 25), Image.NEAREST).save(out)

if __name__ == "__main__":
    render(sys.argv[1], shades=len(sys.argv) > 2 and sys.argv[2] == "shades")
