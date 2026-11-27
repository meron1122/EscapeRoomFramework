#!/usr/bin/env python3
"""Composes the simulator's soundtrack from code: a calm seamless bed per room, accents for what the players and the
game master do, a loud finale and an alarm.

Run: python3 simulator/music/compose.py   (needs numpy and ffmpeg with libmp3lame)
Everything is synthesised here (ambient beds; a darksynth finale), so the music is original and free of licences.
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


@cache
def swell(chord, dur, fade, hi, seed=0):
    """A chord that breathes in and out: a low-passed supersaw with raised-cosine fades `fade` seconds long."""
    n, k = int(dur * SR), int(fade * SR)
    x = sum(supersaw(hz(m), n, 0.14, seed=seed + i) for i, m in enumerate(chord)) / len(chord)
    env = np.ones(n)
    env[:k] = 0.5 - 0.5 * np.cos(np.pi * np.arange(k) / k)
    env[-k:] = env[:k][::-1]
    return spectral(x, hi=hi, slope=6) * env


# The three rooms share the notes of F minor, so the accents (F minor pentatonic) fit every one of them:
# the lobby rests on F (cold), the habit floor on A flat (sweet, a little too sweet), the core on C (Phrygian, dark).
CALM = {  # drone root, four chords of 16 s each, how bright the pad is
    "lobby": (41, ((53, 56, 60, 67), (49, 56, 60, 65), (46, 53, 56, 61), (48, 55, 58, 63)), 1100),
    "habit": (44, ((56, 60, 63, 67), (53, 56, 60, 63), (49, 56, 60, 65), (51, 58, 60, 65)), 1500),
    "core": (36, ((48, 55, 60, 63), (49, 56, 60, 67), (46, 53, 58, 61), (48, 55, 61, 63)), 800),
}


def calm(name):
    """A room's bed, 64 s: a drone, four chords that breathe into each other and a few far bells. No drums and no
    melody: the rhythm of a game is what the players do, and the game plays that as accents (see accents())."""
    root, chords, hi = CALM[name]
    tr, r = Track(60, 16), rng(sum(name.encode()))  # a beat is a second
    for i, chord in enumerate(chords):
        tr.add("pad", i * 16 - 3, swell(chord, 22, 6, hi, seed=10 * i))  # 6 s of each chord lie over the next
        for k in range(3):
            m = chord[r.integers(1, 4)] + 12 * (1 + (r.random() < 0.3))
            tr.add("bell", i * 16 + 2 + k * 5 + r.uniform(-1.5, 1.5), fm_bell(m, 3.0, 1.2, 2.0),
                   pan=r.uniform(-0.6, 0.6), gain=r.uniform(0.5, 1.0))
    n = tr.n
    whole = lambda m: round(float(hz(m)) * n / SR) * SR / n  # whole cycles per loop: the drone meets itself at the seam
    t = tv(n)
    drone = (np.sin(2 * np.pi * whole(root) * t) + 0.5 * np.sin(2 * np.pi * whole(root + 7) * t) * (0.5 + 0.5 * lfo(n, 1 / 32))
             + 0.35 * np.sin(2 * np.pi * whole(root + 12) * t) * (0.5 + 0.5 * lfo(n, 1 / 21, 1.0))) * (0.75 + 0.25 * lfo(n, 1 / 16))
    mix = mixdown({"pad": reverb(tr.buses["pad"], 5.0, 0.5, hi=3000),
                   "bell": reverb(echo(tr.buses["bell"], tr.at(1.5), 0.5, 0.4, hi=2500), 6.0, 0.7, hi=3500) * 0.4,
                   "drone": drone * 0.12})
    mix = mix - mix.mean(axis=1, keepdims=True)
    # A bed, not a track: no limiter, well under the finale; the bells' peaks stay 3 dB under full scale for the encoder.
    write(name, mix * min(10 ** (-17 / 20) / rms(mix), 0.7 / np.abs(mix).max()))


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
    write("final", master(mix, rms_db=-13.0))  # the game plays music 3 dB hotter since the beds went calm


def accents():
    """What happens in the room, as notes over the calm beds (all on F minor pentatonic, so they fit any room):
    solve, work (a player starts on a puzzle; the game varies its pitch), wrong, door, help; and `his`, lower and
    darker than anything the players make, for a move of the game master."""
    def out(name, sec, notes, wet=0.35):
        tr = Track(60, 2)  # 8 s of room, so the reverb tail never wraps; cut to `sec` below
        for at, sig, pan, gain in notes:
            tr.add("x", at, sig, pan=pan, gain=gain)
        x = reverb(tr.buses["x"], 2.2, wet, hi=4000)[:, : int(sec * SR)]
        x[:, -int(0.3 * SR):] *= np.linspace(1, 0, int(0.3 * SR))
        write(name, x * (0.7 / np.abs(x).max()))

    bell = lambda m, dur=1.6: fm_bell(m, dur, 1.5, 2.0)
    dull = lambda m, dur: spectral(fm_bell(m, dur, 2.5, 1.41), hi=1800)  # an inharmonic clang
    out("solve", 2.6, [(0.11 * i, bell(m), -0.5 + 0.25 * i, 0.7) for i, m in enumerate((72, 75, 77, 80))]
        + [(0.46, bell(84, 2.0), 0.0, 1.0), (0.1, swell((53, 60, 68), 2.2, 0.7, 1400), None, 0.5)])
    out("work", 0.9, [(0.0, fm_bell(65, 0.7, 0.8, 1.0), 0.0, 1.0)], wet=0.25)
    out("wrong", 1.0, [(0.0, dull(61, 0.35), -0.2, 1.0), (0.16, dull(60, 0.6), 0.2, 0.9)], wet=0.2)
    out("door", 2.4, [(0.0, riser(1.2, 300.0, 3000.0), None, 0.12), (0.0, swell((53, 60), 2.2, 0.8, 1200), None, 1.0),
                      (0.9, bell(65), -0.3, 0.5), (1.1, bell(72), 0.3, 0.6)])
    out("help", 1.6, [(0.0, bell(72, 1.0), -0.3, 0.9), (0.22, bell(77, 1.2), 0.3, 1.0), (0.66, bell(77, 0.8), 0.0, 0.3)])
    n = int(1.6 * SR)
    boom = edges(np.sin(2 * np.pi * np.cumsum(38 + 60 * np.exp(-tv(n) / 0.05)) / SR) * np.exp(-tv(n) / 0.5), r=0.05)
    out("his", 3.2, [(0.0, dull(53, 0.8)[::-1].copy(), 0.0, 0.5), (0.8, boom, 0.0, 0.5),
                     (0.8, fm_bell(53, 2.4, 3.0, 1.41), -0.2, 0.9), (0.86, fm_bell(54, 2.0, 2.0, 1.41), 0.3, 0.3)], wet=0.5)

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
    for part in (*(functools.partial(calm, room) for room in CALM), final, accents):
        t0 = time.time()
        part()
        print(f"       ({time.time() - t0:.1f} s to render)")
