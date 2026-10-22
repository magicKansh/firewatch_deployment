"""Thermal-frame analysis and heat-signature rendering for the MLX90640 (32x24).

Pure numpy/Pillow so it can be tested without the sensor attached.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROWS, COLS = 24, 32


@dataclass
class ThermalStats:
    max_temp: float
    ambient: float
    threshold: float
    hot_percent: float      # % of frame above threshold (0 when the largest cluster is too small)
    cluster: int            # pixels in the largest connected hot cluster
    hotspot_x: float        # cluster centroid in sensor columns (0..31), -1 when none
    hotspot_y: float        # cluster centroid in sensor rows (0..23), -1 when none
    bbox: tuple | None      # (x0, y0, x1, y1) inclusive sensor coordinates of the cluster


def to_grid(frame, flip_h=False, flip_v=False):
    temps = np.asarray(frame, dtype=np.float32).reshape(ROWS, COLS)
    if flip_h:
        temps = temps[:, ::-1]
    if flip_v:
        temps = temps[::-1, :]
    return temps


def largest_component(mask):
    """Return a boolean mask of the largest 8-connected True region."""
    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    best = []
    for y, x in zip(*np.where(mask)):
        if seen[y, x]:
            continue
        stack, region = [(y, x)], []
        seen[y, x] = True
        while stack:
            cy, cx = stack.pop()
            region.append((cy, cx))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        if len(region) > len(best):
            best = region
    out = np.zeros_like(mask, dtype=bool)
    if best:
        ys, xs = zip(*best)
        out[list(ys), list(xs)] = True
    return out


def analyze(temps, hot_temp_c, ambient_delta_c, min_cluster_pixels):
    """Find the hottest cluster. Returns None when the frame has no valid pixels."""
    valid = np.isfinite(temps)
    if not valid.any():
        return None
    vals = temps[valid]
    max_temp = float(vals.max())
    ambient = float(np.percentile(vals, 60))
    threshold = max(hot_temp_c, ambient + ambient_delta_c)
    hot = valid & (temps >= threshold)
    blob = largest_component(hot)
    cluster = int(blob.sum())
    if cluster:
        ys, xs = np.nonzero(blob)
        hx, hy = float(xs.mean()), float(ys.mean())
        bbox = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
    else:
        hx = hy = -1.0
        bbox = None
    hot_percent = float(hot.sum() / valid.sum() * 100.0) if cluster >= min_cluster_pixels else 0.0
    return ThermalStats(max_temp, ambient, threshold, hot_percent, cluster, hx, hy, bbox)


# Small "inferno"-style palette: black -> purple -> red -> orange -> yellow -> white.
_ANCHORS = np.array([
    [0, 0, 4], [40, 11, 84], [101, 21, 110], [159, 42, 99],
    [212, 72, 66], [245, 125, 21], [250, 193, 39], [252, 255, 164],
], dtype=np.float32)
_LUT = np.stack([
    np.interp(np.linspace(0, len(_ANCHORS) - 1, 256), np.arange(len(_ANCHORS)), _ANCHORS[:, c])
    for c in range(3)
], axis=1).astype(np.uint8)


def _font(size):
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def render(temps, stats=None, title="", scale=20, timestamp=None):
    """Render a colour heat map with the hotspot boxed and a caption bar. Returns a PIL image."""
    grid = np.where(np.isfinite(temps), temps, np.nanmin(temps) if np.isfinite(temps).any() else 0)
    lo = float(grid.min())
    # A minimum 30 C span keeps quiet scenes dark, so a sun-warmed field doesn't look like a fire.
    hi = max(float(grid.max()), lo + 30.0)
    width, height = COLS * scale, ROWS * scale
    smooth = Image.fromarray(np.ascontiguousarray(grid, dtype=np.float32)).resize((width, height), Image.BICUBIC)
    norm = np.clip((np.asarray(smooth) - lo) / (hi - lo), 0, 1)
    rgb = _LUT[(norm * 255).astype(np.uint8)]

    bar = 54
    canvas = Image.new("RGB", (width, height + bar), (17, 17, 17))
    canvas.paste(Image.fromarray(rgb), (0, 0))
    draw = ImageDraw.Draw(canvas)

    if stats and stats.bbox:
        x0, y0, x1, y1 = stats.bbox
        pad = scale // 2
        draw.rectangle([x0 * scale - pad, y0 * scale - pad, (x1 + 1) * scale + pad, (y1 + 1) * scale + pad],
                       outline=(0, 255, 200), width=3)

    stamp = (timestamp or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC")
    big, small = _font(18), _font(14)
    draw.text((10, height + 6), title or "Firewatch thermal", fill=(255, 255, 255), font=big)
    draw.text((width - 10 - draw.textlength(stamp, font=small), height + 8), stamp, fill=(200, 200, 200), font=small)
    if stats:
        line = (f"max {stats.max_temp:.1f} C   background {stats.ambient:.1f} C   "
                f"threshold {stats.threshold:.0f} C   hot area {stats.hot_percent:.1f}%")
    else:
        line = f"range {lo:.1f} - {float(grid.max()):.1f} C"
    draw.text((10, height + 30), line, fill=(230, 230, 230), font=small)
    return canvas
