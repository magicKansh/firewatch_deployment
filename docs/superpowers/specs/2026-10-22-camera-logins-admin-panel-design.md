# Camera logins and admin control panel: design

Date: 2026-10-22 · Status: approved in chat, awaiting spec review · Base: `main` @ 372f463

## Goal

1. Each Raspberry Pi camera gets its own **login** for the people who look after it. Camera
   locations, status and unreviewed detections are visible only to people signed in to that camera
   (and to the admin), not to every visitor.
2. An **admin control panel** where the admin manages camera logins and creates **fake fires** to
   test the alert pipeline and the map/routing, without the public ever seeing them.

## Decisions made with the user

| Question | Decision |
|---|---|
| What stays public? | Confirmed real fires (map, recent list, route avoidance). Nothing camera-specific. |
| Login model | Per-camera login: camera ID + password. One browser session can hold several cameras. |
| Session mechanism | Server-side sessions in SQLite (revocable), passwords hashed with stdlib `scrypt`. |
| Fake fires | (a) simulate a camera detection through the full alert pipeline; (b) drop a confirmed fire on the map. |
| Test fire visibility | Admin only, labelled TEST. Never public, never shown to camera users. |

## Visibility

| | Visitor | Signed in to camera(s) | Admin |
|---|---|---|---|
| Confirmed real fires: map, recent list, route avoidance | yes | yes | yes |
| Camera markers, status, camera list | no | own cameras | all |
| Unconfirmed and false-alarm events, pending-review count | no | own cameras | all |
| Review from the dashboard (no SMS token needed) | no | own cameras | all |
| Test fires (labelled TEST), incl. in route avoidance | no | no | yes |

`PUBLIC_STATUSES` (`confirmed`, `active`, `resolved`) keeps its meaning for real events.

## Authentication

**Viewer.** A single dependency `get_viewer(request) -> Viewer(is_admin: bool, camera_ids: frozenset)`
reads the session cookie. No/expired/unknown cookie means an anonymous viewer. The existing admin
Bearer token (`Authorization: Bearer $FIREWATCH_ADMIN_TOKEN`) also yields an admin viewer, so the
current admin API and tests keep working. Helper methods: `viewer.can_see_camera(id)`,
`viewer.sees_tests`.

**Camera login.** `GET/POST /login` with camera ID + password. Verified against
`cameras.login_password_hash` (scrypt, per-password random salt, constant-time compare). Disabled
cameras and cameras without a password cannot sign in. Wrong ID and wrong password give the same
message. When already signed in, the page offers "Add another camera" and adds the camera to the
existing session. `POST /logout` deletes the session.

**Admin login.** `GET/POST /admin/login`; the password is `FIREWATCH_ADMIN_TOKEN`
(constant-time compare). If the token is not configured, admin login is disabled.

**Sessions.** Tables `sessions(token_hash PK, is_admin, created_at, expires_at)` and
`session_cameras(token_hash, camera_id, PK both, FK cascade)`. Cookie `fw_session`: random 32-byte
token, `HttpOnly`, `SameSite=Lax`, `Secure` when `PUBLIC_BASE_URL` is https, 30-day expiry. Only the
SHA-256 of the token is stored. Expired rows are purged on login.

**Revocation.** Resetting a camera's login password deletes that camera's `session_cameras` rows
(sessions holding other cameras keep those). Disabling a camera does the same.

**Brute force.** Both login POSTs are rate limited with the existing slowapi limiter (10/minute per IP).

**CSRF.** All state-changing browser actions are POST forms; `SameSite=Lax` stops cross-site POSTs
from carrying the cookie. No GET changes state.

## Admin control panel (`/admin`, admin session required, else redirect to `/admin/login`)

**Cameras table:** ID, name, location, online status, last seen, login set (yes/no).

- *Add camera* (ID, name, lat, lon): creates it and shows the Pi key and the login password **once**.
- *Reset login password*: new password shown once; signs that camera out everywhere.
- *Reset Pi key*: new key shown once (must be copied to the Pi's config).
- *Enable / disable.*

Secrets are shown on a one-time result page and never stored in plain text.

**Test fires:**

1. *Simulate camera detection*: camera, max temperature (default 350 °C), hot-spot size in sensor
   pixels (default 12), "send real alerts" (default on). The server builds a synthetic 24×32 frame,
   runs `thermal.analyze` and `thermal.render` (same code as the Pi) and creates an **unconfirmed,
   `is_test=1`** event through the same event-creation function as `/api/camera-event`. If alerts are
   on, SMS/email go to that camera's subscribers with `[TEST] ` prefixed. The result shows an
   *Open review page* link.
2. *Drop fire on map*: lat/lon (filled by clicking the admin map, or typed) and severity
   (Low/Moderate/High/Extreme). Creates a **confirmed, `is_test=1`** event attached to the hidden
   internal camera `__test__` (disabled, never listed, cannot log in, created on demand).
3. *Test fire list*: each with status, camera, time, review link and delete; plus *Delete all test fires*.
   Deleting removes the rows (cascades to observations/tokens/notification attempts) and their image files.

**Isolation rules for test events:**

- Dedupe and false-alarm suppression only match events with the same `is_test` value.
- Every public/camera-user query filters `is_test=0`; only admin viewers get test events.
- Route avoidance for admins includes confirmed test fires; for everyone else it does not.

**Not in scope:** managing SMS/email recipients in the panel (stays in `manage.py` and the admin API).

## Data changes (auto-migrated in `init_db`)

- `cameras.login_password_hash TEXT` (NULL = no login yet).
- `device_events.is_test INTEGER NOT NULL DEFAULT 0`.
- `sessions`, `session_cameras` tables as above.

## Code changes

| File | Change |
|---|---|
| `auth.py` (new) | Password hashing, session create/lookup/delete, `Viewer`, `get_viewer`, `/login`, `/logout`, `/admin/login`. |
| `admin_panel.py` (new) | `/admin` page and its POST actions (cameras, simulate, drop, delete). |
| `templates/login.html`, `templates/admin.html`, `templates/admin_secret.html` (new) | Pages, same visual style as `review.html`. |
| `device_api.py` | Extract `create_event(...)` from `camera_event` (used by uploads and simulations; takes `is_test`). Review page/POST also accept a session that can see the camera (`reviewed_by` = `login:<camera>` or `admin`). `/api/device-events` and `/api/cameras/status` filter by viewer; `counts.pending_review` only covers visible cameras. |
| `fire_data.py` | Load `is_test`; cache unchanged; `fires_for(viewer)` filters; `get_avoid_geojson(viewer)` per viewer (cache keyed by "sees tests"). |
| `map.py` | `build_map_figure(viewer)`: camera markers and non-public events only for allowed cameras; TEST fires (admin) styled distinctly. Header shows Sign in / Sign out / Admin. |
| `routes.py` | Pass the viewer to avoidance. |
| `camera_api.py` | Admin API: add `POST /api/admin/cameras/{id}/login-password` (generates, returns once, revokes that camera's sessions); disabling a camera also revokes them. Resetting the Pi key does not touch logins. All camera listings exclude `__test__`. |
| `notifications.py` | Accept an `is_test` flag → `[TEST] ` prefix on SMS and email subject. |
| `manage.py` | `set-camera-password CAM-ID` (prints a generated password). |
| `requirements.txt` | Add `numpy`, `Pillow` (server renders simulated heat-maps). |
| `templates/index.html` | Camera section and Review buttons only when signed in; header auth links. |
| `tests/test_server.py` | `TemporaryDirectory(ignore_cleanup_errors=True)` so tests pass on Windows too. |

## Errors

- Wrong credentials: 401 page with a generic message, form re-shown.
- Expired session: treated as visitor (no error).
- `/admin*` without admin: browser → redirect to `/admin/login`; API → 401/403 JSON.
- Review via session for a camera the viewer cannot see: same "invalid link" page as a bad token.
- Simulate on a disabled or unknown camera: 400 with message on the panel.

## Testing

New `tests/test_auth_admin.py` (unittest style, like `test_server.py`):

- Visibility matrix: for visitor / camera-A login / admin, check `/api/device-events`,
  `/api/cameras/status`, map figure data and avoidance polygons against the table above
  (cameras A and B, real and test events in each status).
- Login: correct/wrong password, disabled camera, camera without password, two cameras in one
  session, logout, expiry, rate limit (11th attempt → 429).
- Password reset revokes only that camera; admin token login; admin pages redirect when not admin.
- Review via session (own camera ok, other camera rejected) and via token still works.
- Simulate: creates unconfirmed test event with an image, `[TEST]` alert sent, never public, does
  not dedupe into a real event and vice versa. Drop: confirmed test fire in admin avoidance only.
  Delete-all removes rows and files.

Plus a browser run against a local server: sign in as camera, as admin, simulate, review, drop, delete.

## Rollout

After deploying, existing cameras have no login until the admin sets one in the panel (or with
`manage.py set-camera-password`). The public map loses camera markers immediately; that is intended.
