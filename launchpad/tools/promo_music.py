"""Synthesizes the promo video's soundtrack (no samples, no licences): a 120 BPM electronic loop
in A minor with a riser into the drop and an impact under the outro card.

    python3 tools/promo_music.py out.wav <seconds> [<outro start, seconds>]
"""
import sys
import wave

import numpy as np

SR = 44100
BPM = 120
BEAT = 60 / BPM
BAR = 4 * BEAT
rng = np.random.default_rng(7)

PROGRESSION = [  # Am F C G, MIDI notes
    (57, [57, 60, 64]),
    (53, [53, 57, 60]),
    (48, [55, 60, 64]),
    (55, [55, 59, 62]),
]


def hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def tone(freq, dur, harmonics, decay=None, detune=0.0):
    """Additive tone: harmonics is a list of amplitudes; decay (per second) can rise with the harmonic."""
    n = int(SR * dur)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for k, a in enumerate(harmonics, start=1):
        f = freq * k * (1 + detune)
        if f > SR / 2.2:
            break
        env = np.exp(-t * decay * k) if decay else 1.0
        out += a * np.sin(2 * np.pi * f * t + rng.uniform(0, 2 * np.pi)) * env
    return out


def adsr(n, attack, release, total=None):
    env = np.ones(n)
    a = max(1, int(SR * attack))
    r = max(1, int(SR * release))
    env[:a] = np.linspace(0, 1, a)
    env[-r:] *= np.linspace(1, 0, r)
    return env


def kick():
    n = int(SR * 0.42)
    t = np.arange(n) / SR
    f = 46 + 120 * np.exp(-t * 30)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 7.5)
    click = rng.standard_normal(n) * np.exp(-t * 300) * 0.25
    return np.tanh((body + click) * 1.6) * 0.85


def clap():
    n = int(SR * 0.3)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    band = np.convolve(noise, np.ones(4) / 4, "same") - np.convolve(noise, np.ones(28) / 28, "same")
    env = np.zeros(n)
    for start in (0.0, 0.009, 0.018):
        env += np.where(t >= start, np.exp(-(t - start) * 60), 0) * 0.5
    env += np.where(t >= 0.02, np.exp(-(t - 0.02) * 16), 0)
    return band * env * 0.55


def hat(open_hat=False):
    n = int(SR * (0.22 if open_hat else 0.06))
    t = np.arange(n) / SR
    noise = np.diff(rng.standard_normal(n + 1))
    return noise * np.exp(-t * (14 if open_hat else 75)) * (0.11 if open_hat else 0.08)


def place(track, sound, at):
    i = int(at * SR)
    if i >= len(track):
        return
    j = min(len(track), i + len(sound))
    track[i:j] += sound[: j - i]


def main(path, seconds, outro_at=None):
    n = int(SR * seconds)
    drums = np.zeros(n)
    bass = np.zeros(n)
    pad_l = np.zeros(n)
    pad_r = np.zeros(n)
    arp = np.zeros(n)
    fx = np.zeros(n)
    duck = np.ones(n)

    drop = 2 * BAR  # beat starts after a two-bar intro
    outro = max(drop + BAR, outro_at if outro_at else seconds - 5.4)  # the outro card: impact, then pads only
    bars = int(np.ceil(seconds / BAR))

    for b in range(bars):
        root, chord = PROGRESSION[b % 4]
        start = b * BAR
        # Pad: three detuned voices per chord tone, soft attack, both channels.
        for note in chord:
            for det, side in ((-0.004, 0), (0.0, 2), (0.004, 1)):
                v = tone(hz(note), BAR, [1, 0.5, 0.33, 0.22, 0.14], detune=det) * 0.045
                v *= adsr(len(v), 0.35, 0.3)
                if side in (0, 2):
                    place(pad_l, v, start)
                if side in (1, 2):
                    place(pad_r, v, start)
        if start < drop - 1e-6 or start >= outro:
            continue
        # Drums: four on the floor, clap on 2 and 4, hats on the offbeats.
        for beat in range(4):
            at = start + beat * BEAT
            place(drums, kick(), at)
            k = int(at * SR)
            m = min(n, k + int(SR * 0.32))
            duck[k:m] = np.minimum(duck[k:m], 0.35 + 0.65 * np.linspace(0, 1, m - k) ** 1.6)
            if beat in (1, 3):
                place(drums, clap(), at)
            place(drums, hat(open_hat=(beat == 3)), at + BEAT / 2)
            place(drums, hat(), at + BEAT * 0.25)
        # Bass: root on the offbeat eighths.
        for e in range(8):
            if e % 2 == 1:
                v = tone(hz(root - 12), BEAT / 2 * 0.9, [1, 0.55, 0.35, 0.2, 0.12], decay=1.2) * 0.32
                v *= adsr(len(v), 0.005, 0.04)
                place(bass, v, start + e * BEAT / 2)
        # Arp: sixteenths through the chord over two octaves.
        notes = [chord[0] + 12, chord[1] + 12, chord[2] + 12, chord[1] + 24]
        for s in range(16):
            note = notes[s % 4] + (12 if s % 8 == 7 else 0)
            v = tone(hz(note), 0.24, [1, 0.45, 0.25, 0.12], decay=9) * 0.07
            place(arp, v * adsr(len(v), 0.002, 0.05), start + s * BEAT / 4)

    # Riser into the drop: noise and a rising whistle.
    rise = int(SR * (drop - 0.05))
    t = np.arange(rise) / SR
    k = (t / t[-1]) ** 2
    noise = np.diff(rng.standard_normal(rise + 1)) * 0.06 * k
    whistle = np.sin(2 * np.pi * np.cumsum(300 + 1700 * k) / SR) * 0.03 * k
    fx[:rise] += noise + whistle
    # Whoosh and impact under the outro.
    w = int(SR * 0.7)
    tw = np.arange(w) / SR
    whoosh = np.diff(rng.standard_normal(w + 1)) * np.sin(np.pi * tw / tw[-1]) ** 2 * 0.12
    place(fx, whoosh, outro - 0.7)
    tb = np.arange(int(SR * 2.4)) / SR
    boom = np.sin(2 * np.pi * np.cumsum(30 + 60 * np.exp(-tb * 6)) / SR) * np.exp(-tb * 1.6) * 0.75
    boom += rng.standard_normal(len(tb)) * np.exp(-tb * 18) * 0.25
    place(fx, boom, outro)

    # Drums, bass and arp stop dead just before the impact; the pads and the arp's echoes ring on.
    gate = np.ones(n)
    g0, g1 = int((outro - 0.035) * SR), int((outro - 0.015) * SR)
    gate[g0:g1] = np.linspace(1, 0, len(gate[g0:g1]))
    gate[g1:] = 0
    drums *= gate
    bass *= gate
    arp *= gate

    # Delay on the arp, ping-ponged.
    d = int(SR * BEAT * 0.75)
    arp_l = arp.copy()
    arp_r = np.zeros(n)
    arp_r[d:] += arp[:-d] * 0.45
    arp_l[2 * d :] += arp[: -2 * d] * 0.22

    pad_l *= duck
    pad_r *= duck
    bass *= duck
    left = drums + bass + pad_l + arp_l + fx
    right = drums + bass + pad_r + arp_r + fx
    stereo = np.stack([left, right], axis=1)
    stereo = np.tanh(stereo * 1.15)
    stereo /= np.max(np.abs(stereo)) / 0.89
    pcm = (stereo * 32767).astype("<i2")
    with wave.open(path, "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(pcm.tobytes())


if __name__ == "__main__":
    main(sys.argv[1], float(sys.argv[2]), float(sys.argv[3]) if len(sys.argv) > 3 else None)
