"""Camera autopilot vision for the ROV (used by rov_gui.py, which owns the camera).

Works on the same frames the dashboard shows (after FLIP and PICTURE adjustments), shrunk to about
140x100 pixels so it is quick on a Raspberry Pi 4. Needs only numpy.

  find_line(img, colour)   -> {"found", "offset", "angle", "points"}   line following
  find_target(img, colour) -> {"found", "x", "y", "area"}              colour target following
  HeadingTracker.update()  -> {"found", "heading"}                     camera heading hold

Positions are -1..1 across the picture (x: left -1 .. right +1, y: top -1 .. bottom +1).
"""
import numpy as np

# hue ranges in degrees (underwater colours shift: use PICTURE > WARMTH to help red/orange)
HUES = {"red": [(340, 360), (0, 18)], "orange": [(18, 42)], "yellow": [(42, 70)], "green": [(70, 165)],
        "blue": [(165, 255)], "pink": [(285, 340)]}


def to_array(ppm, step=4):
    """PPM frame (b'P6\\nW H\\n255\\n' + RGB) -> float array (h/step, w/step, 3) in 0..1."""
    _, size, _, data = ppm.split(b"\n", 3)
    w, h = (int(v) for v in size.split())
    img = np.frombuffer(data, np.uint8, count=w * h * 3).reshape(h, w, 3)
    return img[::step, ::step].astype(np.float32) / 255.0


def colour_mask(img, colour):
    """True where the picture has `colour` ('dark', 'white' or a name in HUES)."""
    mx = img.max(axis=2)
    mn = img.min(axis=2)
    d = mx - mn
    sat = np.where(mx > 1e-3, d / np.maximum(mx, 1e-3), 0.0)
    if colour == "dark":
        return mx < min(0.35, 0.6 * float(np.median(mx)))
    if colour == "white":
        return (mx > max(0.7, float(np.percentile(mx, 90)))) & (sat < 0.25)
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    dd = np.maximum(d, 1e-6)
    hue = np.where(mx == r, ((g - b) / dd) % 6, np.where(mx == g, (b - r) / dd + 2, (r - g) / dd + 4)) * 60
    m = np.zeros(mx.shape, bool)
    for lo, hi in HUES.get(colour, []):
        m |= (hue >= lo) & (hue < hi)
    return m & (sat > 0.35) & (mx > 0.15)


def find_line(img, colour="dark"):
    """Line on the bottom of the picture (camera looking forward-down or down).
    offset: where the near end of the line is (+ = right). angle: where it heads (+ = curving right)."""
    h, w = img.shape[:2]
    edges = np.linspace(h // 3, h, 4).astype(int)       # 3 bands across the lower 2/3
    xs = np.arange(w)
    pts = []
    for y0, y1 in zip(edges[:-1], edges[1:]):
        # each band judged on its own, so uneven lighting (darker further down) doesn't look like line
        cols = colour_mask(img[y0:y1], colour).sum(axis=0)
        total = cols.sum()
        size = (y1 - y0) * w
        if 0.015 * size <= total <= 0.6 * size:        # some line, but not the whole picture
            pts.append(((y0 + y1) / 2, float((cols * xs).sum() / total)))
    if len(pts) < 2:
        return {"found": False}
    (_, x_far), (_, x_near) = pts[0], pts[-1]
    return {"found": True, "offset": (x_near - w / 2) / (w / 2), "angle": (x_far - x_near) / (w / 2),
            "points": [(x / w, y / h) for y, x in pts]}


def find_target(img, colour="orange"):
    """Biggest patch of `colour`: its centre (x, y) and how much of the picture it fills (area 0..1)."""
    m = colour_mask(img, colour)
    h, w = m.shape
    if m.sum() < 0.003 * m.size:
        return {"found": False}
    ys, xs = np.nonzero(m)
    mx, my = np.median(xs), np.median(ys)
    keep = (np.abs(xs - mx) < w * 0.25) & (np.abs(ys - my) < h * 0.25)   # ignore scattered specks
    n = int(keep.sum())
    if n < 0.003 * m.size:
        return {"found": False}
    cx, cy = float(xs[keep].mean()), float(ys[keep].mean())
    return {"found": True, "x": (cx - w / 2) / (w / 2), "y": (cy - h / 2) / (h / 2), "area": n / m.size}


class HeadingTracker:
    """Estimates how far the ROV has turned since `epoch` changed, from how the picture slides sideways
    (phase correlation between frames). heading > 0 = turned right, as a share of the picture width.
    Needs a picture with detail (pool floor, rocks); in plain water it reports found = False."""

    def __init__(self):
        self.epoch = None
        self.prev = None
        self.total = 0.0
        self.win = None

    def update(self, img, epoch):
        g = img.mean(axis=2)
        if self.win is None or self.win.shape != g.shape:
            self.win = np.outer(np.hanning(g.shape[0]), np.hanning(g.shape[1]))
            self.prev = None
        if epoch != self.epoch:
            self.epoch, self.prev, self.total = epoch, None, 0.0
        detail = float(g.std())
        f = np.fft.fft2((g - g.mean()) * self.win)
        found = False
        if self.prev is not None and detail > 0.02:
            r = f * np.conj(self.prev)
            r /= np.abs(r) + 1e-9
            corr = np.fft.ifft2(r).real
            iy, ix = np.unravel_index(int(np.argmax(corr)), corr.shape)
            w = corr.shape[1]
            dx = ix - w if ix > w // 2 else ix                   # picture moved dx pixels to the right
            if corr.max() > 0.08 and abs(dx) < w // 4:
                self.total -= dx / w                             # picture moves left when the ROV turns right
                found = True
        self.prev = f
        return {"found": found, "heading": self.total, "detail": detail}
