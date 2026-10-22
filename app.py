from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from pathlib import Path

from database import IMAGE_DIR, init_db, migrate_legacy_csvs
from ratelimit import limiter

BASE_DIR = Path(__file__).resolve().parent
init_db()
migrate_legacy_csvs()

app = FastAPI(title="Firewatch")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
image_dir = IMAGE_DIR
image_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.mount("/images", StaticFiles(directory=image_dir), name="images")

from device_api import router as device_router
from camera_api import router as camera_router
from auth import router as auth_router
app.include_router(device_router)
app.include_router(camera_router)
app.include_router(auth_router)
