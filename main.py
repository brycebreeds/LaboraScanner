import platform
import tkinter as tk
from tkinter import ttk, messagebox
import json
import uuid
import csv
import io
import threading
import time
import re
import logging
import subprocess
import sys
import os
from datetime import datetime
from pathlib import Path

from UniAPI import UniAPI

# ── Git setup ─────────────────────────────────────────────────────────────────
# The launcher sets LABORA_GIT to the bundled portable git exe on Windows.
# On Linux/macOS it is not set, so we fall back to whatever 'git' is in PATH.
_GIT_EXE = os.environ.get("LABORA_GIT", "git")

# Portable git on Windows has its own config scope and won't see the global
# ~/.gitconfig the launcher wrote. Pass safe.directory=* inline on every
# git call so ownership checks never block us regardless of config files.
_GIT_SAFE = ["-c", "safe.directory=*"]

def _check_git_available() -> bool:
    """Return True if git is present and usable."""
    try:
        r = subprocess.run([_GIT_EXE, "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

GIT_AVAILABLE = _check_git_available()
if not GIT_AVAILABLE:
    logging.getLogger("scanner").warning(
        "git not found — UPD_ update barcodes will be disabled"
    )

# ── Connectivity debug patch ───────────────────────────────────────────────────
# We wrap the two low-level transport methods so that when DEBUG_CONNECTIVITY is
# True every API call fails exactly as it would during a real network outage.
_real_do_post = UniAPI.do_post
_real_do_get  = UniAPI.do_get

def _debug_do_post(self, url, data, timeout=10):
    if DEBUG_CONNECTIVITY:
        log.debug(f"[DEBUG_CON] Blocked POST → {url}")
        return '{"http_code": 666, "detail": "simulated connectivity loss"}'
    return _real_do_post(self, url, data, timeout)

def _debug_do_get(self, url, data, timeout=10):
    if DEBUG_CONNECTIVITY:
        log.debug(f"[DEBUG_CON] Blocked GET  → {url}")
        return '{"http_code": 666, "detail": "simulated connectivity loss"}'
    return _real_do_get(self, url, data, timeout)

UniAPI.do_post = _debug_do_post
UniAPI.do_get  = _debug_do_get

# ── Files ─────────────────────────────────────────────────────────────────────
# When launched via the LABORA launcher exe, LABORA_ROOT points to the install
# directory so settings/data files are always found next to the exe.
_labora_root = os.environ.get("LABORA_ROOT")
if _labora_root:
    SCRIPT_DIR = Path(_labora_root)
else:
    SCRIPT_DIR = Path(__file__).parent

SETTINGS_FILE  = SCRIPT_DIR / "settings.json"
DATA_FILE      = SCRIPT_DIR / "scanner_data.json"
RETRY_INTERVAL = 30    # seconds
SYNC_INTERVAL  = 3600  # seconds — hourly SFTP sync

# ── Debug flags ───────────────────────────────────────────────────────────────
DEBUG_CONNECTIVITY = False   # toggled by scanning DEBUG_CON

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scanner")

# ── LABORA Theme ──────────────────────────────────────────────────────────────
RED         = "#C0181A"
RED_DARK    = "#9e1315"
RED_LIGHT   = "#fdeaea"
WHITE       = "#ffffff"
OFF_WHITE   = "#f7f7f8"
GRAY_50     = "#f2f2f4"
GRAY_100    = "#e5e5e9"
GRAY_200    = "#d0d0d6"
GRAY_400    = "#9898a6"
GRAY_600    = "#5a5a6e"
GRAY_800    = "#2a2a38"
BLACK       = "#111118"
SUCCESS     = "#1a7f4b"
DANGER      = "#c0181a"
WARNING     = "#b45309"

BG          = WHITE
SURFACE     = OFF_WHITE
SURFACE2    = GRAY_50
BORDER      = GRAY_200
TEXT        = BLACK
TEXT_DIM    = GRAY_600
TEXT_MUTED  = GRAY_400
ACCENT      = RED
ACCENT_DIM  = RED_DARK
ACCENT_BG   = RED_LIGHT

# Cross-platform fonts — Segoe UI is Windows only
_SYS = platform.system()
if _SYS == "Darwin":
    _UI_FONT   = "SF Pro Text"
    _MONO_FONT = "Menlo"
elif _SYS == "Linux":
    _UI_FONT   = "DejaVu Sans"
    _MONO_FONT = "DejaVu Sans Mono"
else:
    _UI_FONT   = "Segoe UI"
    _MONO_FONT = "Courier New"

FONT        = (_UI_FONT, 10)
FONT_BOLD   = (_UI_FONT, 10, "bold")
FONT_MONO   = (_MONO_FONT, 10)
FONT_LARGE  = (_UI_FONT, 15, "bold")
FONT_SM     = (_UI_FONT, 9)
FONT_XS     = (_UI_FONT, 8)

# ── Barcode patterns ──────────────────────────────────────────────────────────
BATCH_RE    = re.compile(r"^BAT_\d+$",          re.IGNORECASE)
UPDATE_RE   = re.compile(r"^UPD_[0-9a-f]{7}$",  re.IGNORECASE)
IMPORT_RE   = re.compile(r"^IMPORT$",            re.IGNORECASE)
SEL_RE      = re.compile(r"^SEL_(\d+)$",         re.IGNORECASE)
NUMERIC_RE  = re.compile(r"^\d+$")
DEBUG_CON_RE = re.compile(r"^DEBUG_CON$",        re.IGNORECASE)

# ── Shoe-by-shoe scanning constants ───────────────────────────────────────────
SBSS_SETGROUP_ID  = "23"
SBSS_BARCODE_PROP = "SuborderClientBarcodeshoeByShoe"
SBSS_COUNT_PROP   = "SuborderCompletedShoeCount"


# ── Settings ──────────────────────────────────────────────────────────────────
def load_settings():
    log.debug(f"Loading settings from: {SETTINGS_FILE}")
    if not SETTINGS_FILE.exists():
        log.error("settings.json not found")
        return None, "settings.json not found next to the script."
    try:
        s = json.loads(SETTINGS_FILE.read_text())
    except json.JSONDecodeError as e:
        log.error(f"settings.json JSON error: {e}")
        return None, f"settings.json is invalid JSON:\n{e}"
    if not s.get("baseurl"):
        log.error("settings.json missing 'baseurl'")
        return None, 'settings.json is missing "baseurl".'
    if "users" not in s:
        s["users"] = []   # will be populated by fetch_users_from_api
    if not isinstance(s["users"], list):
        log.error("settings.json 'users' must be a list")
        return None, 'settings.json "users" must be a list.'
    for u in s["users"]:
        if not u.get("name") or not u.get("token"):
            log.warning(f"User entry missing name or token (will be refreshed from API): {u}")
    if not s.get("instance_id"):
        s["instance_id"] = uuid.uuid4().hex[:12]
        try:
            SETTINGS_FILE.write_text(json.dumps(s, indent=2))
            log.info(f"Generated new instance_id: {s['instance_id']}")
        except Exception as e:
            log.warning(f"Could not persist instance_id: {e}")
    log.info(f"Settings loaded — baseurl={s['baseurl']}  instance_id={s['instance_id']}  users={[u['name'] for u in s['users']]}")
    return s, None


# ── Runtime data ──────────────────────────────────────────────────────────────
def load_data():
    if DATA_FILE.exists():
        try:
            d = json.loads(DATA_FILE.read_text())
            log.debug(f"Loaded scanner_data.json — {len(d.get('scans', []))} scans, {len(d.get('updates', []))} updates on record")
            return d
        except Exception as e:
            log.warning(f"Could not parse scanner_data.json, starting fresh: {e}")
    return {"session": None, "scans": [], "updates": []}


def save_data(data):
    DATA_FILE.write_text(json.dumps(data, indent=2))
    log.debug("scanner_data.json saved")



# ── SFTP helpers ──────────────────────────────────────────────────────────────
BACKUP_DIR = SCRIPT_DIR / "backups"

DAILY_CSV_COLUMNS = [
    "date", "time", "user_name",
    "batch_id", "entity_id",
    "shoe", "size", "qty",
    "status_label", "status_value",
    "result", "attempts", "id",
]


def _sftp_connect(sftp_cfg):
    """Open and return (transport, sftp). Caller must close both."""
    import paramiko
    transport = paramiko.Transport((sftp_cfg["host"], int(sftp_cfg.get("port", 22))))
    transport.connect(username=sftp_cfg.get("username", ""),
                      password=sftp_cfg.get("password", ""))
    return transport, paramiko.SFTPClient.from_transport(transport)


def _sftp_makedirs(sftp, remote_dir):
    """Create remote_dir recursively, tolerating permission errors on existing dirs."""
    cur = ""
    for part in remote_dir.rstrip("/").lstrip("/").split("/"):
        cur += "/" + part
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            try:
                sftp.mkdir(cur)
                log.debug(f"SFTP mkdir {cur}")
            except Exception as e:
                log.debug(f"SFTP mkdir({cur}) skipped: {e}")
        except Exception:
            log.debug(f"SFTP stat({cur}) failed — assuming exists")


def _scan_date(scan):
    """Return YYYYMMDD string for a scan from its id prefix (most reliable)."""
    sid = scan.get("id", "")
    if len(sid) >= 8 and sid[:8].isdigit():
        return sid[:8]
    return datetime.now().strftime("%Y%m%d")


def build_daily_csv(scans, date_str):
    """Build CSV of all scans whose date == date_str, sorted chronologically."""
    day_scans = sorted(
        [s for s in scans if _scan_date(s) == date_str],
        key=lambda s: s.get("id", "")
    )
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=DAILY_CSV_COLUMNS,
                            extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for scan in day_scans:
        row = {col: scan.get(col, "") for col in DAILY_CSV_COLUMNS}
        row["date"] = date_str[:4] + "-" + date_str[4:6] + "-" + date_str[6:]
        writer.writerow(row)
    return buf.getvalue(), len(day_scans)


def sftp_list_daily_files(settings):
    """
    Return a sorted list of dicts describing daily CSV files on the server
    that belong to this instance.
    Each dict: { name, remote_path, date_str, rows_approx }
    Returns [] on any failure.
    """
    sftp_cfg = settings.get("sftp")
    if not sftp_cfg or not sftp_cfg.get("host"):
        return []
    instance_id  = settings.get("instance_id", "")
    remote_base  = sftp_cfg.get("remote_dir", "/").rstrip("/")
    remote_daily = remote_base + "/daily"
    try:
        transport, sftp = _sftp_connect(sftp_cfg)
        try:
            entries = sftp.listdir_attr(remote_daily)
            files = []
            for e in entries:
                name = e.filename
                if instance_id and instance_id not in name:
                    continue
                if not name.endswith("_scans.csv"):
                    continue
                # filename: YYYYMMDD_<instance_id>_scans.csv
                date_str = name[:8] if name[:8].isdigit() else "?"
                files.append({
                    "name":        name,
                    "remote_path": remote_daily + "/" + name,
                    "date_str":    date_str,
                    "size":        e.st_size,
                })
            files.sort(key=lambda f: f["date_str"], reverse=True)
            return files
        finally:
            sftp.close()
            transport.close()
    except Exception as e:
        log.error(f"sftp_list_daily_files failed: {e}")
        return []


def sftp_download_csv(settings, remote_path):
    """
    Download a remote CSV and return a list of row dicts.
    Returns [] on failure.
    """
    sftp_cfg = settings.get("sftp")
    if not sftp_cfg or not sftp_cfg.get("host"):
        return []
    try:
        transport, sftp = _sftp_connect(sftp_cfg)
        try:
            buf = io.BytesIO()
            sftp.getfo(remote_path, buf)
            buf.seek(0)
            reader = csv.DictReader(io.TextIOWrapper(buf, encoding="utf-8"))
            return list(reader)
        finally:
            sftp.close()
            transport.close()
    except Exception as e:
        log.error(f"sftp_download_csv({remote_path}) failed: {e}")
        return []


def run_hourly_sync(data, settings):
    """
    Runs every SYNC_INTERVAL seconds and once immediately on login.

    1. For every unique date in scanner_data.json, builds/replaces
       <remote_dir>/daily/<date>_<instance_id>_scans.csv.
    2. Uploads any local backups/*.csv not yet on the server.
    """
    sftp_cfg = settings.get("sftp")
    if not sftp_cfg or not sftp_cfg.get("host"):
        log.debug("run_hourly_sync — no SFTP config, skipping")
        return

    instance_id    = settings.get("instance_id", "unknown")
    remote_base    = sftp_cfg.get("remote_dir", "/").rstrip("/")
    remote_daily   = remote_base + "/daily"
    remote_backups = remote_base + "/backups"

    try:
        transport, sftp = _sftp_connect(sftp_cfg)
    except Exception as e:
        log.error(f"run_hourly_sync — SFTP connect failed: {e}")
        return

    try:
        _sftp_makedirs(sftp, remote_daily)
        _sftp_makedirs(sftp, remote_backups)

        # ── Daily CSVs ────────────────────────────────────────────────────────
        scans = data.get("scans", [])
        dates = sorted({_scan_date(s) for s in scans})
        if not dates:
            log.info("run_hourly_sync — no scans to export")
        for date_str in dates:
            csv_text, count = build_daily_csv(scans, date_str)
            remote_path = f"{remote_daily}/{date_str}_{instance_id}_scans.csv"
            try:
                sftp.putfo(io.BytesIO(csv_text.encode("utf-8")), remote_path)
                log.info(f"Daily CSV → {date_str}_{instance_id}_scans.csv  ({count} rows)")
            except Exception as e:
                log.error(f"Daily CSV upload failed ({date_str}): {e}")

        # ── Backup sync ───────────────────────────────────────────────────────
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            on_server = set(sftp.listdir(remote_backups))
        except Exception as e:
            log.warning(f"Could not list remote backups: {e}")
            on_server = set()

        uploaded = 0
        for f in sorted(BACKUP_DIR.glob("*.csv")):
            if f.name not in on_server:
                try:
                    sftp.putfo(io.BytesIO(f.read_bytes()), f"{remote_backups}/{f.name}")
                    log.info(f"Backup synced — {f.name}")
                    uploaded += 1
                except Exception as e:
                    log.error(f"Backup sync failed ({f.name}): {e}")
            else:
                log.debug(f"Backup already on server — {f.name}")

        log.info(f"run_hourly_sync done — {len(dates)} daily, {uploaded} backups uploaded")

    finally:
        try:
            sftp.close()
            transport.close()
        except Exception:
            pass


def export_session_snapshot(data, settings, progress_cb=None):
    """
    Saves a point-in-time snapshot of all scans to backups/ and syncs to SFTP.
    scanner_data.json is NOT cleared.
    """
    sess      = data.get("session") or {}
    user_name = sess.get("user_name", "unknown").replace(" ", "_")
    stamp     = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename  = f"{stamp}_{user_name}.csv"

    if progress_cb:
        progress_cb("Building session snapshot…")

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=DAILY_CSV_COLUMNS,
                            extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for scan in sorted(data.get("scans", []), key=lambda s: s.get("id", "")):
        row = {col: scan.get(col, "") for col in DAILY_CSV_COLUMNS}
        ds  = _scan_date(scan)
        row["date"] = ds[:4] + "-" + ds[4:6] + "-" + ds[6:]
        writer.writerow(row)
    csv_text = buf.getvalue()

    count = len(data.get("scans", []))
    log.info(f"Session snapshot — {count} scan(s) → {filename}")

    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        (BACKUP_DIR / filename).write_text(csv_text, encoding="utf-8")
        log.info(f"Snapshot saved: {BACKUP_DIR / filename}")
    except Exception as e:
        log.error(f"Could not save local snapshot: {e}")

    sftp_cfg = settings.get("sftp")
    if sftp_cfg and sftp_cfg.get("host"):
        if progress_cb:
            progress_cb("Syncing to SFTP…")
        try:
            run_hourly_sync(data, settings)
        except Exception as e:
            log.error(f"SFTP sync during sign-out failed: {e}")
    else:
        log.info("No SFTP config — skipping upload")


# ── User list ─────────────────────────────────────────────────────────────────
USERS_ENTITY_ID    = "3"
USERS_SET_NAME     = "Factory Line User - Org"
USERS_SETGROUP     = "Factory Line User"
USERS_PROPERTIES   = [["id", "view", "id"], "name", ["userForceLogin", "view", "token"], "allowShoeByShoeScanning"]
USERS_DIRECTION    = "child"

def fetch_users_from_api(api, settings):
    """
    Pull the live user list from the API and merge/overwrite settings["users"].
    Each remote user has: id, name, forcelogin (used as the login token).
    Returns True on success, False on failure (callers keep the cached list).
    """
    log.info("Fetching user list from API …")
    try:
        result = api.get_related_entity_data(
            USERS_ENTITY_ID,
            USERS_SET_NAME,
            USERS_SETGROUP,
            USERS_PROPERTIES,
            USERS_DIRECTION,
        )
    except Exception as e:
        log.error(f"fetch_users_from_api exception: {e}")
        return False

    if not result:
        log.warning("fetch_users_from_api — API returned empty/false result, keeping cached list")
        return False

    # The API returns a dict keyed by entity-id; values are property dicts.
    # Normalise into a flat list regardless of whether it comes back as a
    # list or a dict.
    raw_users = result if isinstance(result, list) else list(result.values())

    users = []
    for u in raw_users:
        uid   = str(u.get("id")        or u.get("Id")        or "").strip()
        name  = str(u.get("name")      or u.get("Name")      or "").strip()
        token = str(u.get("token")or u.get("Token")or "").strip()
        if not uid or not name or not token:
            log.warning(f"Skipping incomplete user record from API: {u}")
            continue
        raw_sbss = u.get("allowShoeByShoeScanning", "")
        allow_sbss = str(raw_sbss).strip().lower() in ("1", "true", "yes")
        users.append({"id": uid, "name": name, "token": token, "allowShoeByShoeScanning": allow_sbss})

    if not users:
        log.warning("fetch_users_from_api — no valid users parsed from API response")
        return False

    settings["users"] = users
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
        log.info(f"settings.json updated — {len(users)} user(s): {[u['name'] for u in users]}")
    except Exception as e:
        log.error(f"Could not write settings.json: {e}")

    return True


# ── Git helpers ───────────────────────────────────────────────────────────────
def git_current_hash(short=True):
    """Return current HEAD commit hash, or None if git is unavailable."""
    if not GIT_AVAILABLE:
        return None
    try:
        args = [_GIT_EXE] + _GIT_SAFE + (["rev-parse", "--short=7", "HEAD"] if short else ["rev-parse", "HEAD"])
        result = subprocess.run(args, cwd=SCRIPT_DIR, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        log.warning(f"git_current_hash failed: {e}")
    return None


def git_checkout_and_pull(commit_hash):
    """
    Fetch from origin and checkout the specified commit hash.
    Returns (success: bool, message: str)
    """
    if not GIT_AVAILABLE:
        return False, "git is not available on this machine"
    try:
        log.info(f"git fetch origin")
        fetch = subprocess.run(
            [_GIT_EXE] + _GIT_SAFE + ["fetch", "origin"],
            cwd=SCRIPT_DIR, capture_output=True, text=True, timeout=30
        )
        log.debug(f"fetch stdout: {fetch.stdout}  stderr: {fetch.stderr}")
        if fetch.returncode != 0:
            return False, f"git fetch failed: {fetch.stderr.strip()}"

        # Exclude runtime files from checkout — these belong to the site,
        # not the repo, and must never be overwritten by an update.
        runtime_files = ["scanner_data.json", "settings.json", "launcher.log",
                         ".deps_installed"]
        for f in runtime_files:
            skip = subprocess.run(
                [_GIT_EXE] + _GIT_SAFE + ["update-index", "--skip-worktree", f],
                cwd=SCRIPT_DIR, capture_output=True, text=True, timeout=10
            )
            log.debug(f"skip-worktree {f}: {skip.returncode}")

        log.info(f"git checkout {commit_hash}")
        checkout = subprocess.run(
            [_GIT_EXE] + _GIT_SAFE + ["checkout", "-f", commit_hash],
            cwd=SCRIPT_DIR, capture_output=True, text=True, timeout=15
        )
        log.debug(f"checkout stdout: {checkout.stdout}  stderr: {checkout.stderr}")
        if checkout.returncode != 0:
            return False, f"git checkout failed: {checkout.stderr.strip()}"

        return True, f"Checked out {commit_hash}"
    except subprocess.TimeoutExpired:
        return False, "git operation timed out"
    except Exception as e:
        return False, str(e)


def restart_app():
    """Restart the application — works on Windows, Linux, and macOS."""
    log.info("Restarting app…")
    # os.execv is not reliable on Windows; use subprocess + exit instead.
    if platform.system() == "Windows":
        subprocess.Popen([sys.executable] + sys.argv)
        sys.exit(0)
    else:
        os.execv(sys.executable, [sys.executable] + sys.argv)



# ── App ───────────────────────────────────────────────────────────────────────
class ScannerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LABORA — Barcode Scanner")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(700, 520)
        self.after(0, lambda: self.state("zoomed"))

        self.settings          = None
        self.data              = load_data()
        self.api               = None
        self.user_status       = None
        self.user_status_label = None
        self.running           = True
        self._pending_user_switch = None   # user dict awaiting confirm barcode

        self._import_buffer = ""   # accumulates keystrokes for global IMPORT catch
        self._debug_banner  = None # connectivity debug mode banner widget
        self._net_icon      = None # topbar connectivity icon label
        self._net_pill      = None # coloured frame around the icon
        self._net_online    = None # True / False / None (unknown)
        self._topbar        = None # topbar frame ref for bg flash
        self._style()
        self._boot()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_retry_loop()
        self._start_connectivity_poll()
        self.bind_all("<Key>", self._global_import_intercept)

    # ── Boot ──────────────────────────────────────────────────────────────────
    def _boot(self):
        log.info("App booting")
        current = git_current_hash()
        if current:
            log.info(f"Running at commit: {current}")
        settings, err = load_settings()
        if err:
            self._show_settings_error(err)
            return
        self.settings = settings
        sess = self.data.get("session")
        if sess and sess.get("token"):
            log.info(f"Found stored session for '{sess.get('user_name')}', attempting restore")
            self._try_restore_session(sess)
        else:
            log.info("No stored session — showing login")
            self._show_login()

    # ── Styling ───────────────────────────────────────────────────────────────
    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".",
            background=BG, foreground=TEXT, font=FONT,
            bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        s.configure("TFrame",         background=BG)
        s.configure("Surface.TFrame", background=SURFACE)
        s.configure("TLabel",         background=BG,      foreground=TEXT,     font=FONT)
        s.configure("Dim.TLabel",     background=BG,      foreground=TEXT_DIM, font=FONT_SM)
        s.configure("Surface.TLabel", background=SURFACE, foreground=TEXT,     font=FONT)
        s.configure("TEntry",
            fieldbackground=WHITE, foreground=TEXT,
            insertcolor=TEXT, bordercolor=BORDER,
            lightcolor=BORDER, darkcolor=BORDER,
            selectbackground=ACCENT_BG, selectforeground=TEXT,
            font=FONT)
        s.map("TEntry",
            bordercolor=[("focus", ACCENT)],
            lightcolor=[("focus", ACCENT)],
            darkcolor=[("focus", ACCENT)])
        s.configure("TCombobox",
            fieldbackground=WHITE, foreground=TEXT,
            selectbackground=ACCENT_BG, selectforeground=TEXT,
            bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
            arrowcolor=GRAY_600, font=FONT)
        s.map("TCombobox",
            fieldbackground=[("readonly", WHITE)],
            bordercolor=[("focus", ACCENT)],
            lightcolor=[("focus", ACCENT)])
        s.configure("Accent.TButton",
            background=ACCENT, foreground=WHITE,
            font=FONT_BOLD, borderwidth=0,
            focuscolor=ACCENT_DIM, relief="flat",
            padding=(14, 7))
        s.map("Accent.TButton",
            background=[("active", ACCENT_DIM), ("pressed", ACCENT_DIM)])
        s.configure("Ghost.TButton",
            background=BG, foreground=TEXT_DIM,
            font=FONT_SM, borderwidth=1,
            relief="flat", padding=(8, 4))
        s.map("Ghost.TButton",
            background=[("active", GRAY_50)],
            foreground=[("active", TEXT)])
        s.configure("Treeview",
            background=WHITE, foreground=TEXT,
            fieldbackground=WHITE, rowheight=30,
            font=FONT, borderwidth=0)
        s.configure("Treeview.Heading",
            background=GRAY_50, foreground=TEXT_DIM,
            font=FONT_SM, relief="flat", borderwidth=0)
        s.map("Treeview",
            background=[("selected", ACCENT_BG)],
            foreground=[("selected", TEXT)])
        s.configure("TSeparator", background=BORDER)
        s.configure("TScrollbar",
            background=GRAY_100, troughcolor=GRAY_50,
            bordercolor=BORDER, arrowcolor=GRAY_400,
            relief="flat")

    # ── Settings error ────────────────────────────────────────────────────────
    def _show_settings_error(self, msg):
        self._clear()
        self.geometry("480x230")
        f = tk.Frame(self, bg=WHITE, padx=32, pady=28)
        f.pack(expand=True, fill="both", padx=24, pady=24)
        f.configure(highlightbackground=BORDER, highlightthickness=1)
        tk.Label(f, text="Configuration error", font=FONT_BOLD,
                 bg=WHITE, fg=DANGER).pack(anchor="w", pady=(0, 8))
        tk.Label(f, text=msg, font=FONT_SM, bg=WHITE, fg=TEXT_DIM,
                 wraplength=400, justify="left").pack(anchor="w", pady=(0, 12))
        tk.Label(f, text=f"Expected: {SETTINGS_FILE}",
                 font=FONT_XS, bg=WHITE, fg=TEXT_MUTED).pack(anchor="w")

    # ── Login view ────────────────────────────────────────────────────────────
    def _show_login(self):
        self._clear()
        self.geometry("420x300")
        log.debug("Rendering login screen")

        outer = tk.Frame(self, bg=BG)
        outer.pack(expand=True, fill="both")
        tk.Frame(outer, bg=ACCENT, height=4).pack(fill="x")

        card = tk.Frame(outer, bg=WHITE, padx=36, pady=32)
        card.pack(expand=True, padx=32, pady=24)
        card.configure(highlightbackground=BORDER, highlightthickness=1)

        logo_row = tk.Frame(card, bg=WHITE)
        logo_row.pack(anchor="w", pady=(0, 20))
        tk.Label(logo_row, text="LABORA", font=(_UI_FONT, 16, "bold"),
                 bg=WHITE, fg=ACCENT).pack(side="left")
        tk.Label(logo_row, text=" · Barcode Scanner", font=(_UI_FONT, 13),
                 bg=WHITE, fg=GRAY_400).pack(side="left", pady=(2, 0))

        tk.Label(card, text="Select user", font=FONT_SM,
                 bg=WHITE, fg=TEXT_DIM).pack(anchor="w")

        self._user_var    = tk.StringVar()
        self._login_combo = ttk.Combobox(card, textvariable=self._user_var,
                                         values=[], state="disabled", width=32)
        self._login_combo.pack(fill="x", pady=(4, 18))

        self._login_err = tk.Label(card, text="Loading users…", font=FONT_SM,
                                   bg=WHITE, fg=TEXT_DIM)
        self._login_err.pack(anchor="w", pady=(0, 8))

        self._login_btn = ttk.Button(card, text="Sign in", style="Accent.TButton",
                                     command=self._do_login, state="disabled")
        self._login_btn.pack(fill="x")

        self.bind("<Return>", lambda e: self._do_login())

        # Populate immediately if users are cached, otherwise fetch in background.
        if self.settings.get("users"):
            self._populate_login_combo()
        else:
            threading.Thread(target=self._fetch_users_for_login, daemon=True).start()

    def _fetch_users_for_login(self):
        """Background fetch used when no users are cached (e.g. first run or sign-out)."""
        log.info("Login screen: fetching users before showing combo")
        try:
            seed_token = ""
            for u in (self.settings.get("users") or []):
                if u.get("token"):
                    seed_token = u["token"]
                    break
            tmp_api = UniAPI(self.settings["baseurl"], token=seed_token)
            fetch_users_from_api(tmp_api, self.settings)
        except Exception as e:
            log.error(f"_fetch_users_for_login failed: {e}")
        self.after(0, self._populate_login_combo)

    def _populate_login_combo(self):
        """Fill / refresh the login combo with whatever is now in settings['users']."""
        user_names = [u["name"] for u in self.settings.get("users", [])]
        if not user_names:
            log.warning("No users available for login combo")
            self._login_err.config(
                text="Could not load users. Check network / settings.json.", fg=DANGER)
            return
        last = (self.data.get("session") or {}).get("user_name", "")
        self._user_var.set(last if last in user_names else user_names[0])
        self._login_combo.config(values=user_names, state="readonly")
        self._login_btn.config(state="normal")
        self._login_err.config(text="", fg=DANGER)
        self._login_combo.focus()
        log.debug(f"Login combo populated with {len(user_names)} user(s)")

    def _do_login(self):
        name = self._user_var.get()
        user = next((u for u in self.settings["users"] if u["name"] == name), None)
        if not user:
            log.warning(f"User '{name}' not found in settings")
            self._login_err.config(text="User not found in settings.")
            return

        log.info(f"Login attempt for user '{name}'")
        self._login_err.config(text="Signing in…", fg=TEXT_DIM)
        self.update()

        def attempt():
            api = UniAPI(self.settings["baseurl"], token=user["token"])
            log.debug(f"Calling flogin for '{name}' — baseurl={self.settings['baseurl']}")
            detail = api.flogin(user["token"])
            if detail:
                log.info(f"flogin succeeded — entityID={api.entityID}  token={api.token[:12]}…")
                self.api = api
                fetch_users_from_api(api, self.settings)
                props = api.get_entity_property(api.entityID, [
                    "PhaseToEnter",
                    ["PhaseToEnter", "view", "PhaseToEnterName"]
                ])
                log.debug(f"get_entity_property response: {props}")
                status       = (props or {}).get("PhaseToEnter")
                status_label = (props or {}).get("PhaseToEnterName") or status
                log.info(f"User PhaseToEnter = '{status}'  label = '{status_label}'")
                self.after(0, lambda: self._on_login_success(name, detail, status, status_label))
            else:
                log.error(f"flogin failed for user '{name}'")
                self.after(0, lambda: self._login_err.config(
                    text="Login failed. Check the token in settings.json.", fg=DANGER))

        threading.Thread(target=attempt, daemon=True).start()

    def _on_login_success(self, user_name, detail, status, status_label=None):
        self.user_status       = status
        self.user_status_label = status_label or status
        self.data["session"] = {
            "user_name":    user_name,
            "token":        self.api.token,
            "entity_id":    self.api.entityID,
            "status":       status,
            "status_label": self.user_status_label,
        }
        save_data(self.data)
        log.info(f"Session saved for '{user_name}' (status='{status}'  label='{self.user_status_label}')")
        self._show_main()
        self._start_sync_loop()

    # ── Session restore ───────────────────────────────────────────────────────
    def _try_restore_session(self, sess):
        self._clear()
        self.geometry("400x160")
        f = tk.Frame(self, bg=BG)
        f.pack(expand=True)
        tk.Frame(self, bg=ACCENT, height=4).place(x=0, y=0, relwidth=1)
        tk.Label(f, text=f"Signing in as {sess.get('user_name', 'user')}…",
                 font=FONT, bg=BG, fg=TEXT_DIM).pack(pady=(24, 0))

        def attempt():
            user_name = sess.get("user_name", "")
            user = next((u for u in self.settings["users"] if u["name"] == user_name), None)
            token = user["token"] if user else sess.get("token", "")
            log.info(f"Restoring session for '{user_name}' — using token from settings.json")

            api = UniAPI(self.settings["baseurl"], token=token)
            detail = api.flogin(token)
            if detail:
                log.info(f"Session restored — entityID={api.entityID}")
                self.api = api
                fetch_users_from_api(api, self.settings)
                props = api.get_entity_property(api.entityID, [
                    "PhaseToEnter",
                    ["PhaseToEnter", "view", "PhaseToEnterName"]
                ])
                log.debug(f"get_entity_property response: {props}")
                status       = (props or {}).get("PhaseToEnter")
                status_label = (props or {}).get("PhaseToEnterName") or status
                log.info(f"PhaseToEnter = '{status}'  label = '{status_label}'")
                self.user_status       = status
                self.user_status_label = status_label
                self.data["session"].update({
                    "token":        api.token,
                    "entity_id":    api.entityID,
                    "status":       status,
                    "status_label": status_label,
                })
                save_data(self.data)
                self.after(0, self._show_main)
            else:
                log.warning("Session restore failed — returning to login")
                self.data["session"] = None
                save_data(self.data)
                self.after(0, self._show_login)

        threading.Thread(target=attempt, daemon=True).start()

    # ── Main view ─────────────────────────────────────────────────────────────
    def _show_main(self):
        self._clear()
        self.geometry("820x620")
        self.unbind("<Return>")
        log.debug("Rendering main scanner screen")

        sess           = self.data.get("session") or {}
        user_name      = sess.get("user_name", "User")
        status_display = self.user_status_label or self.user_status or "—"

        # ── Top red bar ───────────────────────────────────────────────────────
        topbar = tk.Frame(self, bg=ACCENT, padx=16, pady=0)
        topbar.pack(fill="x")
        tk.Label(topbar, text="LABORA", font=(_UI_FONT, 11, "bold"),
                 bg=ACCENT, fg=WHITE).pack(side="left", pady=8)
        tk.Label(topbar, text=" · Barcode Scanner", font=(_UI_FONT, 10),
                 bg=ACCENT, fg="#f0a0a0").pack(side="left", pady=8)

        # Show current commit hash in topbar
        current_hash = git_current_hash()
        if current_hash:
            tk.Label(topbar, text=f"v{current_hash}", font=FONT_XS,
                     bg=ACCENT, fg="#f0a0a0").pack(side="left", padx=(12, 0), pady=9)

        right_top = tk.Frame(topbar, bg=ACCENT)
        right_top.pack(side="right")

        # Connectivity pill — updated by the background poll loop.
        # _net_pill_frame holds a coloured bg; _net_icon is the label inside it.
        net_text, pill_bg, pill_fg = self._net_icon_state()
        self._net_pill = tk.Frame(right_top, bg=pill_bg, padx=8, pady=2)
        self._net_pill.pack(side="left", padx=(0, 10), pady=6)
        self._net_icon = tk.Label(self._net_pill, text=net_text,
                                  font=FONT_BOLD, bg=pill_bg, fg=pill_fg)
        self._net_icon.pack()

        # Keep a ref to topbar so we can flash its bg when offline
        self._topbar = topbar

        tk.Label(right_top, text=user_name, font=FONT_SM,
                 bg=ACCENT, fg="#fcdede").pack(side="left", padx=(0, 8), pady=8)
        ttk.Button(right_top, text="Sign out", style="Ghost.TButton",
                   command=self._sign_out).pack(side="left", pady=4)

        # ── Status pill bar ───────────────────────────────────────────────────
        status_bar = tk.Frame(self, bg=ACCENT_BG, padx=16, pady=8)
        status_bar.pack(fill="x")
        status_bar.configure(highlightbackground=BORDER, highlightthickness=1)
        tk.Label(status_bar, text="Scanning into phase:",
                 font=FONT_SM, bg=ACCENT_BG, fg=TEXT_DIM).pack(side="left")
        tk.Label(status_bar, text=f"  {status_display}  ",
                 font=FONT_BOLD, bg=ACCENT, fg=WHITE,
                 padx=8, pady=3).pack(side="left", padx=8)
        self._pending_lbl = tk.Label(status_bar, text="",
                                     font=FONT_SM, bg=ACCENT_BG, fg=WARNING)
        self._pending_lbl.pack(side="right")

        # ── Scan input ────────────────────────────────────────────────────────
        scan_frame = tk.Frame(self, bg=BG, padx=20, pady=16)
        scan_frame.pack(fill="x")
        tk.Label(scan_frame, text="Scan barcode", font=FONT_BOLD,
                 bg=BG, fg=TEXT).pack(anchor="w", pady=(0, 6))
        input_row = tk.Frame(scan_frame, bg=BG)
        input_row.pack(fill="x")
        self._scan_var   = tk.StringVar()
        self._scan_entry = ttk.Entry(input_row, textvariable=self._scan_var,
                                     font=FONT_MONO, width=38)
        self._scan_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self._scan_entry.focus()
        ttk.Button(input_row, text="Submit", style="Accent.TButton",
                   command=self._submit_scan).pack(side="left", padx=(10, 0))
        self._scan_msg = tk.Label(scan_frame, text="", font=FONT_SM,
                                  bg=BG, fg=TEXT_DIM)
        self._scan_msg.pack(anchor="w", pady=(6, 0))
        self.bind("<Return>", lambda e: self._submit_scan())
        self.bind_all("<Key>", self._global_keypress)

        # ── Divider ───────────────────────────────────────────────────────────
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=20)

        # ── Log header ────────────────────────────────────────────────────────
        log_bar = tk.Frame(self, bg=BG, padx=20, pady=10)
        log_bar.pack(fill="x")
        tk.Label(log_bar, text="Scan log", font=FONT_BOLD,
                 bg=BG, fg=TEXT).pack(side="left")

        # ── Log table ─────────────────────────────────────────────────────────
        cols      = ("time", "batch_id", "shoe", "size", "qty", "result")
        container = tk.Frame(self, bg=BG, padx=20)
        container.pack(fill="both", expand=True, pady=(0, 20))

        self._tree = ttk.Treeview(container, columns=cols,
                                  show="headings", selectmode="none")
        self._tree.heading("time",     text="Time")
        self._tree.heading("batch_id", text="Batch ID")
        self._tree.heading("shoe",     text="Shoe")
        self._tree.heading("size",     text="Size")
        self._tree.heading("qty",      text="QTY")
        self._tree.heading("result",   text="")
        self._tree.column("time",     width=75,  anchor="w", stretch=False)
        self._tree.column("batch_id", width=130, anchor="w", stretch=False)
        self._tree.column("shoe",     width=260, anchor="w")
        self._tree.column("size",     width=80,  anchor="w", stretch=False)
        self._tree.column("qty",      width=60,  anchor="center", stretch=False)
        self._tree.column("result",   width=40,  anchor="center", stretch=False)

        sb = ttk.Scrollbar(container, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._tree.tag_configure("ok",      foreground=SUCCESS)
        self._tree.tag_configure("pending", foreground=WARNING)
        self._tree.tag_configure("fail",    foreground=DANGER)
        self._tree.tag_configure("update",  foreground=ACCENT)

        self._load_log()

        # Re-inject the debug banner if connectivity simulation is still active
        if DEBUG_CONNECTIVITY:
            self._show_debug_banner()

    # ── Log helpers ───────────────────────────────────────────────────────────
    RESULT_ICONS = {
        "ok":      "✓",
        "pending": "⟳",
        "fail":    "✗",
    }

    def _row_values(self, scan):
        icon = self.RESULT_ICONS.get(scan.get("result", "pending"), "⟳")
        return (
            scan["time"],
            scan.get("batch_id", scan.get("id", "")),
            scan.get("shoe", ""),
            scan.get("size", ""),
            scan.get("qty",  ""),
            icon,
        )

    def _load_log(self):
        pending_count = sum(1 for s in self.data.get("scans", []) if s.get("result") == "pending")
        log.debug(f"Loading scan log — {len(self.data.get('scans', []))} total, {pending_count} pending")
        for scan in reversed(self.data.get("scans", [])):
            self._insert_row(scan, prepend=False)
        # Also show update events in the log
        for upd in reversed(self.data.get("updates", [])):
            self._insert_update_row(upd, prepend=False)
        self._refresh_pending_label()

    def _insert_row(self, scan, prepend=True):
        tag = scan.get("result", "pending")
        pos = 0 if prepend else "end"
        self._tree.insert("", pos, iid=scan["id"],
                          values=self._row_values(scan), tags=(tag,))

    def _insert_update_row(self, upd, prepend=True):
        pos = 0 if prepend else "end"
        icon = "✓" if upd.get("result") == "ok" else "✗"
        self._tree.insert("", pos, iid=upd["id"],
                          values=(upd["time"], upd["barcode"], "— App update —",
                                  upd.get("commit", ""), "", icon),
                          tags=("update",))

    def _update_row(self, scan):
        tag = scan.get("result", "pending")
        try:
            self._tree.item(scan["id"], values=self._row_values(scan), tags=(tag,))
        except Exception:
            pass

    def _refresh_pending_label(self):
        n = sum(1 for s in self.data.get("scans", []) if s.get("result") == "pending")
        self._pending_lbl.config(text=f"⟳ {n} pending retry" if n else "")

    # ── Global key capture ────────────────────────────────────────────────────
    def _global_keypress(self, event):
        if event.widget is self._scan_entry:
            return
        if event.keysym == "Return":
            self._submit_scan()
            return
        if event.char and event.char.isprintable() and not event.state & 0x4:
            current = self._scan_var.get()
            self._scan_var.set(current + event.char)
            self._scan_entry.focus()
            self._scan_entry.icursor(tk.END)

    # ── Scan dispatch ─────────────────────────────────────────────────────────
    BARCODE_RE  = re.compile(r"^BAT_\d+$",         re.IGNORECASE)
    UPDATE_RE = re.compile(r"^UPD_[0-9a-f]{7,12}$", re.IGNORECASE)
    USER_RE     = re.compile(r"^USR_\d+$",          re.IGNORECASE)
    CONFIRM_RE  = re.compile(r"^YES$",       re.IGNORECASE)
    CANCEL_RE   = re.compile(r"^NO$",        re.IGNORECASE)
    SEL_RE      = re.compile(r"^SEL_(\d+)$",        re.IGNORECASE)

    def _submit_scan(self):
        raw = self._scan_var.get().strip()
        if not raw:
            return
        self._scan_var.set("")
        self._scan_entry.focus()

        if DEBUG_CON_RE.match(raw):
            self._toggle_debug_connectivity()
        elif raw.upper() == "IMPORT":
            self._open_import_screen()
        elif self.UPDATE_RE.match(raw):
            self._handle_update_barcode(raw)
        elif self.USER_RE.match(raw):
            self._handle_user_barcode(raw)
        elif self.CONFIRM_RE.match(raw):
            self._handle_user_confirm()
        elif self.CANCEL_RE.match(raw):
            self._handle_user_cancel()
        elif self.SEL_RE.match(raw):
            self._handle_import_select(raw)
        elif self.BARCODE_RE.match(raw):
            self._handle_batch_barcode(raw)
        elif NUMERIC_RE.match(raw) and self._user_allows_sbss():
            self._handle_sbss_barcode(raw)
        else:
            log.warning(f"Unrecognised barcode rejected: '{raw}'")
            self._scan_msg.config(
                text=f"Invalid barcode: {raw}", fg=DANGER)

    # ── Update barcode handling ───────────────────────────────────────────────
    def _handle_update_barcode(self, raw):
        normalised = raw.upper()

        # Duplicate check against stored updates
        already = next(
            (u for u in self.data.get("updates", [])
             if u.get("barcode", "").upper() == normalised),
            None
        )
        if already:
            log.warning(f"Update barcode already used: '{raw}' at {already['time']}")
            self._scan_msg.config(
                text=f"Update barcode already used (scanned at {already['time']})", fg=WARNING)
            return

        commit_hash = raw[4:].lower()  # strip UPD_
        log.info(f"Update barcode scanned — commit={commit_hash}")
        self._scan_msg.config(text=f"Updating to {commit_hash}…", fg=WARNING)

        # Record the attempt immediately
        upd = {
            "id":      f"UPD-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
            "time":    datetime.now().strftime("%H:%M:%S"),
            "barcode": raw,
            "commit":  commit_hash,
            "result":  "pending",
        }
        self.data.setdefault("updates", []).append(upd)
        save_data(self.data)
        self._insert_update_row(upd, prepend=True)

        def do_update():
            success, msg = git_checkout_and_pull(commit_hash)
            upd["result"] = "ok" if success else "fail"
            self._persist_update(upd)
            if success:
                log.info(f"Update succeeded: {msg} — restarting")
                self.after(0, lambda: self._scan_msg.config(
                    text=f"Updated to {commit_hash} — restarting…", fg=SUCCESS))
                self.after(1500, restart_app)
            else:
                log.error(f"Update failed: {msg}")
                self.after(0, lambda: self._scan_msg.config(
                    text=f"Update failed: {msg}", fg=DANGER))
            self.after(0, lambda u=upd: self._update_update_row(u))

        threading.Thread(target=do_update, daemon=True).start()

    def _persist_update(self, upd):
        for i, u in enumerate(self.data.get("updates", [])):
            if u["id"] == upd["id"]:
                self.data["updates"][i] = upd
                break
        save_data(self.data)

    def _update_update_row(self, upd):
        icon = "✓" if upd.get("result") == "ok" else "✗"
        tag  = "ok" if upd.get("result") == "ok" else "fail"
        try:
            self._tree.item(upd["id"],
                values=(upd["time"], upd["barcode"], "— App update —",
                        upd.get("commit", ""), "", icon),
                tags=(tag,))
        except Exception:
            pass

    # ── Batch barcode handling ────────────────────────────────────────────────
    def _handle_batch_barcode(self, raw):
        normalised = raw.upper()

        current_user = (self.data.get("session") or {}).get("user_name", "")
        already = next(
            (s for s in self.data.get("scans", [])
             if s.get("batch_id", "").upper() == normalised
             and s.get("user_name") == current_user),
            None
        )
        if already:
            log.warning(f"Duplicate batch barcode rejected: '{raw}' already scanned at {already['time']}")
            self._scan_msg.config(
                text=f"Already scanned: {raw}  (scanned at {already['time']})", fg=WARNING)
            return

        if not self.user_status:
            log.warning("Scan attempted but user has no PhaseToEnter value")
            self._scan_msg.config(
                text="No PhaseToEnter value on your account.", fg=DANGER)
            return

        entity_id    = normalised[4:].lstrip("0") or "0"
        status_label = self.user_status_label or self.user_status
        log.debug(f"Batch barcode '{raw}' → entity_id='{entity_id}'")

        now  = datetime.now()
        scan = {
            "id":           f"{now.strftime('%Y%m%d%H%M%S%f')}-{entity_id}",
            "date":         now.strftime("%Y-%m-%d"),
            "time":         now.strftime("%H:%M:%S"),
            "user_name":    (self.data.get("session") or {}).get("user_name", ""),
            "batch_id":     raw,
            "entity_id":    entity_id,
            "status_label": status_label,
            "status_value": self.user_status,
            "shoe":         "…",
            "size":         "…",
            "qty":          "…",
            "result":       "pending",
            "attempts":     0,
        }

        log.info(f"Scan queued — batch={raw}  entity={entity_id}  phase={self.user_status}  label={status_label}")
        self.data.setdefault("scans", []).append(scan)
        save_data(self.data)
        self._insert_row(scan, prepend=True)
        self._refresh_pending_label()
        self._scan_msg.config(text=f"Queued: {raw}", fg=TEXT_DIM)

        threading.Thread(target=self._process_scan, args=(scan,), daemon=True).start()

    def _process_scan(self, scan):
        entity_id = scan["entity_id"]

        log.debug(f"Verifying entity exists: entity_id={entity_id}")
        exists_check = self.api.get_entity_property(entity_id, [["id", "view", "id"]])
        log.debug(f"Entity existence check response: {exists_check}")
        if exists_check is None or exists_check is False:
            log.warning(f"Entity check inconclusive (offline?) for entity_id={entity_id} — proceeding anyway")
        elif not exists_check.get("id"):
            log.error(f"Entity not found on system: entity_id={entity_id}")
            scan["result"] = "fail"
            scan["shoe"] = scan["size"] = scan["qty"] = "—"
            self._persist_scan(scan)
            self.after(0, lambda s=scan: self._update_row(s))
            self.after(0, self._refresh_pending_label)
            self.after(0, lambda: self._scan_msg.config(
                text=f"Entity not found: {scan['batch_id']}", fg=DANGER))
            return
        else:
            log.info(f"Entity confirmed — id={exists_check['id']}")

        log.debug(f"Fetching shoe info for entity={entity_id}")
        props = self.api.get_entity_property(entity_id, [
            ["BatchSuborderShoe", "view"],
            ["BatchSuborderSize", "view"],
            ["BatchQty", "view"],
        ])
        log.debug(f"Shoe info response: {props}")
        if props:
            scan["shoe"] = props.get("BatchSuborderShoe") or "—"
            scan["size"] = props.get("BatchSuborderSize") or "—"
            scan["qty"]  = props.get("BatchQty")          or "—"
            log.info(f"Shoe info — shoe='{scan['shoe']}'  size='{scan['size']}'  qty='{scan['qty']}'")
        else:
            scan["shoe"] = scan["size"] = scan["qty"] = "—"
            log.warning(f"Could not fetch shoe info for entity={entity_id}")

        self.after(0, lambda s=scan: self._update_row(s))
        self._push_scan(scan)

    def _push_scan(self, scan):
        scan["attempts"] = scan.get("attempts", 0) + 1
        print(scan)
        api_value  = scan.get("status_value") or scan.get("status_label")
        phase_str  = str(scan.get("status_value") or "").strip()

        props = {}
        if phase_str in ("299", "301"):
            props["BatchUppersProductionPhase"] = api_value
            if phase_str == "299":
                props["UnitProductionPhase"] =  "303"
            if phase_str == "301":
                props["UnitProductionPhase"] =  "304"
        elif phase_str in ("302", "300"):
            props["BatchBottomsProductionPhase"] =  api_value
            if phase_str == "300":
                props["UnitProductionPhase"] =  "303"
            if phase_str == "302":
                props["UnitProductionPhase"] =  "304"
        else:
            props["UnitProductionPhase"] =  api_value

        if phase_str == "95":
            props["BatchUppersProductionPhase"] = ""
            props["BatchBottomsProductionPhase"] = ""
            props["UnitProductionPhase"] =  "95"

        log.debug(
            f"Pushing update — entity={scan['entity_id']}  "
            f"phase_value={api_value}  prop={props}  attempt={scan['attempts']}"
        )
        result = self.api.update_entity_property(
            scan["entity_id"],
            props
        )
        log.debug(f"update_entity_property response: {result}")

        if result is not None:
            scan["result"] = "ok"
            msg, col = f"✓ {scan['batch_id']} updated", SUCCESS
            log.info(f"Update succeeded — entity={scan['entity_id']}")
        else:
            scan["result"] = "pending"
            msg, col = f"✗ {scan['batch_id']} failed — will retry", DANGER
            log.warning(f"Update failed — entity={scan['entity_id']}  will retry in {RETRY_INTERVAL}s")

        self._persist_scan(scan)
        self.after(0, lambda s=scan: self._update_row(s))
        self.after(0, self._refresh_pending_label)
        self.after(0, lambda m=msg, c=col: self._scan_msg.config(text=m, fg=c))

    # ── Shoe-by-shoe scanning ────────────────────────────────────────────────
    def _user_allows_sbss(self):
        """Return True if the currently logged-in user has allowShoeByShoeScanning set."""
        user_name = (self.data.get("session") or {}).get("user_name", "")
        user = next((u for u in self.settings.get("users", []) if u["name"] == user_name), {})
        return bool(user.get("allowShoeByShoeScanning"))

    def _handle_sbss_barcode(self, raw):
        log.info(f"Shoe-by-shoe barcode scanned: '{raw}'")
        self._scan_msg.config(text=f"Looking up {raw}…", fg=TEXT_DIM)
        threading.Thread(target=self._process_sbss_scan, args=(raw,), daemon=True).start()

    def _process_sbss_scan(self, barcode):
        now = datetime.now()
        scan = {
            "id":        f"{now.strftime('%Y%m%d%H%M%S%f')}-SBSS-{barcode}",
            "type":      "sbss",
            "date":      now.strftime("%Y-%m-%d"),
            "time":      now.strftime("%H:%M:%S"),
            "user_name": (self.data.get("session") or {}).get("user_name", ""),
            "batch_id":  barcode,
            "entity_id": "",
            "shoe":      "shoe-by-shoe",
            "size":      "",
            "qty":       "",
            "result":    "pending",
            "attempts":  0,
        }
        self.data.setdefault("scans", []).append(scan)
        save_data(self.data)
        self.after(0, lambda s=scan: self._insert_row(s, prepend=True))
        self.after(0, self._refresh_pending_label)
        self._do_sbss_attempt(scan)

    def _do_sbss_attempt(self, scan):
        """Resolve entity list and increment the first entity with room. Safe to call on retry."""
        barcode = scan["batch_id"]
        scan["attempts"] = scan.get("attempts", 0) + 1
        log.debug(f"SBSS: attempt {scan['attempts']} for barcode='{barcode}'")

        # Step 1 — resolve entity ID(s)
        response = self.api.get_entity_id_from_unique_property_value(
            SBSS_SETGROUP_ID, SBSS_BARCODE_PROP, barcode)
        log.debug(f"SBSS: get_entity_id response={response}")

        if not response:
            log.warning(f"SBSS: lookup failed for '{barcode}' — will retry")
            scan["result"] = "pending"
            self._persist_scan(scan)
            self.after(0, lambda s=scan: self._update_row(s))
            self.after(0, self._refresh_pending_label)
            self.after(0, lambda: self._scan_msg.config(
                text=f"⟳ {barcode} — lookup failed, will retry", fg=WARNING))
            return

        if isinstance(response, dict) and "list" in response:
            entity_ids = [str(e).strip() for e in response["list"] if str(e).strip()]
        elif isinstance(response, dict):
            eid = str(response.get("id") or response.get("entityID") or "").strip()
            entity_ids = [eid] if eid else []
        else:
            entity_ids = [str(response).strip()]
        entity_ids = [e for e in entity_ids if e]

        if not entity_ids:
            log.error(f"SBSS: could not resolve any entity_id for barcode '{barcode}'")
            scan["result"] = "fail"
            self._persist_scan(scan)
            self.after(0, lambda s=scan: self._update_row(s))
            self.after(0, self._refresh_pending_label)
            self.after(0, lambda: self._scan_msg.config(
                text=f"Could not resolve entity for: {barcode}", fg=DANGER))
            return

        log.info(f"SBSS: resolved entity_ids={entity_ids}")

        # Step 2 — walk entities; increment the first one with room
        for entity_id in entity_ids:
            props = self.api.get_entity_property(
                entity_id, [[SBSS_COUNT_PROP, "view"], ["OrderLineQty", "view"]])
            log.debug(f"SBSS: props for entity={entity_id}: {props}")

            if props is False or props is None:
                log.warning(f"SBSS: could not fetch props for entity={entity_id}, skipping")
                continue

            try:
                current_count = int(props.get(SBSS_COUNT_PROP) or 0)
            except (ValueError, TypeError):
                current_count = 0
            try:
                line_qty = int(props.get("OrderLineQty") or 0)
            except (ValueError, TypeError):
                line_qty = 0

            log.info(f"SBSS: entity={entity_id}  count={current_count}  qty={line_qty}")

            if line_qty > 0 and current_count >= line_qty:
                log.info(f"SBSS: entity={entity_id} full ({current_count}/{line_qty}), moving on")
                continue

            # This entity has room — increment
            new_count = current_count + 1
            result = self.api.update_entity_property(
                entity_id, {SBSS_COUNT_PROP: str(new_count)})
            log.debug(f"SBSS: update_entity_property response={result}")

            if result is not None:
                scan["result"]    = "ok"
                scan["entity_id"] = entity_id
                scan["qty"]       = f"{new_count}/{line_qty}"
                self._persist_scan(scan)
                self.after(0, lambda s=scan: self._update_row(s))
                self.after(0, self._refresh_pending_label)
                self.after(0, lambda b=barcode, n=new_count, q=line_qty: self._scan_msg.config(
                    text=f"✓ {b} — {n}/{q}", fg=SUCCESS))
                log.info(f"SBSS: update succeeded — entity={entity_id}  {new_count}/{line_qty}")
            else:
                scan["result"] = "pending"
                self._persist_scan(scan)
                self.after(0, lambda s=scan: self._update_row(s))
                self.after(0, self._refresh_pending_label)
                self.after(0, lambda b=barcode: self._scan_msg.config(
                    text=f"⟳ {b} — update failed, will retry", fg=WARNING))
                log.warning(f"SBSS: update failed — entity={entity_id}  will retry in {RETRY_INTERVAL}s")
            return

        # All entities full — nothing left to do, mark done
        scan["result"] = "ok"
        self._persist_scan(scan)
        self.after(0, lambda s=scan: self._update_row(s))
        self.after(0, self._refresh_pending_label)
        log.warning(f"SBSS: all entities full for barcode '{barcode}'")
        self.after(0, lambda b=barcode: self._scan_msg.config(
            text=f"All orders full for barcode: {b}", fg=WARNING))

    def _persist_scan(self, updated):
        for i, s in enumerate(self.data.get("scans", [])):
            if s["id"] == updated["id"]:
                self.data["scans"][i] = updated
                break
        save_data(self.data)

    # ── Retry loop ────────────────────────────────────────────────────────────
    def _start_retry_loop(self):
        log.debug(f"Retry loop started — interval={RETRY_INTERVAL}s")
        def loop():
            while self.running:
                time.sleep(RETRY_INTERVAL)
                if not self.running or not self.api:
                    continue
                pending = [s for s in self.data.get("scans", [])
                           if s.get("result") == "pending"]
                if pending:
                    log.info(f"Retry loop — retrying {len(pending)} pending scan(s)")
                    for scan in pending:
                        if scan.get("type") == "sbss":
                            self._do_sbss_attempt(scan)
                        else:
                            self._push_scan(scan)
                else:
                    log.debug("Retry loop — no pending scans")

        threading.Thread(target=loop, daemon=True).start()

    # ── User-switch barcode handling ──────────────────────────────────────────
    def _handle_user_barcode(self, raw):
        user_id = raw[4:]  # strip USR_
        # Refresh user list so we always have the latest tokens / names
        if self.api:
            fetch_users_from_api(self.api, self.settings)
        user = next((u for u in self.settings["users"] if str(u.get("id", "")) == user_id), None)
        if not user:
            log.warning(f"USR barcode '{raw}' — no user with id={user_id} in settings")
            self._scan_msg.config(text=f"Unknown user ID: {user_id}", fg=DANGER)
            return

        current_name = (self.data.get("session") or {}).get("user_name", "")
        if user["name"] == current_name:
            log.info(f"USR barcode scanned for already-logged-in user '{user['name']}' — ignoring")
            self._scan_msg.config(text=f"Already signed in as {user['name']}", fg=TEXT_DIM)
            return

        log.info(f"USR barcode — requesting switch to '{user['name']}'")
        self._pending_user_switch = user
        self._show_user_confirm_overlay(user)

    def _show_user_confirm_overlay(self, user):
        """Show a full-screen overlay with CONFIRM/CANCEL barcodes rendered as images."""
        try:
            import barcode as bc
            from barcode.writer import ImageWriter
            from PIL import Image, ImageTk
            import io
            has_barcode_lib = True
        except ImportError:
            has_barcode_lib = False

        # Dim overlay frame over the whole window
        overlay = tk.Frame(self, bg=GRAY_800)
        overlay.place(x=0, y=0, relwidth=1, relheight=1)
        self._user_confirm_overlay = overlay

        tk.Frame(overlay, bg=ACCENT, height=4).pack(fill="x")

        inner = tk.Frame(overlay, bg=GRAY_800, padx=40, pady=32)
        inner.pack(expand=True)

        tk.Label(inner, text="Switch user?", font=FONT_LARGE,
                 bg=GRAY_800, fg=WHITE).pack(pady=(0, 6))
        tk.Label(inner,
                 text=f"Scan CONFIRM to sign in as  {user['name']}\nor CANCEL to stay logged in.",
                 font=FONT, bg=GRAY_800, fg=GRAY_400,
                 justify="center").pack(pady=(0, 28))

        barcodes_row = tk.Frame(inner, bg=GRAY_800)
        barcodes_row.pack()

        def make_barcode_image(value, label_text, parent, bg):
            col = tk.Frame(parent, bg=bg, padx=20)
            col.pack(side="left", padx=16)
            tk.Label(col, text=label_text, font=FONT_BOLD,
                     bg=bg, fg=WHITE).pack(pady=(0, 8))
            if has_barcode_lib:
                try:
                    buf = io.BytesIO()
                    code = bc.get("code128", value, writer=ImageWriter())
                    code.write(buf, options={
                        "module_height": 18,
                        "text_distance": 4,
                        "font_size": 11,
                        "quiet_zone": 6,
                        "write_text": True,
                    })
                    buf.seek(0)
                    img = Image.open(buf).convert("RGB")
                    img = img.resize((260, 100), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    lbl = tk.Label(col, image=photo, bg=bg)
                    lbl.image = photo  # keep reference
                    lbl.pack()
                    return col
                except Exception as e:
                    log.warning(f"Barcode render failed for {value}: {e}")
            # Fallback: just show the text value
            tk.Label(col, text=value, font=FONT_MONO,
                     bg=bg, fg=WHITE,
                     relief="solid", padx=12, pady=10).pack()
            return col

        make_barcode_image("YES", "✓  CONFIRM", barcodes_row, SUCCESS)
        make_barcode_image("NO",  "✗  CANCEL",  barcodes_row, DANGER)

        log.debug("User-switch confirmation overlay shown")

    def _dismiss_user_confirm_overlay(self):
        if hasattr(self, "_user_confirm_overlay") and self._user_confirm_overlay:
            try:
                self._user_confirm_overlay.destroy()
            except Exception:
                pass
            self._user_confirm_overlay = None
        self._pending_user_switch = None


    def _handle_user_cancel(self):
        if not self._pending_user_switch:
            log.debug("USRCANCEL scanned but no pending switch — ignoring")
            return
        cancelled_name = self._pending_user_switch["name"]
        self._dismiss_user_confirm_overlay()
        log.info(f"User switch cancelled — staying as current user")
        self._scan_msg.config(text=f"Switch to {cancelled_name} cancelled", fg=TEXT_DIM)

    # ── Global IMPORT intercept (works on any screen) ────────────────────────
    def _global_import_intercept(self, event):
        """Accumulate keypresses globally; trigger import screen on 'IMPORT\n'."""
        if not event.char or not event.char.isprintable():
            if event.keysym == "Return" and self._import_buffer.upper() == "IMPORT":
                self._import_buffer = ""
                self._open_import_screen()
                return
            if event.keysym == "Return":
                self._import_buffer = ""
            return
        self._import_buffer += event.char
        # Keep buffer short — IMPORT is 6 chars
        if len(self._import_buffer) > 8:
            self._import_buffer = self._import_buffer[-8:]

    # ── Import screen ──────────────────────────────────────────────────────────
    def _open_import_screen(self):
        log.info("Opening import screen")
        self._import_files       = []   # list of file dicts from server
        self._import_selected    = None # index into _import_files
        self._import_confirming  = False

        self._clear()
        self.geometry("820x620")
        self.unbind("<Return>")

        # ── Top bar ───────────────────────────────────────────────────────────
        topbar = tk.Frame(self, bg=ACCENT, padx=16, pady=0)
        topbar.pack(fill="x")
        tk.Label(topbar, text="LABORA", font=(_UI_FONT, 11, "bold"),
                 bg=ACCENT, fg=WHITE).pack(side="left", pady=8)
        tk.Label(topbar, text=" · Restore from backup", font=(_UI_FONT, 10),
                 bg=ACCENT, fg="#f0a0a0").pack(side="left", pady=8)
        ttk.Button(topbar, text="← Back", style="Ghost.TButton",
                   command=self._import_exit).pack(side="right", pady=4)

        # ── Status / instruction bar ──────────────────────────────────────────
        self._import_info_bar = tk.Frame(self, bg=ACCENT_BG, padx=16, pady=8)
        self._import_info_bar.pack(fill="x")
        self._import_info_bar.configure(highlightbackground=BORDER, highlightthickness=1)
        self._import_status_lbl = tk.Label(
            self._import_info_bar,
            text="Fetching file list from server…",
            font=FONT_SM, bg=ACCENT_BG, fg=TEXT_DIM)
        self._import_status_lbl.pack(side="left")

        # ── File list table ───────────────────────────────────────────────────
        cols      = ("sel", "filename", "date", "size")
        container = tk.Frame(self, bg=BG, padx=20, pady=12)
        container.pack(fill="both", expand=True)

        self._import_tree = ttk.Treeview(container, columns=cols,
                                         show="headings", selectmode="none")
        self._import_tree.heading("sel",      text="Scan")
        self._import_tree.heading("filename", text="Filename")
        self._import_tree.heading("date",     text="Date")
        self._import_tree.heading("size",     text="Size")
        self._import_tree.column("sel",      width=70,  anchor="center", stretch=False)
        self._import_tree.column("filename", width=400, anchor="w")
        self._import_tree.column("date",     width=100, anchor="center", stretch=False)
        self._import_tree.column("size",     width=90,  anchor="e",      stretch=False)

        sb = ttk.Scrollbar(container, orient="vertical",
                           command=self._import_tree.yview)
        self._import_tree.configure(yscrollcommand=sb.set)
        self._import_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._import_tree.tag_configure("selected", foreground=ACCENT,
                                        background=ACCENT_BG)

        # ── Confirmation panel (hidden until a file is selected) ──────────────
        self._import_confirm_frame = tk.Frame(self, bg=SURFACE, padx=20, pady=14)
        self._import_confirm_frame.configure(
            highlightbackground=BORDER, highlightthickness=1)
        # Don't pack yet — shown after selection

        tk.Label(self._import_confirm_frame,
                 text="Confirm import?", font=FONT_BOLD,
                 bg=SURFACE, fg=TEXT).pack(anchor="w")
        self._import_confirm_lbl = tk.Label(
            self._import_confirm_frame, text="",
            font=FONT_SM, bg=SURFACE, fg=TEXT_DIM, justify="left")
        self._import_confirm_lbl.pack(anchor="w", pady=(4, 10))

        btn_row = tk.Frame(self._import_confirm_frame, bg=SURFACE)
        btn_row.pack(anchor="w")
        ttk.Button(btn_row, text="✓  YES — import",
                   style="Accent.TButton",
                   command=self._import_do_import).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="✗  NO — go back",
                   style="Ghost.TButton",
                   command=self._import_deselect).pack(side="left")
        tk.Label(self._import_confirm_frame,
                 text="Or scan  YES  /  NO",
                 font=FONT_XS, bg=SURFACE, fg=TEXT_MUTED).pack(anchor="w", pady=(8, 0))

        # Bind Return for YES/NO during confirmation
        self.bind("<Return>", lambda e: self._submit_scan())
        self._import_scan_var   = tk.StringVar()
        self._scan_var          = self._import_scan_var   # reuse global submit
        self._import_scan_entry = tk.Entry(self, textvariable=self._import_scan_var)
        # Hidden entry still captures barcode scanner input
        self._import_scan_entry.place(x=-500, y=-500)
        self._import_scan_entry.focus()
        self.bind_all("<Key>", self._import_key_capture)

        # Fetch file list in background
        threading.Thread(target=self._import_fetch_files, daemon=True).start()

    def _import_key_capture(self, event):
        """Route keystrokes into the hidden entry on the import screen."""
        if event.keysym == "Return":
            self._import_handle_barcode(self._import_scan_var.get().strip())
            self._import_scan_var.set("")
            return
        if event.char and event.char.isprintable() and not event.state & 0x4:
            self._import_scan_var.set(self._import_scan_var.get() + event.char)

    def _import_handle_barcode(self, raw):
        if not raw:
            return
        log.debug(f"Import screen barcode: '{raw}'")
        upper = raw.upper()
        if upper == "EXIT" or upper == "IMPORT":
            self._import_exit()
        elif upper == "YES":
            self._import_do_import()
        elif upper == "NO":
            if self._import_confirming:
                self._import_deselect()
            else:
                self._import_exit()
        else:
            m = SEL_RE.match(raw)
            if m:
                self._import_select(int(m.group(1)))

    def _import_fetch_files(self):
        files = sftp_list_daily_files(self.settings)
        self.after(0, lambda: self._import_populate(files))

    def _import_populate(self, files):
        self._import_files = files
        self._import_tree.delete(*self._import_tree.get_children())

        if not files:
            self._import_status_lbl.config(
                text="No files found on server for this instance. Check SFTP config.",
                fg=DANGER)
            return

        for i, f in enumerate(files, start=1):
            date_display = (f["date_str"][:4] + "-" + f["date_str"][4:6] + "-" +
                            f["date_str"][6:]) if len(f["date_str"]) == 8 else f["date_str"]
            size_kb = f"{f['size'] / 1024:.1f} KB" if f.get("size") else "?"
            self._import_tree.insert("", "end", iid=str(i),
                values=(f"SEL_{i}", f["name"], date_display, size_kb))

        self._import_status_lbl.config(
            text=f"{len(files)} file(s) found — scan SEL_N to select, EXIT to go back.",
            fg=TEXT_DIM)

    def _import_select(self, n):
        if n < 1 or n > len(self._import_files):
            self._import_status_lbl.config(
                text=f"No file #{n} — choose between 1 and {len(self._import_files)}",
                fg=DANGER)
            return
        self._import_selected   = n - 1   # 0-indexed
        self._import_confirming = True
        f = self._import_files[self._import_selected]

        # Highlight row
        for iid in self._import_tree.get_children():
            self._import_tree.item(iid, tags=())
        self._import_tree.item(str(n), tags=("selected",))
        self._import_tree.see(str(n))

        date_display = (f["date_str"][:4] + "-" + f["date_str"][4:6] + "-" +
                        f["date_str"][6:]) if len(f["date_str"]) == 8 else f["date_str"]
        existing = len(self.data.get("scans", []))
        self._import_confirm_lbl.config(
            text=f"File:  {f['name']}\nDate:  {date_display}\n"
                 f"Current scans in memory:  {existing}\n"
                 f"Duplicates will be skipped.")
        self._import_confirm_frame.pack(fill="x", padx=20, pady=(0, 16))
        self._import_status_lbl.config(
            text="Scan YES to confirm import, NO to go back to the list.",
            fg=WARNING)
        log.info(f"Import: file #{n} selected — {f['name']}")

    def _import_deselect(self):
        self._import_selected   = None
        self._import_confirming = False
        self._import_confirm_frame.pack_forget()
        for iid in self._import_tree.get_children():
            self._import_tree.item(iid, tags=())
        self._import_status_lbl.config(
            text=f"{len(self._import_files)} file(s) — scan SEL_N to select, EXIT to go back.",
            fg=TEXT_DIM)

    def _import_do_import(self):
        if self._import_selected is None:
            return
        f = self._import_files[self._import_selected]
        log.info(f"Import confirmed — downloading {f['remote_path']}")
        self._import_status_lbl.config(text="Downloading…", fg=WARNING)
        self._import_confirm_frame.pack_forget()
        self.update()

        def do_download():
            rows = sftp_download_csv(self.settings, f["remote_path"])
            if not rows:
                self.after(0, lambda: self._import_status_lbl.config(
                    text="Download failed or file is empty.", fg=DANGER))
                return

            # Merge — skip duplicates by id
            existing_ids = {s["id"] for s in self.data.get("scans", [])}
            added = 0
            for row in rows:
                rid = row.get("id", "").strip()
                if not rid or rid in existing_ids:
                    continue
                # Normalise row into a scan dict
                scan = {
                    "id":           rid,
                    "date":         row.get("date", ""),
                    "time":         row.get("time", ""),
                    "user_name":    row.get("user_name", ""),
                    "batch_id":     row.get("batch_id", ""),
                    "entity_id":    row.get("entity_id", ""),
                    "shoe":         row.get("shoe", ""),
                    "size":         row.get("size", ""),
                    "qty":          row.get("qty", ""),
                    "status_label": row.get("status_label", ""),
                    "status_value": row.get("status_value", ""),
                    "result":       row.get("result", ""),
                    "attempts":     int(row.get("attempts") or 0),
                }
                self.data.setdefault("scans", []).append(scan)
                existing_ids.add(rid)
                added += 1

            save_data(self.data)
            log.info(f"Import complete — {added} new scan(s) added, "
                     f"{len(rows) - added} duplicate(s) skipped")

            def finish():
                self._import_exit(
                    status=f"✓ Imported {added} scan(s) — {len(rows)-added} duplicate(s) skipped.")
            self.after(0, finish)

        threading.Thread(target=do_download, daemon=True).start()

    def _import_exit(self, status=None):
        log.info("Leaving import screen")
        self.unbind_all("<Key>")
        self.bind_all("<Key>", self._global_import_intercept)
        # Return to wherever we came from
        if self.data.get("session") and self.api:
            self._show_main()
            if status:
                self._scan_msg.config(text=status, fg=SUCCESS)
        else:
            self._show_login()

    # ── Hourly sync loop ──────────────────────────────────────────────────────
    def _start_sync_loop(self):
        log.debug(f"Sync loop started — interval={SYNC_INTERVAL}s")
        def loop():
            if self.running and self.settings:
                try:
                    run_hourly_sync(self.data, self.settings)
                except Exception as e:
                    log.error(f"Sync loop initial run failed: {e}")
            while self.running:
                time.sleep(SYNC_INTERVAL)
                if not self.running:
                    break
                log.info("Sync loop — running hourly sync")
                try:
                    run_hourly_sync(self.data, self.settings)
                except Exception as e:
                    log.error(f"Sync loop error: {e}")
        threading.Thread(target=loop, daemon=True).start()

    # ── Export overlay ─────────────────────────────────────────────────────────
    def _show_export_overlay(self, message="Working…"):
        if not getattr(self, "_export_overlay", None):
            overlay = tk.Frame(self, bg=GRAY_800)
            overlay.place(x=0, y=0, relwidth=1, relheight=1)
            overlay.lift()
            self._export_overlay = overlay
            tk.Frame(overlay, bg=ACCENT, height=4).pack(fill="x")
            inner = tk.Frame(overlay, bg=GRAY_800, padx=40, pady=40)
            inner.pack(expand=True)
            tk.Label(inner, text="LABORA", font=(_UI_FONT, 13, "bold"),
                     bg=GRAY_800, fg=ACCENT).pack()
            self._export_status_lbl = tk.Label(
                inner, text=message, font=FONT, bg=GRAY_800, fg=GRAY_400)
            self._export_status_lbl.pack(pady=(20, 0))
        else:
            try:
                self._export_status_lbl.config(text=message)
            except Exception:
                pass
        self.update()

    def _dismiss_export_overlay(self):
        if getattr(self, "_export_overlay", None):
            try:
                self._export_overlay.destroy()
            except Exception:
                pass
            self._export_overlay = None

    # ── Connectivity indicator ─────────────────────────────────────────────────
    NET_POLL_INTERVAL = 10   # seconds between liveness checks

    def _net_icon_state(self):
        """Return (text, pill_bg, pill_fg) for the current connectivity state."""
        if DEBUG_CONNECTIVITY:
            return "⚠ NO CONNECTION (debug)", WARNING, WHITE
        if self._net_online is True:
            return "● online", SUCCESS, WHITE
        if self._net_online is False:
            return "⚠ NO CONNECTION", DANGER, WHITE
        return "● …", GRAY_600, WHITE

    def _refresh_net_icon(self):
        """Push current state into the pill and topbar background (main-thread only)."""
        if not self._net_icon or not self._net_pill:
            return
        try:
            text, pill_bg, pill_fg = self._net_icon_state()
            self._net_pill.config(bg=pill_bg)
            self._net_icon.config(text=text, bg=pill_bg, fg=pill_fg)
            # Flash the entire topbar amber when offline so it's unmissable
            offline = (self._net_online is False) or DEBUG_CONNECTIVITY
            bar_bg  = WARNING if offline else ACCENT
            if self._topbar:
                self._topbar.config(bg=bar_bg)
                for child in self._topbar.winfo_children():
                    try:
                        child.config(bg=bar_bg)
                        for gc in child.winfo_children():
                            try:
                                # Don't recolour the pill itself
                                if gc is not self._net_pill and gc is not self._net_icon:
                                    gc.config(bg=bar_bg)
                            except Exception:
                                pass
                    except Exception:
                        pass
        except Exception:
            pass

    def _start_connectivity_poll(self):
        """Spawn a daemon thread that pings the baseurl every NET_POLL_INTERVAL seconds."""
        def poll():
            while self.running:
                self._check_connectivity()
                time.sleep(self.NET_POLL_INTERVAL)
        threading.Thread(target=poll, daemon=True).start()

    def _check_connectivity(self):
        """Probe baseurl with a short GET; update _net_online and the icon."""
        if DEBUG_CONNECTIVITY:
            online = False
        else:
            try:
                import requests as _req
                baseurl = (self.settings or {}).get("baseurl", "")
                if not baseurl:
                    online = False
                else:
                    r = _req.get(baseurl, timeout=5)
                    online = r.status_code < 500
            except Exception:
                online = False

        changed = (online != self._net_online)
        self._net_online = online
        if changed or True:   # always refresh so icon appears on first paint
            self.after(0, self._refresh_net_icon)

    # ── Connectivity debug mode ────────────────────────────────────────────────
    def _toggle_debug_connectivity(self):
        global DEBUG_CONNECTIVITY
        DEBUG_CONNECTIVITY = not DEBUG_CONNECTIVITY
        state = "ENABLED" if DEBUG_CONNECTIVITY else "DISABLED"
        log.warning(f"[DEBUG_CON] Connectivity simulation {state}")

        # Immediately re-probe (or simulate) so the icon reflects the new state
        threading.Thread(target=self._check_connectivity, daemon=True).start()

        if DEBUG_CONNECTIVITY:
            self._show_debug_banner()
            self._scan_msg.config(
                text="⚠ Connectivity debug ON — all API calls are now failing",
                fg=WARNING)
        else:
            self._hide_debug_banner()
            self._scan_msg.config(
                text="✓ Connectivity debug OFF — normal operation resumed",
                fg=SUCCESS)

    def _show_debug_banner(self):
        """Inject a persistent warning banner just below the topbar."""
        if getattr(self, "_debug_banner", None):
            return  # already visible
        # Place the banner as the second widget (after the topbar)
        banner = tk.Frame(self, bg=WARNING, padx=16, pady=6)
        tk.Label(
            banner,
            text="⚠  CONNECTIVITY DEBUG MODE ACTIVE — all API calls are being blocked  ⚠",
            font=FONT_BOLD,
            bg=WARNING,
            fg=WHITE,
        ).pack(side="left")
        tk.Label(
            banner,
            text="Scan DEBUG_CON to disable",
            font=FONT_SM,
            bg=WARNING,
            fg="#fff8e1",
        ).pack(side="right")

        # Insert after the topbar (index 1 in the pack order)
        children = self.winfo_children()
        topbar   = children[0] if children else None
        banner.pack(fill="x", after=topbar) if topbar else banner.pack(fill="x")
        self._debug_banner = banner

    def _hide_debug_banner(self):
        if getattr(self, "_debug_banner", None):
            try:
                self._debug_banner.destroy()
            except Exception:
                pass
            self._debug_banner = None

    # ── Sign out ───────────────────────────────────────────────────────────────
    def _sign_out(self):
        user_name = (self.data.get("session") or {}).get("user_name", "user")
        scans     = len(self.data.get("scans", []))
        if not messagebox.askyesno(
            "Sign out",
            f"Sign out as {user_name}?\n\n"
            f"A snapshot of {scans} scan(s) will be backed up.\n"
            f"All scan history remains saved."
        ):
            return

        log.info(f"User signing out: {user_name}")
        self.unbind("<Return>")
        export_data = dict(self.data)

        def do_export():
            def progress(msg):
                self.after(0, lambda m=msg: self._show_export_overlay(m))
            self.after(0, lambda: self._show_export_overlay("Saving snapshot…"))
            export_session_snapshot(export_data, self.settings, progress_cb=progress)
            self.data["session"] = None
            save_data(self.data)
            self.api               = None
            self.user_status       = None
            self.user_status_label = None
            self.after(0, lambda: (self._dismiss_export_overlay(), self._show_login()))

        threading.Thread(target=do_export, daemon=True).start()

    # ── User-switch confirm ────────────────────────────────────────────────────
    def _handle_user_confirm(self):
        if not self._pending_user_switch:
            log.debug("USRCONFIRM scanned but no pending switch — ignoring")
            return
        user    = self._pending_user_switch
        old_api = self.api
        self._dismiss_user_confirm_overlay()
        log.info(f"User switch confirmed — logging in as '{user['name']}'")

        export_data            = dict(self.data)
        self.api               = None
        self.user_status       = None
        self.user_status_label = None

        def do_switch():
            def progress(msg):
                self.after(0, lambda m=msg: self._show_export_overlay(m))
            self.after(0, lambda: self._show_export_overlay(
                f"Saving snapshot before switching to {user['name']}…"))
            export_session_snapshot(export_data, self.settings, progress_cb=progress)
            self.data["session"] = None
            save_data(self.data)

            self.after(0, lambda: self._show_export_overlay(
                f"Signing in as {user['name']}…"))
            if old_api:
                fetch_users_from_api(old_api, self.settings)
            fresh_user = next(
                (u for u in self.settings["users"]
                 if str(u.get("id", "")) == str(user.get("id", ""))),
                user
            )
            api    = UniAPI(self.settings["baseurl"], token=fresh_user["token"])
            detail = api.flogin(fresh_user["token"])
            if detail:
                log.info(f"Switch login succeeded — entityID={api.entityID}")
                self.api = api
                props = api.get_entity_property(api.entityID, [
                    "PhaseToEnter",
                    ["PhaseToEnter", "view", "PhaseToEnterName"]
                ])
                status       = (props or {}).get("PhaseToEnter")
                status_label = (props or {}).get("PhaseToEnterName") or status
                def finish_ok(n=fresh_user["name"], d=detail, s=status, sl=status_label):
                    self._dismiss_export_overlay()
                    self._on_login_success(n, d, s, sl)
                self.after(0, finish_ok)
            else:
                log.error(f"Switch login failed for '{fresh_user['name']}'")
                def finish_fail():
                    self._dismiss_export_overlay()
                    self._show_login()
                self.after(0, finish_fail)

        threading.Thread(target=do_switch, daemon=True).start()

        # ── Cleanup ───────────────────────────────────────────────────────────────
    def _on_close(self):
        log.info("App closing")
        self.running = False
        self.destroy()

    def _clear(self):
        self.unbind_all("<Key>")
        for w in self.winfo_children():
            w.destroy()
        # Banner widget was destroyed above; reset the reference.
        # _show_main will re-inject it if the mode is still active.
        self._debug_banner = None
        # Net icon widget is gone; the poll loop checks for None before updating.
        self._net_icon = None
        self._net_pill = None
        self._topbar   = None


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=== LABORA Barcode Scanner starting ===")
    app = ScannerApp()
    app.mainloop()