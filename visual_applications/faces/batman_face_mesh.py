"""
Batman head mesh for HoloAvatar — a drop-in sibling of `core.avatar_mesh`.

It exposes exactly the contract `HoloAvatar` consumes (`get_head_mesh`, `JAW_MAX`,
`JAW_PIVOT`), so the renderer lights, poses and animates it unchanged: jaw drop,
brow lift, lip spread, blinking, gaze and the viseme lip-sync all keep working.

What is different from the human head
-------------------------------------
* The geometry is procedural. There is no MediaPipe landmark table behind it, so
  the head is a stack of low-poly rings (a superellipse per row, squarer towards
  the jaw) with analytic relief added on top: scowling brow ridge, recessed eye
  sockets, a pyramidal nose, cheekbones, a squared chin.
* The cowl: everything above a diagonal line under the nose and along the jaw is
  raised a hair and dimmed through `fade`, which reads as a mask over bare skin
  without needing per-face colours.
* Two pointed ears are separate cones whose base ring is solved to lie ON the
  skull, so no buried geometry shows through the wireframe.
* The "landmarks" (eye slits, brows, inner and outer lips) are decal rings that
  sit just in front of the surface. They are not part of any face or edge, which
  is how the renderer wants them: it paints the openings itself.

Units match the human mesh: head-half-heights, +y up, +z towards the camera.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np

# Jaw hinge sits at ear level, a little behind the face. At JAW_MAX the chin drops
# about 0.2 head-half-heights, which is a clearly open mouth.
JAW_MAX = 0.30
JAW_PIVOT = (0.0, -0.02, -0.28)

N_U = 28          # columns around the skull; column 0 is the nose line
N_NECK = 20

# (y, half-width, front depth, back depth, superellipse exponent)
# Exponent 2 is an ellipse; higher squares the section off, which is what gives
# the lower face its heavy, blocky jaw.
_ROWS = np.array([
    [0.93, 0.27, 0.27, 0.30, 2.2],
    [0.82, 0.47, 0.46, 0.56, 2.2],
    [0.70, 0.59, 0.55, 0.72, 2.3],
    [0.56, 0.69, 0.61, 0.80, 2.4],
    [0.42, 0.74, 0.65, 0.83, 2.5],
    [0.28, 0.75, 0.67, 0.84, 2.5],
    [0.14, 0.74, 0.66, 0.83, 2.5],
    [0.00, 0.72, 0.64, 0.80, 2.6],
    [-0.14, 0.71, 0.63, 0.76, 2.8],
    [-0.27, 0.70, 0.61, 0.70, 3.0],
    [-0.37, 0.69, 0.60, 0.66, 3.2],
    [-0.46, 0.68, 0.60, 0.62, 3.3],   # upper lip line: jaw weight 0
    [-0.54, 0.67, 0.60, 0.58, 3.4],   # lower lip line: jaw weight 1
    [-0.66, 0.64, 0.59, 0.52, 3.5],
    [-0.79, 0.58, 0.57, 0.44, 3.6],
    [-0.91, 0.48, 0.54, 0.35, 3.4],
    [-1.01, 0.30, 0.46, 0.22, 2.8],
], dtype=np.float64)

_Y_MOUTH = -0.50          # between the two lip rows
_MOUTH_HALF = 0.24        # mouth corner, as an angle from the nose line (rad)
_COWL_FADE = 0.78         # brightness of the cowl against bare skin

# (y, half-width x, half-depth z, centre z) for the neck / collar, and its fade.
_NECK = np.array([
    [-1.00, 0.38, 0.34, -0.10],
    [-1.18, 0.38, 0.36, -0.10],
    [-1.42, 0.44, 0.40, -0.10],
    [-1.64, 0.52, 0.44, -0.08],
    [-1.86, 0.62, 0.50, -0.06],
])
_NECK_FADE = np.array([0.0, 0.60, 0.86, 0.60, 0.0])


# ── small helpers ───────────────────────────────────────────────────────────

def _smooth(e0, e1, x):
    t = np.clip((np.asarray(x, float) - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _sig(x):
    return 1.0 / (1.0 + np.exp(-np.asarray(x, float)))


def _wrap(theta):
    """Absolute angle from the nose line, 0..pi."""
    return np.abs((np.asarray(theta, float) + np.pi) % (2.0 * np.pi) - np.pi)


def _cowl_edge(t):
    """Height of the cowl's lower edge at angle t from the nose line: just under
    the nose in front, sweeping down along the cheek to the jaw hinge."""
    return -0.33 - 0.30 * _smooth(0.0, 1.15, t)


def _profile(y):
    ys = _ROWS[::-1, 0]
    return tuple(np.interp(y, ys, _ROWS[::-1, k]) for k in (1, 2, 3, 4))


def _relief(theta, y):
    """Outward displacement of the smooth skull at (theta, y)."""
    t = _wrap(theta)
    y = np.asarray(y, float)
    d = np.zeros(np.broadcast(t, y).shape)

    # Heavy scowling brow ridge: low at the nose, rising towards the temples.
    yb = 0.25 + 0.15 * np.clip(t / 0.9, 0.0, 1.0)
    d += 0.110 * np.exp(-((y - yb) / 0.08) ** 2) * np.exp(-(t / 1.1) ** 2)

    # Eye sockets sit back under the ridge.
    d -= 0.030 * np.exp(-((y - 0.08) / 0.09) ** 2) * np.exp(-((t - 0.40) / 0.30) ** 2)

    # Nose: a ridge that grows towards the tip and falls away under it.
    ny = np.interp(y, [-0.40, -0.30, -0.22, -0.14, 0.00, 0.16, 0.30, 0.40],
                   [0.00, 0.06, 0.19, 0.25, 0.15, 0.08, 0.02, 0.00])
    d += ny * np.exp(-(t / 0.17) ** 2)

    # Cheekbones, the lips, and a square chin.
    d += 0.045 * np.exp(-((y + 0.12) / 0.12) ** 2) * np.exp(-((t - 0.65) / 0.28) ** 2)
    d += 0.030 * np.exp(-((y - _Y_MOUTH) / 0.07) ** 2) * np.exp(-(t / 0.40) ** 2)
    d += 0.070 * np.exp(-((y + 0.93) / 0.12) ** 2) * np.exp(-(t / 0.42) ** 2)

    # The cowl stands a little proud of the skin below it.
    d += 0.034 * _sig((y - _cowl_edge(t)) / 0.045)
    return d


def _surf(theta, y, lift=0.0):
    """Point on the (smooth) head surface at angle `theta` from the nose line and
    height `y`. Broadcasts; returns (x, y, z)."""
    theta = np.asarray(theta, float)
    y = np.asarray(y, float)
    a, zf, zb, n = _profile(y)
    s, c = np.sin(theta), np.cos(theta)
    e = 2.0 / n
    x = a * np.sign(s) * np.abs(s) ** e
    z = np.where(c >= 0.0, zf, zb) * np.sign(c) * np.abs(c) ** e
    d = _relief(theta, y) + lift
    return x + d * s, y + 0.0 * x, z + d * c


def _crown_y(x, z):
    """Height of the skull surface over the point (x, z)."""
    th = math.atan2(x, z)
    ys = np.linspace(0.55, 1.0, 160)
    px, _py, pz = _surf(np.full_like(ys, th), ys)
    r = np.hypot(px, pz)                    # shrinks as y rises
    return float(np.interp(math.hypot(x, z), r[::-1], ys[::-1]))


# ── builder ─────────────────────────────────────────────────────────────────

class _Acc:
    """Accumulates vertices and their per-vertex weights."""

    def __init__(self):
        self.p, self.jaw, self.brow, self.lips, self.fade, self.n = [], [], [], [], [], 0

    def add(self, pts, jaw, brow, lips, fade):
        pts = np.asarray(pts, float).reshape(-1, 3)
        k = len(pts)
        for lst, v in ((self.jaw, jaw), (self.brow, brow), (self.lips, lips), (self.fade, fade)):
            lst.append(np.broadcast_to(np.asarray(v, float), (k,)).copy())
        self.p.append(pts)
        start = self.n
        self.n += k
        return start


def _grid_faces(rows, cols, base, down=True):
    """Triangles for a cylinder-like grid of vertices, wound to face outwards.
    `down` says whether successive rows go down (head, neck) or up (ears)."""
    f = []
    for r in range(rows - 1):
        for k in range(cols):
            k1 = (k + 1) % cols
            A, B = base + r * cols + k, base + r * cols + k1
            C, D = base + (r + 1) * cols + k, base + (r + 1) * cols + k1
            f += [(A, C, B), (B, C, D)] if down else [(A, B, C), (B, D, C)]
    return f


def _build():
    acc = _Acc()
    faces, groups = [], []

    # ── skull ───────────────────────────────────────────────────────────────
    th = np.arange(N_U) * 2.0 * np.pi / N_U
    ys = _ROWS[:, 0]
    TH, Y = np.meshgrid(th, ys)
    X, Yv, Z = _surf(TH, Y)
    t = _wrap(TH)

    apex = acc.add([[0.0, 1.0, -0.04]], 0.0, 0.0, 0.0, _COWL_FADE)
    jaw = np.clip((-0.46 - Y) / 0.08, 0.0, 1.0) * (1.0 - _smooth(1.9, 2.6, t))
    brow = 0.9 * np.exp(-((Y - 0.34) / 0.14) ** 2) * np.exp(-(t / 1.0) ** 2)
    lips = np.exp(-((X / 0.38) ** 2 + ((Y - _Y_MOUTH) / 0.16) ** 2))
    fade = 1.0 - (1.0 - _COWL_FADE) * _sig((Y - _cowl_edge(t)) / 0.045)
    ring0 = acc.add(np.stack([X, Yv, Z], -1).reshape(-1, 3),
                    jaw.ravel(), brow.ravel(), lips.ravel(), fade.ravel())
    cap = acc.add([[0.0, -1.07, 0.10]], 1.0, 0.0, 0.0, 1.0)

    R = len(ys)
    for k in range(N_U):
        k1 = (k + 1) % N_U
        faces.append((apex, ring0 + k, ring0 + k1))
        faces.append((cap, ring0 + (R - 1) * N_U + k1, ring0 + (R - 1) * N_U + k))
    faces += _grid_faces(R, N_U, ring0, down=True)
    groups += [1] * (len(faces) - len(groups))

    # ── neck / collar ───────────────────────────────────────────────────────
    nth = np.arange(N_NECK) * 2.0 * np.pi / N_NECK
    e = 2.0 / 2.4
    neck0 = acc.n
    for (y, ax, az, zc), fd in zip(_NECK, _NECK_FADE):
        x = ax * np.sign(np.sin(nth)) * np.abs(np.sin(nth)) ** e
        z = zc + az * np.sign(np.cos(nth)) * np.abs(np.cos(nth)) ** e
        acc.add(np.stack([x, np.full(N_NECK, y), z], -1), 0.0, 0.0, 0.0, fd)
    nf = _grid_faces(len(_NECK), N_NECK, neck0, down=True)
    faces += nf
    groups += [0] * len(nf)

    # ── ears ────────────────────────────────────────────────────────────────
    ts = [0.0, 0.22, 0.52, 0.80]
    sc = [1.00, 0.82, 0.52, 0.24]
    ang = np.arange(6) * 2.0 * np.pi / 6
    for sx in (-1.0, 1.0):
        c0 = np.array([sx * 0.40, 0.0, -0.10])
        tip = np.array([sx * 0.50, 1.42, -0.22])
        dx, dz = 0.21 * np.sin(ang), 0.18 * np.cos(ang)
        y0 = np.array([_crown_y(c0[0] + a, c0[2] + b) - 0.01 for a, b in zip(dx, dz)])
        yc = float(y0.mean())
        e0 = acc.n
        for tt, s in zip(ts, sc):
            cx = c0[0] + (tip[0] - c0[0]) * tt
            cz = c0[2] + (tip[2] - c0[2]) * tt
            yy = yc + (tip[1] - yc) * tt + (y0 - yc) * (1.0 - tt)
            acc.add(np.stack([cx + s * dx, yy, cz + s * dz], -1), 0.0, 0.0, 0.0, _COWL_FADE)
        tipv = acc.add([tip], 0.0, 0.0, 0.0, _COWL_FADE)
        ef = _grid_faces(len(ts), 6, e0, down=False)
        last = e0 + (len(ts) - 1) * 6
        ef += [(tipv, last + k, last + (k + 1) % 6) for k in range(6)]
        faces += ef
        groups += [1] * len(ef)

    n_surface = acc.n

    # ── decals: eyes, brows, lips ───────────────────────────────────────────
    def place(thetas, ys_, lift=0.014):
        x, y, z = _surf(np.asarray(thetas, float), np.asarray(ys_, float), lift)
        return np.stack([np.broadcast_to(x, np.shape(thetas)),
                         np.broadcast_to(y, np.shape(thetas)),
                         np.broadcast_to(z, np.shape(thetas))], -1)

    def brow_weight(pts):
        tt = _wrap(np.arctan2(pts[:, 0], pts[:, 2]))
        return 0.9 * np.exp(-((pts[:, 1] - 0.34) / 0.14) ** 2) * np.exp(-(tt / 1.0) ** 2)

    lm = {}

    # Eye slits: narrow, slanted down towards the nose. Batman's eyes are the
    # white lenses of the cowl, so the renderer's iris and pupil read as glowing
    # slits inside them.
    for key, sgn in (("eye_l", -1.0), ("eye_r", 1.0)):
        phi = np.arange(8) * 2.0 * np.pi / 8
        u, v = np.cos(phi), np.sin(phi)
        th_e = sgn * (0.42 + 0.26 * u)
        y_e = 0.09 + 0.042 * v + 0.028 * u
        pts = place(th_e, y_e)
        lm[key] = acc.add(pts, 0.0, brow_weight(pts), 0.0, 1.0)
        lm[key] = np.arange(lm[key], lm[key] + 8)

    # Brows trace the crest of the ridge, inner end low.
    for key, sgn in (("brow_l", -1.0), ("brow_r", 1.0)):
        tb = np.linspace(0.10, 0.80, 5)
        pts = place(sgn * tb, 0.25 + 0.15 * np.clip(tb / 0.9, 0, 1) - 0.02, lift=0.02)
        i0 = acc.add(pts, 0.0, 1.0, 0.0, 1.0)
        lm[key] = np.arange(i0, i0 + 5)

    # Inner lip ring: lower lip left→right (11 points), then upper lip back
    # right→left (9 points) — the order the renderer's teeth strip relies on.
    s = np.linspace(0.0, 1.0, 11)
    th_low = -_MOUTH_HALF + 2.0 * _MOUTH_HALF * s
    y_low = _Y_MOUTH - 0.010 * np.sin(np.pi * s)
    th_up = (_MOUTH_HALF - 2.0 * _MOUTH_HALF * s)[1:-1]
    y_up = _Y_MOUTH + 0.012 * np.sin(np.pi * s[1:-1])
    pts = place(np.concatenate([th_low, th_up]), np.concatenate([y_low, y_up]), lift=0.016)
    jw = np.concatenate([[0.5], np.ones(9), [0.5], np.zeros(9)])
    i0 = acc.add(pts, jw, 0.0, 1.0, 1.0)
    lm["lips_in"] = np.arange(i0, i0 + 20)

    # Outer lip outline: slightly fuller than the inner ring.
    phi = np.arange(16) * 2.0 * np.pi / 16
    th_o = 1.18 * _MOUTH_HALF * np.cos(phi)
    y_o = _Y_MOUTH + 0.050 * np.sin(phi) * (0.55 + 0.45 * np.cos(phi) ** 2)
    pts = place(th_o, y_o, lift=0.016)
    jw = np.clip(0.5 - 1.2 * np.sin(phi), 0.0, 1.0)
    i0 = acc.add(pts, jw, 0.0, 1.0, 1.0)
    lm["lips_out"] = np.arange(i0, i0 + 16)

    # ── assemble ────────────────────────────────────────────────────────────
    verts = np.concatenate(acc.p).astype(np.float32)
    f = np.asarray(faces, dtype=np.int32)

    # Area-weighted vertex normals from the faces that actually use each vertex.
    p0, p1, p2 = verts[f[:, 0]], verts[f[:, 1]], verts[f[:, 2]]
    fn = np.cross(p1 - p0, p2 - p0)
    nrm = np.zeros_like(verts)
    for i in range(3):
        np.add.at(nrm, f[:, i], fn)
    ln = np.linalg.norm(nrm, axis=1, keepdims=True)
    nrm = np.where(ln > 1e-12, nrm / np.maximum(ln, 1e-12), 0.0).astype(np.float32)
    # Decals are not on any face: give them the local outward direction.
    for idx in lm.values():
        r = verts[idx]
        o = np.stack([r[:, 0] * 0.6, np.zeros(len(r)), np.maximum(r[:, 2], 0.2)], 1)
        nrm[idx] = (o / np.linalg.norm(o, axis=1, keepdims=True)).astype(np.float32)

    # Unique undirected edges of the surface.
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    e = np.unique(np.sort(e, axis=1), axis=0).astype(np.int32)

    return {
        "verts": verts,
        "normals": nrm,
        "jaw": np.concatenate(acc.jaw).astype(np.float32),
        "brow": np.concatenate(acc.brow).astype(np.float32),
        "lips": np.concatenate(acc.lips).astype(np.float32),
        "lip_centre": np.array([0.0, _Y_MOUTH, 0.60], dtype=np.float32),
        "fade": np.concatenate(acc.fade).astype(np.float32),
        "faces": f,
        "face_group": np.asarray(groups, dtype=np.float32),
        "edges": e,
        "landmarks": {k: np.asarray(v, dtype=np.intp) for k, v in lm.items()},
        "span": (float(verts[:n_surface, 1].max()), float(verts[:n_surface, 1].min())),
    }


@lru_cache(maxsize=1)
def get_head_mesh() -> dict:
    """Build (once) and return the Batman head mesh."""
    return _build()


if __name__ == "__main__":
    m = get_head_mesh()
    print({k: (v.shape if hasattr(v, "shape") else type(v).__name__)
           for k, v in m.items() if k != "landmarks"})
    print("span", m["span"], "landmarks", {k: len(v) for k, v in m["landmarks"].items()})
