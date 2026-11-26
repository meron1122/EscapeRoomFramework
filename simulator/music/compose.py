#!/usr/bin/env python3
"""Composes the simulator's soundtrack from code: one seamless loop per room, a finale and two effects.

Run: python3 simulator/music/compose.py   (needs numpy and ffmpeg with libmp3lame)
Everything is synthesised here (darksynth / EBM / industrial), so the music is original and free of licences.
Every loop is circular: reverb, delay, filter, chorus and limiter state wrap around the loop end, so it loops
without a seam.
"""
import functools
import os
import pathlib
import subprocess
import tempfile
import time
import wave

import numpy as np

SR = 44100
OUT = pathlib.Path(__file__).resolve().parent.parent / "godot" / "audio"
cache = functools.lru_cache(maxsize=None)  # notes repeat a lot; cached buffers are never modified in place


def hz(m):
    return 440.0 * 2 ** ((np.asarray(m, float) - 69) / 12)


def tv(n):
    return np.arange(n) / SR


def rng(seed):
    return np.random.default_rng(seed)


def st(x):
    return x if x.ndim == 2 else np.vstack([x, x])


def rms(x):
    return float(np.sqrt(np.mean(x ** 2)))


def db(v):
    return 20 * np.log10(max(v, 1e-12))


def spectral(x, lo=None, hi=None, slope=4):
    """Static low/high-pass in the frequency domain (circular, so also right for whole loops)."""
    X, f = np.fft.rfft(x), np.fft.rfftfreq(x.shape[-1], 1 / SR)
    f[0] = 1e-6
    if hi:
        X = X / np.sqrt(1 + (f / hi) ** slope)
    if lo:
        X = X / np.sqrt(1 + (lo / f) ** slope)
    return np.fft.irfft(X, x.shape[-1])


def edges(x, a=0.002, r=0.01):
    na, nr = max(1, int(a * SR)), max(1, int(r * SR))
    x = x.copy()
    x[..., :na] *= np.linspace(0, 1, na)
    x[..., -nr:] *= np.linspace(1, 0, nr)
    return x


def lfo(n, rate, phase=0.0):
    """Sine LFO rounded to whole cycles per buffer, so loops stay seamless."""
    rate = max(1, round(rate * n / SR)) * SR / n
    return np.sin(2 * np.pi * rate * tv(n) + phase)


# ---------- oscillators ----------

def osc(f, n, shape="saw", ph0=0.0, pw=0.5):
    """PolyBLEP saw or pulse (band-limited, no aliasing on high notes), or sine; f may vary per sample."""
    dt = np.broadcast_to(np.asarray(f, float), (n,)) / SR
    ph = (ph0 + np.cumsum(dt) - dt) % 1.0
    if shape == "sine":
        return np.sin(2 * np.pi * ph)

    def saw(p):
        s = 2 * p - 1
        a = p < dt
        u = p[a] / dt[a]
        s[a] -= u + u - u * u - 1
        b = p > 1 - dt
        u = (p[b] - 1) / dt[b]
        s[b] -= u * u + u + u + 1
        return s
    if shape == "saw":
        return saw(ph)
    return saw(ph) - saw((ph + pw) % 1.0)  # zero-mean for any pulse width


DETUNE = (-0.110, -0.063, -0.020, 0.0, 0.020, 0.062, 0.107)  # JP-8000 style spread


def supersaw(f, n, detune=0.25, width=0.9, seed=0):
    """Seven detuned saws spread across the stereo field."""
    r, out = rng(seed), np.zeros((2, n))
    for i, d in enumerate(DETUNE):
        v = osc(np.asarray(f) * (1 + d * detune), n, ph0=r.random()) * (1.0 if d == 0 else 0.7)
        p = (i - 3) / 3 * width
        out[0] += v * np.cos((p + 1) * np.pi / 4)
        out[1] += v * np.sin((p + 1) * np.pi / 4)
    return out / 3


# ---------- effects ----------

def svf(x, fc, q=0.707, mode="lp", loop=True):
    """Per-sample TPT state-variable filter with a per-sample cutoff, for resonant sweeps.
    loop=True first runs the loop's last half second, so the state at the start matches the end."""
    if x.ndim == 2:
        return np.vstack([svf(c, fc, q, mode, loop) for c in x])
    n = len(x)
    fc = np.broadcast_to(np.asarray(fc, float), (n,))
    w = min(n, SR // 2) if loop else 0
    x, fc = np.concatenate([x[n - w:], x]), np.concatenate([fc[n - w:], fc])
    g = np.tan(np.pi * np.clip(fc, 20, SR * 0.45) / SR)
    k = 1 / q
    a1 = 1 / (1 + g * (g + k))
    a2, a3 = g * a1, g * g * a1
    lp, bp = [0.0] * len(x), [0.0] * len(x)
    s1 = s2 = 0.0
    for i, (v0, b1, b2, b3) in enumerate(zip(x.tolist(), a1.tolist(), a2.tolist(), a3.tolist())):
        v3 = v0 - s2
        v1 = b1 * s1 + b2 * v3
        v2 = s2 + b2 * s1 + b3 * v3
        s1, s2 = 2 * v1 - s1, 2 * v2 - s2
        lp[i], bp[i] = v2, v1
    lp, bp = np.array(lp[w:]), np.array(bp[w:])
    return {"lp": lp, "bp": bp * k, "hp": x[w:] - k * bp - lp}[mode]


def drive(x, amount, os=2):
    """tanh distortion run at os x the sample rate (FFT resampling) so the new harmonics don't fold back."""
    n = x.shape[-1]
    y = np.tanh(np.fft.irfft(np.fft.rfft(x), n * os) * os * amount)
    return np.fft.irfft(np.fft.rfft(y)[..., : n // 2 + 1], n) / os


def crush(x, bits=8, hold=1):
    """Bit depth and sample-rate reduction."""
    if hold > 1:
        x = x[..., np.arange(x.shape[-1]) // hold * hold]
    q = 2.0 ** (bits - 1)
    return np.round(x * q) / q


def chorus(x, ms=9.0, depth=2.5, rate=0.4, mix=0.6):
    """Modulated delay with linear interpolation; the two sides get LFOs in quadrature for width."""
    x = st(x)
    n = x.shape[1]
    out = x.copy()
    for ch in range(2):
        pos = np.arange(n) - (ms + 2 * ch + depth * lfo(n, rate, ch * np.pi / 2)) * SR / 1000
        i0 = np.floor(pos).astype(np.int64)
        fr = pos - i0
        out[ch] += mix * (x[ch][i0 % n] * (1 - fr) + x[ch][(i0 + 1) % n] * fr)
    return out


def reverb(x, seconds=2.4, mix=0.3, hi=4500, seed=11):
    """Noise-impulse reverb by circular convolution: the tail wraps into the start of the loop."""
    x = st(x)
    n = x.shape[1]
    L = min(int(seconds * SR), n)
    out = np.zeros_like(x)
    for ch in range(2):
        ir = spectral(rng(seed + ch).standard_normal(L) * np.exp(-tv(L) / (seconds / 6.9)), hi=hi)
        ir[: int(0.012 * SR)] = 0
        ir /= np.sqrt(np.sum(ir ** 2))
        out[ch] = np.fft.irfft(np.fft.rfft(x[ch]) * np.fft.rfft(ir, n), n)
    return x + out * mix


def gated(x, gate=0.3, mix=1.1, seed=31):
    """80s gated reverb: a dense noise tail chopped off after `gate` seconds."""
    L = int(gate * SR)
    ir = spectral(rng(seed).standard_normal(L), lo=250, hi=7000) * np.linspace(1, 0.5, L)
    ir[-int(0.006 * SR):] *= np.linspace(1, 0, int(0.006 * SR))
    ir /= np.sqrt(np.sum(ir ** 2))
    m = len(x) + L - 1
    wet = np.fft.irfft(np.fft.rfft(x, m) * np.fft.rfft(ir, m), m)
    return np.concatenate([x, np.zeros(L - 1)]) + mix * wet


def echo(x, samples, feedback=0.45, mix=0.35, repeats=6, hi=None):
    """Ping-pong delay; np.roll wraps the repeats around the loop."""
    x = st(x)
    wet = spectral(x, hi=hi) if hi else x
    out, g = x.copy(), mix
    for k in range(1, repeats + 1):
        s = np.roll(wet, samples * k, axis=1)
        out += (s[::-1] if k % 2 else s) * g
        g *= feedback
    return out


def lag(x, sec):
    """One-pole smoothing (glides, de-clicking) as a circular FFT convolution."""
    n = x.shape[-1]
    k = np.exp(-np.arange(n) / (sec * SR))
    return np.fft.irfft(np.fft.rfft(x) * np.fft.rfft(k / k.sum()), n)


def since(n, onsets):
    """Per sample: samples since the latest onset (wrapping around the loop) and that onset's index."""
    on = np.unique(np.asarray(onsets, np.int64) % n)
    marks = np.full(n, -1)
    marks[on] = np.arange(len(on))
    last = np.maximum.accumulate(marks)
    d = np.arange(n) - on[last]  # before the first onset last == -1, i.e. the loop's final onset
    d[last < 0] += n
    return d, last


def stutter(x, start, length, piece, fade=64):
    """Stutter edit: the region replays its first `piece` samples over and over (de-clicked)."""
    n = x.shape[1]
    seg = edges(x[:, (start + np.arange(piece)) % n], fade / SR, fade / SR)
    x[:, (start - fade + np.arange(fade)) % n] *= np.linspace(1, 0, fade)
    x[:, (start + np.arange(length)) % n] = np.tile(seg, -(-length // piece))[:, :length]
    x[:, (start + length + np.arange(fade)) % n] *= np.linspace(0, 1, fade)


def limit(x, ceiling, block=128, release=0.06):
    """Look-ahead peak limiter on blocks; the release state laps the loop twice so it wraps cleanly."""
    n = x.shape[1]
    nb = -(-n // block)
    p = np.abs(np.pad(x, ((0, 0), (0, nb * block - n)), mode="wrap")).max(axis=0).reshape(nb, block).max(axis=1)
    want = np.minimum(1.0, ceiling / np.maximum(p, 1e-9)).tolist()
    rel = 1 - np.exp(-block / (release * SR))
    g, cur = [1.0] * nb, 1.0
    for _ in range(2):
        for i, w in enumerate(want):
            cur = min(w, cur + (1 - cur) * rel)
            g[i] = cur
    g = np.array(g)
    g = np.minimum(g, np.minimum(np.roll(g, 1), np.roll(g, -1)))  # each block's gain also covers its neighbours
    gain = np.interp(np.arange(n), np.arange(nb) * block + block / 2, g, period=nb * block)
    return np.clip(x * gain, -ceiling, ceiling)


def master(x, rms_db=-10.0, ceiling_db=-3.0):
    """Oversampled soft clip, then the limiter pushed until the loop sits at the target loudness.
    The ceiling leaves room for the ~1.5 dB the MP3 encoder adds to the peaks."""
    x = x - x.mean(axis=1, keepdims=True)
    x = drive(x * (0.4 / rms(x)), 1.0)  # shaves ~3 dB off the drum transients so the limiter works less
    ceiling, g = 10 ** (ceiling_db / 20), 10 ** (rms_db / 20) / rms(x)
    for _ in range(12):
        y = limit(x * g, ceiling)
        g *= 10 ** (1.5 * (rms_db - db(rms(y))) / 20)  # limiting flattens the response, so over-correct
    return y


def report(name, x):
    m = x.mean(axis=0)
    P, f = np.abs(np.fft.rfft(m)) ** 2, np.fft.rfftfreq(len(m), 1 / SR)
    lo, mid, hi = (P[(f >= a) & (f < b)].sum() / P.sum() * 100 for a, b in ((0, 120), (120, 2000), (2000, SR)))
    print(f"{name:6s} {x.shape[1] / SR:5.1f} s  rms {db(rms(x)):6.1f} dBFS  peak {db(np.abs(x).max()):5.1f} dBFS  "
          f"energy <120 Hz {lo:4.1f}%  120 Hz-2 kHz {mid:4.1f}%  >2 kHz {hi:4.1f}%")


def mixdown(stems):
    """Sums the stems; STEMS=1 in the environment prints each stem's level and spectral split, for tuning."""
    if os.environ.get("STEMS"):
        for k, v in stems.items():
            report(f"  {k}", st(v))
    return sum(st(v) for v in stems.values())


def write(name, stereo):
    OUT.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(stereo.T, -1, 1) * 32767).astype("<i2")
    with tempfile.TemporaryDirectory() as tmp:
        wav = pathlib.Path(tmp) / f"{name}.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "192k",
                        str(OUT / f"{name}.mp3")], check=True)
    report(name, stereo)


# ---------- drums ----------

@cache
def kick(base=48.0, punch=200.0, decay=0.28, dist=3.0):
    n = int(0.5 * SR)
    t = tv(n)
    f = base + punch * np.exp(-t / 0.016) + base * 0.5 * np.exp(-t / 0.08)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / decay)
    click = spectral(rng(1).standard_normal(n), lo=2000, hi=9000) * np.exp(-t / 0.003)
    return edges(np.tanh(dist * (body + 0.5 * click)) / np.tanh(dist), a=0.0003, r=0.03)


@cache
def snare(tone=190.0, gate=0.0, seed=2):
    n = int(0.25 * SR)
    t = tv(n)
    noise = spectral(rng(seed).standard_normal(n), lo=900, hi=11000) * np.exp(-t / 0.07)
    ph = 2 * np.pi * np.cumsum(tone * (1 + 0.5 * np.exp(-t / 0.01))) / SR
    body = (np.sin(ph) + 0.4 * np.sin(1.62 * ph)) * np.exp(-t / 0.05)
    x = np.tanh(2.0 * (0.5 * noise + body)) * 0.8
    return edges(gated(x, gate) if gate else x, a=0.0003, r=0.01)


@cache
def tom(f0, gate=0.3):
    n = int(0.45 * SR)
    t = tv(n)
    body = np.sin(2 * np.pi * np.cumsum(f0 * (1 + 0.7 * np.exp(-t / 0.03))) / SR) * np.exp(-t / 0.2)
    x = np.tanh(2.5 * (body + 0.15 * spectral(rng(12).standard_normal(n), lo=600) * np.exp(-t / 0.03)))
    return edges(gated(x, gate, 0.8), a=0.0003)


@cache
def clap(seed=3):
    n = int(0.35 * SR)
    t = tv(n)
    noise = spectral(rng(seed).standard_normal(n), lo=900, hi=5000)
    burst = sum(np.exp(-np.maximum(t - d, 0) / 0.008) * (t >= d) for d in (0, 0.009, 0.018, 0.027)) * 0.6
    tail = np.exp(-t / 0.11) * (t >= 0.027)
    return edges(np.tanh(0.6 * noise * (burst + tail)), a=0.0003)


@cache
def hat(decay=0.014, seed=4):
    n = int(min(2.0, decay * 6 + 0.02) * SR)
    t = tv(n)
    return edges(spectral(rng(seed).standard_normal(n), lo=7500) * np.exp(-t / decay) * 0.35, a=0.0003, r=0.004)


@cache
def metal(f0=330.0, decay=0.08, seed=5):
    """Inharmonic pulse cluster plus an FM ring: pipes, anvils, industrial hits."""
    n = int(min(1.5, decay * 7 + 0.02) * SR)
    t = tv(n)
    r = rng(seed)
    cluster = sum(osc(f0 * k, n, "pulse", ph0=r.random()) for k in (1, 1.4823, 1.8019, 2.5476, 2.6305, 3.8968))
    ring = np.sin(2 * np.pi * f0 * 2.76 * t + 2.5 * np.exp(-t / 0.03) * np.sin(2 * np.pi * f0 * 3.9 * t))
    x = spectral(cluster, lo=f0 * 1.5) * 0.25 * np.exp(-t / decay) + 0.6 * ring * np.exp(-t / (decay * 1.6))
    return edges(np.tanh(1.8 * x) * 0.6, a=0.0003)


@cache
def blip(m, dur=0.06, shape="pulse", bits=6):
    """Notification ping: a short chirped, bit-crushed tone."""
    n = int(dur * SR)
    t = tv(n)
    x = osc(hz(m) * (1 + 0.06 * np.exp(-t / 0.005)), n, shape, pw=0.3) * np.exp(-t / (dur / 3))
    return edges(crush(x * 0.6, bits, 2), a=0.0005, r=0.004)


@cache
def riser(sec, lo=250.0, hi=9000.0, seed=9):
    """White noise through a resonant band-pass sweeping up, swelling into the downbeat."""
    n = int(sec * SR)
    u = np.arange(n) / n
    fc = lo * (hi / lo) ** (u ** 1.6)
    out = np.vstack([svf(rng(seed + c).standard_normal(n), fc * (1 + 0.05 * c), q=5, mode="bp", loop=False) for c in (0, 1)])
    return edges(out * u ** 2.5, a=0.01, r=0.004)


@cache
def sweep_up(sec, m0=40, m1=76):
    """Pitch riser: a supersaw gliding up, getting louder."""
    n = int(sec * SR)
    u = np.arange(n) / n
    return edges(spectral(supersaw(hz(m0 + (m1 - m0) * u ** 1.5), n, 0.4, seed=4), lo=200) * u ** 2, a=0.01, r=0.004)


@cache
def impact(base=60.0):
    """Sub drop, distorted boom and a noise crack for section downbeats."""
    n = int(2.5 * SR)
    t = tv(n)
    f = 30 + base * np.exp(-t / 0.35) + 150 * np.exp(-t / 0.015)
    boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.9)
    crack = spectral(rng(10).standard_normal(n), hi=3000) * np.exp(-t / 0.25)
    return edges(np.tanh(2.5 * (boom + 0.6 * crack)), a=0.0003, r=0.3)


# ---------- synths ----------

@cache
def bass_note(m, dur, pulse=0.0):
    """Raw bass oscillator (saw plus an optional pulse layer); the bus filter and distortion shape it."""
    n = int(dur * SR)
    f = hz(m)
    return edges(osc(f, n) + pulse * osc(f * 1.004, n, "pulse", pw=0.3), a=0.001, r=0.006)


@cache
def sub_note(m, dur):
    m += 12 * (m < 28)  # keep the sub at E1 (41 Hz) or above; lower only eats headroom
    return edges(osc(hz(m), int(dur * SR), "sine"), a=0.004, r=0.01)


@cache
def reese_note(m, dur):
    """Reese: saws a few cents apart beating against each other, plus a square for body."""
    n = int(dur * SR)
    f = hz(m)
    x = osc(f * 2 ** (-0.14 / 12), n) + osc(f * 2 ** (0.14 / 12), n, ph0=0.37) + 0.6 * osc(f, n, "pulse", ph0=0.11)
    return edges(x / 2, a=0.004, r=0.015)


@cache
def pluck(m, dur, shape="pulse", decay=0.12, pw=0.3):
    n = int(dur * SR)
    return edges(osc(hz(m), n, shape, pw=pw) * np.exp(-tv(n) / decay), a=0.0008, r=0.008)


@cache
def stab(chord, dur, detune=0.15, seed=0, decay=None):
    """Short supersaw chord."""
    n = int(dur * SR)
    x = sum(supersaw(hz(m), n, detune, seed=seed + i) for i, m in enumerate(chord)) / len(chord)
    return edges(x * np.exp(-tv(n) / (decay or max(dur / 2.5, 0.02))), a=0.001, r=0.01)


@cache
def pad(chord, dur, detune=0.12, hi=1800, seed=0):
    """Wide detuned supersaw chord with a slow attack."""
    n = int((dur + 0.5) * SR)
    t = tv(n)
    x = sum(supersaw(hz(m), n, detune, seed=seed + i) for i, m in enumerate(chord)) / len(chord)
    env = np.minimum(t / 0.3, 1) * np.where(t < dur, 1, np.exp(-(t - dur) / 0.15))
    return edges(spectral(x, hi=hi) * env, r=0.05)


@cache
def fm_bell(m, dur, index=3.0, ratio=3.5):
    n = int(dur * SR)
    t, f = tv(n), hz(m)
    return edges(np.sin(2 * np.pi * f * t + index * np.exp(-t / 0.2) * np.sin(2 * np.pi * ratio * f * t)) * np.exp(-t / (dur / 3)))


@cache
def digital_stab(chord, dur):
    """Sterile digital chord stab: thin pulses and FM, bit-crushed and sample-rate reduced."""
    n = int(dur * SR)
    t = tv(n)
    x = sum(0.5 * osc(hz(m), n, "pulse", pw=0.2) + np.sin(2 * np.pi * hz(m) * t + 2 * np.exp(-t / 0.05)
                                                          * np.sin(2 * np.pi * 4 * hz(m) * t)) for m in chord)
    return edges(spectral(crush(x * np.exp(-t / 0.08) / len(chord), 5, 3), lo=400), a=0.0005)


@cache
def lead_note(m, dur, bend=0.0, detune=0.3):
    """Distorted supersaw note; bend drags the pitch (semitones) down over the note like a tape stop."""
    n = int(dur * SR)
    u = np.arange(n) / n
    x = drive(supersaw(hz(m + bend * u ** 2), n, detune), 2.0)
    return edges(spectral(x, lo=150, hi=7000) * (1 - 0.3 * u), a=0.002, r=0.04)


VOWELS = {"ah": ((750, 1.0, 90), (1150, 0.6, 110), (2600, 0.25, 160)),
          "oh": ((450, 1.0, 80), (800, 0.6, 90), (2830, 0.15, 160))}


@cache
def choir(chord, dur, vowel="ah", seed=3):
    """Additive choir: harmonics weighted by vowel formants, three vibrato voices per note across the field."""
    n = int((dur + 0.8) * SR)
    t, r = tv(n), rng(seed)
    out = np.zeros((2, n))
    for m in chord:
        for v in range(3):
            f0 = float(hz(m)) * 2 ** ((v - 1) * 0.12 / 12)
            ph = 2 * np.pi * np.cumsum(f0 * (1 + 0.006 * np.sin(2 * np.pi * (4.5 + r.random()) * t + 6.3 * r.random()))) / SR
            voice = np.zeros(n)
            for k in range(1, int(5000 / f0) + 1):
                a = sum(g / (1 + ((k * f0 - F) / bw) ** 2) for F, g, bw in VOWELS[vowel]) + 0.02 / k
                voice += a * np.sin(k * ph + 6.3 * r.random())
            p = (v - 1) * 0.8
            out[0] += voice * np.cos((p + 1) * np.pi / 4)
            out[1] += voice * np.sin((p + 1) * np.pi / 4)
    env = np.minimum(t / 0.6, 1) * np.where(t < dur, 1, np.exp(-(t - dur) / 0.25))
    return edges(out * env / (3 * len(chord)), r=0.05)


# ---------- arrangement ----------

class Track:
    def __init__(self, bpm, bars):
        self.beat = 60 / bpm
        self.n = int(round(bars * 4 * self.beat * SR))
        self.buses, self.hits = {}, {}

    def at(self, beats):
        return int(round(beats * self.beat * SR))

    def add(self, bus, beats, sig, pan=None, gain=1.0):
        """Places a sound (a mono bus when pan is None and the sound is mono); what runs past the end wraps."""
        if pan is not None and sig.ndim == 1:
            sig = np.vstack([sig * np.cos((pan + 1) * np.pi / 4), sig * np.sin((pan + 1) * np.pi / 4)])
        sig = sig[..., : self.n] * gain
        buf = self.buses.setdefault(bus, np.zeros(sig.shape[:-1] + (self.n,)))
        i = self.at(beats) % self.n
        k = min(sig.shape[-1], self.n - i)
        buf[..., i:i + k] += sig[..., :k]
        buf[..., : sig.shape[-1] - k] += sig[..., k:]
        self.hits.setdefault(bus, {})[i] = gain

    def env(self, bus, decay):
        """Decay envelope retriggered by the bus's notes and scaled by their gain (filter envelopes)."""
        hits = self.hits[bus]
        d, last = since(self.n, list(hits))
        acc = np.array([hits[i] for i in sorted(hits)])
        return acc[last] * np.exp(-d / (decay * SR)) * np.minimum(1, d / (0.002 * SR))

    def duck(self, bus, depth=0.6, release=0.15):
        """Sidechain gain that dips at every hit on the bus and recovers."""
        d, _ = since(self.n, list(self.hits[bus]))
        return 1 - depth * np.exp(-d / (release * SR)) * np.minimum(1, d / (0.002 * SR))

    def curve(self, points):
        """Piecewise-linear automation over the loop; points are (beat, value)."""
        b, v = zip(*points)
        return np.interp(np.arange(self.n) / (self.beat * SR), b, v)

    def line(self, notes, glide=0.04):
        """A monophonic melody as loop-length pitch (MIDI, gliding), gate and time-since-note curves.
        notes are (beat, beats, midi)."""
        pitch, gate, starts = np.full(self.n, np.nan), np.zeros(self.n), []
        for b, l, m in notes:
            idx = np.arange(self.at(b), self.at(b + l)) % self.n
            pitch[idx], gate[idx] = m, 1
            starts.append(self.at(b))
        last = np.maximum.accumulate(np.where(np.isnan(pitch), -1, np.arange(self.n)))
        last[last < 0] = last[-1]  # before the first note, hold the loop's last note
        return lag(pitch[last], glide), lag(gate, 0.004), since(self.n, starts)[0]

    def edit(self, mix, edits):
        """Stutter edits on the mix; edits are (beat, beats, piece beats)."""
        for b, l, p in edits:
            stutter(mix, self.at(b), self.at(l), self.at(p))


def rolls(bars, at=3.0):
    """Progressive stutter (1/8, 1/16, 1/32 pieces) over the last beat(s) of the given bars."""
    return [e for bar in bars for e in ((bar * 4 + at, 0.5, 0.25), (bar * 4 + at + 0.5, 0.25, 0.0625),
                                        (bar * 4 + at + 0.75, 0.25, 0.03125))]


def hol():
    """Room 1, the corporation's lobby: cold corporate menace. F minor, 127 bpm, 32 bars.
    A (bars 0-15): Fm Db Bbm C. B (16-31): Fm Gb Db C, open hats, cold pad and the Z jingle on a supersaw."""
    tr = Track(127, 32)
    q = tr.beat / 4
    A = [(41, (65, 68, 72, 77)), (37, (65, 68, 73, 77)), (46, (65, 70, 73, 77)), (36, (64, 67, 72, 76))]
    B = [(41, (65, 68, 72, 77)), (42, (66, 70, 73, 78)), (37, (65, 68, 73, 77)), (36, (64, 67, 72, 76))]
    steps = (0, 2, 1, 3, 2, 0, 3, 1, 0, 2, 1, 3, 2, 3, 1, 2)
    jingle = ((72, 0.5), (68, 0.5), (78, 1.5))  # the corporate "Z" (once G E C, bright) turned to 5, b3, b9
    for bar in range(32):
        b0, late = bar * 4, bar >= 16
        root, chord = (B if late else A)[bar // 2 % 4]
        for k in range(3 if bar in (15, 31) else 4):
            tr.add("kick", b0 + k, kick(44.0, 210.0, 0.22, 3.0))
        for k in (1, 3):
            tr.add("clap", b0 + k, clap(), pan=0.0)
            tr.add("clap", b0 + k, snare(205.0), pan=0.0, gain=0.5)
        for s in range(16):
            tr.add("hat", b0 + s / 4, hat(seed=4 + s % 3), pan=0.3, gain=(0.5, 0.25, 0.9, 0.3)[s % 4])
            if s % 4:
                tr.add("bass", b0 + s / 4, bass_note(root + 12 * (s % 4 == 3), 0.9 * q, 0.5), gain=1.0 if s % 4 == 1 else 0.7)
            tr.add("arp", b0 + s / 4, pluck(chord[steps[s]] + 12 * (late and s % 8 >= 4), 0.95 * q, "pulse", 0.09),
                   gain=1.0 if s % 4 == 0 else 0.8)
            if bar >= 8 and s % 8 in (3, 6):
                tr.add("tick", b0 + s / 4, blip(103, 0.02, "pulse", 4), pan=-0.6 if s % 8 == 3 else 0.6)
        tr.add("sub", b0, sub_note(root - 12, 4 * tr.beat))
        if late:
            for k in range(4):
                tr.add("hat", b0 + k + 0.5, hat(0.1, 7), pan=-0.25, gain=0.6)
            if bar % 2 == 0:
                tr.add("pad", b0, pad(chord, 8 * tr.beat, seed=bar))
        if bar >= 8:
            for b in (0.75, 2.5, 3.25) if late else (0.75, 2.5):
                tr.add("stab", b0 + b, digital_stab(tuple(m + 12 for m in chord[1:]), 0.4 * tr.beat), pan=0.0)
        if bar in (0, 8) or (late and bar % 4 == 0):
            for i, (m, l) in enumerate(jingle):
                tr.add("jingle", b0 + 2 + i * 0.5, crush(fm_bell(m + 12, l * tr.beat + 0.3), 6, 2), pan=0.25)
                if late:
                    tr.add("lead", b0 + 2 + i * 0.5, lead_note(m - 12, l * tr.beat, -12.0 if bar == 28 and i == 2 else 0.0))
    tr.add("fx", 56, riser(8 * tr.beat))
    tr.add("fx", 120, riser(8 * tr.beat))
    for b in (0, 64):
        tr.add("fx", b, impact(), pan=0.0)

    open_ = tr.curve([(0, 1.0), (32, 1.4), (64, 2.0), (112, 2.4), (128, 1.0)])  # filters open towards B
    duck, soft = tr.duck("kick", 0.8, 0.12), tr.duck("kick", 0.45, 0.12)
    bass = drive(svf(tr.buses["bass"], 110 * open_ * 2 ** (4 * tr.env("bass", 0.07)), q=2.5) * 3, 1.0)
    bass = spectral(bass, hi=4500) * duck
    arp = svf(tr.buses["arp"], 450 * open_ ** 1.5 * 2 ** (2.5 * tr.env("arp", 0.06)), q=3.0)
    arp = reverb(echo(arp, tr.at(0.75), 0.4, 0.35, hi=3000), 1.8, 0.25) * soft
    mix = mixdown({
        "kick": tr.buses["kick"] * 0.32, "bass": bass * 0.3, "sub": tr.buses["sub"] * duck * 0.19,
        "clap": reverb(tr.buses["clap"], 1.2, 0.3) * 1.15, "hat": tr.buses["hat"] * 1.5, "arp": arp * 0.18,
        "tick": tr.buses["tick"] * 0.9, "stab": reverb(echo(tr.buses["stab"], tr.at(0.75), 0.45, 0.4), 2.0, 0.3) * 1.0 * soft,
        "pad": reverb(tr.buses["pad"], 2.5, 0.4) * 0.62 * duck,
        "jingle": reverb(echo(tr.buses["jingle"], tr.at(0.5), 0.4, 0.3), 2.5, 0.4) * 0.56,
        "lead": reverb(echo(tr.buses["lead"], tr.at(0.75), 0.35, 0.3), 2.5, 0.35) * 0.57,
        "fx": reverb(tr.buses["fx"], 2.5, 0.4) * 0.36})
    tr.edit(mix, [(7 * 4 + 3.5, 0.5, 0.125), (23 * 4 + 3.5, 0.5, 0.125)] + rolls((15, 31)))
    write("hol", master(mix))


def pietro():
    """Room 2, the addiction lab: EBM / industrial groove. G minor, 125 bpm, 32 bars.
    A: squelchy 16th bass riff (G G G G Eb Eb F F), rumble kick, metal percussion, notification glitches.
    B: new riff (G G Bb Bb Eb Eb D D), gritty supersaw stabs and a square-wave hook."""
    tr = Track(125, 32)
    q = tr.beat / 4
    roots = ((31, 31, 31, 31, 27, 27, 29, 29), (31, 31, 34, 34, 27, 27, 26, 26))
    riffs = ((0, 0, 12, 0, 0, 12, 0, 0, 12, 0, 0, 12, 0, 10, 12, 7), (0, 12, 0, 0, 12, 0, 0, 12, 0, 0, 12, 0, 15, 12, 10, 12))
    triad = {31: (0, 3, 7), 34: (0, 4, 7), 27: (0, 4, 7), 29: (0, 4, 7), 26: (0, 4, 7)}
    hook = ((0, 0.5, 7), (0.75, 0.25, 10), (1, 0.5, 14), (1.75, 0.25, 12), (2.5, 0.5, 10), (3, 0.75, 8))
    pings = (79, 82, 84, 86, 89, 91, 94)
    r = rng(21)
    for bar in range(32):
        b0, late = bar * 4, bar >= 16
        root, riff = roots[late][bar % 8], riffs[late]
        for k in range(4):
            tr.add("kick", b0 + k, kick(50.0, 160.0, 0.25, 5.0))
        if bar >= 8:
            for k in (1, 3):
                tr.add("snare", b0 + k, clap(), pan=0.0)
                tr.add("snare", b0 + k, metal(540.0, 0.05, 6), pan=0.1, gain=0.5)
                tr.add("snare", b0 + k, snare(220.0), pan=0.0, gain=0.5)
        for s in range(16):
            if bar >= 4 or s % 2 == 0:
                tr.add("hat", b0 + s / 4, crush(hat(seed=8 + s % 2), 6, 2), pan=-0.3, gain=(0.7, 0.3, 0.5, 0.3)[s % 4])
            tr.add("bass", b0 + s / 4, bass_note(root + riff[s], 0.8 * q, 0.6), gain=1.0 if riff[s] else 0.65)
            if bar >= 4 and s in ((2, 7, 10, 13, 15) if late else (2, 7, 10, 13)):
                tr.add("metal", b0 + s / 4, metal(330.0 if s % 2 else 495.0, 0.06, s), pan=-0.6 if s % 2 else 0.6, gain=0.5)
        if bar % 2:
            tr.add("metal", b0 + 3.75, metal(880.0, 0.22, 3), pan=0.4, gain=0.6)
        tr.add("sub", b0, sub_note(root, 4 * tr.beat))
        if late:
            for k in range(4):
                tr.add("hat", b0 + k + 0.5, hat(0.08, 9), pan=0.3, gain=0.5)
            for b in (0.5, 2.5):
                tr.add("stab", b0 + b, stab(tuple(root + 36 + i for i in triad[root]), 0.35 * tr.beat, 0.1), gain=0.8)
            for b, l, iv in hook:
                tr.add("hook", b0 + b, pluck(root + 36 + iv + 12 * (bar >= 24 and bar % 2 == 1), l * tr.beat, "pulse", 0.25, 0.5))
        for _ in range(r.integers(1, 4 if late else 3)):  # notification glitches, some stuttering
            pos, m, pan = r.integers(0, 16) / 4, int(pings[r.integers(len(pings))]), r.uniform(-0.7, 0.7)
            for j in range(6 if r.random() < 0.3 else 1):
                tr.add("blip", b0 + pos + j * 0.125, blip(m, 0.05), pan=pan, gain=0.85 ** j)
            tr.add("blip", b0 + pos + 0.18, blip(m + 5, 0.07), pan=pan)
    tr.add("fx", 56, riser(8 * tr.beat))
    tr.add("fx", 120, riser(8 * tr.beat))
    tr.add("fx", 64, impact(), pan=0.0)

    duck = tr.duck("kick", 0.45, 0.1)
    swell = 0.5 - 0.5 * lfo(tr.n, 4 * SR / tr.n, np.pi / 2)  # filter opens and closes every 8 bars
    bass = drive(svf(tr.buses["bass"], (170 + 280 * swell) * 2 ** (4.5 * tr.env("bass", 0.05)), q=4.0) * 2.5, 1.0)
    bass = (0.6 * bass + 0.4 * crush(bass, 7, 2)) * duck
    k = tr.buses["kick"]
    rumble = drive(spectral(reverb(k, 2.0, 1.0)[0] - k, hi=150) * 3, 1.0) * tr.duck("kick", 0.95, 0.22)
    hook = reverb(echo(chorus(spectral(tr.buses["hook"], hi=5000)), tr.at(0.75), 0.4, 0.35, hi=3500), 2.0, 0.3)
    mix = mixdown({
        "kick": k * 0.29, "rumble": rumble * 0.18, "bass": bass * 0.28, "sub": tr.buses["sub"] * duck * 0.14,
        "snare": reverb(tr.buses["snare"], 0.9, 0.3, hi=6000) * 1.5, "hat": tr.buses["hat"] * 1.85,
        "metal": reverb(tr.buses["metal"], 1.2, 0.3) * 0.67, "blip": reverb(echo(tr.buses["blip"], tr.at(0.5), 0.35, 0.3), 1.5, 0.3) * 0.6,
        "stab": reverb(drive(tr.buses["stab"] * 2, 1.0), 1.5, 0.3) * 1.9 * duck, "hook": hook * 0.17,
        "fx": reverb(tr.buses["fx"], 2.5, 0.4) * 0.5})
    tr.edit(mix, [(b * 4 + 3.5, 0.5, 0.125) for b in (3, 7, 11, 19, 23, 27)] + rolls((15, 31)))
    write("pietro", master(mix))


def jadro():
    """Room 3, the server core: heavy darksynth. D minor, 112 bpm, 32 bars.
    A (0-15): Dm Bb Gm A, 3-3-2 reese bass, gated snare, dark choir; arp from bar 8.
    B (16-31): Bb C Dm A, four-on-the-floor, galloping reese, screaming supersaw lead."""
    tr = Track(112, 32)
    q = tr.beat / 4
    A = [(38, (50, 57, 62, 65)), (34, (50, 58, 62, 65)), (31, (50, 58, 62, 67)), (33, (49, 57, 61, 64))]
    B = [(34, (50, 58, 62, 65)), (36, (48, 55, 60, 64)), (38, (50, 57, 62, 65)), (33, (49, 57, 61, 64))]
    melody = ((0, 3, 74), (3, 1, 72), (4, 2, 70), (6, 1, 72), (7, 1, 74), (8, 3, 76), (11, 1, 77), (12, 2, 79),
              (14, 2, 76), (16, 2, 77), (18, 1, 76), (19, 1, 74), (20, 4, 81), (24, 2, 73), (26, 2, 76), (28, 3.5, 74))
    arp_steps = (0, 1, 2, 3, 2, 1)
    for bar in range(32):
        b0, late = bar * 4, bar >= 16
        root, chord = (B if late else A)[bar // 2 % 4]
        for k in ((0, 1, 2, 3, 3.5) if bar % 2 else (0, 1, 2, 3)) if late else (0, 2, 2.75):
            tr.add("kick", b0 + k, kick(46.0, 230.0, 0.28, 3.5))
        for k in (1, 3):
            tr.add("snare", b0 + k, snare(185.0, 0.32), pan=0.0)
        for s in range(16 if bar >= 8 else 8):
            tr.add("hat", b0 + s * (0.25 if bar >= 8 else 0.5), hat(seed=13 + s % 2), pan=0.35, gain=(0.8, 0.35)[s % 2])
        if bar % 2 == 0:
            tr.add("choir", b0, choir(chord, 8 * tr.beat, "oh" if late else "ah"))
        tr.add("sub", b0, sub_note(root - 12, 4 * tr.beat))
        if late:
            for s in range(16):
                if s % 4:
                    tr.add("reese", b0 + s / 4, reese_note(root, 0.9 * q), gain=1.0 if s % 4 == 1 else 0.75)
            for k in range(4):
                tr.add("ride", b0 + k, metal(1250.0, 0.18, 4), pan=-0.4)
        else:
            for b, l in ((0, 1.5), (1.5, 1.0), (2.5, 1.5)):
                tr.add("reese", b0 + b, reese_note(root, l * tr.beat - 0.01))
        if bar >= 8:
            for s in range(16):
                tr.add("arp", b0 + s / 4, pluck(chord[arp_steps[(bar * 16 + s) % 6]] + 12, 0.9 * q, "pulse", 0.1, 0.35))
    for i, f in enumerate((170.0, 140.0, 115.0, 95.0)):
        tr.add("snare", 15 * 4 + 3 + i * 0.25, tom(f), pan=0.5 - 0.33 * i, gain=0.5)
    for b in (0, 64, 96):
        tr.add("ride", b, hat(0.6, 14), pan=0.3, gain=2.0)
    tr.add("fx", 56, riser(8 * tr.beat))
    tr.add("fx", 120, riser(8 * tr.beat))
    for b in (0, 64):
        tr.add("fx", b, impact(55.0), pan=0.0)
    pitch, gate, d = tr.line([(64 + b, l, m) for b, l, m in melody]
                             + [(96 + b, l, m + 12 * (16 <= b < 24)) for b, l, m in melody], glide=0.05)

    duck = tr.duck("kick", 0.6, 0.18)
    wob = 0.5 + 0.5 * lfo(tr.n, 16 * SR / tr.n)  # one filter wobble per 2 bars
    fc = tr.curve([(0, 320), (64, 600), (64.01, 800), (128, 1000)]) * (1 + 1.2 * wob) * 2 ** (1.5 * tr.env("reese", 0.08))
    reese = spectral(drive(svf(tr.buses["reese"], fc, q=1.6) * 4, 1.0), lo=30, hi=4000) * duck
    arp = svf(tr.buses["arp"], tr.curve([(0, 400), (32, 400), (64, 2200), (128, 3000)]) * 2 ** (2 * tr.env("arp", 0.05)), q=2.5)
    vib = 0.3 * np.clip((d / SR - 0.3) / 0.4, 0, 1) * lfo(tr.n, 5.5)
    f = hz(pitch + vib)
    lead = drive(supersaw(f, tr.n, 0.35, seed=5) + 0.4 * st(osc(f / 2, tr.n, "pulse", pw=0.35)), 2.5)
    lead = reverb(echo(spectral(lead, lo=180, hi=8000) * gate, tr.at(0.75), 0.35, 0.3, hi=4000), 2.8, 0.35)
    mix = mixdown({
        "kick": tr.buses["kick"] * 0.35, "reese": reese * 0.35, "sub": tr.buses["sub"] * duck * 0.19,
        "snare": reverb(tr.buses["snare"], 1.0, 0.15) * 0.73, "hat": tr.buses["hat"] * 1.95,
        "ride": reverb(tr.buses["ride"], 1.5, 0.3) * 0.43,
        "choir": reverb(chorus(tr.buses["choir"]), 3.5, 0.5) * 0.47 * tr.duck("kick", 0.35, 0.2),
        "arp": reverb(echo(arp, tr.at(0.75), 0.4, 0.35, hi=3000), 2.0, 0.3) * 0.2 * duck, "lead": lead * 0.25,
        "fx": reverb(tr.buses["fx"], 2.5, 0.4) * 0.45})
    tr.edit(mix, [(15 * 4 + 2.5, 0.5, 0.0625), (23 * 4 + 3.5, 0.5, 0.125)] + rolls((31,)))
    write("jadro", master(mix))


def final():
    """The countdown after the bomb is armed: fastest and harshest. E minor, 155 bpm, 32 bars.
    A (0-15): Em C D B, galloping bass, two-tone siren lead from bar 8. B (16-31): Em F D B, trance-gated
    supersaw chords, wailing siren then the two-tone again with countdown beeps; risers, impacts and snare rolls."""
    tr = Track(155, 32)
    q = tr.beat / 4
    A = [(40, (64, 67, 71)), (36, (64, 67, 72)), (38, (62, 66, 69)), (35, (63, 66, 71))]
    B = [(40, (64, 67, 71)), (41, (65, 69, 72)), (38, (62, 66, 69)), (35, (63, 66, 71))]
    gate16 = (1, 0, 1, 1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 1, 0, 1)
    siren, wail = [], []
    for bar in range(32):
        b0, late = bar * 4, bar >= 16
        root, chord = (B if late else A)[bar // 2 % 4]
        for k in range(4):
            tr.add("kick", b0 + k, kick(52.0, 220.0, 0.17, 4.0))
        for k in (1, 3):
            tr.add("snare", b0 + k, clap(), pan=0.0, gain=0.8)
            tr.add("snare", b0 + k, snare(230.0), pan=0.0, gain=0.7)
        if bar in (15, 31):
            for i, b in enumerate((2, 2.25, 2.5, 2.75) + tuple(3 + j / 8 for j in range(8))):  # 16ths, then 32nds
                tr.add("snare", b0 + b, snare(240.0 + 10 * i), pan=0.0, gain=0.3 + 0.06 * i)
        if bar in (7, 23):
            for i in range(4):
                tr.add("snare", b0 + 3 + i * 0.25, snare(240.0), pan=0.0, gain=0.35 + 0.1 * i)
        for s in range(16):
            tr.add("hat", b0 + s / 4, hat(seed=15 + s % 3), pan=0.3, gain=(0.6, 0.3, 0.9, 0.35)[s % 4])
            if s % 4:
                tr.add("bass", b0 + s / 4, bass_note(root + 12 * (late and s % 4 == 3), 0.85 * q, 0.5), gain=1.0 if s % 4 == 1 else 0.75)
            if late and gate16[s]:
                tr.add("gate", b0 + s / 4, stab(tuple(m + 12 for m in chord) + (chord[0] + 24,), 0.95 * q, 0.22, 1, 0.5))
        tr.add("sub", b0, sub_note(root - 12, 4 * tr.beat))
        if bar >= 8:
            for k in range(4):
                tr.add("hat", b0 + k + 0.5, hat(0.09, 16), pan=-0.3, gain=0.6)
        if late:
            for k in range(4):
                tr.add("ride", b0 + k, metal(1100.0, 0.15, 7), pan=-0.4)
        if bar >= 24:
            for b in (np.arange(0, 4, 0.5) if bar == 31 else range(4)):
                tr.add("beep", b0 + b, blip(95, 0.08, "sine", 8), pan=0.0)
        if 8 <= bar < 16 or bar >= 24 or bar in (2, 3, 6, 7):
            siren += [(b0 + i * 0.5, 0.5, 76 if i % 2 == 0 else 82) for i in range(8)]
        elif late:
            wail += [(b0 + i, 1, 64 if i % 2 == 0 else 70) for i in range(4)]
    for b, sec in ((28, 4), (48, 16), (92, 4), (112, 16)):
        tr.add("fx", b, riser(sec * tr.beat))
        if sec == 16:
            tr.add("fx", b, sweep_up(sec * tr.beat), gain=0.5)
    for b in (0, 64):
        tr.add("fx", b, impact(70.0), pan=0.0)
    for b in (32, 96):
        tr.add("ride", b, hat(0.6, 17), pan=0.3, gain=2.0)

    duck = tr.duck("kick", 0.8, 0.07)
    bass = drive(svf(tr.buses["bass"], tr.curve([(0, 160), (64, 220), (128, 300)]) * 2 ** (4 * tr.env("bass", 0.05)), q=2.0) * 4, 1.0)
    bass = spectral(bass, hi=5000) * duck
    sirens = 0
    for notes, glide, gain in ((siren, 0.02, 1.0), (wail, 0.12, 0.8)):
        pitch, g, _ = tr.line(notes, glide)
        f = hz(pitch + 0.15 * lfo(tr.n, 6.0))
        s = drive(osc(f, tr.n, "pulse") * 0.6 + osc(f * 1.006, tr.n) * 0.5, 2.5)
        sirens = sirens + spectral(s, lo=300, hi=6000) * g * gain
    sirens = reverb(echo(chorus(sirens, mix=0.4), tr.at(0.5), 0.35, 0.3, hi=4000), 1.8, 0.3)
    mix = mixdown({
        "kick": tr.buses["kick"] * 0.32, "bass": bass * 0.29, "sub": tr.buses["sub"] * duck * 0.18,
        "snare": reverb(tr.buses["snare"], 1.0, 0.25) * 1.07, "hat": tr.buses["hat"] * 1.3,
        "ride": reverb(tr.buses["ride"], 1.5, 0.3) * 0.34,
        "gate": reverb(spectral(tr.buses["gate"], lo=150, hi=6000), 1.8, 0.3) * 1.35 * duck, "sirens": sirens * 0.13,
        "beep": reverb(tr.buses["beep"], 1.0, 0.2) * 1.2, "fx": reverb(tr.buses["fx"], 2.5, 0.4) * 0.4})
    tr.edit(mix, [(7 * 4 + 3.5, 0.5, 0.0625), (23 * 4 + 3.5, 0.5, 0.0625), (15 * 4 + 3.5, 0.5, 0.03125), (31 * 4 + 3.5, 0.5, 0.03125)])
    write("final", master(mix))


def effects():
    # solve: a crushed zap and rising blips into a bright supersaw stab with a thump (1.8 s)
    tr = Track(120, 2)  # 4 s of room so the reverb tail doesn't wrap; cut to 1.8 s below
    s = 1 / tr.beat  # beats per second
    n = int(0.09 * SR)
    zap = osc(300 * (4000 / 300) ** (np.arange(n) / n), n, "pulse", pw=0.3) * np.linspace(1, 0.4, n)
    tr.add("x", 0, edges(crush(zap * 0.5, 5, 2), 0.0005, 0.005), pan=0.0, gain=0.6)
    for i, m in enumerate((81, 88, 93, 100)):
        tr.add("x", i * 0.045 * s, blip(m, 0.07, "pulse", 6), pan=-0.6 + 0.4 * i, gain=0.9)
    tr.add("x", 0.18 * s, drive(stab((57, 64, 69, 73, 76, 81), 0.7, 0.22, seed=3), 2.0), gain=0.8)
    tr.add("x", 0.18 * s, kick(60.0, 300.0, 0.15, 2.0), pan=0.0, gain=0.7)
    for i, m in enumerate((93, 97, 100, 105, 100, 105)):
        tr.add("x", (0.26 + i * 0.05) * s, blip(m, 0.05, "pulse", 5), pan=0.6 if i % 2 else -0.6, gain=0.5 * 0.85 ** i)
    solve = reverb(tr.buses["x"], 1.0, 0.25)[:, : int(1.8 * SR)]
    solve[:, -int(0.3 * SR):] *= np.linspace(1, 0, int(0.3 * SR))
    write("solve", solve * (0.7 / np.abs(solve).max()))

    # alarm: two whooping sweeps plus a pulsing tritone horn, 4.8 s; every rate is a whole number of cycles per loop
    n = int(4.8 * SR)
    t = tv(n)
    u = (t % 2.4) / 2.4
    shape = np.where(u < 0.85, u / 0.85, 0.5 + 0.5 * np.cos(np.pi * (u - 0.85) / 0.15))
    whole = lambda f: f * round(f.sum() / SR) / (f.sum() / SR)  # whole cycles: the waveform meets itself at the loop point
    f = 480 * (1450 / 480) ** shape
    whoop = spectral(drive(osc(whole(f), n, "pulse", pw=0.45) + 0.6 * osc(whole(f * 1.007), n), 2.5), lo=250, hi=6500)
    whoop *= 1 - 0.3 * (0.5 + 0.5 * lfo(n, 25.0))
    horn = drive(osc(415.0, n, "pulse", pw=0.3) + osc(587.5, n, "pulse", pw=0.3), 2.0)
    horn *= lag(((t * 2.5) % 1 < 0.5).astype(float), 0.004)
    alarm = reverb(whoop + 0.45 * horn, 0.6, 0.15)
    write("alarm", alarm * (0.7 / np.abs(alarm).max()))


if __name__ == "__main__":
    for part in (hol, pietro, jadro, final, effects):
        t0 = time.time()
        part()
        print(f"       ({time.time() - t0:.1f} s to render)")
