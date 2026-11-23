"""MVSync matching engine (spec §4.3–4.6).

Two-stage search:
  1. coarse: FFT normalised cross-correlation of the summed onset envelope (spec §5) against
     every ready MV, rate r in rate_range (0.01 steps) -> top peaks.
  2. verify: candidates re-scored with the per-band-group onset matrix (multi-channel NCC),
     which separates songs far better than the summed envelope.

Locking (spec §4.4) uses a multi-hypothesis tracker: every hypothesis (MV, offset, r) is
re-scored in every window near its predicted position, so a hypothesis survives even when
it is not in a window's top 5. LOCK = seen in >= 2 consecutive windows, mean score >=
theta_lock, and better than the best *other* hypothesis by >= theta_margin (identical
repeats -> no lock until the music disambiguates).

Clock-agnostic: t0 = window start on the driver clock (live worker: epoch seconds).
"""
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import scipy.fft as sfft

from mvsync_features import FPS

IDLE, SEARCHING, LOCKED, HOLD, LOST = "IDLE", "SEARCHING", "LOCKED", "HOLD", "LOST"
IDENTIFYING_ONLINE, FETCHING = "IDENTIFYING_ONLINE", "FETCHING"


@dataclass
class Cand:
    id: str
    pos: float      # MV position (s) at window start t0
    r: float        # MV seconds per DJ second
    score: float
    t0: float

    def pos_at(self, t):
        return self.pos + self.r * (t - self.t0)


def _stretch(q, r):
    """Resample a DJ-time query (T or T x B) onto the MV time grid for speed ratio r."""
    n = len(q)
    L = int(round(n * r))
    src = np.arange(L) / r
    x = np.arange(n)
    if q.ndim == 1:
        return np.interp(src, x, q).astype(np.float32)
    return np.stack([np.interp(src, x, q[:, b]) for b in range(q.shape[1])], 1).astype(np.float32)


def _znorm(v):
    v = v - v.mean(axis=0)
    s = np.sqrt(float((v * v).sum()))
    return v / s if s > 1e-9 else None


class RefBank:
    """Reference features: id -> (onset[T], bands[T, B]). Caches the coarse FFT matrix."""

    def __init__(self):
        self.refs = {}
        self.bands = {}
        self._cache_key = None
        self._cache = None
        self._ver = 0

    def add(self, vid, feat):
        onset, bands = feat
        self.refs[vid] = np.asarray(onset, np.float32)
        self.bands[vid] = np.ascontiguousarray(bands, np.float32)
        self._ver += 1

    def remove(self, vid):
        if self.refs.pop(vid, None) is not None:
            self.bands.pop(vid, None)
            self._ver += 1

    def ids(self):
        return list(self.refs)

    def _matrix(self, ids, max_l):
        key = (tuple(ids), self._ver, max_l)
        if key == self._cache_key:
            return self._cache
        lens = np.array([len(self.refs[i]) for i in ids])
        nfft = sfft.next_fast_len(int(lens.max()) + max_l + 1, real=True)
        R = np.zeros((len(ids), nfft), np.float32)
        for k, i in enumerate(ids):
            R[k, :lens[k]] = self.refs[i]
        FR = sfft.rfft(R, axis=1, workers=-1).astype(np.complex64)
        C1 = np.zeros((len(ids), nfft + 1), np.float64)
        C2 = np.zeros((len(ids), nfft + 1), np.float64)
        np.cumsum(R, axis=1, out=C1[:, 1:])
        np.cumsum(R.astype(np.float64) ** 2, axis=1, out=C2[:, 1:])
        self._cache_key = key
        self._cache = (nfft, lens, FR, C1, C2)
        return self._cache

    # ---------------------------------------------------------------- stage 1
    def coarse(self, q, t0, rates, ids, n_peaks=40, per_mv=4, nms_s=0.5):
        ids = [i for i in ids if i in self.refs]
        if not ids:
            return []
        max_l = int(round(len(q) * max(rates))) + 1
        nfft, lens, FR, C1, C2 = self._matrix(ids, max_l)
        n_ids = len(ids)
        best = np.full((n_ids, nfft), -1.0, np.float32)
        best_r = np.zeros((n_ids, nfft), np.float32)
        for r in rates:
            qr = _znorm(_stretch(q, r))
            if qr is None:
                return []
            L = len(qr)
            FQ = np.conj(sfft.rfft(qr, nfft)).astype(np.complex64)
            corr = sfft.irfft(FR * FQ[None, :], nfft, axis=1, workers=-1)
            K = nfft + 1 - L
            s1 = C1[:, L:L + K] - C1[:, :K]
            var = (C2[:, L:L + K] - C2[:, :K]) - s1 * s1 / L
            with np.errstate(invalid="ignore", divide="ignore"):
                v = corr[:, :K] / np.sqrt(np.maximum(var, 1e-6))
            valid = (np.arange(K)[None, :] <= (lens[:, None] - L)) & (var > 1e-3)
            v = np.where(valid, v, -1.0).astype(np.float32)
            upd = v > best[:, :K]
            best[:, :K][upd] = v[upd]
            best_r[:, :K][upd] = r
        rad = int(nms_s * FPS)
        cands = []
        for k in range(n_ids):
            row = best[k, :lens[k]]
            if len(row) == 0:
                continue
            m = min(len(row), 256)
            top = np.argpartition(row, -m)[-m:]
            top = top[np.argsort(row[top])[::-1]]
            taken = []
            for j in top:
                if row[j] <= 0:
                    break
                if all(abs(j - t) > rad for t in taken):
                    taken.append(j)
                    cands.append(Cand(ids[k], j / FPS, float(best_r[k, j]), float(row[j]), t0))
                    if len(taken) >= per_mv:
                        break
        cands.sort(key=lambda c: -c.score)
        return cands[:n_peaks]

    # ---------------------------------------------------------------- stage 2
    def score_mb(self, vid, qb, t0, pos, rates, win):
        """Multi-band NCC around pos (MV s at t0), |dpos| <= win, over rates.
        Returns best Cand (sub-frame interpolated) or None."""
        R = self.bands.get(vid)
        if R is None:
            return None
        n_ref = len(R)
        c0 = int(round(pos * FPS))
        w = max(1, int(round(win * FPS)))
        best = None
        for r in rates:
            qs = _znorm(_stretch(qb, r))
            if qs is None:
                return None
            L = len(qs)
            lo, hi = max(0, c0 - w), min(n_ref - L, c0 + w)
            if hi < lo:
                continue
            seg = R[lo:hi + L]                                   # (K+L-1, B)
            win_v = np.lib.stride_tricks.sliding_window_view(seg, L, axis=0)  # (K, B, L)
            num = np.einsum("kbl,lb->k", win_v, qs, optimize=True)
            c1 = np.concatenate([np.zeros((1, seg.shape[1])), np.cumsum(seg, 0, dtype=np.float64)])
            c2 = np.concatenate([np.zeros((1, seg.shape[1])),
                                 np.cumsum(seg.astype(np.float64) ** 2, 0)])
            K = hi - lo + 1
            s1 = c1[L:L + K] - c1[:K]
            var = ((c2[L:L + K] - c2[:K]) - s1 * s1 / L).sum(1)
            ncc = np.where(var > 1e-3, num / np.sqrt(np.maximum(var, 1e-6)), -1.0)
            j = int(np.argmax(ncc))
            s = float(ncc[j])
            if best is None or s > best[0]:
                dj = 0.0
                if 0 < j < K - 1:
                    a, b, cc = ncc[j - 1], ncc[j], ncc[j + 1]
                    den = a - 2 * b + cc
                    if abs(den) > 1e-9:
                        dj = float(np.clip(0.5 * (a - cc) / den, -0.5, 0.5))
                best = (s, (lo + j + dj) / FPS, float(r))
        if best is None:
            return None
        return Cand(vid, best[1], best[2], best[0], t0)

    def verify(self, c, qb, dpos=0.06, dr=0.004, rstep=0.002):
        rates = np.arange(c.r - dr, c.r + dr + 1e-9, rstep)
        return self.score_mb(c.id, qb, c.t0, c.pos, rates, dpos)

    def global_search(self, q, qb, t0, rates, ids=None, top_k=5, n_verify=48, per_mv=16,
                      region_s=0.75):
        """Coarse + verify. Returns top_k Cands by multi-band score.
        The summed envelope is nearly a pulse train, so every beat-aligned offset scores
        about the same: coarse only picks *regions* (NMS 2*region_s apart) and the verify
        stage searches +-region_s (more than one beat) around each with the band features."""
        ids = self.ids() if ids is None else ids
        coarse = self.coarse(q, t0, rates, ids, n_peaks=n_verify, per_mv=per_mv,
                             nms_s=2 * region_s)
        out = []
        for c in coarse:
            v = self.verify(c, qb, dpos=region_s)
            if v is not None:
                out.append(v)
        out.sort(key=lambda c: -c.score)
        return out[:top_k]


# ===================================================================== hypotheses
@dataclass
class Hyp:
    id: str
    pos: float
    r: float
    t0: float
    scores: deque = field(default_factory=lambda: deque(maxlen=6))
    hid: int = 0
    seen: int = 0          # consecutive windows with a valid re-score

    def pos_at(self, t):
        return self.pos + self.r * (t - self.t0)

    def avg(self, n=None):
        s = list(self.scores)[-n:] if n else list(self.scores)
        return float(np.mean(s)) if s else 0.0


class HypTracker:
    """Pure multi-hypothesis bookkeeping (spec §4.4). Re-used by sim_eval replay."""

    def __init__(self, theta_lock=0.6, theta_margin=0.03, pos_tol=0.08, r_tol=0.01, max_hyps=16,
                 early_factor=2.0):
        self.theta_lock = theta_lock
        self.theta_margin = theta_margin
        self.early_factor = early_factor  # stricter margin when only 2 windows seen
        self.pos_tol = pos_tol
        self.r_tol = r_tol
        self.max_hyps = max_hyps
        self.hyps = []
        self._next = 1

    def reset(self):
        self.hyps = []

    def update(self, bank, qb, t0, new_cands, ids_ok=None):
        """Re-score existing hyps at t0, merge new candidates, prune."""
        kept = []
        for h in self.hyps:
            if ids_ok is not None and h.id not in ids_ok:
                continue
            pred = h.pos_at(t0)
            c = bank.score_mb(h.id, qb, t0, pred,
                              np.arange(h.r - 0.004, h.r + 0.004 + 1e-9, 0.002), self.pos_tol)
            if c is None:
                continue
            h.pos, h.r, h.t0 = c.pos, c.r, t0
            h.scores.append(c.score)
            h.seen += 1
            kept.append(h)
        for c in new_cands:
            dup = next((h for h in kept if h.id == c.id and abs(h.pos - c.pos) <= self.pos_tol * 1.5
                        and abs(h.r - c.r) <= self.r_tol), None)
            if dup is not None:
                continue
            h = Hyp(c.id, c.pos, c.r, t0, hid=self._next, seen=1)
            h.scores.append(c.score)
            self._next += 1
            kept.append(h)
        # merge near-duplicates (keep the older/stronger)
        kept.sort(key=lambda h: (-len(h.scores), -h.avg()))
        merged = []
        for h in kept:
            if any(m.id == h.id and abs(m.pos - h.pos) <= self.pos_tol for m in merged):
                continue
            merged.append(h)
        merged = [h for h in merged if not (len(h.scores) >= 3 and h.avg(3) < 0.25)]
        merged.sort(key=lambda h: -h.avg(3))
        self.hyps = merged[:self.max_hyps]
        return self.hyps

    def margin_vs(self, h, others):
        """h's lead over the best other hypothesis, compared on the same recent windows."""
        lead = None
        for o in others:
            if o is h:
                continue
            n = max(1, min(len(h.scores), len(o.scores)))
            d = h.avg(n) - o.avg(n)
            lead = d if lead is None else min(lead, d)
        return 1.0 if lead is None else lead

    def best_lockable(self):
        cands = [h for h in self.hyps if len(h.scores) >= 2 and h.seen >= 2
                 and h.avg(4) >= self.theta_lock]
        if not cands:
            return None, 0.0
        h = max(cands, key=lambda x: x.avg(4))
        m = self.margin_vs(h, self.hyps)
        need = self.theta_margin * (self.early_factor if h.seen < 3 else 1.0)
        return (h if m >= need else None), m

    def best_identity(self):
        """Best hypothesis whose *MV* is clearly identified (leads every other MV's hypotheses
        by 2*theta_margin) even if its position is ambiguous within that MV."""
        cands = [h for h in self.hyps if len(h.scores) >= 2 and h.avg(4) >= self.theta_lock]
        if not cands:
            return None
        h = max(cands, key=lambda x: x.avg(4))
        m = self.margin_vs(h, [o for o in self.hyps if o.id != h.id])
        return h if m >= 2 * self.theta_margin else None


# ===================================================================== engine
@dataclass
class Estimate:
    id: str = ""
    pos: float = 0.0     # MV position at t_ref
    rate: float = 1.0
    conf: float = 0.0
    t_ref: float = 0.0

    def pos_at(self, t):
        return self.pos + self.rate * (t - self.t_ref)


@dataclass
class EngineCfg:
    theta_lock: float = 0.6
    theta_hold: float = 0.45
    theta_margin: float = 0.03
    rate_range: tuple = (0.84, 1.16)   # ±16 % (DJ pitch fader / key lock)
    rate_step: float = 0.01
    window_s: float = 8.0
    track_win: float = 1.5
    track_rate: float = 0.02
    jump_thresh: float = 0.5
    jump_count: int = 3
    hold_count: int = 3
    lost_after: float = 20.0
    switch_count: int = 3
    tentative: bool = True   # play the identified MV (beat-synced) while position is ambiguous
    stall_reset_s: float = 5.0   # conf frozen this long (with sound) -> reset and search again; 0 = off


class Engine:
    def __init__(self, bank, cfg=None, logger=None):
        self.bank = bank
        self.cfg = cfg or EngineCfg()
        self.ht = HypTracker(self.cfg.theta_lock, self.cfg.theta_margin)
        self.state = IDLE
        self.est = Estimate()
        self.cur_hid = None
        self.track_changed = False
        self.allowed_ids = None
        self.manual_id = None
        self.excluded = set()
        self.last_global = []
        self.last_margin = 0.0
        self.pts = deque(maxlen=12)
        self._dev = 0
        self._low = 0
        self._hold_since = None
        self._switch = (None, 0)
        self.last_track_score = 0.0
        self.tent_hid = None
        self._tent_switch = 0
        self.prov_id = None        # MV shown at a provisional position (online verify) until LOCKED
        self._conf_last = None
        self._conf_t = None
        self._stall_armed = True
        self.log = logger or (lambda *a: None)

    # -------------------------------------------------------------- params
    def set_param(self, name, value):
        if name == "theta_lock":
            self.cfg.theta_lock = self.ht.theta_lock = float(value)
        elif name == "theta_margin":
            self.cfg.theta_margin = self.ht.theta_margin = float(value)
        elif hasattr(self.cfg, name):
            setattr(self.cfg, name, type(getattr(self.cfg, name))(value))

    def rates(self):
        lo, hi = self.cfg.rate_range
        return np.round(np.arange(lo, hi + 1e-9, self.cfg.rate_step), 4)

    def search_ids(self):
        if self.manual_id:
            return [self.manual_id] if self.manual_id in self.bank.refs else []
        ids = self.bank.ids() if self.allowed_ids is None else \
            [i for i in self.allowed_ids if i in self.bank.refs]
        return [i for i in ids if i not in self.excluded]

    def relock(self):
        self.prov_id = None
        self.ht.reset()
        self.pts.clear()
        self._switch = (None, 0)
        self.cur_hid = None
        self.tent_hid = None
        self._set_state(SEARCHING)

    def _set_state(self, s):
        if s != self.state:
            self.log(f"state {self.state} -> {s}")
            self.state = s

    def _adopt(self, h):
        changed = h.id != self.est.id
        if self.prov_id and self.prov_id == h.id and self.est.id == h.id:
            self.log(f"provisional -> lock {h.id}: position error {self.est.pos_at(h.t0) - h.pos:+.3f} s")
        self.pts.clear()
        self.cur_hid = h.hid
        c = Cand(h.id, h.pos, h.r, h.scores[-1], h.t0)
        self._add_point(c)
        self.est.conf = h.avg(4)
        self._dev = self._low = 0
        self._hold_since = None
        self._switch = (None, 0)
        self.tent_hid = None
        self.prov_id = None
        self.track_changed = self.track_changed or changed
        self._set_state(LOCKED)

    def _add_point(self, c):
        half = self.cfg.window_s / 2
        self.pts.append((c.t0 + half, c.pos + c.r * half, max(c.score, 1e-3)))
        if len(self.pts) >= 3:
            t, p, w = map(np.array, zip(*self.pts))
            tm = np.average(t, weights=w)
            pm = np.average(p, weights=w)
            den = np.sum(w * (t - tm) ** 2)
            r = np.sum(w * (t - tm) * (p - pm)) / den if den > 1e-6 else c.r
            lo, hi = self.cfg.rate_range
            r = float(np.clip(r, lo - 0.02, hi + 0.02))
            t_ref = float(t[-1])
            self.est = Estimate(c.id, float(pm + r * (t_ref - tm)), r, c.score, t_ref)
        else:
            self.est = Estimate(c.id, c.pos + c.r * half, c.r, c.score, c.t0 + half)

    def _current_hyp(self, t0):
        for h in self.ht.hyps:
            if h.hid == self.cur_hid:
                return h
        # re-associate by position (hyp may have been re-created)
        for h in self.ht.hyps:
            if h.id == self.est.id and abs(h.pos - self.est.pos_at(t0)) <= 0.3:
                self.cur_hid = h.hid
                return h
        return None

    # -------------------------------------------------------------- steps
    def stall_check(self, t):
        """Call once per global step while there is sound. est.conf only moves while the current
        hypothesis is alive; when the song changes that hypothesis is dropped and conf freezes
        (HOLD -> LOST could then sit for 20 s+). If it has not moved for stall_reset_s, drop
        everything and search again. Fires once per stall; re-armed when conf moves again."""
        # only HOLD / LOST: there the current hypothesis can be gone for good. In SEARCHING a
        # tentative MV's conf may sit still while the library keeps searching (A3: repeated resets
        # cancelled the online lookup again and again)
        if self.cfg.stall_reset_s <= 0 or self.state not in (HOLD, LOST) or self.manual_id:
            self._conf_t = None
            return False
        c = round(float(self.est.conf), 6)
        if self._conf_t is None or c != self._conf_last:
            if self._conf_last is not None and c != self._conf_last:
                self._stall_armed = True
            self._conf_last, self._conf_t = c, t
            return False
        # conf 0 = nothing identified yet (plain SEARCHING): a reset would only restart the search
        if self._stall_armed and c > 0 and t - self._conf_t >= self.cfg.stall_reset_s:
            self.log(f"conf {c:.3f} unchanged for {t - self._conf_t:.0f} s ({self.state}) -> reset, searching again")
            self.relock()
            self.est.conf = 0.0
            self._conf_last, self._conf_t = 0.0, t
            self._stall_armed = False
            return True
        return False

    def silent(self, t):
        self._conf_t = None        # silence does not count as a stall
        if self.state in (LOCKED, HOLD):
            if self._hold_since is None:
                self._hold_since = t
            self._set_state(HOLD)
        elif self.state != IDLE:
            self._set_state(HOLD if self.est.id else IDLE)
        self.ht.reset()

    def global_step(self, q, qb, t0):
        """Full search + hypothesis update (1 Hz). Returns (hyps, decision)."""
        ids = self.search_ids()
        cands = self.bank.global_search(q, qb, t0, self.rates(), ids, top_k=8)
        hyps = self.ht.update(self.bank, qb, t0, cands, ids_ok=set(ids))
        self.last_global = hyps
        best, margin = self.ht.best_lockable()
        self.last_margin = margin
        decision = ""
        if self.state in (IDLE, SEARCHING, LOST):
            if self.state == IDLE:
                self._set_state(SEARCHING)
            if best is not None and self._prov_far(best, hyps):
                decision = f"provisional-wait {best.id}@{best.pos:.2f}"
            elif best is not None:
                self._adopt(best)
                decision = f"lock {best.id}@{best.pos:.2f}"
            else:
                decision = self._tentative(t0)
            return hyps, decision
        cur = self._current_hyp(t0)
        if cur is not None:
            self.est.conf = cur.avg(4)
        if self.state == HOLD and best is None:
            # stale track in HOLD while another MV is clearly playing (ambiguous position)
            t = self.ht.best_identity() if self.cfg.tentative else None
            if t is not None and t.id != self.est.id:
                self._tent_switch += 1
                if self._tent_switch >= self.cfg.switch_count:
                    self._set_state(SEARCHING)
                    return hyps, self._tentative(t0)
            else:
                self._tent_switch = 0
        if best is None or (cur is not None and best.hid == cur.hid):
            self._switch = (None, 0)
            return hyps, decision
        # a different hypothesis is lockable: does it clearly beat the current one?
        lead = 1.0 if cur is None else best.avg(min(4, len(best.scores))) - cur.avg(
            min(4, len(best.scores)))
        if self.state == HOLD and (cur is None or lead >= self.cfg.theta_margin):
            self._adopt(best)
            return hyps, f"switch(hold) {best.id}@{best.pos:.2f}"
        need, count = self.cfg.theta_margin, self.cfg.switch_count
        if best.id == self.est.id and self.state == LOCKED:
            # same MV elsewhere while tracking is healthy: usually a repeated section, so only
            # move when the other position is much better (real jumps are also caught by
            # track_step deviation -> SEARCHING)
            need, count = self.cfg.theta_margin * 4, 2
        if lead >= need:
            k = self._switch[1] + 1 if self._switch[0] == best.hid else 1
            self._switch = (best.hid, k)
            if k >= count:
                self._adopt(best)
                decision = f"switch {best.id}@{best.pos:.2f}"
        else:
            self._switch = (None, 0)
        return hyps, decision

    def provisional(self, c):
        """An online MV was just verified (Cand c from its audio check): show it at that position
        right away, before the library matching LOCKs (~2.5 s). State stays SEARCHING (TD shows
        PROVISIONAL); the normal tentative / lock steps take over from here."""
        if self.state == LOCKED or self.manual_id:
            return False
        if self.state != SEARCHING:
            self.relock()          # HOLD / LOST on the previous song: drop it
        changed = c.id != self.est.id
        self.est = Estimate(c.id, c.pos, c.r, c.score, c.t0)
        self.prov_id = c.id
        self.tent_hid = None
        self.track_changed = self.track_changed or changed
        self.log(f"provisional {c.id}@{c.pos:.2f} r={c.r:.3f} t0={c.t0:.3f} (online verify {c.score:.3f})")
        return True

    def _prov_far(self, best, hyps):
        """While an online MV plays at its verify position: hold back a lock on the same MV far
        (> 1 s) from that position. A repeated section can reach 2 windows first (11/14:
        海鮮☆ハイウェイ locked at 4.8 s while the verify said 60 s; the 60 s hypothesis scored
        higher one window later, but a LOCKED MV only moves on a large lead -> 44 s off).
        Wait while a hypothesis near the provisional position scores about as well, or until
        the far one has 3 windows (5 at most)."""
        if not self.prov_id or best.id != self.prov_id or self.est.id != self.prov_id:
            return False
        if abs(best.pos - self.est.pos_at(best.t0)) <= 1.0 or best.seen >= 5:
            return False
        near = [h for h in hyps if h.id == best.id and h is not best
                and abs(h.pos - self.est.pos_at(h.t0)) <= 1.0]
        if any(h.scores[-1] >= best.scores[-1] - self.cfg.theta_margin for h in near):
            return True
        return best.seen < 3

    def _tentative(self, t0):
        """SEARCHING with an identified MV but ambiguous position: follow the best hypothesis
        (not LOCKED) so TD shows the right MV in beat sync. Sticky to avoid flapping."""
        if not self.cfg.tentative:
            return ""
        t = self.ht.best_identity()
        if t is None:
            return ""
        cur = next((h for h in self.ht.hyps if h.hid == self.tent_hid), None)
        if cur is not None and cur.id == t.id and cur.hid != t.hid and \
                t.avg(4) - cur.avg(4) < self.cfg.theta_margin:
            t = cur
        changed = t.id != self.est.id
        self.tent_hid = t.hid
        self._tent_switch = 0
        self.est = Estimate(t.id, t.pos, t.r, t.avg(4), t.t0)
        self.track_changed = self.track_changed or changed
        return f"tentative {t.id}@{t.pos:.2f}"

    def track_step(self, qb, t0):
        """Local tracking (2 Hz) while LOCKED/HOLD (spec §4.5)."""
        if self.state not in (LOCKED, HOLD) or not self.est.id:
            return None
        pred = self.est.pos_at(t0)
        r0 = self.est.rate
        rr = np.arange(r0 - self.cfg.track_rate, r0 + self.cfg.track_rate + 1e-9, 0.002)
        c = self.bank.score_mb(self.est.id, qb, t0, pred, rr, self.cfg.track_win)
        if c is None:
            return None
        self.last_track_score = c.score
        if c.score >= self.cfg.theta_hold:
            self._low = 0
            if abs(c.pos - pred) > self.cfg.jump_thresh:
                self._dev += 1
                if self._dev >= self.cfg.jump_count:
                    self.log("jump detected -> SEARCHING")
                    self._dev = 0
                    self.relock()
                return c
            self._dev = 0
            self._add_point(c)
            self._hold_since = None
            self._set_state(LOCKED)
        else:
            self._low += 1
            if self._low >= self.cfg.hold_count and self.state == LOCKED:
                self._hold_since = t0 + self.cfg.window_s
                self._set_state(HOLD)
            if self.state == HOLD and self._hold_since is not None and \
                    t0 + self.cfg.window_s - self._hold_since > self.cfg.lost_after:
                self._set_state(LOST)
        return c
