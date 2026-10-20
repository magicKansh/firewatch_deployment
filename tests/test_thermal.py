import numpy as np

from thermal import analyze, render_heatmap

SETTINGS = dict(hot_temp_c=80, ambient_delta_c=25, min_cluster_pixels=6, min_hot_percent=0.5)


def frame(background=22.0):
    return np.full((24, 32), background, dtype=np.float32)


def test_cool_scene_is_not_hot():
    stats = analyze(frame(), **SETTINGS)
    assert not stats["hot"]
    assert stats["cluster_pixels"] == 0
    assert stats["bbox"] is None


def test_hot_cluster_is_hot_and_boxed():
    temps = frame()
    temps[10:13, 20:23] = 300.0  # 9 connected pixels
    stats = analyze(temps, **SETTINGS)
    assert stats["hot"]
    assert stats["cluster_pixels"] == 9
    assert stats["bbox"] == (20, 10, 22, 12)  # x0, y0, x1, y1 in sensor pixels
    assert stats["max_temp"] == 300.0
    assert abs(stats["ambient"] - 22.0) < 0.01


def test_single_hot_pixel_is_ignored():
    temps = frame()
    temps[5, 5] = 400.0
    assert not analyze(temps, **SETTINGS)["hot"]


def test_scattered_hot_pixels_do_not_form_a_cluster():
    temps = frame()
    for x in range(0, 32, 4):
        temps[0, x] = 200.0
    stats = analyze(temps, **SETTINGS)
    assert stats["cluster_pixels"] == 1
    assert not stats["hot"]


def test_warm_day_raises_threshold_above_ambient():
    # 85 C patch on a 70 C scene is only 15 C above background: not a fire signature.
    temps = frame(70.0)
    temps[10:14, 10:14] = 85.0
    assert not analyze(temps, **SETTINGS)["hot"]


def test_nan_pixels_are_ignored():
    temps = frame()
    temps[0, :] = np.nan
    temps[10:13, 20:23] = 300.0
    assert analyze(temps, **SETTINGS)["hot"]


def test_all_nan_frame_returns_none():
    assert analyze(np.full((24, 32), np.nan), **SETTINGS) is None


def test_render_heatmap_size_and_colour():
    temps = frame()
    temps[10:13, 20:23] = 300.0
    stats = analyze(temps, **SETTINGS)
    image = render_heatmap(temps, stats, label="CAM-001 2026-10-20 12:00 UTC")
    assert image.size == (640, 480)
    assert image.mode == "RGB"
    # The hot spot renders brighter than the cool background.
    hot = np.asarray(image.getpixel((21 * 20 + 10, 11 * 20 + 10)), dtype=int).sum()
    cool = np.asarray(image.getpixel((40, 40)), dtype=int).sum()
    assert hot > cool + 200


def test_render_heatmap_handles_flat_frame():
    temps = frame()
    image = render_heatmap(temps, analyze(temps, **SETTINGS), label="flat")
    assert image.size == (640, 480)
