"""Hardware-free thermal frame analysis and heat-map rendering for the Pi node.

Kept separate from sensor_monitor.py so it can be tested on any machine.
"""
import numpy as np
from PIL import Image, ImageDraw

SCALE = 20  # 32x24 sensor pixels -> 640x480 image

# Black -> purple -> red -> orange -> pale yellow, similar to common thermal palettes.
_PALETTE = np.array(
    [(0, 0, 0), (80, 0, 120), (200, 30, 30), (255, 150, 0), (255, 255, 200)],
    dtype=np.float32,
)


def _largest_component(mask):
    """Return (pixel_count, bbox) of the largest 8-connected True region."""
    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    best_count, best_bbox = 0, None
    for y, x in zip(*np.where(mask)):
        if seen[y, x]:
            continue
        stack = [(y, x)]
        seen[y, x] = True
        count = 0
        x0, y0, x1, y1 = x, y, x, y
        while stack:
            cy, cx = stack.pop()
            count += 1
            x0, y0, x1, y1 = min(x0, cx), min(y0, cy), max(x1, cx), max(y1, cy)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        if count > best_count:
            best_count, best_bbox = count, (int(x0), int(y0), int(x1), int(y1))
    return best_count, best_bbox


def analyze(temps, hot_temp_c, ambient_delta_c, min_cluster_pixels, min_hot_percent):
    """Summarise one 24x32 frame of temperatures (C). Returns None if no pixel is valid.

    A pixel is hot when it is above both HOT_TEMP_C and the scene background plus
    AMBIENT_DELTA_C. The frame is hot when the largest connected hot cluster is big
    enough, which rejects single noisy pixels and scattered sun glints.
    """
    temps = np.asarray(temps, dtype=np.float32).reshape(24, 32)
    valid = np.isfinite(temps)
    if not valid.any():
        return None
    values = temps[valid]
    max_temp = float(values.max())
    ambient = float(np.percentile(values, 60))
    threshold = max(hot_temp_c, ambient + ambient_delta_c)
    hot_mask = valid & (temps >= threshold)
    cluster, bbox = _largest_component(hot_mask)
    hot_percent = float(hot_mask.sum() / valid.sum() * 100.0)
    hot = cluster >= min_cluster_pixels and hot_percent >= min_hot_percent
    return {
        "hot": bool(hot),
        "max_temp": max_temp,
        "ambient": ambient,
        "threshold": threshold,
        "hot_percent": hot_percent if hot else 0.0,
        "cluster_pixels": cluster,
        "bbox": bbox if cluster else None,
    }


def _colorize(normalized):
    """Map values in 0..1 to RGB using the palette."""
    positions = normalized * (len(_PALETTE) - 1)
    low = np.clip(np.floor(positions).astype(int), 0, len(_PALETTE) - 2)
    frac = (positions - low)[..., None]
    rgb = _PALETTE[low] * (1 - frac) + _PALETTE[low + 1] * frac
    return rgb.astype(np.uint8)


def render_heatmap(temps, stats, label=""):
    """Render a 640x480 heat-map with the hot cluster boxed and temperatures in a footer."""
    temps = np.asarray(temps, dtype=np.float32).reshape(24, 32)
    valid = np.isfinite(temps)
    low = float(temps[valid].min()) if valid.any() else 0.0
    high = max(float(temps[valid].max()) if valid.any() else 0.0, low + 10.0)
    filled = np.where(valid, temps, low)

    width, height = 32 * SCALE, 24 * SCALE
    smooth = np.asarray(Image.fromarray(filled).resize((width, height), Image.BICUBIC))
    # Give the ordinary scene the lower half of the palette and hot spots the upper half,
    # so a 300 C fire does not flatten trees, ground and horizon into black.
    scene_high = min(max(float(np.percentile(filled, 95)), low + 3.0), high - 1.0)
    normalized = np.interp(smooth, [low, scene_high, high], [0.0, 0.5, 1.0])
    image = Image.fromarray(_colorize(normalized))

    draw = ImageDraw.Draw(image)
    if stats and stats.get("bbox"):
        x0, y0, x1, y1 = stats["bbox"]
        draw.rectangle(
            (x0 * SCALE - 2, y0 * SCALE - 2, (x1 + 1) * SCALE + 1, (y1 + 1) * SCALE + 1),
            outline=(0, 255, 255), width=3,
        )
    if stats:
        text = f"max {stats['max_temp']:.1f} C   background {stats['ambient']:.1f} C   {label}"
    else:
        text = label
    draw.rectangle((0, height - 22, width, height), fill=(0, 0, 0))
    draw.text((8, height - 17), text, fill=(255, 255, 255))
    return image
