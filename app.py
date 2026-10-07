from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from pathlib import Path
import os

from database import init_db, migrate_legacy_csvs

BASE_DIR = Path(__file__).resolve().parent
init_db()
migrate_legacy_csvs()

app = FastAPI(title="Firewatch")
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
image_dir = Path(os.getenv("FIREWATCH_IMAGE_DIR", BASE_DIR / "images"))
image_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.mount("/images", StaticFiles(directory=image_dir), name="images")

from device_api import router as device_router
from camera_api import router as camera_router
app.include_router(device_router)
app.include_router(camera_router)
