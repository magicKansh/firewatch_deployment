"""Pi-side file handling: rolling RAM snapshots and an on-disk outbox for failed uploads."""
import json
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def default_snapshot_dir():
    # /dev/shm is RAM-backed on Raspberry Pi OS, so per-minute snapshots don't wear the SD card.
    shm = Path("/dev/shm")
    return (shm if shm.is_dir() else Path(tempfile.gettempdir())) / "firewatch"


def utc_stamp(moment=None):
    return (moment or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


class SnapshotRing:
    """Keeps only the newest `keep` snapshots; everything older is deleted."""

    def __init__(self, directory, keep=5):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keep = max(0, int(keep))
        self.count = 0
        for stale in self.dir.glob("snap_*.jpg"):   # leftovers from a previous run
            stale.unlink(missing_ok=True)

    def add(self, image, stamp=None):
        self.count += 1   # keeps names unique and in order even within the same second
        path = self.dir / f"snap_{stamp or utc_stamp()}_{self.count:06d}.jpg"
        image.save(path, format="JPEG", quality=85)
        self.prune()
        return path

    def recent(self, count):
        files = sorted(self.dir.glob("snap_*.jpg"))
        return files[-count:] if count > 0 else []

    def prune(self):
        files = sorted(self.dir.glob("snap_*.jpg"))
        for old in files[:max(0, len(files) - self.keep)]:
            old.unlink(missing_ok=True)


class PermanentUploadError(Exception):
    """The server rejected the event (bad key, invalid data); retrying won't help."""


class Outbox:
    """Events that could not be uploaded, stored on disk and retried later (oldest first)."""

    def __init__(self, directory, max_items=50):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_items = max(1, int(max_items))

    def items(self):
        return sorted(p for p in self.dir.iterdir() if p.is_dir() and (p / "meta.json").exists())

    def put(self, form, files):
        """files: list of (field_name, path, mime). Files are moved into the outbox."""
        item = self.dir / f"{utc_stamp()}_{time.time_ns()}"   # sortable = oldest first
        item.mkdir()
        stored = []
        for index, (field, path, mime) in enumerate(files):
            path = Path(path)
            if not path.exists():
                continue
            name = f"{index}_{path.name}"
            shutil.move(str(path), item / name)
            stored.append([field, name, mime])
        (item / "meta.json").write_text(json.dumps({"form": form, "files": stored}))
        items = self.items()
        for old in items[:max(0, len(items) - self.max_items)]:
            shutil.rmtree(old, ignore_errors=True)
        return item

    def flush(self, send):
        """Retry queued events with send(form, files). Stops at the first transient failure.

        Returns (sent, dropped) counts.
        """
        sent = dropped = 0
        for item in self.items():
            meta = json.loads((item / "meta.json").read_text())
            files = [(field, item / name, mime) for field, name, mime in meta["files"]]
            try:
                send(meta["form"], files)
                sent += 1
            except PermanentUploadError as exc:
                print(f"Dropping queued event {item.name}: {exc}")
                dropped += 1
            except Exception as exc:
                print(f"Outbox retry failed, will try later: {exc}")
                break
            shutil.rmtree(item, ignore_errors=True)
        return sent, dropped
