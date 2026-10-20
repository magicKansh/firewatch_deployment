import os
import sys
import tempfile
from pathlib import Path

# Point the server at a throwaway database and image folder before any app module is imported.
_tmp = Path(tempfile.mkdtemp(prefix="firewatch-tests-"))
os.environ["FIREWATCH_DB"] = str(_tmp / "test.db")
os.environ["FIREWATCH_IMAGE_DIR"] = str(_tmp / "images")
os.environ.pop("FIRMS_API_KEY", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
