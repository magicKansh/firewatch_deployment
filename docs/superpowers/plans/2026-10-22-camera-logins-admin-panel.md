# Camera Logins and Admin Control Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-camera logins that hide camera details from the public, plus an admin panel that manages camera logins and creates admin-only test fires.

**Architecture:** A new `auth.py` turns each request into a `Viewer` (anonymous, camera user with a set of camera IDs, or admin) from a SQLite-backed session cookie or the existing admin Bearer token. Every read path (APIs, map figure, route avoidance, review page) filters by that viewer. A new `admin_panel.py` serves `/admin`; a new `test_fires.py` creates/deletes `is_test=1` events through a shared `create_event()` extracted from `device_api.camera_event`.

**Tech Stack:** FastAPI, Jinja2, SQLite (stdlib `sqlite3`), slowapi, stdlib `hashlib.scrypt`, numpy + Pillow (`thermal.py`), Plotly (dashboard), Leaflet 1.9.4 (admin click-to-place map), unittest/pytest + FastAPI `TestClient`.

**Spec:** `docs/superpowers/specs/2026-10-22-camera-logins-admin-panel-design.md`

## Global Constraints

- Base branch `main` @ 372f463; work on `feature/camera-logins-admin`.
- No new runtime dependencies except `numpy` and `Pillow` in `requirements.txt` (CI already installs them).
- Public statuses: `PUBLIC_STATUSES = ("confirmed", "active", "resolved")`; move the constant to `database.py`, keep `from database import PUBLIC_STATUSES` re-export in `device_api.py` (map.py imports it from there).
- Session cookie name `fw_session`; 32-byte `secrets.token_urlsafe`; only `sha256(token)` stored; `HttpOnly`, `SameSite=Lax`, `Secure` iff `PUBLIC_BASE_URL` starts with `https://`; lifetime 30 days.
- Passwords: `hashlib.scrypt(n=2**14, r=8, p=1, dklen=32)`, 16-byte random salt, stored as `scrypt$<salt_hex>$<hash_hex>`; compare with `hmac.compare_digest`.
- Generated camera login passwords: `secrets.token_urlsafe(12)`.
- Admin login password = `FIREWATCH_ADMIN_TOKEN`; empty/unset token disables admin login.
- Login POSTs (`/login`, `/admin/login`) rate-limited `10/minute` per IP via the existing `app.limiter`.
- Wrong camera ID and wrong password produce the identical message: `Camera ID or password is wrong.`
- Hidden test camera ID `TEST_CAMERA_ID = "__test__"` (in `database.py`); never listed, never loginable.
- Test fire notifications prefix `[TEST] ` on SMS body and email subject.
- Simulation defaults: max temperature `350` °C, hot spot `12` sensor pixels, alerts on. Valid ranges: temperature 100–1000, hot pixels 6–200.
- Drop-fire severities → `severity_index`: Low 10, Moderate 30, High 55, Extreme 85.
- No GET request changes state. All browser actions are POST forms.
- Tests must pass on Windows and Linux (`TemporaryDirectory(ignore_cleanup_errors=True)`).

## Review Focus

1. Camera ID typed with spaces (`" CAM-A "`) on the login form → expected to sign in (trimmed); case stays significant. Test in Task 2.
2. A session that holds a camera which is later disabled → that camera silently drops out of the viewer; the rest of the session keeps working. Test in Task 1.
3. `FIREWATCH_ADMIN_TOKEN` unset and someone posts an empty admin password → must be rejected, never an admin session. Test in Task 2.
4. Simulation form with out-of-range values (hot pixels 0 or 999, temperature 20) → 400 with a message on the panel, no event created. Test in Task 8.
5. *Delete all test fires* when some image files are already gone → completes without error and removes the rows. Test in Task 8.

---

## File Structure

| File | Responsibility |
|---|---|
| `auth.py` (new) | Password hashing, sessions, `Viewer`, `get_viewer`, login/logout/admin-login routes. |
| `admin_panel.py` (new) | `/admin` page and its POST actions. |
| `test_fires.py` (new) | Synthetic frames, simulate detection, drop fire, list/delete test fires. |
| `templates/login.html`, `templates/admin.html`, `templates/admin_secret.html` (new) | Pages in the style of `templates/review.html` (same CSS variables, header with FW mark). |
| `database.py` | New columns/tables, `PUBLIC_STATUSES`, `TEST_CAMERA_ID`. |
| `device_api.py` | `create_event()` extraction, `is_test` isolation, viewer-scoped reads, session review. |
| `camera_api.py` | Camera helper functions shared by API + panel; login-password endpoint; revocation; hide `__test__`. |
| `fire_data.py`, `map.py`, `routes.py`, `templates/index.html` | Viewer-scoped map, avoidance and dashboard. |
| `notifications.py` | `[TEST]` prefix. |
| `manage.py` | `set-camera-password`. |
| `tests/support.py` (new) | Shared `ServerTestCase` base. |
| `tests/test_auth_admin.py` (new) | All new tests. |

---

### Task 1: Schema, password hashing, sessions and `Viewer`

**Files:**
- Modify: `database.py` (init_db tables + migrations, constants)
- Create: `auth.py` (core only, no routes yet)
- Create: `tests/support.py`, `tests/test_auth_admin.py`
- Modify: `tests/test_server.py:41` (`tempfile.TemporaryDirectory(ignore_cleanup_errors=True)`)

**Interfaces:**
- Produces (`database.py`): `PUBLIC_STATUSES`, `TEST_CAMERA_ID = "__test__"`; columns `cameras.login_password_hash TEXT`, `device_events.is_test INTEGER NOT NULL DEFAULT 0`; tables `sessions(token_hash TEXT PRIMARY KEY, is_admin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)` and `session_cameras(token_hash TEXT NOT NULL REFERENCES sessions(token_hash) ON DELETE CASCADE, camera_id TEXT NOT NULL REFERENCES cameras(camera_id) ON DELETE CASCADE, PRIMARY KEY(token_hash, camera_id))`.
- Produces (`auth.py`):
  - `hash_password(password: str) -> str`, `verify_password(password: str, stored: str | None) -> bool`, `generate_password() -> str`
  - `@dataclass(frozen=True) class Viewer: is_admin: bool = False; camera_ids: frozenset = frozenset()` with `can_see_camera(camera_id: str) -> bool` (admin or member), property `sees_tests -> bool` (= `is_admin`), property `is_anonymous -> bool`
  - `ANONYMOUS = Viewer()`
  - `create_session(conn, *, is_admin: bool = False, camera_ids=()) -> str` (returns raw token; also purges expired sessions)
  - `add_camera_to_session(conn, token: str, camera_id: str) -> None`
  - `load_viewer(conn, token: str | None) -> Viewer` (expired/unknown → `ANONYMOUS`; only cameras with `enabled=1` and `camera_id != TEST_CAMERA_ID` count)
  - `delete_session(conn, token: str) -> None`, `revoke_camera_sessions(conn, camera_id: str) -> None`
  - `get_viewer(request: Request) -> Viewer` — FastAPI dependency: valid admin Bearer header (`camera_api.is_admin`) → `Viewer(is_admin=True)`; else cookie `fw_session` via `load_viewer`.
- Produces (`tests/support.py`): `class ServerTestCase(unittest.TestCase)` with:
  - `setUp`: temp dir (`ignore_cleanup_errors=True`); env exactly as `tests/test_server.py` setUp; pops modules `database, fire_data, notifications, camera_api, auth, device_api, test_fires, admin_panel, app, routes, map` then imports `map`; `self.client = self.new_client()`; Twilio fake recording kwargs in `self.sent`; creates cameras `CAM-A` "Alpha Ridge" (41.1, -73.2) and `CAM-B` "Bravo Hill" (42.0, -74.0) via `POST /api/admin/cameras` with `ADMIN` header; `self.keys = {id: camera_key}`; adds subscriber `'*'` phone `+15555550123` via `POST /api/admin/subscribers` so alerts are recorded in `self.sent`.
  - `new_client() -> TestClient` with `base_url="https://testserver"` (so `Secure` cookies are sent).
  - `db()` → `sys.modules["database"].get_connection()`.
  - Later tasks add helpers here (named in their Interfaces).

- [ ] **Step 1: Write failing tests** in `tests/test_auth_admin.py`, class `AuthCoreTests(ServerTestCase)`:

```python
def test_password_hash_roundtrip(self):
    auth = sys.modules["auth"]
    stored = auth.hash_password("pw-123")
    self.assertTrue(stored.startswith("scrypt$"))
    self.assertTrue(auth.verify_password("pw-123", stored))
    self.assertFalse(auth.verify_password("pw-124", stored))
    self.assertFalse(auth.verify_password("pw-123", None))
    self.assertNotEqual(stored, auth.hash_password("pw-123"))  # salted

def test_session_viewer_and_revocation(self):
    auth = sys.modules["auth"]
    with self.db() as conn:
        token = auth.create_session(conn, camera_ids=["CAM-A"])
        auth.add_camera_to_session(conn, token, "CAM-B")
        viewer = auth.load_viewer(conn, token)
        self.assertEqual(viewer.camera_ids, frozenset({"CAM-A", "CAM-B"}))
        self.assertTrue(viewer.can_see_camera("CAM-B") and not viewer.sees_tests)
        auth.revoke_camera_sessions(conn, "CAM-A")
        self.assertEqual(auth.load_viewer(conn, token).camera_ids, frozenset({"CAM-B"}))
        row = conn.execute("SELECT * FROM sessions").fetchone()
        self.assertNotEqual(row["token_hash"], token)  # only the hash is stored

def test_disabled_camera_drops_out_of_session(self):  # Review Focus 2
    auth = sys.modules["auth"]
    with self.db() as conn:
        token = auth.create_session(conn, camera_ids=["CAM-A", "CAM-B"])
        conn.execute("UPDATE cameras SET enabled=0 WHERE camera_id='CAM-B'")
        self.assertEqual(auth.load_viewer(conn, token).camera_ids, frozenset({"CAM-A"}))

def test_expired_and_unknown_sessions_are_anonymous(self):
    auth = sys.modules["auth"]
    with self.db() as conn:
        token = auth.create_session(conn, camera_ids=["CAM-A"])
        conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
        self.assertTrue(auth.load_viewer(conn, token).is_anonymous)
        self.assertTrue(auth.load_viewer(conn, "nope").is_anonymous)
        self.assertTrue(auth.load_viewer(conn, None).is_anonymous)
```

- [ ] **Step 2: Run** `python -m pytest tests/test_auth_admin.py -q` — Expected: FAIL (`auth` module missing).
- [ ] **Step 3: Implement** the `database.py` changes and `auth.py` core per Interfaces (no routes).
- [ ] **Step 4: Run** `python -m pytest -q` — Expected: all pass, including the previously Windows-failing `tests/test_server.py`.
- [ ] **Step 5: Commit** `git commit -m "Add sessions, password hashing and Viewer"`.

---

### Task 2: Login, logout and admin login pages

**Files:**
- Modify: `auth.py` (add `router`), `app.py` (include `auth.router`)
- Create: `templates/login.html`
- Test: `tests/test_auth_admin.py`, `tests/support.py`

**Interfaces:**
- Consumes: Task 1 `auth` functions.
- Produces routes: `GET /login`, `POST /login` (form `camera_id`, `password`), `POST /logout`, `GET /admin/login`, `POST /admin/login` (form `password`). Successful camera login → 303 to `/`; admin login → 303 to `/admin`; failure → 401 with the page re-rendered and message `Camera ID or password is wrong.` (admin: `Wrong admin password.`; admin disabled: page says `Admin login is not configured.` and POST returns 403). Already-signed-in camera users posting `/login` get the camera added to their session. `/logout` deletes session + clears cookie → 303 to `/`.
- Produces (`tests/support.py`): `set_login_password(camera_id) -> str` (direct DB: `auth.hash_password(auth.generate_password())`, returns the plain password); `login_camera(client, camera_id, password) -> Response`; `login_admin(client) -> Response` (password `test-admin-token`).
- Login template: `login.html` used for both forms via context `mode in {"camera", "admin"}`, `error`, `viewer`.

- [ ] **Step 1: Write failing tests** class `LoginTests(ServerTestCase)`:

```python
def test_camera_login_sets_secure_cookie_and_logout(self):
    pw = self.set_login_password("CAM-A")
    r = self.login_camera(self.client, "CAM-A", pw)
    self.assertEqual(r.status_code, 303)
    cookie = r.headers["set-cookie"]
    for part in ("fw_session=", "HttpOnly", "Secure", "SameSite=lax"):
        self.assertIn(part.lower(), cookie.lower())
    self.assertEqual(self.client.post("/logout", follow_redirects=False).status_code, 303)
    with self.db() as conn:
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

def test_wrong_id_and_wrong_password_look_identical(self):
    self.set_login_password("CAM-A")
    a = self.login_camera(self.new_client(), "CAM-A", "wrong")
    b = self.login_camera(self.new_client(), "CAM-ZZZ", "wrong")
    self.assertEqual((a.status_code, b.status_code), (401, 401))
    self.assertIn("Camera ID or password is wrong.", a.text)
    self.assertIn("Camera ID or password is wrong.", b.text)

def test_no_password_or_disabled_camera_cannot_login(self):
    self.assertEqual(self.login_camera(self.client, "CAM-B", "").status_code, 401)  # never set
    pw = self.set_login_password("CAM-B")
    with self.db() as conn:
        conn.execute("UPDATE cameras SET enabled=0 WHERE camera_id='CAM-B'")
    self.assertEqual(self.login_camera(self.client, "CAM-B", pw).status_code, 401)

def test_login_trims_camera_id(self):  # Review Focus 1
    pw = self.set_login_password("CAM-A")
    self.assertEqual(self.login_camera(self.client, "  CAM-A ", pw).status_code, 303)
    self.assertEqual(self.login_camera(self.new_client(), "cam-a", pw).status_code, 401)

def test_two_cameras_in_one_session(self):
    pa, pb = self.set_login_password("CAM-A"), self.set_login_password("CAM-B")
    self.login_camera(self.client, "CAM-A", pa)
    self.login_camera(self.client, "CAM-B", pb)
    with self.db() as conn:
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM session_cameras").fetchone()[0], 2)

def test_admin_login(self):
    self.assertEqual(self.client.post("/admin/login", data={"password": "nope"}).status_code, 401)
    r = self.login_admin(self.client)
    self.assertEqual((r.status_code, r.headers["location"]), (303, "/admin"))

def test_admin_login_disabled_without_token(self):  # Review Focus 3
    os.environ["FIREWATCH_ADMIN_TOKEN"] = ""
    for name in ["camera_api", "auth", "device_api", "test_fires", "admin_panel", "app", "routes", "map"]:
        sys.modules.pop(name, None)
    client = TestClient(importlib.import_module("map").app, base_url="https://testserver")
    self.assertEqual(client.post("/admin/login", data={"password": ""}, follow_redirects=False).status_code, 403)

def test_login_rate_limited(self):
    codes = [self.login_camera(self.client, "CAM-A", "x").status_code for _ in range(11)]
    self.assertEqual(codes[-1], 429)
```

- [ ] **Step 2: Run** `python -m pytest tests/test_auth_admin.py -q -k Login` — Expected: FAIL (404s).
- [ ] **Step 3: Implement** the routes and `login.html` (decorate POSTs with `@limiter.limit("10/minute")` from `app`; the endpoint signature must include `request: Request`). Strip `camera_id`; reject `TEST_CAMERA_ID`.
- [ ] **Step 4: Run** `python -m pytest -q` — Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Add camera and admin login pages"`.

---

### Task 3: `create_event()` extraction, test-event isolation and `[TEST]` alerts

**Files:**
- Modify: `device_api.py:180-260` (`camera_event`, `_find_open_event`, `_find_dismissed_match`), `notifications.py` (`_alert_text`, email subject)
- Test: `tests/test_auth_admin.py`, `tests/support.py`

**Interfaces:**
- Produces (`device_api.py`):
  - `new_event_id() -> str` (`uuid.uuid4().hex[:12]`)
  - `create_event(conn, event_id: str, camera: dict, *, fire_size_percent: float, max_temperature_c: float, duration_seconds: float, image_path: str = "", video_path: str = "", context_paths: list[str] = (), visual_fire_score: float = 0.0, detection_method: str = "thermal", hotspot_x: float = -1.0, hotspot_y: float = -1.0, first_seen: datetime | None = None, location: tuple[float, float] | None = None, status: str = "unconfirmed", severity: tuple[float, str] | None = None, is_test: bool = False, now: datetime | None = None) -> tuple[dict, str | None]` — inserts the row, first observation and (only when `status == "unconfirmed"`) a confirmation token; returns `(row, raw_token_or_None)`. `location` overrides camera coordinates; `severity` overrides `_severity()`. `row["is_test"]` is `1`/`0`.
  - `notify_new_event(row: dict, token: str) -> dict` (wraps `send_fire_confirmation({**row, "confirm_token": token})` in the existing try/except).
  - `_find_open_event(conn, camera_id, now, is_test=False)` and `_find_dismissed_match(..., is_test=False)` add `AND is_test=?`.
- `notifications`: when `event.get("is_test")` is truthy, SMS body and email subject start with `[TEST] `.
- Produces (`tests/support.py`): `upload_event(camera_id, temp=150.0, hotspot=(10.0, 8.0)) -> dict` (posts `/api/camera-event` like `test_server.py.send_event`, returns JSON); `token_from_last_sms() -> str` (regex `token=([\w-]+)` on `self.sent[-1]["body"]`); `make_event(camera_id="CAM-A", *, status="unconfirmed", is_test=False, location=None) -> tuple[dict, str | None]` (calls `create_event` directly with `fire_size_percent=2.0, max_temperature_c=300.0, duration_seconds=5`; bypasses dedupe).

- [ ] **Step 1: Write failing tests** class `TestEventIsolationTests(ServerTestCase)`:

```python
def test_real_upload_does_not_merge_into_open_test_event(self):
    test_row, _ = self.make_event(is_test=True)
    real = self.upload_event("CAM-A")
    self.assertNotIn("deduplicated", real)
    self.assertNotEqual(real["event_id"], test_row["event_id"])

def test_test_event_does_not_merge_into_open_real_event(self):
    real = self.upload_event("CAM-A")
    test_row, _ = self.make_event(is_test=True)
    self.assertNotEqual(real["event_id"], test_row["event_id"])

def test_test_alert_is_prefixed(self):
    row, token = self.make_event(is_test=True)
    sys.modules["device_api"].notify_new_event(row, token)
    self.assertTrue(self.sent[-1]["body"].startswith("[TEST] "))

def test_upload_still_works_through_create_event(self):
    first = self.upload_event("CAM-A")
    self.assertEqual(first["status"], "unconfirmed")
    self.assertTrue(self.upload_event("CAM-A")["deduplicated"])
```

- [ ] **Step 2: Run** `python -m pytest tests/test_auth_admin.py -q -k Isolation` — Expected: FAIL.
- [ ] **Step 3: Implement** the extraction (behaviour of `/api/camera-event` unchanged otherwise) and the notification prefix.
- [ ] **Step 4: Run** `python -m pytest -q` — Expected: all pass (existing `tests/test_server.py` proves no regression).
- [ ] **Step 5: Commit** `git commit -m "Extract create_event and isolate test events"`.

---

### Task 4: Viewer-scoped read APIs

**Files:**
- Modify: `device_api.py` (`device_events`, `camera_status`), `camera_api.py` (`list_cameras` excludes `TEST_CAMERA_ID`)
- Test: `tests/test_auth_admin.py`

**Interfaces:**
- Consumes: `auth.get_viewer`, `Viewer`.
- `GET /api/device-events` (`viewer: Viewer = Depends(get_viewer)`, keeps `status`, `limit`): admin → all rows incl. tests, response events include `is_test`; others → `is_test=0 AND (status IN PUBLIC_STATUSES OR camera_id IN viewer.camera_ids)`; `PRIVATE_FIELDS` stripped for non-admins as today. `counts.pending_review` = unconfirmed, `is_test=0`, among cameras the viewer can see (anonymous → 0). `counts.confirmed` = public real events.
- `GET /api/cameras/status`: admin → all except `__test__`; camera user → own; anonymous → `{"cameras": []}`.

- [ ] **Step 1: Write failing tests** class `VisibilityApiTests(ServerTestCase)`. In `setUp` (after `super().setUp()`): log in `self.cam_a` client to CAM-A, `self.admin` client as admin, `self.anon = self.new_client()`; create events and store their IDs: `self.a_unconfirmed` (upload CAM-A), `self.b_unconfirmed` (upload CAM-B), `self.b_confirmed` (`make_event("CAM-B", status="confirmed")`), `self.test_confirmed` (`make_event("CAM-A", status="confirmed", is_test=True, location=(45.0, -70.0))`), `self.test_unconfirmed` (`make_event("CAM-A", is_test=True)`). Put this in `tests/support.py` as `make_visibility_fixture()` (Task 5 reuses it).

```python
def ids(self, client):
    return {e["event_id"] for e in client.get("/api/device-events").json()["events"]}

def test_event_visibility_matrix(self):
    self.assertEqual(self.ids(self.anon), {self.b_confirmed})
    self.assertEqual(self.ids(self.cam_a), {self.b_confirmed, self.a_unconfirmed})
    self.assertEqual(self.ids(self.admin), {self.b_confirmed, self.a_unconfirmed, self.b_unconfirmed,
                                            self.test_confirmed, self.test_unconfirmed})

def test_pending_counts_are_scoped(self):
    count = lambda c: c.get("/api/device-events").json()["counts"]["pending_review"]
    self.assertEqual((count(self.anon), count(self.cam_a), count(self.admin)), (0, 1, 2))

def test_camera_status_is_scoped(self):
    names = lambda c: [x["camera_id"] for x in c.get("/api/cameras/status").json()["cameras"]]
    self.assertEqual(names(self.anon), [])
    self.assertEqual(names(self.cam_a), ["CAM-A"])
    self.assertEqual(names(self.admin), ["CAM-A", "CAM-B"])  # never "__test__"

def test_bearer_admin_still_works(self):
    r = self.anon.get("/api/device-events", headers=ADMIN)
    self.assertEqual(len(r.json()["events"]), 5)
```

- [ ] **Step 2: Run** `python -m pytest tests/test_auth_admin.py -q -k VisibilityApi` — Expected: FAIL.
- [ ] **Step 3: Implement.** (The fixture's test events use CAM-A, so the hidden `__test__` camera from Task 8 isn't needed yet.)
- [ ] **Step 4: Run** `python -m pytest -q` — Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Scope event and camera APIs to the signed-in viewer"`.

---

### Task 5: Viewer-scoped map, route avoidance and dashboard

**Files:**
- Modify: `fire_data.py`, `map.py` (`_camera_rows`, `build_map_figure`, `/api/map-figure`, `/`), `routes.py` (`call_ors_route`, `calculate_routes`), `templates/index.html`
- Test: `tests/test_auth_admin.py`

**Interfaces:**
- Consumes: `Viewer`, `ANONYMOUS`, `get_viewer`, `PUBLIC_STATUSES`.
- Produces (`fire_data.py`): `_load_device_fires()` also returns `is_test`; `fires_for(viewer: Viewer) -> pandas.DataFrame` (non-device rows always; device rows where `is_test==0 and (status in PUBLIC_STATUSES or viewer.can_see_camera(camera_id))`, or `is_test==1 and viewer.sees_tests`); `get_avoid_geojson(viewer: Viewer = ANONYMOUS) -> dict | None` (reported + satellite + confirmed real; plus confirmed test fires when `viewer.sees_tests`); avoidance cache becomes a dict keyed by `viewer.sees_tests`, cleared by `invalidate_cache()`.
- Produces (`routes.py`): `call_ors_route(start, end, profile, avoid=None)`; `calculate_routes` takes `viewer: Viewer = Depends(get_viewer)` and computes `avoid = get_avoid_geojson(viewer)` once.
- Produces (`map.py`): `_camera_rows(viewer)`, `build_map_figure(viewer) -> (fig, cameras, data)`; device events grouped by `FIRE_STYLE` status (non-public statuses only appear when visible); test events in one extra trace named `"TEST fires"` colour `#7c3aed`, hover starting `TEST ·`. Both routes take the viewer dependency; `/` passes `viewer` to the template.
- `index.html`: header shows `Sign in` (→ `/login`) when anonymous; otherwise the signed-in camera IDs, a POST `Sign out` button, and `Admin` (→ `/admin`) for admins. "Camera network" section and "Awaiting owner review" metric render only when `not viewer.is_anonymous`. In the events list, an unconfirmed event gets a `Review` link to `/review/{event_id}`.

- [ ] **Step 1: Write failing tests** class `VisibilityMapTests(ServerTestCase)` calling `make_visibility_fixture()` in `setUp`:

```python
def test_map_figure_hides_other_cameras(self):
    anon = self.anon.get("/api/map-figure").text
    self.assertNotIn("Alpha Ridge", anon)
    self.assertNotIn("TEST fires", anon)
    mine = self.cam_a.get("/api/map-figure").text
    self.assertIn("Alpha Ridge", mine)
    self.assertNotIn("TEST fires", mine)
    self.assertIn("TEST fires", self.admin.get("/api/map-figure").text)

def test_avoidance_includes_test_fires_only_for_admin(self):
    fd = sys.modules["fire_data"]
    auth = sys.modules["auth"]
    public = fd.get_avoid_geojson(auth.ANONYMOUS)
    admin = fd.get_avoid_geojson(auth.Viewer(is_admin=True))
    self.assertNotEqual(public, admin)  # admin also avoids the confirmed test fire

def test_dashboard_header(self):
    self.assertIn('href="/login"', self.anon.get("/").text)
    self.assertNotIn("Camera network", self.anon.get("/").text)
    page = self.cam_a.get("/").text
    self.assertIn("Sign out", page)
    self.assertIn("Camera network", page)
    self.assertIn('href="/admin"', self.admin.get("/").text)
```

  (The fixture's confirmed test fire sits at (45.0, -70.0), far from the real confirmed fire, so the polygons differ.)
- [ ] **Step 2: Run** `python -m pytest tests/test_auth_admin.py -q -k VisibilityMap` — Expected: FAIL.
- [ ] **Step 3: Implement** the changes above.
- [ ] **Step 4: Run** `python -m pytest -q` — Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Scope map, routing avoidance and dashboard to the viewer"`.

---

### Task 6: Review via signed-in session

**Files:**
- Modify: `device_api.py` (`review_page`, `submit_review`), `templates/review.html` (form keeps working with empty token)
- Test: `tests/test_auth_admin.py`

**Interfaces:**
- Consumes: `get_viewer`, `Viewer.can_see_camera`, `Viewer.sees_tests`.
- Rule: a viewer has *session access* to an event when `viewer.can_see_camera(row["camera_id"])` and (`row["is_test"] == 0` or `viewer.sees_tests`). With session access and no valid token, the page state is `"ok"` if `status == "unconfirmed"` else `"used"`. `submit_review` accepts `token: str = Form("")`; with a valid `"ok"` token it behaves as today; otherwise with session access and `status == "unconfirmed"` it calls `_apply_review(..., reviewer="admin" if viewer.is_admin else f"login:{camera_id}")`; otherwise 403. Redirect: `/review/{id}?token={token}` if a token was given, else `/review/{id}`.

- [ ] **Step 1: Write failing tests** class `SessionReviewTests(ServerTestCase)`:

```python
def test_owner_can_review_without_token(self):
    event_id = self.upload_event("CAM-A")["event_id"]
    client = self.new_client(); self.login_camera(client, "CAM-A", self.set_login_password("CAM-A"))
    self.assertIn('name="decision"', client.get(f"/review/{event_id}").text)
    client.post(f"/review/{event_id}", data={"decision": "fire"})
    with self.db() as conn:
        row = conn.execute("SELECT status, reviewed_by FROM device_events WHERE event_id=?", (event_id,)).fetchone()
    self.assertEqual(tuple(row), ("confirmed", "login:CAM-A"))

def test_other_camera_cannot_review(self):
    event_id = self.upload_event("CAM-B")["event_id"]
    client = self.new_client(); self.login_camera(client, "CAM-A", self.set_login_password("CAM-A"))
    self.assertEqual(client.get(f"/review/{event_id}").status_code, 403)
    self.assertEqual(client.post(f"/review/{event_id}", data={"decision": "fire"}).status_code, 403)

def test_camera_user_cannot_open_test_event(self):
    row, _ = self.make_event("CAM-A", is_test=True)
    client = self.new_client(); self.login_camera(client, "CAM-A", self.set_login_password("CAM-A"))
    self.assertEqual(client.get(f"/review/{row['event_id']}").status_code, 403)

def test_token_link_still_works_anonymously(self):
    event_id = self.upload_event("CAM-A")["event_id"]
    token = self.token_from_last_sms()
    self.new_client().post(f"/review/{event_id}", data={"token": token, "decision": "not_fire", "label": "vehicle"})
    with self.db() as conn:
        self.assertEqual(conn.execute("SELECT status FROM device_events WHERE event_id=?", (event_id,)).fetchone()[0], "false_alarm")
```

- [ ] **Step 2: Run** `python -m pytest tests/test_auth_admin.py -q -k SessionReview` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python -m pytest -q` — Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Let signed-in camera users and admins review from the dashboard"`.

---

### Task 7: Camera management (API, CLI, admin panel cameras section)

**Files:**
- Modify: `camera_api.py`, `manage.py`
- Create: `admin_panel.py`, `templates/admin.html`, `templates/admin_secret.html`; include router in `app.py`
- Test: `tests/test_auth_admin.py`

**Interfaces:**
- Produces (`camera_api.py`, all take an open `conn`):
  - `create_camera(conn, camera_id: str, latitude: float, longitude: float, name: str = "") -> tuple[str, str]` → `(pi_key, login_password)`
  - `reset_login_password(conn, camera_id: str) -> str` (sets hash, revokes sessions)
  - `reset_pi_key(conn, camera_id: str) -> str`
  - `set_camera_enabled(conn, camera_id: str, enabled: bool) -> None` (disable revokes sessions)
  - each raises `KeyError` for unknown or `TEST_CAMERA_ID`.
  - `POST /api/admin/cameras` response adds `"login_password"`. New `POST /api/admin/cameras/{camera_id}/login-password` → `{"ok": true, "camera_id", "login_password"}`. `DELETE`/`PUT enabled=false` revoke sessions.
- Produces (`manage.py`): `python manage.py set-camera-password CAM-ID` prints `Login password for CAM-ID: <pw>`.
- Produces (`admin_panel.py`): `router`; dependency `require_admin_page(viewer)` → raises redirect to `/admin/login` (303) for non-admins (use an `HTTPException`-free approach: return `RedirectResponse` from each handler via a small helper `_guard(viewer) -> RedirectResponse | None`). Routes: `GET /admin`; `POST /admin/cameras` (form `camera_id, name, latitude, longitude`) → `admin_secret.html` showing Pi key + login password; `POST /admin/cameras/{id}/login-password` → secret page; `POST /admin/cameras/{id}/pi-key` → secret page; `POST /admin/cameras/{id}/enabled` (form `enabled` = `"1"`/`"0"`) → 303 `/admin`. Validation errors re-render `/admin` with status 400 and a message.
- `admin.html` context: `cameras` (list of dicts with `has_login`), `test_fires` (empty list until Task 8), `message`.

- [ ] **Step 1: Write failing tests** class `AdminCameraTests(ServerTestCase)`:

```python
def test_admin_pages_require_admin(self):
    r = self.new_client().get("/admin", follow_redirects=False)
    self.assertEqual((r.status_code, r.headers["location"]), (303, "/admin/login"))
    cam = self.new_client(); self.login_camera(cam, "CAM-A", self.set_login_password("CAM-A"))
    self.assertEqual(cam.get("/admin", follow_redirects=False).status_code, 303)
    self.assertEqual(self.new_client().post("/admin/cameras/CAM-A/login-password", follow_redirects=False).status_code, 303)

def test_add_camera_shows_both_secrets_once(self):
    admin = self.new_client(); self.login_admin(admin)
    page = admin.post("/admin/cameras", data={"camera_id": "CAM-C", "name": "Cedar", "latitude": "40", "longitude": "-75"}).text
    with self.db() as conn:
        row = conn.execute("SELECT camera_key, login_password_hash FROM cameras WHERE camera_id='CAM-C'").fetchone()
    pw = re.search(r'data-secret="login">([^<]+)<', page).group(1)
    self.assertTrue(sys.modules["auth"].verify_password(pw, row["login_password_hash"]))
    self.assertNotIn(pw, admin.get("/admin").text)

def test_reset_password_signs_camera_out(self):
    old = self.set_login_password("CAM-A")
    user = self.new_client(); self.login_camera(user, "CAM-A", old)
    admin = self.new_client(); self.login_admin(admin)
    admin.post("/admin/cameras/CAM-A/login-password")
    self.assertEqual(user.get("/api/cameras/status").json()["cameras"], [])
    self.assertEqual(self.login_camera(self.new_client(), "CAM-A", old).status_code, 401)

def test_api_login_password_endpoint(self):
    r = self.client.post("/api/admin/cameras/CAM-B/login-password", headers=ADMIN)
    self.assertEqual(self.login_camera(self.new_client(), "CAM-B", r.json()["login_password"]).status_code, 303)
```

  The secret page marks secrets as `<code data-secret="login">…</code>` and `<code data-secret="pi_key">…</code>`.
- [ ] **Step 2: Run** `python -m pytest tests/test_auth_admin.py -q -k AdminCamera` — Expected: FAIL.
- [ ] **Step 3: Implement** helpers, endpoints, CLI command, panel cameras section and secret page.
- [ ] **Step 4: Run** `python -m pytest -q` — Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Add admin panel camera management and login passwords"`.

---

### Task 8: Test fires (simulate, drop, list, delete)

**Files:**
- Create: `test_fires.py`
- Modify: `admin_panel.py`, `templates/admin.html` (Leaflet 1.9.4 map for click-to-place from `https://unpkg.com/leaflet@1.9.4/dist/leaflet.js` + `.css`), `requirements.txt` (`numpy`, `Pillow`)
- Test: `tests/test_auth_admin.py`

**Interfaces:**
- Consumes: `create_event`, `new_event_id`, `notify_new_event` (Task 3); `thermal.analyze(temps, 80.0, 25.0, 6)`, `thermal.render(temps, stats, title=...)`; `IMAGE_DIR`; `TEST_CAMERA_ID`.
- Produces (`test_fires.py`):
  - `ensure_test_camera(conn) -> dict` (creates `__test__`: `enabled=0`, random hashed key, lat/lon 0, name `Test fires`)
  - `synthetic_frame(max_temperature_c: float, hot_pixels: int, ambient_c: float = 20.0) -> numpy.ndarray` shape (24, 32); exactly `hot_pixels` connected pixels ≥ `max_temperature_c - 20`, the hottest equal to `max_temperature_c`; rest near ambient with ±0.5 noise.
  - `simulate_detection(camera_id: str, max_temperature_c: float = 350.0, hot_pixels: int = 12, send_alerts: bool = True) -> dict` → `{"event_id", "token", "notifications"}`; saves `images/<event_id>.jpg`; `detection_method="admin-simulation"`, `is_test=True`; raises `ValueError` (message shown on the panel) for unknown/disabled/`__test__` camera or out-of-range inputs.
  - `drop_test_fire(latitude: float, longitude: float, severity: str) -> str` → event_id; confirmed, `is_test=True`, camera `__test__`, `location=(lat, lon)`, `severity=(SEVERITY_INDEX[severity], severity)`; `ValueError` on bad coordinates or severity.
  - `list_test_fires() -> list[dict]` newest first; `delete_test_fires(event_ids: list[str] | None = None) -> int` (None = all; removes the event's `image`, `video` and `context_images` files under `IMAGE_DIR`, ignoring missing files; returns rows deleted).
- Produces (`admin_panel.py`): `POST /admin/test-fires/simulate` (form `camera_id, max_temperature_c, hot_pixels, send_alerts`) → admin page with message and an `Open review page` link `/review/{id}?token=...`; `POST /admin/test-fires/drop` (form `latitude, longitude, severity`); `POST /admin/test-fires/{event_id}/delete`; `POST /admin/test-fires/delete-all`. `ValueError` → status 400 with the message.

- [ ] **Step 1: Write failing tests** class `TestFireTests(ServerTestCase)` (admin client logged in in `setUp`):

```python
def test_synthetic_frame_matches_requested_size(self):
    tf, thermal = sys.modules["test_fires"], importlib.import_module("thermal")
    stats = thermal.analyze(tf.synthetic_frame(350.0, 12), 80.0, 25.0, 6)
    self.assertEqual(stats.cluster, 12)
    self.assertAlmostEqual(stats.max_temp, 350.0, delta=0.01)

def test_simulate_runs_full_pipeline_privately(self):
    page = self.admin.post("/admin/test-fires/simulate",
                           data={"camera_id": "CAM-A", "max_temperature_c": "350", "hot_pixels": "12", "send_alerts": "1"})
    self.assertIn("Open review page", page.text)
    self.assertTrue(self.sent[-1]["body"].startswith("[TEST] "))
    event = self.admin.get("/api/device-events").json()["events"][0]
    self.assertEqual((event["is_test"], event["status"]), (1, "unconfirmed"))
    self.assertTrue((Path(os.environ["FIREWATCH_IMAGE_DIR"]) / f"{event['event_id']}.jpg").is_file())
    self.assertEqual(self.new_client().get("/api/device-events").json()["events"], [])

def test_simulate_without_alerts_sends_nothing(self):
    before = len(self.sent)
    self.admin.post("/admin/test-fires/simulate", data={"camera_id": "CAM-A", "max_temperature_c": "350", "hot_pixels": "12"})
    self.assertEqual(len(self.sent), before)

def test_simulate_rejects_bad_input(self):  # Review Focus 4
    for data in ({"hot_pixels": "0"}, {"hot_pixels": "999"}, {"max_temperature_c": "20"}, {"camera_id": "__test__"}):
        form = {"camera_id": "CAM-A", "max_temperature_c": "350", "hot_pixels": "12", **data}
        self.assertEqual(self.admin.post("/admin/test-fires/simulate", data=form).status_code, 400)
    self.assertEqual(sys.modules["test_fires"].list_test_fires(), [])

def test_drop_fire_is_confirmed_admin_only(self):
    self.admin.post("/admin/test-fires/drop", data={"latitude": "45.0", "longitude": "-70.0", "severity": "High"})
    [fire] = sys.modules["test_fires"].list_test_fires()
    self.assertEqual((fire["status"], fire["camera_id"], fire["magnitude"]), ("confirmed", "__test__", "High"))
    self.assertNotIn("TEST fires", self.new_client().get("/api/map-figure").text)
    self.assertNotIn("__test__", self.admin.get("/admin").text.split("Test fires")[0])  # not in the camera table

def test_delete_all_tolerates_missing_files(self):  # Review Focus 5
    tf = sys.modules["test_fires"]
    event_id = tf.simulate_detection("CAM-A", send_alerts=False)["event_id"]
    tf.drop_test_fire(45.0, -70.0, "Low")
    (Path(os.environ["FIREWATCH_IMAGE_DIR"]) / f"{event_id}.jpg").unlink()
    self.admin.post("/admin/test-fires/delete-all")
    self.assertEqual(tf.list_test_fires(), [])

def test_delete_one_removes_its_image(self):
    tf = sys.modules["test_fires"]
    event_id = tf.simulate_detection("CAM-A", send_alerts=False)["event_id"]
    self.admin.post(f"/admin/test-fires/{event_id}/delete")
    self.assertFalse((Path(os.environ["FIREWATCH_IMAGE_DIR"]) / f"{event_id}.jpg").exists())
```

- [ ] **Step 2: Run** `python -m pytest tests/test_auth_admin.py -q -k TestFire` — Expected: FAIL.
- [ ] **Step 3: Implement** `test_fires.py`, the panel routes and the test-fire section of `admin.html` (simulate form, drop form with Leaflet map whose click fills lat/lon, list with delete buttons, delete-all button). Add `numpy` and `Pillow` to `requirements.txt`.
- [ ] **Step 4: Run** `python -m pytest -q` — Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Add admin test fires: simulate detection, drop on map, delete"`.

---

### Task 9: Docs and end-to-end check

**Files:**
- Modify: `README_DISTRIBUTED.md` (new "Logins and admin panel" section: setting camera passwords via panel/`manage.py set-camera-password`, what each viewer sees, test fires), `README_RENDER.md` (rollout note: cameras need logins after deploy)

- [ ] **Step 1:** Write the README sections (content per spec "Visibility", "Admin control panel", "Rollout").
- [ ] **Step 2:** Run `python -m pytest -q` — Expected: all pass, 0 failures.
- [ ] **Step 3:** Browser check against a local server (throwaway DB, `FIREWATCH_ADMIN_TOKEN` set): visitor sees no camera markers/list; admin login → add camera → copy login password → camera login in a second tab shows only that camera; simulate detection → open review page → answer Fire → fire appears for admin only (TEST); drop fire by clicking the map; delete all test fires. Check phone width (375 px) for `/login` and `/admin`, and no console errors.
- [ ] **Step 4: Commit** `git commit -m "Document camera logins and admin panel"`.
