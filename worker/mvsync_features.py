"""MVSync feature extraction (spec §5). Shared by ingest (ref.npz) and live matching.

Input: mono float32 at 22050 Hz.
Output: onset envelope on a 50 fps grid, locally normalised.
"""
import os
import subprocess

import numpy as np

FEAT_VERSION = 2
NGROUPS = 8          # band groups for the verification feature (40 bands / 8 = 5 each)
SR = 22050
NFFT = 2048
HOP = 512
FPS = 50
NBANDS = 40
FMIN, FMAX = 30.0, 11000.0

_BAND_MATRIX = None


def _band_matrix():
    global _BAND_MATRIX
    if _BAND_MATRIX is None:
        freqs = np.fft.rfftfreq(NFFT, 1.0 / SR)
        edges = np.geomspace(FMIN, FMAX, NBANDS + 1)
        m = np.zeros((len(freqs), NBANDS), np.float32)
        for b in range(NBANDS):
            sel = (freqs >= edges[b]) & (freqs < edges[b + 1])
            if not sel.any():  # very narrow low bands: take the nearest bin
                sel = np.zeros(len(freqs), bool)
                sel[np.argmin(np.abs(freqs - np.sqrt(edges[b] * edges[b + 1])))] = True
            m[sel, b] = 1.0 / sel.sum()
        _BAND_MATRIX = m
    return _BAND_MATRIX


def _moving_mean(x, n):
    if n <= 1:
        return x.copy()
    c = np.concatenate(([0.0], np.cumsum(x, dtype=np.float64)))
    half = n // 2
    idx = np.arange(len(x))
    lo = np.clip(idx - half, 0, len(x))
    hi = np.clip(idx - half + n, 0, len(x))
    return ((c[hi] - c[lo]) / np.maximum(hi - lo, 1)).astype(np.float32)


def onset_envelope(y, groups=False):
    """Raw spectral-flux onset envelope at SR/HOP frames per second.
    groups=True also returns the per-band-group flux (frames x NGROUPS)."""
    y = np.asarray(y, np.float32)
    if len(y) < NFFT:
        y = np.pad(y, (0, NFFT - len(y)))
    n_frames = 1 + (len(y) - NFFT) // HOP
    frames = np.lib.stride_tricks.as_strided(
        y, shape=(n_frames, NFFT), strides=(y.strides[0] * HOP, y.strides[0]))
    win = np.hanning(NFFT).astype(np.float32)
    mag = np.abs(np.fft.rfft(frames * win, axis=1)).astype(np.float32) * (2.0 / win.sum())
    bands = mag @ _band_matrix()
    lb = np.log1p(100.0 * bands)
    d = np.diff(lb, axis=0, prepend=lb[:1])
    np.maximum(d, 0, out=d)
    if groups:
        return d.sum(axis=1), d.reshape(len(d), NGROUPS, NBANDS // NGROUPS).sum(axis=2)
    return d.sum(axis=1)


def postprocess(env, fps=FPS):
    x = env - _moving_mean(env, int(round(1.0 * fps)))
    np.maximum(x, 0, out=x)
    rms = np.sqrt(_moving_mean(x * x, int(round(3.0 * fps))))
    return (x / (rms + 1e-3)).astype(np.float32)


def _grid(y, env):
    t_src = (np.arange(len(env)) * HOP + NFFT / 2) / SR
    t_out = np.arange(int(len(y) / SR * FPS)) / FPS
    return np.interp(t_out, t_src, env).astype(np.float32)


def features(y, sr=SR):
    """Mono audio (at SR) -> normalised onset envelope at FPS (spec §5; coarse search)."""
    if sr != SR:
        y = resample(y, sr, SR)
    return postprocess(_grid(y, onset_envelope(y)))


def features_full(y, sr=SR):
    """-> (onset[T], bands[T, NGROUPS]). bands = the same onset processing per band group,
    used to verify/score candidates (the summed envelope alone is not discriminative enough
    between songs with similar 4/4 drum patterns)."""
    if sr != SR:
        y = resample(y, sr, SR)
    env, g = onset_envelope(y, groups=True)
    onset = postprocess(_grid(y, env))
    bands = np.stack([postprocess(_grid(y, g[:, b])) for b in range(NGROUPS)], axis=1)
    return onset, bands.astype(np.float32)


def estimate_bpm(onset, fps=FPS, lo=70.0, hi=180.0):
    """Tempo of a reference onset envelope via autocorrelation (for out_ctrl bpm_est)."""
    x = np.asarray(onset, np.float64)
    if len(x) < fps * 10:
        return 0.0
    x = x - x.mean()
    n = 1 << int(np.ceil(np.log2(2 * len(x))))
    f = np.fft.rfft(x, n)
    ac = np.fft.irfft(f * np.conj(f), n)[:len(x)]
    lags = np.arange(int(fps * 60 / hi), int(fps * 60 / lo) + 1)
    bpms = 60.0 * fps / lags
    w = np.exp(-0.5 * (np.log2(bpms / 120.0) / 0.9) ** 2)   # mild prior around 120
    k = int(np.argmax(ac[lags] * w))
    j = lags[k]
    if 0 < k < len(lags) - 1:                                  # parabolic refine
        a, b, c = ac[j - 1], ac[j], ac[j + 1]
        den = a - 2 * b + c
        if abs(den) > 1e-12:
            j = j + 0.5 * (a - c) / den
    return float(60.0 * fps / j)


def resample(y, sr_in, sr_out):
    if sr_in == sr_out:
        return np.asarray(y, np.float32)
    from math import gcd
    from scipy.signal import resample_poly
    g = gcd(int(sr_in), int(sr_out))
    return resample_poly(y, sr_out // g, sr_in // g).astype(np.float32)


def decode_audio(path, ffmpeg="ffmpeg", sr=SR):
    """Decode any media file to mono float32 at sr using ffmpeg."""
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1",
           "-ar", str(sr), "-f", "f32le", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(out, np.float32).copy()


def save_ref(path, feat):
    onset, bands = feat
    tmp = str(path) + ".tmp.npz"
    np.savez_compressed(tmp, onset=onset.astype(np.float32), bands=bands.astype(np.float16),
                        fps=FPS, version=FEAT_VERSION)
    os.replace(tmp, path)


def load_ref(path):
    """-> (onset, bands) or None if missing/outdated."""
    with np.load(path) as z:
        if int(z["version"]) != FEAT_VERSION or int(z["fps"]) != FPS or "bands" not in z:
            return None
        return z["onset"].astype(np.float32), z["bands"].astype(np.float32)
