#!/usr/bin/env python3
"""Fotoarhiva — a local app that gathers all your photos in one place.

Start: the installer (Windows) or ./start.sh. Everything else is set up inside the app.
Commands for advanced users:
  python fotoarhiva.py run [--no-browser]   # server + automatic scanning (default)
  python fotoarhiva.py organize             # a single scan, no interface
  python fotoarhiva.py shortcut             # create a Desktop shortcut (Windows)
"""
import hashlib, hmac, io, json, secrets, math, os, re, shutil, sqlite3, struct, sys, threading, time, uuid, webbrowser, mimetypes, subprocess, platform
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs

APP_DIR = Path(__file__).resolve().parent
# Settings, models and log: in %LOCALAPPDATA%\Fotoarhiva on Windows (kept across upgrades)
if os.environ.get("FOTOARHIVA_DATA"):
    DATA_DIR = Path(os.environ["FOTOARHIVA_DATA"])
elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
    DATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "Fotoarhiva"
else:
    DATA_DIR = APP_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = DATA_DIR / "fotoarhiva.log"
if sys.stdout is None or sys.stderr is None:  # started without a console (pythonw) → log to a file
    _log = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
os.environ["FOTOARHIVA_LOG"] = str(LOG_PATH)
SAFE = "--safe" in sys.argv   # safe mode: no models, so the app can always open
try:
    import faulthandler
    _crash_log = open(LOG_PATH, "a", encoding="utf-8")
    faulthandler.enable(_crash_log)  # if something crashes in the C/C++ part, leave a trace in the log
except Exception:
    pass

from PIL import Image, ImageOps, ImageDraw
Image.MAX_IMAGE_PIXELS = None
HEIF = False
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF = True
except Exception:
    pass

sys.path.insert(0, str(APP_DIR))
from places import Places  # noqa: E402
from ai import AI, MODELS, DEFAULT_MODEL, LABELS, MK2EN, BUILTIN, labels_public  # noqa: E402
from cloud import CloudAI, CLOUD_MODELS, DEFAULT_CLOUD_MODEL  # noqa: E402
from vlm import LocalVLM  # noqa: E402
from faces import Faces  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
import urllib.request, urllib.parse  # noqa: E402

VERSION = "2.8"
NEWS_VERSION = "2.8"  # the "what's new" message is shown once per feature version
CONFIG_PATH = DATA_DIR / "config.json"
IS_WIN = platform.system() == "Windows"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".dng", ".cr2", ".nef", ".arw"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".webm", ".mts"}
BROWSER_NATIVE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".m4v", ".webm", ".mov"}
SKIP_DIRS = {"node_modules", "AppData", "Library", "$RECYCLE.BIN", "System Volume Information", "__pycache__", ".git",
             "Windows", "Program Files", "Program Files (x86)", "ProgramData", ".venv", "site-packages"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
THUMB = 480
MIN_FILE_AGE = 10


def find_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = find_ffmpeg()
NOWIN = {"creationflags": 0x08000000} if IS_WIN else {}  # no black console windows for ffmpeg/powershell


# ----------------------------------------------------------------- config
def default_library():
    pics = Path.home() / "Pictures"
    return str((pics if pics.exists() else Path.home()) / "Fotoarhiva")


def load_config():
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print("config.json is damaged, using default settings:", e)
    cfg.setdefault("library", default_library())
    cfg.setdefault("sources", [])
    cfg.setdefault("mode", "copy")
    cfg.setdefault("watch_seconds", 30)
    cfg.setdefault("port", 8765)
    cfg.setdefault("configured", bool(cfg["sources"]))
    cfg.setdefault("ai", True)
    cfg.setdefault("model", DEFAULT_MODEL)
    cfg.setdefault("my_places", [])
    cfg.setdefault("describer", "local")  # off | local | cloud
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def is_inside(child: Path, parent: Path):
    try:
        child, parent = child.resolve(), parent.resolve()
    except Exception:
        return False
    return child == parent or parent in child.parents


# ----------------------------------------------------------------- recognition helpers
def date_from_name(name):
    m = re.search(r"(?<!\d)(199\d|20[0-3]\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
                  r"(?:[-_ T.]?([01]\d|2[0-3])[-_.]?([0-5]\d)[-_.]?([0-5]\d)?)?(?!\d)", name)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 12), int(m[5] or 0), int(m[6] or 0))
        except ValueError:
            pass
    m = re.search(r"(?<!\d)(1[3-9]\d{11})(?!\d)", name)  # unix milliseconds, e.g. FB_IMG_1690000000000
    if m:
        try:
            d = datetime.fromtimestamp(int(m[1]) / 1000)
            if 2005 <= d.year <= datetime.now().year:
                return d
        except Exception:
            pass
    return None


BROWSER_VIDEO = {".mp4", ".m4v", ".mov", ".webm"}
DIRECT_VCODECS = {"h264", "vp8", "vp9", "av1", "hevc"}   # hevc: Chrome plays it on most Windows PCs; if not, it is converted
DIRECT_ACODECS = {"", "aac", "mp3", "opus", "vorbis"}


def _local_from_utc(text):
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def video_info(p: Path):
    """Duration, size, codecs, rotation, GPS and capture date of a video, read with ffmpeg."""
    info = {}
    if not FFMPEG:
        return info
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(p)], capture_output=True, timeout=60, **NOWIN)
        t = r.stderr.decode("utf-8", "replace")
    except Exception:
        return info
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", t)
    if m:
        info["dur"] = round(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)), 2)
    m = re.search(r"Stream #[^\n]*?: Video: (\w+)[^\n]*?[ ,](\d{2,5})x(\d{2,5})[ ,\[]", t)
    if m:
        info["vcodec"], info["w"], info["h"] = m.group(1), int(m.group(2)), int(m.group(3))
    m = re.search(r"Stream #[^\n]*?: Audio: (\w+)", t)
    info["acodec"] = m.group(1) if m else ""
    rot = re.search(r"rotation of (-?[\d.]+) degrees", t) or re.search(r"\brotate\s*:\s*(-?\d+)", t)
    if rot and "w" in info and round(abs(float(rot.group(1)))) % 180 == 90:
        info["w"], info["h"] = info["h"], info["w"]
    m = re.search(r"Input #0, ([\w,]+), from", t)
    info["fmt"] = m.group(1) if m else ""
    m = re.search(r"location(?:\.ISO6709)?\s*:\s*([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", t)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180 and (abs(lat) > 1e-6 or abs(lon) > 1e-6):
            info["lat"], info["lon"] = lat, lon
    m = re.search(r"com\.apple\.quicktime\.creationdate\s*:\s*(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", t)
    if m:  # iPhone: local time of the recording
        info["taken"] = datetime.fromisoformat(m.group(1))
    else:
        m = re.search(r"creation_time\s*:\s*(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z?)", t)
        d = _local_from_utc(m.group(1)) if m else None
        if d and 1995 <= d.year <= datetime.now().year + 1:
            info["taken"] = d
    return info


def video_frame(p: Path, at=1.0, size=1024):
    """One frame of a video as JPEG bytes (for recognition)."""
    for ss in (["-ss", f"{at:.2f}"], []):
        try:
            r = subprocess.run([FFMPEG, "-v", "error", *ss, "-i", str(p), "-frames:v", "1",
                                "-vf", f"scale='min({size},iw)':-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                               capture_output=True, timeout=60, **NOWIN)
            if r.stdout:
                return r.stdout
        except Exception:
            pass
    raise RuntimeError("no frame")


def thumb_time(dur):
    """A frame a little into the video (the first frames are often dark or blurry)."""
    if not dur or dur <= 0:
        return 1.0
    return max(0.0, min(dur * 0.15, 8.0))


def delete_to_recycle_bin(p: Path):
    """Windows: moves the file to the Recycle Bin (it can be restored). Elsewhere: deletes it. Returns an error or None."""
    if not IS_WIN:
        try:
            p.unlink()
            return None
        except OSError as e:
            return str(e)
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p), ("pTo", ctypes.c_void_p),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_void_p)]
    buf = ctypes.create_unicode_buffer(str(p.resolve()) + "\0")  # the list must end with two zeros
    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, ctypes.cast(buf, ctypes.c_void_p), None,
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
    try:
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception as e:
        return str(e)
    if rc or op.fAnyOperationsAborted:
        return f"Windows error {rc}"
    return "the file is still there" if p.exists() else None


DUP_BITS = 22          # how many of the 256 picture-hash bits may differ between two copies of the same photo
DUP_BITS_NO_AI = 14    # stricter when there are no recognition vectors to confirm it
DUP_COS = 0.90         # and the recognition vectors must agree this much
FACE_AUTO = 0.46       # a face gets a person's name automatically above this similarity to a face you named
FACE_MARGIN = 0.04     # … and only if it is clearly closer to that person than to anyone else
POPCOUNT = bytes(bin(i).count("1") for i in range(256))


def dhash(im):
    """256-bit picture hash: survives resizing, recompression and small edits."""
    import numpy as np
    a = np.asarray(im.convert("L").resize((17, 16), Image.BILINEAR), dtype=np.int16)
    return np.packbits(a[:, 1:] > a[:, :-1]).tobytes()


def codec_str(vi):
    return f'{vi.get("vcodec", "")}/{vi.get("acodec", "")}/{vi.get("fmt", "")}'


def video_date(p: Path):
    """Capture date from MP4/MOV (mvhd atom)."""
    try:
        with open(p, "rb") as f:
            size = p.stat().st_size
            pos = 0
            while pos < size:
                f.seek(pos)
                hdr = f.read(16)
                if len(hdr) < 8:
                    return None
                sz, typ = struct.unpack(">I4s", hdr[:8])
                hl = 8
                if sz == 1:
                    sz, hl = struct.unpack(">Q", hdr[8:16])[0], 16
                elif sz == 0:
                    sz = size - pos
                if typ == b"moov":
                    f.seek(pos + hl)
                    data = f.read(min(sz - hl, 64 << 20))
                    i = data.find(b"mvhd")
                    if i < 4:
                        return None
                    ver = data[i + 4]
                    ts = struct.unpack(">Q", data[i + 8:i + 16])[0] if ver == 1 else struct.unpack(">I", data[i + 8:i + 12])[0]
                    if ts <= 0:
                        return None
                    d = datetime.fromtimestamp(ts - 2082844800)
                    return d if 1995 <= d.year <= datetime.now().year + 1 else None
                if sz < 8:
                    return None
                pos += sz
    except Exception:
        return None
    return None


EDITORS = ("snapseed", "picsart", "lightroom", "photoshop", "canva", "vsco", "gimp", "facetune", "remini", "inshot", "capcut")


def detect_source(name, path, cam, software=""):
    n, p = name.lower(), str(path).lower().replace("\\", "/")
    sw = (software or "").lower()
    if (any(k in n for k in ("screenshot", "screen shot", "screen_shot", "screen capture", "screencapture", "snip"))
            or "/screenshots/" in p or "/screenshots/" in p or n.startswith("capture")):
        return "Screenshot"
    if re.search(r"(img|vid|aud|ptt|stk)-\d{8}-wa\d+", n) or "whatsapp" in p:
        return "WhatsApp"
    if "viber" in p or n.startswith("viber_"):
        return "Viber"
    if n.startswith("received_") or n.startswith("fb_img") or "messenger" in p or "/facebook" in p:
        return "Facebook / Messenger"
    if "instagram" in p or n.startswith("ig_"):
        return "Instagram"
    if "telegram" in p:
        return "Telegram"
    if "snapchat" in p or n.startswith("snapchat"):
        return "Snapchat"
    if any(e in sw for e in EDITORS) or any(e in p for e in EDITORS) or "edited" in n or "_edit" in n:
        return "Edited"
    if cam:
        return "Camera"
    if "/downloads/" in p or "/downloads/" in p or "/download/" in p:
        return "Internet"
    return "Other"


def detect_kind(ext, source, cam, w, h):
    if ext in VIDEO_EXT:
        return "video"
    if source == "Screenshot":
        return "screenshot"
    if ext == ".png" and not cam and w and h:
        r = max(w, h) / min(w, h)
        if (h > w and 1.7 <= r <= 2.4 and w >= 640) or (w > h and 1.55 <= r <= 1.8 and w >= 1280):
            return "screenshot"
    return "photo"


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def geocode(q):
    """Address search via OpenStreetMap (Nominatim). Only when the user searches for a place."""
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 6, "accept-language": "mk,en", "addressdetails": 0})
    req = urllib.request.Request(url, headers={"User-Agent": "Fotoarhiva/2.1 (desktop photo organizer)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        rows = json.loads(r.read().decode("utf-8"))
    return [{"name": x.get("display_name", ""), "lat": float(x["lat"]), "lon": float(x["lon"])} for x in rows]


# ----------------------------------------------------------------- migration from the Macedonian versions (≤ 2.4)
OLD_LIB_NAME = "\u0424\u043e\u0442\u043e\u0430\u0440\u0445\u0438\u0432\u0430"  # old default library folder name
OLD_MONTHS = ["Јануари", "Февруари", "Март", "Април", "Мај", "Јуни", "Јули", "Август", "Септември", "Октомври", "Ноември", "Декември"]
OLD_FOLDERS = {"Видеа": "Videos", "Скриншоти": "Screenshots", "Корпа": "Trash"}
OLD_PLACES = {"Дома": "Home", "Работа": "Work", "Викендица": "Holiday home", "Кај родители": "Parents", "Училиште": "School"}
OLD_APPS = {"Скриншот": "Screenshot", "Уредени": "Edited", "Камера": "Camera", "Интернет": "Internet", "Друго": "Other"}


def english_rel(rel):
    parts = rel.split("/")
    out = []
    for part in parts[:-1]:
        m = re.match(r"^(\d\d) - (.+)$", part)
        if m and m.group(2) in OLD_MONTHS:
            part = f"{m.group(1)} - {MONTHS[OLD_MONTHS.index(m.group(2))]}"
        out.append(OLD_FOLDERS.get(part, part))
    return "/".join(out + parts[-1:])


def rename_old_library(cfg):
    """The old default library folder had a Macedonian name; rename it once to "Fotoarhiva"."""
    lib = Path(os.path.expanduser(cfg.get("library", "")))
    if lib.name == OLD_LIB_NAME and lib.exists():
        new = lib.with_name("Fotoarhiva")
        if not new.exists():
            try:
                lib.rename(new)
                cfg["library"] = str(new)
                save_config(cfg)
                print("Library folder renamed to", new)
            except OSError as e:
                print("Could not rename the library folder (it stays as it is):", e)


# ----------------------------------------------------------------- library
class Library:
    def __init__(self, cfg, places: Places):
        self.cfg = cfg
        self.places = places
        self.root = Path(os.path.expanduser(cfg["library"]))
        self.meta = self.root / ".fotoarhiva"
        self.thumbs = self.meta / "thumbs"
        self.previews = self.meta / "previews"
        self.incoming = self.meta / "incoming"
        self.vcache = self.meta / "video"
        self.trash_dir = self.root / "Trash"
        self.vjobs, self.vsem = {}, threading.Semaphore(1)
        self.dups_dirty, self.dup_groups, self.dup_lock = True, [], threading.Lock()
        for p in (self.root, self.meta, self.thumbs, self.previews, self.incoming, self.vcache):
            p.mkdir(parents=True, exist_ok=True)
        if IS_WIN:
            try:
                subprocess.run(["attrib", "+h", str(self.meta)], **NOWIN, capture_output=True)
            except Exception:
                pass
        self.scan_lock = threading.Lock()
        self.db_lock = threading.RLock()
        self.db = sqlite3.connect(self.meta / "index.db", check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.migrate()
        self.migrate_english()
        self.emb_cache = None
        self.status = {"state": "idle", "scanned": 0, "total": 0, "added": 0, "duplicates": 0, "skipped": 0, "errors": 0,
                       "current": "", "version": 0, "last_scan": None, "ai_pending": 0, "ai_done": 0}

    def migrate(self):
        with self.db_lock:
            self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS photos(
              id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, hash TEXT UNIQUE, taken TEXT, size INTEGER,
              w INTEGER, h INTEGER, cam TEXT, kind TEXT, lat REAL, lon REAL, fav INTEGER DEFAULT 0,
              trash INTEGER DEFAULT 0, source TEXT, added TEXT);
            CREATE TABLE IF NOT EXISTS seen(path TEXT PRIMARY KEY, mtime REAL, result TEXT, dup_of INTEGER);
            CREATE TABLE IF NOT EXISTS albums(id INTEGER PRIMARY KEY, name TEXT UNIQUE, color TEXT);
            CREATE TABLE IF NOT EXISTS album_photos(album_id INTEGER, photo_id INTEGER, UNIQUE(album_id, photo_id));
            CREATE TABLE IF NOT EXISTS tags(photo_id INTEGER, tag TEXT, score REAL, by TEXT, UNIQUE(photo_id, tag));
            CREATE TABLE IF NOT EXISTS hidden_tags(photo_id INTEGER, tag TEXT, UNIQUE(photo_id, tag));
            CREATE TABLE IF NOT EXISTS emb(photo_id INTEGER PRIMARY KEY, v BLOB);
            CREATE TABLE IF NOT EXISTS phash(photo_id INTEGER PRIMARY KEY, h BLOB);
            CREATE TABLE IF NOT EXISTS dup_ignore(a INTEGER, b INTEGER, UNIQUE(a, b));
            CREATE TABLE IF NOT EXISTS faces(id INTEGER PRIMARY KEY, photo_id INTEGER, x REAL, y REAL, w REAL, h REAL,
                                             score REAL, v BLOB, person TEXT, by TEXT);
            CREATE INDEX IF NOT EXISTS idx_faces_photo ON faces(photo_id);
            CREATE TABLE IF NOT EXISTS face_reject(face_id INTEGER, person TEXT, UNIQUE(face_id, person));
            CREATE INDEX IF NOT EXISTS idx_photos_taken ON photos(taken);
            CREATE INDEX IF NOT EXISTS idx_tags_photo ON tags(photo_id);
            """)
            cols = {r["name"] for r in self.db.execute("PRAGMA table_info(photos)")}
            for c, t in (("app", "TEXT"), ("orig", "TEXT"), ("place", "TEXT"), ("country", "TEXT"),
                         ("rot", "INTEGER DEFAULT 0"), ("ai_done", "INTEGER DEFAULT 0"), ("how", "TEXT"), ("caption", "TEXT"), ("cloud_done", "INTEGER DEFAULT 0"),
                         ("dur", "REAL"), ("codec", "TEXT"), ("faces_done", "INTEGER DEFAULT 0")):
                if c not in cols:
                    self.db.execute(f"ALTER TABLE photos ADD COLUMN {c} {t}")
            try:
                self.db.execute("ALTER TABLE seen ADD COLUMN dup_of INTEGER")
            except sqlite3.OperationalError:
                pass
            # fill in the new fields for older databases
            for r in self.db.execute("SELECT id,name,path,cam,lat,lon,app,place FROM photos WHERE app IS NULL OR (lat IS NOT NULL AND place IS NULL)").fetchall():
                app = r["app"] or detect_source(r["name"], r["path"], r["cam"])
                place, country = self.places.lookup(r["lat"], r["lon"]) if r["lat"] is not None else (None, None)
                self.db.execute("UPDATE photos SET app=?, place=?, country=? WHERE id=?", (app, place, country, r["id"]))
            self.db.commit()

    def migrate_english(self):
        """One-time move from the Macedonian versions: folder names, tags, sources, places and captions."""
        with self.db_lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
            if self.db.execute("SELECT 1 FROM meta WHERE k='lang' AND v='en'").fetchone():
                return
            moved = 0
            if self.db.execute("SELECT 1 FROM photos LIMIT 1").fetchone():
                print("Converting the library to English…")
            for r in self.db.execute("SELECT id, path FROM photos").fetchall():
                new = english_rel(r["path"])
                if new == r["path"]:
                    continue
                src, dst = self.abs(r["path"]), self.abs(new)
                try:
                    if src.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        if dst.exists():
                            dst = self.unique_dest(dst.parent, dst.name)
                        shutil.move(str(src), str(dst))
                    self.db.execute("UPDATE photos SET path=? WHERE id=?", (self.rel(dst), r["id"]))
                    moved += 1
                except OSError as e:
                    print("  ! could not move", src, e)
            for table in ("tags", "hidden_tags"):
                for mk, en in MK2EN.items():
                    if table == "tags":
                        self.db.execute("INSERT OR IGNORE INTO tags SELECT photo_id, ?, score, by FROM tags WHERE tag=?", (en, mk))
                    else:
                        self.db.execute("INSERT OR IGNORE INTO hidden_tags SELECT photo_id, ? FROM hidden_tags WHERE tag=?", (en, mk))
                    self.db.execute(f"DELETE FROM {table} WHERE tag=?", (mk,))
            for mk, en in OLD_APPS.items():
                self.db.execute("UPDATE photos SET app=? WHERE app=?", (en, mk))
            self.db.execute("UPDATE photos SET source='Imported' WHERE source=?", ("Увезени",))
            for r in self.db.execute("SELECT id, lat, lon FROM photos WHERE lat IS NOT NULL").fetchall():
                place, country = self.places.lookup(r["lat"], r["lon"])
                self.db.execute("UPDATE photos SET place=?, country=? WHERE id=?", (place, country, r["id"]))
            for r in self.db.execute("SELECT id, caption FROM photos WHERE caption LIKE '% | %'").fetchall():
                self.db.execute("UPDATE photos SET caption=? WHERE id=?", (r["caption"].split(" | ", 1)[1], r["id"]))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('lang','en')")
            self.db.commit()
        # remove the now empty old folders
        for dirpath, dirnames, filenames in os.walk(self.root, topdown=False):
            d = Path(dirpath)
            if d != self.root and re.search("[А-Яа-я]", d.name) and not any(d.iterdir()):
                try:
                    d.rmdir()
                except OSError:
                    pass
        if moved:
            print(f"Done: {moved} files moved into English folder names.")

    # ---- helpers
    def rel(self, p: Path):
        return str(p.relative_to(self.root)).replace("\\", "/")

    def abs(self, rel):
        return self.root / rel

    @staticmethod
    def file_hash(p: Path):
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def dest_dir_for(self, taken: datetime, kind: str):
        d = self.root / f"{taken.year}" / f"{taken.month:02d} - {MONTHS[taken.month - 1]}"
        if kind == "video":
            d = d / "Videos"
        elif kind == "screenshot":
            d = d / "Screenshots"
        return d

    @staticmethod
    def unique_dest(dest_dir: Path, name: str):
        dest = dest_dir / name
        stem, suf = Path(name).stem, Path(name).suffix
        n = 2
        while dest.exists():
            dest = dest_dir / f"{stem} ({n}){suf}"
            n += 1
        return dest

    def bump(self):
        self.status["version"] += 1

    def read_image(self, p: Path):
        taken = cam = lat = lon = None
        software = ""
        w = h = 0
        with Image.open(p) as im:
            w, h = im.size
            exif = im.getexif()
            if exif:
                make, model = str(exif.get(271) or "").strip("\x00 "), str(exif.get(272) or "").strip("\x00 ")
                if model.lower().startswith(make.lower()) and make:
                    make = ""
                cam = (" ".join(x for x in (make, model) if x)).strip()[:60] or None
                software = str(exif.get(305) or "")
                try:
                    sub = exif.get_ifd(0x8769)
                except Exception:
                    sub = {}
                dt = sub.get(36867) or sub.get(36868) or exif.get(306)
                if dt:
                    try:
                        taken = datetime.strptime(str(dt).strip("\x00 ")[:19], "%Y:%m:%d %H:%M:%S")
                        if taken.year < 1990:
                            taken = None
                    except ValueError:
                        pass
                try:
                    gps = exif.get_ifd(0x8825)
                    if gps and 2 in gps and 4 in gps:
                        def dms(v, ref):
                            d = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
                            return -d if str(ref).strip("\x00") in ("S", "W") else d
                        lat, lon = dms(gps[2], gps.get(1, "N")), dms(gps[4], gps.get(3, "E"))
                        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (abs(lat) < 1e-6 and abs(lon) < 1e-6):
                            lat = lon = None
                except Exception:
                    pass
                if exif.get(274, 1) in (5, 6, 7, 8):
                    w, h = h, w
        return taken, w, h, cam, lat, lon, software

    def make_thumb(self, pid, f: Path, kind, rot=0, dur=None):
        out = self.thumbs / f"{pid}.jpg"
        try:
            if kind == "video":
                if not FFMPEG:
                    return False
                if out.exists():
                    out.unlink()
                for ss in (["-ss", f"{thumb_time(dur):.2f}"], []):
                    subprocess.run([FFMPEG, "-v", "error", "-y", *ss, "-i", str(f), "-frames:v", "1",
                                    "-vf", f"scale='min({THUMB},iw)':-2", str(out)], timeout=60,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **NOWIN)
                    if out.exists():
                        break
                if rot and out.exists():
                    with Image.open(out) as im:
                        im.rotate(-rot, expand=True).save(out, "JPEG", quality=82)
                self._forget_phash(pid)
                return out.exists()
            with Image.open(f) as im:
                try:
                    im.draft("RGB", (THUMB * 2, THUMB * 2))
                except Exception:
                    pass
                t = ImageOps.exif_transpose(im)
                t.thumbnail((THUMB, THUMB))
                t = t.convert("RGB")
                if rot:
                    t = t.rotate(-rot, expand=True)
                t.save(out, "JPEG", quality=82)
            self._forget_phash(pid)
            return True
        except Exception as e:
            print(f"  ! No thumbnail for {f.name}: {e}")
            return False

    # ---- scanning
    def organize(self):
        if not self.cfg.get("sources") or not self.scan_lock.acquire(blocking=False):
            return
        try:
            self.status.update(state="scanning", scanned=0, total=0, added=0, duplicates=0, skipped=0, errors=0)
            self.bump()
            files = self.collect(self.cfg["sources"])
            self.status["total"] = len(files)
            with self.db_lock:
                seen = {r["path"]: r["mtime"] for r in self.db.execute("SELECT path, mtime FROM seen")}
            now = time.time()
            for i, (src, root_src) in enumerate(files, 1):
                self.status["current"], self.status["scanned"] = str(src), i
                try:
                    st = src.stat()
                    if abs(seen.get(str(src), -1e9) - st.st_mtime) < 1:
                        continue
                    if now - st.st_mtime < MIN_FILE_AGE:
                        self.status["skipped"] += 1
                        continue
                    self.ingest(src, str(root_src), st, orig=str(src), mode=self.cfg.get("mode", "copy"))
                except Exception as e:
                    self.status["errors"] += 1
                    print(f"  ! Problem with {src}: {e}")
        finally:
            self.status.update(state="idle", current="", last_scan=datetime.now().isoformat(timespec="seconds"))
            self.bump()
            self.scan_lock.release()

    def collect(self, sources):
        out = []
        lib = self.root.resolve()
        for si, s in enumerate(sources):
            sp = Path(os.path.expanduser(s))
            if not sp.exists() or is_inside(sp, lib):
                continue
            for dirpath, dirnames, filenames in os.walk(sp):
                dp = Path(dirpath)
                dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS and not is_inside(dp / d, lib))
                for fn in filenames:
                    ext = Path(fn).suffix.lower()
                    if (ext in IMAGE_EXT or ext in VIDEO_EXT) and not fn.startswith("._"):
                        low = fn.lower()
                        copyish = int(any(k in low for k in ("copy", "copy", "(1)", "(2)", "(3)")))
                        out.append(((si, copyish, str(dp / fn).lower()), dp / fn, sp))
        out.sort(key=lambda t: t[0])
        return [(f, s) for _, f, s in out]

    def ingest(self, src: Path, root_src: str, st, orig: str, mode="copy", record_seen=True):
        """Add one file. Returns 'added' or 'duplicate'."""
        h = self.file_hash(src)
        with self.db_lock:
            dup = self.db.execute("SELECT id FROM photos WHERE hash=?", (h,)).fetchone()
            if dup:
                if record_seen:
                    self.db.execute("INSERT OR REPLACE INTO seen VALUES(?,?,?,?)", (str(src), st.st_mtime, "duplicate", dup["id"]))
                    self.db.commit()
                self.status["duplicates"] += 1
                return "duplicate", dup["id"]
        ext = src.suffix.lower()
        name = Path(orig).name
        taken = cam = lat = lon = None
        w = h_ = 0
        software = ""
        if ext in IMAGE_EXT:
            try:
                taken, w, h_, cam, lat, lon, software = self.read_image(src)
            except Exception as e:
                print(f"  ! Cannot read {name}: {e}")
        vi = {}
        if ext in VIDEO_EXT:
            vi = video_info(src)
            taken = vi.get("taken") or (video_date(src) if ext in (".mp4", ".mov", ".m4v", ".3gp") else None)
            w, h_ = vi.get("w", 0), vi.get("h", 0)
            lat, lon = vi.get("lat"), vi.get("lon")
        taken = taken or date_from_name(name) or datetime.fromtimestamp(min(st.st_mtime, getattr(st, "st_ctime", st.st_mtime)))
        app = detect_source(name, orig, cam, software)
        kind = detect_kind(ext, app, cam, w, h_)
        place, country = self.places.lookup(lat, lon)
        dest_dir = self.dest_dir_for(taken, kind)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = self.unique_dest(dest_dir, name)
        if mode == "move":
            shutil.move(str(src), str(dest))
        else:
            shutil.copy2(str(src), str(dest))
        with self.db_lock:
            cur = self.db.execute(
                "INSERT INTO photos(path,name,hash,taken,size,w,h,cam,kind,lat,lon,source,added,app,orig,place,country,ai_done,how,dur,codec) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.rel(dest), dest.name, h, taken.isoformat(timespec="seconds"), st.st_size, w, h_, cam, kind, lat, lon,
                 root_src, datetime.now().isoformat(timespec="seconds"), app, orig, place, country, 0,
                 "upload" if not record_seen else mode, vi.get("dur"), codec_str(vi) if vi else None))
            pid = cur.lastrowid
            if record_seen:
                self.db.execute("INSERT OR REPLACE INTO seen VALUES(?,?,?,?)", (str(src), st.st_mtime, "added", pid))
            self.db.commit()
        self.make_thumb(pid, dest, kind, dur=vi.get("dur"))
        self.dups_dirty = True
        self.status["added"] += 1
        self.bump()
        return "added", pid

    def ingest_upload(self, tmp: Path, name: str, mtime: float):
        st = tmp.stat()
        os.utime(tmp, (mtime, mtime))
        st = tmp.stat()
        try:
            res, pid = self.ingest(tmp, "Imported", st, orig=name, mode="move", record_seen=False)
        finally:
            if tmp.exists():
                tmp.unlink()
        return res, pid

    # ---- AI
    def ai_pending(self):
        with self.db_lock:
            return self.db.execute("SELECT COUNT(*) c FROM photos WHERE ai_done=0 AND trash=0").fetchone()["c"]

    def ai_step(self, ai, batch=16):
        with self.db_lock:
            rows = self.db.execute("SELECT id,kind,cam,path,taken,app FROM photos WHERE ai_done=0 AND trash=0 LIMIT ?", (batch,)).fetchall()
        if not rows:
            return False
        import numpy as np
        ims, ok = [], []
        for r in rows:
            t = self.thumbs / f"{r['id']}.jpg"
            try:
                with Image.open(t) as im:
                    ims.append(im.convert("RGB"))
                ok.append(r)
            except Exception:
                with self.db_lock:
                    self.db.execute("UPDATE photos SET ai_done=2 WHERE id=?", (r["id"],))
        if ims:
            E = ai.embed(ims)
            results = ai.classify(E)
            with self.db_lock:
                for r, e, tags in zip(ok, E, results):
                    pid = r["id"]
                    self._write_ai_tags(pid, tags)
                    self.db.execute("INSERT OR REPLACE INTO emb VALUES(?,?)", (pid, e.astype(np.float16).tobytes()))
                    top = tags[0][0] if tags else ""
                    # AI fixes the screenshot type only when there is no camera data and no 'Screenshot' name
                    if not r["cam"] and r["kind"] != "video" and r["app"] != "Screenshot":
                        new_kind = r["kind"]
                        if top == "Screenshot":
                            new_kind = "screenshot"
                        elif r["kind"] == "screenshot" and top and top not in ("Documents", "Memes & graphics", "Map", "Notes"):
                            new_kind = "photo"
                        if new_kind != r["kind"]:
                            self._move_kind(pid, r["path"], datetime.fromisoformat(r["taken"]), new_kind)
                    self.db.execute("UPDATE photos SET ai_done=1 WHERE id=?", (pid,))
                self.db.commit()
            self.emb_cache = None
            self.apply_learned([r["id"] for r in ok])
        self.status["ai_done"] += len(rows)
        self.bump()
        return True

    def _write_ai_tags(self, pid, tags):
        hidden = {x["tag"] for x in self.db.execute("SELECT tag FROM hidden_tags WHERE photo_id=?", (pid,))}
        self.db.execute("DELETE FROM tags WHERE photo_id=? AND by='ai'", (pid,))
        if self.db.execute("SELECT 1 FROM photos WHERE id=? AND cloud_done=1", (pid,)).fetchone():
            return  # a describer already described it: its tags take priority
        user = {x["tag"] for x in self.db.execute("SELECT tag FROM tags WHERE photo_id=? AND by!='ai'", (pid,))}
        for t, sc in tags:
            if t.startswith("_") or t == "Screenshot" or t in hidden or t in user:
                continue  # screenshots are a separate type; rejected tags never come back
            self.db.execute("INSERT OR REPLACE INTO tags VALUES(?,?,?,?)", (pid, t, sc, "ai"))

    def reclassify(self, ai):
        """New categories or new logic: tags are recomputed from the stored vectors, without a new analysis."""
        import numpy as np
        ids, M = self.emb_matrix()
        if not len(ids) or ai.L is None or M.shape[1] != ai.L.shape[1]:
            return
        for k in range(0, len(ids), 512):
            res = ai.classify(M[k:k + 512])
            with self.db_lock:
                for pid, tags in zip(ids[k:k + 512], res):
                    self._write_ai_tags(int(pid), tags)
                self.db.commit()
        self.learn_all()
        self.bump()

    # ---- learning from the user: a hand-made tag spreads to many similar photos
    def learn(self, tag):
        import numpy as np
        ids, M = self.emb_matrix()
        with self.db_lock:
            self.db.execute("DELETE FROM tags WHERE tag=? AND by='learned'", (tag,))
            ex = [r["photo_id"] for r in self.db.execute("SELECT photo_id FROM tags WHERE tag=? AND by='user'", (tag,))]
            hidden = {r["photo_id"] for r in self.db.execute("SELECT photo_id FROM hidden_tags WHERE tag=?", (tag,))}
            have = {r["photo_id"] for r in self.db.execute("SELECT photo_id FROM tags WHERE tag=?", (tag,))}
            pos = {int(i): n for n, i in enumerate(ids)}
            rows = [pos[i] for i in ex if i in pos]
            if rows and len(ids) > 1:
                P = M[rows]
                proto = P.mean(0)
                proto /= np.linalg.norm(proto) + 1e-9
                s = M @ proto
                ss = s[[i for i in range(len(ids)) if int(ids[i]) not in set(ex)]] if len(ids) > len(rows) else s
                mu, sd = float(ss.mean()), float(ss.std() or 1e-6)
                # threshold: well above the library average; with 3+ examples, close to the most different example
                own = float((P @ proto).min()) - 0.05 if len(rows) >= 3 else -1.0
                thr = max(min(mu + 2.5 * sd, 0.92), own, 0.60)
                hits = [i for i in np.argsort(-s) if s[i] >= thr][:max(30, 40 * len(rows))]
                for i in hits:
                    pid = int(ids[i])
                    if pid not in have and pid not in hidden:
                        self.db.execute("INSERT OR IGNORE INTO tags VALUES(?,?,?,?)", (pid, tag, float(s[i]), "learned"))
            self.db.commit()
        self.bump()

    def learn_all(self):
        with self.db_lock:
            tags = [r["tag"] for r in self.db.execute("SELECT DISTINCT tag FROM tags WHERE by='user'")]
        for t in tags:
            self.learn(t)

    def apply_learned(self, pids):
        """New photos also get the tags the app has learned."""
        import numpy as np
        if not pids:
            return
        with self.db_lock:
            tags = [r["tag"] for r in self.db.execute("SELECT DISTINCT tag FROM tags WHERE by='user'")]
        for t in tags:
            self.learn(t)

    # ---- describers (Claude or the built-in Qwen3-VL)
    def cloud_pending(self):
        with self.db_lock:
            return self.db.execute("SELECT COUNT(*) c FROM photos WHERE cloud_done=0 AND trash=0").fetchone()["c"]

    def cloud_step(self, cloud, labels, workers=3):
        """Describing with a describer: Claude (cloud) or the built-in Qwen3-VL. Same result shape."""
        with self.db_lock:
            rows = self.db.execute("SELECT id,path,kind,cam,app,taken,rot,dur FROM photos WHERE cloud_done=0 AND trash=0 LIMIT ?",
                                   (workers * 2,)).fetchall()
        if not rows:
            return False
        known = {mk for mk, _ in labels}
        by = "vlm" if isinstance(cloud, LocalVLM) else "cloud"

        def one(r):
            f = self.abs(r["path"])
            try:
                if r["kind"] == "video":
                    jpeg = CloudAI.image_bytes(io.BytesIO(video_frame(f, thumb_time(r["dur"]))), r["rot"] or 0)
                else:
                    jpeg = CloudAI.image_bytes(f, r["rot"] or 0)
            except Exception:
                jpeg = CloudAI.image_bytes(self.thumbs / f"{r['id']}.jpg")  # RAW and similar: via the thumbnail
            return r, cloud.describe(jpeg, labels)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = []
            for fut in [ex.submit(one, r) for r in rows]:
                try:
                    results.append(fut.result())
                except Exception as e:
                    print("Describing:", e)
                    if cloud.state == "error":
                        return False
        with self.db_lock:
            for r, d in results:
                pid = r["id"]
                if d is None:
                    self.db.execute("UPDATE photos SET cloud_done=2 WHERE id=?", (pid,))
                    continue
                hidden = {x["tag"] for x in self.db.execute("SELECT tag FROM hidden_tags WHERE photo_id=?", (pid,))}
                user = {x["tag"] for x in self.db.execute("SELECT tag FROM tags WHERE photo_id=? AND by='user'", (pid,))}
                self.db.execute("DELETE FROM tags WHERE photo_id=? AND by IN ('ai','cloud','vlm')", (pid,))
                tags = [t.strip() for t in (d.get("tags") or []) if t.strip() in known][:5]
                tags += [t.strip()[:40] for t in (d.get("new_tags") or []) if t.strip()][:3]
                for n, t in enumerate(dict.fromkeys(tags)):
                    if t not in hidden and t not in user and t != "Screenshot":
                        self.db.execute("INSERT OR REPLACE INTO tags VALUES(?,?,?,?)", (pid, t, 10.0 - n, by))
                cap = " | ".join(x for x in (d.get("caption_mk", "").strip(), d.get("caption_en", "").strip()) if x)
                self.db.execute("UPDATE photos SET caption=?, cloud_done=1 WHERE id=?", (cap[:500], pid))
                k = d.get("kind")
                if not r["cam"] and r["app"] != "Screenshot" and r["kind"] != "video" and k in ("photo", "screenshot", "document"):
                    nk = "screenshot" if k == "screenshot" else "photo"
                    if nk != r["kind"]:
                        self._move_kind(pid, r["path"], datetime.fromisoformat(r["taken"]), nk)
            self.db.commit()
        self.bump()
        return True

    def _move_kind(self, pid, rel_path, taken, kind):
        src = self.abs(rel_path)
        dest_dir = self.dest_dir_for(taken, kind)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = self.unique_dest(dest_dir, src.name)
        if src.exists() and not rel_path.startswith("Trash/"):
            shutil.move(str(src), str(dest))
            self.db.execute("UPDATE photos SET kind=?, path=? WHERE id=?", (kind, self.rel(dest), pid))
        else:
            self.db.execute("UPDATE photos SET kind=? WHERE id=?", (kind, pid))

    def similar(self, pid, n=24):
        import numpy as np
        ids, M = self.emb_matrix()
        hit = np.where(ids == pid)[0]
        if not len(hit):
            return []
        s = M @ M[hit[0]]
        order = np.argsort(-s)
        mu, sd = float(s.mean()), float(s.std() or 1e-6)
        return [int(ids[i]) for i in order if ids[i] != pid and s[i] > max(0.75, mu + 2.5 * sd)][:n]

    # ---- data for the UI
    def snapshot(self):
        with self.db_lock:
            rows = self.db.execute("SELECT id,path,name,taken,size,w,h,cam,kind,lat,lon,fav,trash,source,app,place,country,rot,ai_done,caption,cloud_done,dur,codec FROM photos ORDER BY taken DESC").fetchall()
            ap, tg = {}, {}
            for r in self.db.execute("SELECT album_id, photo_id FROM album_photos"):
                ap.setdefault(r["photo_id"], []).append(r["album_id"])
            tb = {}
            for r in self.db.execute("SELECT photo_id, tag, by FROM tags ORDER BY by='user' DESC, score DESC"):
                tg.setdefault(r["photo_id"], []).append(r["tag"])
                tb.setdefault(r["photo_id"], {})[r["tag"]] = r["by"]
            albums = [dict(r) for r in self.db.execute("SELECT * FROM albums ORDER BY name")]
            people = {}
            for r in self.db.execute("SELECT DISTINCT photo_id, person FROM faces WHERE person IS NOT NULL"):
                people.setdefault(r["photo_id"], []).append(r["person"])
            dups = self.db.execute("SELECT COUNT(*) c FROM seen WHERE result='duplicate'").fetchone()["c"]
        dups += sum(len(g["ids"]) - 1 for g in self.dup_groups)
        mine = [p for p in self.cfg.get("my_places", []) if p.get("lat") is not None]
        def myplace(lat, lon):
            if lat is None or not mine:
                return None
            best, br = None, 1e18
            for m in mine:  # if places overlap, the smaller one wins (e.g. Work before City)
                r = float(m.get("radius", 300))
                if haversine(lat, lon, m["lat"], m["lon"]) <= r and r < br:
                    best, br = m["name"], r
            return best
        photos = [dict(r, people=people.get(r["id"], []), albums=ap.get(r["id"], []), tags=tg.get(r["id"], []), tagby=tb.get(r["id"], {}), myplace=myplace(r["lat"], r["lon"])) for r in rows]
        try:
            total = shutil.disk_usage(self.root)[0]
        except Exception:
            total = 0
        return {"photos": photos, "albums": albums, "duplicates": dups, "library": str(self.root), "disk_total": total}

    def skipped_duplicates(self):
        """Files outside the library that were skipped because the same file is already in the library."""
        with self.db_lock:
            rows = self.db.execute("""SELECT s.path AS src, s.dup_of AS of_id, p.path AS of_path, p.trash AS of_trash FROM seen s
                                      LEFT JOIN photos p ON p.id = s.dup_of WHERE s.result='duplicate' ORDER BY s.path""").fetchall()
        out, gone = [], []
        for r in rows:
            try:
                size = os.path.getsize(r["src"])
            except OSError:
                gone.append(r["src"])
                continue
            out.append(dict(r, size=size, safe=bool(r["of_path"]) and not r["of_trash"]))
        if gone:
            with self.db_lock:
                self.db.executemany("UPDATE seen SET result='removed' WHERE path=?", [(g,) for g in gone])
                self.db.commit()
        return out

    def remove_skipped(self, paths):
        """Deletes skipped duplicate files outside the library (to the Recycle Bin on Windows).
        Only when the same file is still in the library and the file was not changed in the meantime."""
        with self.db_lock:
            rows = {r["src"]: r for r in self.db.execute(
                """SELECT s.path AS src, p.path AS lp, p.hash AS hash, p.trash AS trash FROM seen s
                   LEFT JOIN photos p ON p.id = s.dup_of WHERE s.result='duplicate'""")}
        done, failed = 0, []
        for sp in paths:
            r = rows.get(sp)
            if not r:
                continue
            src = Path(sp)
            try:
                if not r["lp"] or r["trash"] or not self.abs(r["lp"]).exists():
                    raise RuntimeError("the library copy is missing or in Trash")
                if is_inside(src, self.root):
                    raise RuntimeError("the file is inside the library")
                if src.exists():
                    if self.file_hash(src) != r["hash"]:
                        raise RuntimeError("the file has changed since it was compared")
                    err = delete_to_recycle_bin(src)
                    if err:
                        raise RuntimeError(err)
                with self.db_lock:
                    self.db.execute("UPDATE seen SET result='removed' WHERE path=?", (sp,))
                    self.db.commit()
                done += 1
            except Exception as e:
                failed.append({"path": sp, "error": str(e)})
                print("  ! Could not delete", sp, e)
        self.bump()
        return done, failed

    def _forget_phash(self, pid):
        with self.db_lock:
            self.db.execute("DELETE FROM phash WHERE photo_id=?", (pid,))
        self.dups_dirty = True

    def ensure_phash(self):
        with self.db_lock:
            rows = self.db.execute("SELECT id FROM photos WHERE trash=0 AND id NOT IN (SELECT photo_id FROM phash)").fetchall()
        for n, r in enumerate(rows, 1):
            try:
                with Image.open(self.thumbs / f"{r['id']}.jpg") as im:
                    h = dhash(im)
            except Exception:
                h = b""
            with self.db_lock:
                self.db.execute("INSERT OR REPLACE INTO phash VALUES(?,?)", (r["id"], h))
                if n % 200 == 0:
                    self.db.commit()
        if rows:
            with self.db_lock:
                self.db.commit()
            self.dups_dirty = True

    def find_similar_copies(self):
        """Groups of the same photo in the library in different files (resized, recompressed, sent through an app…)."""
        import numpy as np
        with self.db_lock:
            rows = self.db.execute("""SELECT p.id, p.kind, p.w, p.h, p.size, p.fav, x.h AS ph FROM photos p JOIN phash x ON x.photo_id = p.id
                                      WHERE p.trash = 0 AND length(x.h) = 32 ORDER BY p.id""").fetchall()
            ignored = {(r[0], r[1]) for r in self.db.execute("SELECT a, b FROM dup_ignore")}
        if len(rows) < 2:
            return []
        H = np.frombuffer(b"".join(r["ph"] for r in rows), dtype=np.uint8).reshape(-1, 32)
        pop = np.frombuffer(POPCOUNT, dtype=np.uint8)
        edges = pop[H].sum(1, dtype=np.int32)
        idx = np.nonzero((edges >= 16) & (edges <= 240))[0]      # (almost) flat pictures can't be compared
        is_video = np.array([r["kind"] == "video" for r in rows])
        ids, E = self.emb_matrix()
        pos = {int(i): k for k, i in enumerate(ids)}
        pairs = []
        Hs = H[idx]
        for k in range(0, len(idx), 32):
            D = pop[Hs[k:k + 32, None, :] ^ Hs[None, :, :]].sum(-1, dtype=np.int32)
            for a, b in zip(*np.nonzero(D <= DUP_BITS)):
                a += k
                if a >= b:
                    continue
                i, j = int(idx[a]), int(idx[b])
                if is_video[i] != is_video[j]:
                    continue
                pa, pb = rows[i]["id"], rows[j]["id"]
                if (pa, pb) in ignored:
                    continue
                ea, eb = pos.get(pa), pos.get(pb)
                if ea is not None and eb is not None:
                    if float(E[ea] @ E[eb]) < DUP_COS:
                        continue
                elif D[a - k, b] > DUP_BITS_NO_AI:
                    continue
                pairs.append((i, j))
        parent = list(range(len(rows)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for i, j in pairs:
            parent[find(i)] = find(j)
        groups = {}
        for i, j in pairs:
            for x in (i, j):
                groups.setdefault(find(x), set()).add(x)
        out = []
        for g in groups.values():
            g = sorted(g)
            best = max(g, key=lambda x: ((rows[x]["w"] or 0) * (rows[x]["h"] or 0), rows[x]["size"] or 0, rows[x]["fav"] or 0, -x))
            out.append({"ids": [rows[x]["id"] for x in g], "keep": rows[best]["id"]})
        return out

    def similar_copies(self):
        with self.dup_lock:
            if self.dups_dirty:
                self.dups_dirty = False
                self.ensure_phash()
                self.dups_dirty = False
                try:
                    self.dup_groups = self.find_similar_copies()
                except Exception as e:
                    print("Duplicates:", e)
            return self.dup_groups

    def ignore_duplicates(self, ids):
        ids = sorted(set(ids))
        with self.db_lock:
            for a in range(len(ids)):
                for b in range(a + 1, len(ids)):
                    self.db.execute("INSERT OR IGNORE INTO dup_ignore VALUES(?,?)", (ids[a], ids[b]))
            self.db.commit()
        self.dups_dirty = True

    # ---- people (faces)
    def face_pending(self):
        with self.db_lock:
            return self.db.execute("SELECT COUNT(*) c FROM photos WHERE faces_done=0 AND trash=0").fetchone()["c"]

    def _face_image(self, r):
        """The photo as shown (turned the right way), at most 1600 px; videos through their thumbnail."""
        f = self.abs(r["path"])
        if r["kind"] == "video" or f.suffix.lower() not in IMAGE_EXT:
            im = Image.open(self.thumbs / f"{r['id']}.jpg")
            im.load()
            return im.convert("RGB")
        with Image.open(f) as im:
            try:
                im.draft("RGB", (1600, 1600))
            except Exception:
                pass
            t = ImageOps.exif_transpose(im).convert("RGB")
        t.thumbnail((1600, 1600))
        if r["rot"]:
            t = t.rotate(-r["rot"], expand=True)
        return t

    def face_step(self, faces, batch=8):
        with self.db_lock:
            rows = self.db.execute("SELECT id, path, kind, rot FROM photos WHERE faces_done=0 AND trash=0 LIMIT ?", (batch,)).fetchall()
        if not rows:
            return False
        new = []
        for r in rows:
            try:
                found = faces.analyze(self._face_image(r))
            except Exception:
                found = []
            with self.db_lock:
                old = {x["id"]: x for x in self.db.execute("SELECT id, x, y, w, h, person, by FROM faces WHERE photo_id=?", (r["id"],))}
                for fid in old:
                    crop = self.meta / "faces" / f"{fid}.jpg"
                    if crop.exists():
                        crop.unlink()
                self.db.execute("DELETE FROM faces WHERE photo_id=?", (r["id"],))
                for (x, y, w, h), score, v in found:
                    # a face that was already named keeps its name when the photo is analyzed again (e.g. after rotating)
                    keep = next((o for o in old.values() if o["by"] == "user" and abs(o["x"] - x) < 0.08 and abs(o["y"] - y) < 0.08), None)
                    cur = self.db.execute("INSERT INTO faces(photo_id,x,y,w,h,score,v,person,by) VALUES(?,?,?,?,?,?,?,?,?)",
                                          (r["id"], x, y, w, h, score, v.tobytes(), keep["person"] if keep else None, "user" if keep else None))
                    new.append(cur.lastrowid)
                self.db.execute("UPDATE photos SET faces_done=1 WHERE id=?", (r["id"],))
                self.db.commit()
        if new:
            self.assign_faces(new)
        self.bump()
        return True

    def assign_faces(self, only=None):
        """Faces get the name of the person they look like, compared with the faces you named yourself."""
        import numpy as np
        with self.db_lock:
            refs = self.db.execute("SELECT id, person, v FROM faces WHERE by='user' AND person IS NOT NULL").fetchall()
            if only is not None:
                q = ",".join("?" * len(only))
                targets = self.db.execute(f"SELECT id, person, by, v FROM faces WHERE id IN ({q}) AND (by IS NULL OR by='auto')", list(only)).fetchall()
            else:
                targets = self.db.execute("SELECT id, person, by, v FROM faces WHERE by IS NULL OR by='auto'").fetchall()
            rejected = {(r[0], r[1]) for r in self.db.execute("SELECT face_id, person FROM face_reject")}
        changed = 0
        names = sorted({r["person"] for r in refs})
        R = np.stack([np.frombuffer(r["v"], dtype=np.float32) for r in refs]) if refs else None
        who = np.array([names.index(r["person"]) for r in refs]) if refs else None
        updates = []
        for k in range(0, len(targets), 2048):
            chunk = targets[k:k + 2048]
            best_name = [None] * len(chunk)
            if R is not None:
                T = np.stack([np.frombuffer(t["v"], dtype=np.float32) for t in chunk])
                S = T @ R.T
                per = np.full((len(chunk), len(names)), -1.0, dtype=np.float32)
                for j in range(len(names)):
                    per[:, j] = S[:, who == j].max(1)
                for i, t in enumerate(chunk):
                    order = np.argsort(-per[i])
                    while len(order) and (t["id"], names[order[0]]) in rejected:
                        order = order[1:]
                    if not len(order):
                        continue
                    top = per[i, order[0]]
                    second = max([per[i, j] for j in range(len(names)) if j != order[0]], default=-1.0)
                    if top >= FACE_AUTO and top - second >= FACE_MARGIN:
                        best_name[i] = names[order[0]]
            for i, t in enumerate(chunk):
                n = best_name[i]
                if n != t["person"]:
                    updates.append((n, "auto" if n else None, t["id"]))
        if updates:
            with self.db_lock:
                self.db.executemany("UPDATE faces SET person=?, by=? WHERE id=?", updates)
                self.db.commit()
            changed = len(updates)
        return changed

    def photo_faces(self, pid):
        with self.db_lock:
            return [dict(r) for r in self.db.execute("SELECT id, x, y, w, h, person, by FROM faces WHERE photo_id=? ORDER BY w * h DESC", (pid,))]

    def name_face(self, fid, name):
        """You say who this is (or clear it). Then every face is compared again."""
        name = (name or "").strip()[:40]
        with self.db_lock:
            f = self.db.execute("SELECT id, person, by FROM faces WHERE id=?", (fid,)).fetchone()
            if not f:
                return None
            if name:
                self.db.execute("UPDATE faces SET person=?, by='user' WHERE id=?", (name, fid))
                self.db.execute("DELETE FROM face_reject WHERE face_id=? AND person=?", (fid, name))
            else:
                if f["person"]:
                    self.db.execute("INSERT OR IGNORE INTO face_reject VALUES(?,?)", (fid, f["person"]))
                self.db.execute("UPDATE faces SET person=NULL, by=NULL WHERE id=?", (fid,))
            self.db.commit()
        self.assign_faces()
        self.bump()
        with self.db_lock:
            n = self.db.execute("""SELECT COUNT(DISTINCT f.photo_id) c FROM faces f JOIN photos p ON p.id = f.photo_id
                                   WHERE f.person=? AND p.trash=0""", (name,)).fetchone()["c"] if name else 0
        return n

    def not_person(self, person, ids):
        """These photos are not this person: the name is taken off and never comes back on them."""
        with self.db_lock:
            q = ",".join("?" * len(ids))
            rows = self.db.execute(f"SELECT id FROM faces WHERE person=? AND photo_id IN ({q})", [person, *ids]).fetchall()
            for r in rows:
                self.db.execute("INSERT OR IGNORE INTO face_reject VALUES(?,?)", (r["id"], person))
                self.db.execute("UPDATE faces SET person=NULL, by=NULL WHERE id=?", (r["id"],))
            self.db.commit()
        self.assign_faces()
        self.bump()
        return len(rows)

    def rename_person(self, old, new):
        new = (new or "").strip()[:40]
        with self.db_lock:
            if new:
                self.db.execute("UPDATE faces SET person=? WHERE person=?", (new, old))
                self.db.execute("UPDATE face_reject SET person=? WHERE person=?", (new, old))
            else:  # forget the person
                self.db.execute("UPDATE faces SET person=NULL, by=NULL WHERE person=?", (old,))
                self.db.execute("DELETE FROM face_reject WHERE person=?", (old,))
            self.db.commit()
        self.bump()

    def face_crop(self, fid):
        out = self.meta / "faces" / f"{fid}.jpg"
        if out.exists():
            return out
        with self.db_lock:
            r = self.db.execute("""SELECT f.x, f.y, f.w, f.h, p.id, p.path, p.kind, p.rot FROM faces f JOIN photos p ON p.id = f.photo_id
                                   WHERE f.id=?""", (fid,)).fetchone()
        if not r:
            return None
        im = self._face_image(r)
        W, H = im.size
        cx, cy, side = (r["x"] + r["w"] / 2) * W, (r["y"] + r["h"] / 2) * H, max(r["w"] * W, r["h"] * H) * 1.5
        box = (int(max(0, cx - side / 2)), int(max(0, cy - side / 2)), int(min(W, cx + side / 2)), int(min(H, cy + side / 2)))
        out.parent.mkdir(exist_ok=True)
        c = im.crop(box)
        c.thumbnail((160, 160))
        c.save(out, "JPEG", quality=85)
        return out

    # ---- videos
    def video_backfill(self):
        """Videos added by older versions: duration, codecs, size, GPS and a better thumbnail; then recognition."""
        with self.db_lock:
            rows = self.db.execute("SELECT id, path, rot, lat FROM photos WHERE kind='video' AND dur IS NULL").fetchall()
        if rows:
            print(f"Updating {len(rows)} videos…")
        for r in rows:
            f = self.abs(r["path"])
            vi = video_info(f) if f.exists() else {}
            with self.db_lock:
                self.db.execute("UPDATE photos SET dur=?, codec=?, ai_done=0 WHERE id=?", (vi.get("dur", -1), codec_str(vi) if vi else None, r["id"]))
                if vi.get("w"):
                    self.db.execute("UPDATE photos SET w=?, h=? WHERE id=?", (vi["w"], vi["h"], r["id"]))
                if r["lat"] is None and vi.get("lat") is not None:
                    place, country = self.places.lookup(vi["lat"], vi["lon"])
                    self.db.execute("UPDATE photos SET lat=?, lon=?, place=?, country=? WHERE id=?",
                                    (vi["lat"], vi["lon"], place, country, r["id"]))
                self.db.commit()
            if f.exists():
                self.make_thumb(r["id"], f, "video", r["rot"] or 0, vi.get("dur"))
        if rows:
            self.bump()

    def video_state(self, pid):
        """How the video can be played: directly, from the converted copy, or it is being converted."""
        with self.db_lock:
            r = self.db.execute("SELECT path, codec FROM photos WHERE id=? AND kind='video'", (pid,)).fetchone()
        if not r:
            return {"state": "none"}
        if (self.vcache / f"{pid}.mp4").exists():
            return {"state": "ready", "src": f"/vcache/{pid}.mp4"}
        job = self.vjobs.get(pid)
        if job:
            return dict(job)
        return {"state": "direct" if self.plays_directly(r["path"], r["codec"]) else "convert"}

    @staticmethod
    def plays_directly(path, codec):
        if Path(path).suffix.lower() not in BROWSER_VIDEO:
            return False
        v, a, _ = ((codec or "") + "//").split("/")[:3]
        return not codec or (v in DIRECT_VCODECS and a in DIRECT_ACODECS)

    def prepare_video(self, pid):
        if (self.vcache / f"{pid}.mp4").exists() or self.vjobs.get(pid, {}).get("state") == "working":
            return self.video_state(pid)
        with self.db_lock:
            r = self.db.execute("SELECT path, codec, dur FROM photos WHERE id=? AND kind='video'", (pid,)).fetchone()
        if not r or not FFMPEG:
            return {"state": "error", "error": "Videos can't be converted on this computer."}
        self.vjobs[pid] = {"state": "working", "progress": 0}
        threading.Thread(target=self._convert, args=(pid, self.abs(r["path"]), r["dur"] or 0, r["codec"] or ""), daemon=True).start()
        return dict(self.vjobs[pid])

    def _convert(self, pid, f, dur, codec):
        """Makes a copy that every browser plays (H.264/AAC MP4). Only the copy in .fotoarhiva is written."""
        out, tmp = self.vcache / f"{pid}.mp4", self.vcache / f"{pid}.part.mp4"
        v, a, _ = (codec + "//").split("/")[:3]
        reencode = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-vf", "scale=w='trunc(min(1280,iw)/2)*2':h=-2", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"]
        tries = []
        if v == "h264":  # only the container is wrong (e.g. MKV, AVI, MTS): fast, no quality loss
            tries.append(["-c:v", "copy"] + (["-c:a", "copy"] if a in ("aac", "") else ["-c:a", "aac", "-b:a", "160k"]))
        tries.append(reencode)
        with self.vsem:
            for args in tries:
                cmd = [FFMPEG, "-v", "error", "-y", "-i", str(f), "-map", "0:v:0", "-map", "0:a:0?", *args,
                       "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(tmp)]
                try:
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **NOWIN)
                    for line in proc.stdout:
                        if line.startswith(b"out_time_us=") or line.startswith(b"out_time_ms="):
                            try:
                                t = int(line.split(b"=")[1]) / 1e6
                                if dur > 0:
                                    self.vjobs[pid]["progress"] = round(min(0.99, t / dur), 3)
                            except ValueError:
                                pass
                    rc = proc.wait()
                except Exception as e:
                    print("Video conversion:", e)
                    rc = -1
                if rc == 0 and tmp.exists() and tmp.stat().st_size > 0:
                    tmp.replace(out)
                    self.vjobs[pid] = {"state": "ready", "src": f"/vcache/{pid}.mp4"}
                    return
            if tmp.exists():
                tmp.unlink()
            self.vjobs[pid] = {"state": "error", "error": "This video could not be converted for playback. Use “Open” to play it in another program."}


    def update_photo(self, pid, data):
        self.dups_dirty = True
        with self.db_lock:
            p = self.db.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
            if not p:
                return
            if "fav" in data:
                self.db.execute("UPDATE photos SET fav=? WHERE id=?", (1 if data["fav"] else 0, pid))
            if "rotate" in data:
                rot = (int(p["rot"] or 0) + int(data["rotate"])) % 360
                self.db.execute("UPDATE photos SET rot=?, faces_done=0 WHERE id=?", (rot, pid))
                self.make_thumb(pid, self.abs(p["path"]), p["kind"], rot)
                pv = self.previews / f"{pid}.jpg"
                if pv.exists():
                    pv.unlink()
            if "add_tag" in data:
                t = str(data["add_tag"]).strip()[:40]
                if t:
                    self.db.execute("INSERT OR REPLACE INTO tags VALUES(?,?,?,?)", (pid, t, 1.0, "user"))
                    self.db.execute("DELETE FROM hidden_tags WHERE photo_id=? AND tag=?", (pid, t))
                    self._learn_soon(t)
            if "remove_tag" in data:
                t = str(data["remove_tag"])
                was_user = self.db.execute("SELECT 1 FROM tags WHERE photo_id=? AND tag=? AND by='user'", (pid, t)).fetchone()
                self.db.execute("DELETE FROM tags WHERE photo_id=? AND tag=?", (pid, t))
                self.db.execute("INSERT OR IGNORE INTO hidden_tags VALUES(?,?)", (pid, t))
                if was_user:
                    self._learn_soon(t)
            if "taken" in data:
                try:
                    t = datetime.fromisoformat(str(data["taken"])[:19])
                    self.db.execute("UPDATE photos SET taken=? WHERE id=?", (t.isoformat(timespec="seconds"), pid))
                    if not p["trash"]:
                        self._move_kind(pid, p["path"], t, p["kind"])
                except ValueError:
                    pass
            if "trash" in data:
                src = self.abs(p["path"])
                if data["trash"] and not p["trash"]:
                    self.trash_dir.mkdir(exist_ok=True)
                    dest = self.unique_dest(self.trash_dir, src.name)
                    if src.exists():
                        shutil.move(str(src), str(dest))
                    self.db.execute("UPDATE photos SET trash=1, path=? WHERE id=?", (self.rel(dest), pid))
                elif not data["trash"] and p["trash"]:
                    dest_dir = self.dest_dir_for(datetime.fromisoformat(p["taken"]), p["kind"])
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest = self.unique_dest(dest_dir, p["name"])
                    if src.exists():
                        shutil.move(str(src), str(dest))
                    self.db.execute("UPDATE photos SET trash=0, path=? WHERE id=?", (self.rel(dest), pid))
                self.emb_cache = None
            self.db.commit()
        self.bump()

    def delete_forever(self, ids):
        with self.db_lock:
            for pid in ids:
                p = self.db.execute("SELECT * FROM photos WHERE id=? AND trash=1", (pid,)).fetchone()
                if not p:
                    continue
                f = self.abs(p["path"])
                if f.exists():
                    f.unlink()
                for t in (self.thumbs / f"{pid}.jpg", self.previews / f"{pid}.jpg", self.vcache / f"{pid}.mp4"):
                    if t.exists():
                        t.unlink()
                for fr in self.db.execute("SELECT id FROM faces WHERE photo_id=?", (pid,)).fetchall():
                    self.db.execute("DELETE FROM face_reject WHERE face_id=?", (fr["id"],))
                    crop = self.meta / "faces" / f"{fr['id']}.jpg"
                    if crop.exists():
                        crop.unlink()
                for tbl in ("photos WHERE id", "album_photos WHERE photo_id", "tags WHERE photo_id", "emb WHERE photo_id",
                            "hidden_tags WHERE photo_id", "phash WHERE photo_id", "dup_ignore WHERE a", "dup_ignore WHERE b", "faces WHERE photo_id"):
                    self.db.execute(f"DELETE FROM {tbl}=?", (pid,))
                self.dups_dirty = True
            self.db.commit()
        self.emb_cache = None
        self.bump()

    def create_album(self, name, color):
        with self.db_lock:
            self.db.execute("INSERT OR IGNORE INTO albums(name,color) VALUES(?,?)", (name, color))
            self.db.commit()
            aid = self.db.execute("SELECT id FROM albums WHERE name=?", (name,)).fetchone()["id"]
        self.bump()
        return aid

    def album_op(self, aid, action, ids=(), name=None):
        with self.db_lock:
            if action == "add":
                for pid in ids:
                    self.db.execute("INSERT OR IGNORE INTO album_photos VALUES(?,?)", (aid, pid))
            elif action == "remove":
                for pid in ids:
                    self.db.execute("DELETE FROM album_photos WHERE album_id=? AND photo_id=?", (aid, pid))
            elif action == "rename" and name:
                self.db.execute("UPDATE albums SET name=? WHERE id=?", (name, aid))
            elif action == "delete":
                self.db.execute("DELETE FROM albums WHERE id=?", (aid,))
                self.db.execute("DELETE FROM album_photos WHERE album_id=?", (aid,))
            self.db.commit()
        self.bump()

    def _learn_soon(self, tag):
        # many tags at once (many photos selected) → one learning run per tag
        self._pending_learn = getattr(self, "_pending_learn", set())
        if tag in self._pending_learn:
            return
        self._pending_learn.add(tag)
        def run():
            time.sleep(1.0)
            self._pending_learn.discard(tag)
            try:
                self.learn(tag)
            except Exception as e:
                print("Learning:", e)
        threading.Thread(target=run, daemon=True).start()

    def reset_ai(self):
        """Deletes the old AI tags and vectors (new model or new categories) and analyzes the photos again."""
        with self.db_lock:
            self.db.execute("DELETE FROM tags WHERE by='ai'")
            self.db.execute("DELETE FROM emb")
            self.db.execute("UPDATE photos SET ai_done=0")
            self.db.commit()
        self.emb_cache = None
        self.status["ai_done"] = 0
        self.bump()

    def emb_matrix(self):
        import numpy as np
        with self.db_lock:
            if self.emb_cache is None:
                rows = self.db.execute("SELECT e.photo_id, e.v FROM emb e JOIN photos p ON p.id=e.photo_id WHERE p.trash=0").fetchall()
                ids = np.array([r["photo_id"] for r in rows], dtype=np.int64)
                M = np.stack([np.frombuffer(r["v"], dtype=np.float16) for r in rows]).astype(np.float32) if rows else np.zeros((0, 1), np.float32)
                self.emb_cache = (ids, M)
        return self.emb_cache

    def semantic_search(self, qv, limit=300, min_sim=-1.0):
        import numpy as np
        ids, M = self.emb_matrix()
        if not len(ids) or M.shape[1] != qv.shape[0]:
            return []
        s = M @ qv
        mu, sd = float(s.mean()), float(s.std() or 1e-6)
        order = np.argsort(-s)[:limit]
        top = float(s[order[0]])
        if len(ids) > 20:
            cut = max(mu + 1.6 * sd, top - 6 * sd)
        else:  # few photos: z-scores stay small, so keep the clearly above-average ones
            cut = max(mu + 0.5 * sd, top - 0.08, min_sim)
        return [{"id": int(ids[i]), "score": round(float((s[i] - mu) / sd), 2)} for i in order if s[i] >= cut]

    def reanalyze(self):
        with self.db_lock:
            self.db.execute("UPDATE photos SET ai_done=0 WHERE ai_done IN (1, 2)")
            self.db.commit()
        self.status["ai_done"] = 0

    def preview(self, pid, f: Path, rot):
        out = self.previews / f"{pid}.jpg"
        if not out.exists():
            with Image.open(f) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((2400, 2400))
                im.convert("RGB").save(out, "JPEG", quality=88)
        return out

    def history(self, pid):
        """Where the photo came from: original location, method, date added and other copies."""
        with self.db_lock:
            p = self.db.execute("SELECT id,name,path,orig,source,added,how FROM photos WHERE id=?", (pid,)).fetchone()
            if not p:
                return None
            copies = [r["path"] for r in self.db.execute(
                "SELECT path FROM seen WHERE dup_of=? AND result='duplicate' ORDER BY path", (pid,))]
        how = p["how"] or ("upload" if p["source"] == "Imported" else self.cfg.get("mode", "copy"))
        orig = p["orig"] or ""
        has_path = how != "upload" and (os.sep in orig or "/" in orig or "\\" in orig)
        return {"orig": orig if has_path else "", "upload_name": orig if not has_path else "", "how": how,
                "added": p["added"], "root": p["source"] if p["source"] != "Imported" else "",
                "orig_exists": bool(has_path and Path(orig).exists()),
                "orig_dir_exists": bool(has_path and Path(orig).parent.exists()),
                "copies": [{"path": c, "exists": Path(c).exists()} for c in copies[:50]], "copies_total": len(copies),
                "now": self.rel(self.abs(p["path"]))}

    def photo_path(self, pid):
        with self.db_lock:
            p = self.db.execute("SELECT path FROM photos WHERE id=?", (pid,)).fetchone()
        return self.abs(p["path"]) if p else None


# ----------------------------------------------------------------- system helpers (folder picker, shortcuts)
def open_path(f: Path, select=False):
    try:
        if IS_WIN:
            if select:
                subprocess.Popen(["explorer", "/select,", str(f)])
            else:
                os.startfile(str(f))
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", "-R", str(f)] if select else ["open", str(f)])
        else:
            subprocess.Popen(["xdg-open", str(f.parent if select else f)])
    except Exception as e:
        print("Cannot open:", e)


def pick_folder_dialog(title="Choose a folder"):
    """A real Windows/Mac folder picker (tkinter in a separate process)."""
    code = ("import tkinter as tk, tkinter.filedialog as fd, sys\n"
            "r=tk.Tk(); r.withdraw(); r.attributes('-topmost', True); r.lift(); r.focus_force()\n"
            f"p=fd.askdirectory(parent=r, title={title!r}, mustexist=True)\n"
            "sys.stdout.write(p or '')\n")
    exe = sys.executable
    if IS_WIN and exe.lower().endswith("pythonw.exe"):
        exe = exe[:-5] + ".exe"
    r = subprocess.run([exe, "-c", code], capture_output=True, text=True, timeout=600, **NOWIN)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "the dialog is not available")
    return r.stdout.strip() or None


def list_roots():
    roots = []
    if IS_WIN:
        import string
        for d in string.ascii_uppercase:
            p = f"{d}:\\"
            if os.path.exists(p):
                roots.append({"name": f"Drive {d}:", "path": p})
    else:
        roots.append({"name": "/", "path": "/"})
    home = Path.home()
    for label, p in (("Home folder", home), ("Pictures", home / "Pictures"), ("Desktop", home / "Desktop"),
                     ("Downloads", home / "Downloads"), ("Documents", home / "Documents"), ("OneDrive", home / "OneDrive")):
        if p.exists():
            roots.append({"name": label, "path": str(p)})
    return roots


def browse(path):
    if not path:
        return {"path": "", "parent": None, "dirs": [], "roots": list_roots()}
    p = Path(path)
    dirs = []
    try:
        for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
            try:
                if e.is_dir() and not e.name.startswith((".", "$")) and e.name not in SKIP_DIRS:
                    dirs.append({"name": e.name, "path": e.path})
            except OSError:
                pass
            if len(dirs) >= 800:
                break
    except OSError as e:
        return {"path": str(p), "parent": str(p.parent) if p.parent != p else "", "dirs": [], "roots": list_roots(), "error": str(e)}
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else "", "dirs": dirs, "roots": list_roots()}


def count_media(p: Path, budget=1.5):
    n, t0 = 0, time.time()
    try:
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS and d not in ("Fotoarhiva", OLD_LIB_NAME)]
            n += sum(1 for f in filenames if Path(f).suffix.lower() in IMAGE_EXT or Path(f).suffix.lower() in VIDEO_EXT)
            if time.time() - t0 > budget:
                return n, True
    except Exception:
        pass
    return n, False


def suggest_folders(library):
    home = Path.home()
    cands = [home / "Pictures", home / "Pictures", home / "Downloads", home / "Desktop", home / "Documents", home / "Videos",
             home / "OneDrive" / "Pictures", home / "OneDrive" / "Pictures", home / "OneDrive" / "Desktop",
             home / "Dropbox" / "Camera Uploads", home / "Google Drive"]
    if IS_WIN:
        import string
        for d in string.ascii_uppercase[3:]:  # D: E: … (external drives, memory cards)
            r = Path(f"{d}:\\")
            if r.exists():
                for sub in ("DCIM", "Photos", "Pictures", "Pictures", "Photos", "Slike"):
                    if (r / sub).exists():
                        cands.append(r / sub)
    out, seen = [], set()
    lib = Path(library)
    for c in cands:
        try:
            if not c.exists() or c.resolve() in seen or is_inside(c, lib):
                continue
            seen.add(c.resolve())
        except Exception:
            continue
        n, more = count_media(c)
        out.append({"path": str(c), "name": c.name, "count": n, "more": more})
    return out


def make_icon():
    for cand in (APP_DIR / "fotoarhiva.ico", APP_DIR.parent / "fotoarhiva.ico"):
        if cand.exists():
            return cand
    ico = DATA_DIR / "fotoarhiva.ico"
    if ico.exists():
        return ico
    s = 256
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((8, 8, s - 8, s - 8), radius=56, fill=(43, 95, 217, 255))
    d.rounded_rectangle((52, 70, 204, 186), radius=14, outline=(255, 255, 255, 255), width=14)
    d.ellipse((86, 98, 118, 130), fill=(255, 255, 255, 255))
    d.line((70, 176, 130, 124, 156, 148, 190, 112), fill=(255, 255, 255, 255), width=14, joint="curve")
    im.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ico


def pythonw_exe():
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def win_folder(kind):
    r = subprocess.run(["powershell", "-NoProfile", "-Command", f"[Environment]::GetFolderPath('{kind}')"],
                       capture_output=True, text=True, timeout=30, **NOWIN)
    return Path(r.stdout.strip())


def create_lnk(lnk: Path, args=""):
    ico = make_icon()
    q = lambda x: str(x).replace("'", "''")
    script = APP_DIR / "fotoarhiva.py"
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(lnk)}');"
          f"$s.TargetPath='{q(pythonw_exe())}';"
          f"$s.Arguments='\"{q(script)}\" run {args}';"
          f"$s.WorkingDirectory='{q(APP_DIR)}';"
          f"$s.IconLocation='{q(ico)}';$s.Description='Fotoarhiva';$s.Save()")
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       capture_output=True, text=True, timeout=60, **NOWIN)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])


def uninstaller():
    u = APP_DIR.parent / "Uninstall.exe"
    return u if IS_WIN and u.exists() else None


def startup_lnk(old=False):
    return win_folder("Startup") / ((OLD_LIB_NAME if old else "Fotoarhiva") + ".lnk")


def create_shortcuts():
    if not IS_WIN:
        return "Shortcuts are only made on Windows."
    create_lnk(win_folder("Desktop") / "Fotoarhiva.lnk")
    try:
        create_lnk(win_folder("Programs") / "Fotoarhiva.lnk")
    except Exception:
        pass
    return "ok"


def set_autostart(on):
    if not IS_WIN:
        return "Only available on Windows."
    lnk, old = startup_lnk(), startup_lnk(old=True)
    if old.exists():
        old.unlink()  # shortcut from the Macedonian versions
    if on:
        create_lnk(lnk, "--no-browser")
    elif lnk.exists():
        lnk.unlink()
    return "ok"


def autostart_on():
    try:
        return IS_WIN and (startup_lnk().exists() or startup_lnk(old=True).exists())
    except Exception:
        return False


# ----------------------------------------------------------------- app
class App:
    def __init__(self):
        self.cfg = load_config()
        rename_old_library(self.cfg)
        self.cfg.pop("tag_lang", None)
        self.auth = Auth(self.cfg)
        self.faces = Faces(DATA_DIR / "models")
        self.background = False          # started without a window (Start with Windows): keeps running
        self.last_ui = time.time()       # last request from an open window
        self.bye_at = None               # the window said it is closing
        if not self.cfg.get("configured"):
            self.cfg["seen_version"] = NEWS_VERSION  # a new installation has no "what's new"
        for m in self.cfg.get("my_places", []):  # the old default names of my places
            m["name"] = OLD_PLACES.get(m.get("name"), m.get("name"))
        self.places = Places()
        self.lib = Library(self.cfg, self.places)
        old = DATA_DIR / "models" / "vision.onnx"  # model from version 2.0
        if old.exists():
            for f in ("vision.onnx", "text.onnx", "tokenizer.json"):
                try:
                    (DATA_DIR / "models" / f).unlink()
                except OSError:
                    pass
        self.ai = AI(DATA_DIR / "models", self.cfg.get("model", DEFAULT_MODEL))
        self.ai.set_custom(self.cfg.get("my_categories", []))
        self.cloud = CloudAI()
        if self.cfg.get("cloud_enabled") and self.cfg.get("describer") != "cloud":
            self.cfg["describer"] = "cloud"  # a user who already used Claude
            self.cfg["cloud_enabled"] = True
        self.cloud.configure(self.cfg.get("describer") == "cloud", self.cfg.get("cloud_key"), self.cfg.get("cloud_model"))
        self.vlm = LocalVLM(DATA_DIR / "models")
        self.safe = SAFE
        if SAFE:
            print("Safe mode: the models are not started.")
            self.cloud.configure(False, "", None)
        elif self.cfg.get("configured"):
            self.vlm.configure(self.cfg.get("describer") == "local")
        self.need_reclassify = False
        sig = self.labels_sig()
        old = str(self.cfg.get("labels_sig", ""))
        if old.split(":")[0] != self.ai.model_id:
            self.lib.reset_ai()          # different model → new analysis
        elif old != sig:
            self.need_reclassify = True   # new categories/logic → only re-scoring
        self.cfg["labels_sig"] = sig
        save_config(self.cfg)
        self.wake = threading.Event()
        self.httpd = None
        self._autostart = autostart_on()

    def labels_sig(self):
        from ai import Z_MIN, BACKGROUND
        key = json.dumps([[l[1] for l in LABELS], self.cfg.get("my_categories", []), Z_MIN, BACKGROUND], ensure_ascii=False)
        return f"{self.ai.model_id}:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]

    def cloud_labels(self):
        return [(en, en) for mk, en, _, _ in LABELS if not en.startswith("_")] + \
               [(c["name"], c.get("desc") or c["name"]) for c in self.cfg.get("my_categories", [])]

    def describer(self):
        if not self.cfg.get("configured") or self.safe:
            return None, 0
        d = self.cfg.get("describer")
        if d == "cloud" and self.cloud.state == "ready":
            return self.cloud, 3
        if d == "local" and self.vlm.state == "ready":
            return self.vlm, 1
        return None, 0

    def cloud_worker(self):
        while True:
            try:
                desc, workers = self.describer()
                if desc and self.cfg.get("cloud_running", True) and self.lib.status["state"] != "scanning":
                    if self.lib.cloud_step(desc, self.cloud_labels(), workers=workers):
                        continue
            except Exception as e:
                print("Claude worker:", e)
                time.sleep(10)
            time.sleep(3)

    def status(self):
        s = dict(self.lib.status)
        s["cloud"] = self.cloud.info()
        s["vlm"] = self.vlm.info()
        s["describer"] = "off" if self.safe else self.cfg.get("describer", "off")
        s["safe"] = self.safe
        if self.cfg.get("describer") in ("cloud", "local"):
            n = self.lib.cloud_pending()
            s["cloud"]["pending"] = s["vlm"]["pending"] = n
            s["cloud"]["est_usd"] = round(n * CLOUD_MODELS[self.cloud.model]["usd_per_1000"] / 1000, 2)
        s["ai"] = self.ai.info()
        if self.ai.state == "ready":
            s["ai_pending"] = self.lib.ai_pending()
        s["faces_pending"] = self.lib.face_pending() if self.cfg.get("faces", True) else 0
        return s

    def config_view(self):
        return {"library": str(self.lib.root), "sources": self.cfg["sources"], "mode": self.cfg["mode"],
                "watch_seconds": self.cfg["watch_seconds"], "configured": self.cfg["configured"], "ai": self.cfg.get("ai", True),
                "version": VERSION, "windows": IS_WIN, "heif": HEIF, "ffmpeg": bool(FFMPEG), "autostart": self._autostart,
                "log": str(LOG_PATH), "model": self.ai.model_id, "my_places": self.cfg.get("my_places", []),
                "models_ready": {"ai": self.ai.downloaded(), "vlm": self.vlm.downloaded()},
                "my_categories": self.cfg.get("my_categories", []),
                "cloud_enabled": self.cfg.get("describer") == "cloud", "describer": self.cfg.get("describer", "off"), "cloud_model": self.cloud.model,
                "cloud_key_hint": ("…" + self.cfg["cloud_key"][-4:]) if self.cfg.get("cloud_key") else "",
                "cloud_running": self.cfg.get("cloud_running", True), "login": self.auth.view(),
                "whats_new": bool(self.cfg.get("configured")) and self.cfg.get("seen_version") != NEWS_VERSION,
                "keep_running": bool(self.cfg.get("keep_running")), "background": self.background,
                "faces": self.cfg.get("faces", True), "faces_state": self.faces.state,
                "cloud_models": [{"id": k, "name": v["name"], "usd_per_1000": v["usd_per_1000"]} for k, v in CLOUD_MODELS.items()], "can_uninstall": uninstaller() is not None,
                "models": [{"id": k, "name": v["name"], "name_en": v["name_en"], "about": v["about"], "multilingual": v["multilingual"]} for k, v in MODELS.items()]}

    def update_config(self, data):
        if data.get("seen_version"):
            self.cfg["seen_version"] = NEWS_VERSION
        if "faces" in data:
            self.cfg["faces"] = bool(data["faces"])
        if "keep_running" in data:
            self.cfg["keep_running"] = bool(data["keep_running"])
        if "sources" in data:
            srcs = []
            for s in data["sources"]:
                s = str(s).strip()
                if s and s not in srcs and Path(s).exists() and not is_inside(Path(s), self.lib.root):
                    srcs.append(s)
            self.cfg["sources"] = srcs
        if data.get("mode") in ("copy", "move"):
            self.cfg["mode"] = data["mode"]
        if "watch_seconds" in data:
            self.cfg["watch_seconds"] = max(10, min(3600, int(data["watch_seconds"])))
        if "configured" in data:
            first = bool(data["configured"]) and not self.cfg.get("configured")
            self.cfg["configured"] = bool(data["configured"])
            if first and not self.safe and "describer" not in data:
                self.vlm.configure(self.cfg.get("describer") == "local")
        if "ai" in data:
            self.cfg["ai"] = bool(data["ai"])
        if data.get("describer") in ("off", "local", "cloud"):
            self.cfg["describer"] = data["describer"]
            self.cfg["cloud_enabled"] = data["describer"] == "cloud"
            if self.cfg.get("configured") or data.get("configured"):
                self.vlm.configure(data["describer"] == "local")
            self.cloud.configure(data["describer"] == "cloud", self.cfg.get("cloud_key"), self.cfg.get("cloud_model"))
        if any(k in data for k in ("cloud_enabled", "cloud_key", "cloud_model", "cloud_running")):
            if "cloud_key" in data and str(data["cloud_key"]).strip():
                self.cfg["cloud_key"] = str(data["cloud_key"]).strip()
            if data.get("cloud_model") in CLOUD_MODELS:
                self.cfg["cloud_model"] = data["cloud_model"]
            if "cloud_enabled" in data:
                self.cfg["cloud_enabled"] = bool(data["cloud_enabled"])
                if data["cloud_enabled"]:
                    self.cfg["describer"] = "cloud"
                    self.vlm.configure(False)
                elif self.cfg.get("describer") == "cloud":
                    self.cfg["describer"] = "off"
            if "cloud_running" in data:
                self.cfg["cloud_running"] = bool(data["cloud_running"])
            if data.get("cloud_scope") == "new":
                with self.lib.db_lock:  # existing photos are not sent, only new ones
                    self.lib.db.execute("UPDATE photos SET cloud_done=3 WHERE cloud_done=0")
                    self.lib.db.commit()
            elif data.get("cloud_scope") == "all":
                with self.lib.db_lock:
                    self.lib.db.execute("UPDATE photos SET cloud_done=0 WHERE cloud_done=3")
                    self.lib.db.commit()
            self.cloud.configure(self.cfg.get("describer") == "cloud", self.cfg.get("cloud_key"), self.cfg.get("cloud_model"))
        if "my_categories" in data:
            cats = []
            for c in data["my_categories"]:
                n = str(c.get("name", "")).strip()[:40]
                if n and n not in [x["name"] for x in cats]:
                    cats.append({"name": n, "desc": str(c.get("desc", "")).strip()[:200]})
            self.cfg["my_categories"] = cats
            self.ai.set_custom(cats)
            self.cfg["labels_sig"] = self.labels_sig()
            if self.ai.state == "ready":
                threading.Thread(target=self.lib.reclassify, args=(self.ai,), daemon=True).start()
        if "my_places" in data:
            mp = []
            for m in data["my_places"]:
                try:
                    mp.append({"name": str(m["name"]).strip()[:40] or "Place", "lat": float(m["lat"]), "lon": float(m["lon"]),
                               "radius": max(50, min(20000, int(m.get("radius", 300))))})
                except (KeyError, TypeError, ValueError):
                    pass
            self.cfg["my_places"] = mp
        if data.get("library") and Path(data["library"]).resolve() != self.lib.root.resolve():
            self.cfg["library"] = str(Path(data["library"]))
            with self.lib.scan_lock:
                old = self.lib
                self.lib = Library(self.cfg, self.places)
                self.lib.status["version"] = old.status["version"] + 1
                old.db.close()
        self.lib.cfg = self.cfg
        save_config(self.cfg)
        self.lib.bump()
        self.wake.set()

    def watcher(self):
        while True:
            try:
                if self.cfg.get("configured") and self.cfg.get("sources"):
                    self.lib.organize()
            except Exception as e:
                print("Scan error:", e)
            self.wake.wait(max(10, int(self.cfg.get("watch_seconds", 30))))
            self.wake.clear()

    def ai_worker(self):
        if self.safe:
            return
        while not self.cfg.get("configured"):  # nothing is recognized before the welcome guide is finished
            time.sleep(2)
        if self.ai.state != "missing_deps":
            if self.ai.downloaded():
                self.ai.load()
            elif self.cfg.get("ai_requested") and self.cfg.get("ai", True):
                self.ai.download()  # resume an interrupted download
        if self.ai.state == "ready":
            try:
                self.lib.learn_all()  # learned tags are also computed for photos added in the meantime
            except Exception as e:
                print("Learning:", e)
        while True:
            try:
                if self.need_reclassify and self.ai.state == "ready":
                    self.need_reclassify = False
                    self.lib.reclassify(self.ai)
                if self.ai.state == "ready" and self.cfg.get("ai", True) and self.lib.status["state"] != "scanning":
                    if self.lib.ai_step(self.ai):
                        continue
            except Exception as e:
                print("Recognition error:", e)
                time.sleep(5)
            time.sleep(2)

    def shutdown(self):
        """Quits cleanly: the describing process is stopped and the database is closed."""
        print(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} · Fotoarhiva quits")
        try:
            if self.vlm.child:
                self.vlm.child.kill()
        except Exception:
            pass
        if self.lib.db_lock.acquire(timeout=10):
            try:
                self.lib.db.commit()
                self.lib.db.close()
            except Exception:
                pass
        os._exit(0)

    def idle_watch(self):
        """When the window is closed, the app quits too (unless it should keep working in the background)."""
        while True:
            time.sleep(4)
            if self.background or self.cfg.get("keep_running"):
                self.bye_at = None
                continue
            now = time.time()
            if self.bye_at and now - self.bye_at > 12 and self.last_ui < self.bye_at + 1:
                self.shutdown()   # the window was closed (a reload would have asked for the page again)
            if now - self.last_ui > 600:
                self.shutdown()   # no window has been open for 10 minutes

    def maintenance(self):
        """Background: updates videos from older versions and keeps the list of similar copies up to date."""
        while not self.cfg.get("configured"):
            time.sleep(3)
        updated = None
        while True:
            lib = self.lib  # the library can be moved in Settings
            try:
                if updated is not lib:
                    updated = lib
                    lib.video_backfill()
                if not self.safe and self.cfg.get("faces", True) and lib.status["state"] != "scanning" \
                        and lib.face_pending() and self.faces.load():
                    if lib.face_step(self.faces):
                        time.sleep(0.05)
                        continue
                if lib.dups_dirty and lib.status["state"] != "scanning":
                    lib.similar_copies()
                self.vlm.release_if_idle()
            except Exception as e:
                print("Maintenance:", e)
            time.sleep(20)

    def fix_group(self, old, new, desc, ids):
        """The user said what the photos really are: the wrong tag is removed (and never comes back),
        the new one is added, becomes your category and is learned from the examples so similar photos get fixed too."""
        lib = self.lib
        with lib.db_lock:
            for pid in ids:
                lib.db.execute("DELETE FROM tags WHERE photo_id=? AND tag=?", (pid, old))
                lib.db.execute("INSERT OR IGNORE INTO hidden_tags VALUES(?,?)", (pid, old))
                if new:
                    lib.db.execute("INSERT OR REPLACE INTO tags VALUES(?,?,?,?)", (pid, new, 1.0, "user"))
                    lib.db.execute("DELETE FROM hidden_tags WHERE photo_id=? AND tag=?", (pid, new))
            lib.db.commit()
        if new and new not in BUILTIN and new not in [c["name"] for c in self.cfg.get("my_categories", [])]:
            self.cfg.setdefault("my_categories", []).append({"name": new, "desc": desc or new})
            self.ai.set_custom(self.cfg["my_categories"])
            self.cfg["labels_sig"] = self.labels_sig()
            save_config(self.cfg)

        def later():
            try:
                if new:
                    lib.learn(new)
                if self.ai.state == "ready":
                    lib.reclassify(self.ai)
            except Exception as e:
                print("Group fix:", e)
        threading.Thread(target=later, daemon=True).start()
        lib.bump()
        return len(ids)

    def set_model(self, model_id):
        if model_id not in MODELS or model_id == self.ai.model_id:
            return
        self.cfg["model"] = model_id
        self.ai = AI(DATA_DIR / "models", model_id)
        self.lib.reset_ai()
        self.ai.set_custom(self.cfg.get("my_categories", []))
        self.cfg["labels_sig"] = self.labels_sig()
        save_config(self.cfg)
        if self.ai.state == "not_downloaded":
            threading.Thread(target=self.ai.download, daemon=True).start()
        elif self.ai.downloaded():
            threading.Thread(target=self.ai.load, daemon=True).start()

    def enable_ai(self):
        self.cfg["ai"] = True
        self.cfg["ai_requested"] = True
        save_config(self.cfg)
        if self.ai.state in ("not_downloaded", "error"):
            threading.Thread(target=self.ai.download, daemon=True).start()


# ----------------------------------------------------------------- http
# ----------------------------------------------------------------- login
DEFAULT_USER, DEFAULT_PASSWORD = "admin", "0409979"
SESSION_DAYS = 30


def pw_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()


class Auth:
    """Username and password for opening Fotoarhiva. The default login is admin / 0409979; change it in Settings."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        a = cfg.get("auth")
        if not isinstance(a, dict) or not a.get("hash"):
            self._set(DEFAULT_USER, DEFAULT_PASSWORD, default=True)
        self.path = DATA_DIR / "sessions.json"
        # lets the installer close a running app without signing in (the file is readable only on this computer)
        self.quit_token = secrets.token_urlsafe(24)
        try:
            (DATA_DIR / "quit.token").write_text(self.quit_token, "utf-8")
        except OSError:
            pass
        try:
            self.sessions = {k: float(v) for k, v in json.loads(self.path.read_text("utf-8")).items()}
        except Exception:
            self.sessions = {}

    def _set(self, user, password, default=False):
        salt = secrets.token_hex(16)
        self.cfg["auth"] = {"user": user, "salt": salt, "hash": pw_hash(password, salt), "default": default}
        save_config(self.cfg)

    def check(self, user, password):
        a = self.cfg["auth"]
        ok_user = hmac.compare_digest(str(user).strip().lower(), a["user"].lower())
        ok_pw = hmac.compare_digest(pw_hash(str(password), a["salt"]), a["hash"])
        return ok_user and ok_pw

    def _save(self):
        try:
            self.path.write_text(json.dumps(self.sessions), "utf-8")
        except OSError:
            pass

    def login(self):
        token = secrets.token_urlsafe(32)
        with self.lock:
            now = time.time()
            self.sessions = {k: v for k, v in self.sessions.items() if v > now}
            self.sessions[token] = now + SESSION_DAYS * 86400
            self._save()
        return token

    def valid(self, token):
        return bool(token) and self.sessions.get(token, 0) > time.time()

    def logout(self, token=None):
        with self.lock:
            if token:
                self.sessions.pop(token, None)
            else:
                self.sessions = {}
            self._save()

    def change(self, current, user, password):
        a = self.cfg["auth"]
        if not hmac.compare_digest(pw_hash(str(current), a["salt"]), a["hash"]):
            return "The current password is not correct."
        user = str(user or a["user"]).strip()[:40]
        password = str(password or "")
        if not user:
            return "Enter a username."
        if len(password) < 4:
            return "The new password must have at least 4 characters."
        self._set(user, password)
        return None

    def view(self):
        a = self.cfg["auth"]
        return {"user": a["user"], "default": bool(a.get("default"))}


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fotoarhiva · Sign in</title><style>
:root{--bg:#f3f4f7;--card:#fff;--ink:#16181d;--mut:#6b7080;--line:#dfe2ea;--acc:#2B5FD9;--bad:#d3463c}
@media (prefers-color-scheme:dark){:root{--bg:#14161b;--card:#1d2027;--ink:#eceef3;--mut:#9aa0ad;--line:#30343e;--acc:#5b86f0;--bad:#f07068}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px}
form{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px;width:100%;max-width:360px;display:grid;gap:14px;box-shadow:0 10px 40px rgba(0,0,0,.08)}
h1{margin:0;font-size:22px}p{margin:0;color:var(--mut);font-size:13.5px}label{display:grid;gap:6px;font-size:13px;font-weight:600}
input{font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:transparent;color:inherit}input:focus{outline:2px solid var(--acc);outline-offset:-1px}
button{font:inherit;font-weight:600;padding:11px;border:0;border-radius:10px;background:var(--acc);color:#fff;cursor:pointer}#err{color:var(--bad);font-size:13.5px;min-height:1em}
.logo{width:44px;height:44px;border-radius:12px;background:var(--acc);display:grid;place-items:center}
</style></head><body><form id="f"><div class="logo"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="11" r="2"/><path d="m21 15-4.5-4.5L8 19"/></svg></div>
<h1>Fotoarhiva</h1><p>Sign in to open your photos.</p>
<label>Username<input id="u" autocomplete="username" autofocus required></label>
<label>Password<input id="p" type="password" autocomplete="current-password" required></label>
<div id="err"></div><button>Sign in</button></form>
<script>setInterval(()=>fetch('/api/ping').catch(()=>{}),20000);addEventListener('pagehide',()=>navigator.sendBeacon('/api/bye'));
document.getElementById('f').onsubmit=async e=>{e.preventDefault();const err=document.getElementById('err');err.textContent='';
const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user:document.getElementById('u').value,password:document.getElementById('p').value})});
if(r.ok)location.replace('/');else{err.textContent=(await r.json().catch(()=>({}))).error||'Sign-in failed.';document.getElementById('p').select()}}</script></body></html>"""


def make_handler(app: App):
    ui_path = APP_DIR / "ui.html"
    port = int(app.cfg.get("port", 8765))
    allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def send_json(self, obj, code=200):
            b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def send_bytes(self, b, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def send_file(self, p, cache=True):
            if not p or not Path(p).is_file():
                self.send_error(404)
                return
            p = Path(p)
            ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            if p.suffix.lower() == ".mov":
                ctype = "video/mp4"
            size = p.stat().st_size
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start = int(a) if a else max(0, size - int(b or 0))
                end = min(int(b) if (b and a) else size - 1, size - 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.end_headers()
            with open(p, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def token(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "fa_session":
                    return v
            return ""

        def authed(self):
            return app.auth.valid(self.token())

        def do_GET(self):
            app.last_ui = time.time()
            try:
                if urlparse(self.path).path == "/api/ping":
                    self.send_json({"ok": True})
                    return
                if not self.authed():
                    path = urlparse(self.path).path
                    if path in ("/", "/index.html", "/login"):
                        self.send_bytes(LOGIN_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    else:
                        self.send_json({"error": "Please sign in."}, 401)
                    return
                self.route_get()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def route_get(self):
            u = urlparse(self.path)
            path, q = unquote(u.path), parse_qs(u.query)
            lib = app.lib
            if path in ("/", "/index.html"):
                self.send_bytes(ui_path.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/library":
                self.send_json(lib.snapshot())
            elif path == "/api/status":
                self.send_json(app.status())
            elif path == "/api/config":
                self.send_json(app.config_view())
            elif path == "/api/duplicates":
                self.send_json({"similar": lib.similar_copies(), "skipped": lib.skipped_duplicates(), "windows": IS_WIN})
            elif path.startswith("/api/faces/"):
                pid = path.rsplit("/", 1)[1]
                self.send_json(lib.photo_faces(int(pid)) if pid.isdigit() else [])
            elif path.startswith("/facecrop/"):
                fid = path.split("/")[2].split(".")[0]
                try:
                    self.send_file(lib.face_crop(int(fid)) if fid.isdigit() else None, cache=False)
                except Exception:
                    self.send_error(404)
            elif path.startswith("/api/video/"):
                pid = path.rsplit("/", 1)[1]
                self.send_json(lib.video_state(int(pid)) if pid.isdigit() else {"state": "none"})
            elif path.startswith("/vcache/"):
                pid = path.split("/")[2].split(".")[0]
                self.send_file(lib.vcache / f"{pid}.mp4" if pid.isdigit() else None, cache=False)
            elif path == "/api/suggest-folders":
                self.send_json(suggest_folders(lib.root))
            elif path == "/api/browse":
                self.send_json(browse((q.get("path") or [""])[0]))
            elif path == "/api/labels":
                self.send_json(labels_public())
            elif path == "/api/search":
                q = (q.get("q") or [""])[0].strip()
                if not q or app.ai.state != "ready":
                    self.send_json({"ready": app.ai.state == "ready", "results": []})
                else:
                    self.send_json({"ready": True, "results": lib.semantic_search(app.ai.query_vector(q), min_sim=app.ai.min_sim)})
            elif path == "/api/geocode":
                try:
                    self.send_json(geocode((q.get("q") or [""])[0]))
                except Exception as e:
                    print("Geocode:", e)
                    self.send_json({"error": "Address search failed"}, 502)
            elif path.startswith("/api/history/"):
                pid = path.rsplit("/", 1)[1]
                h = lib.history(int(pid)) if pid.isdigit() else None
                self.send_json(h or {"error": "none"}, 200 if h else 404)
            elif path.startswith("/api/similar/"):
                pid = path.rsplit("/", 1)[1]
                self.send_json(lib.similar(int(pid)) if pid.isdigit() else [])
            elif path.startswith("/thumb/"):
                pid = path.split("/")[2].split(".")[0]
                self.send_file(lib.thumbs / f"{pid}.jpg" if pid.isdigit() else None, cache=False)
            elif path.startswith("/file/"):
                pid = path.split("/")[2]
                if not pid.isdigit():
                    self.send_error(404)
                    return
                f = lib.photo_path(int(pid))
                if f and f.suffix.lower() not in BROWSER_NATIVE and f.suffix.lower() in IMAGE_EXT:
                    try:
                        self.send_file(lib.preview(int(pid), f, 0))
                        return
                    except Exception:
                        pass
                self.send_file(f)
            else:
                self.send_error(404)

        def do_POST(self):
            origin = self.headers.get("Origin")
            if origin and origin not in allowed:
                self.send_json({"error": "forbidden"}, 403)
                return
            u = urlparse(self.path)
            path, q = unquote(u.path), parse_qs(u.query)
            n = int(self.headers.get("Content-Length") or 0)
            lib = app.lib
            if path == "/api/bye":
                if n:
                    self.rfile.read(n)
                app.bye_at = time.time()
                self.send_json({"ok": True})
                return
            app.last_ui = time.time()
            if path == "/api/login":
                try:
                    data = json.loads(self.rfile.read(n) or b"{}") if n else {}
                except ValueError:
                    data = {}
                if app.auth.check(data.get("user", ""), data.get("password", "")):
                    token = app.auth.login()
                    b = b'{"ok": true}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(b)))
                    self.send_header("Set-Cookie", f"fa_session={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Strict")
                    self.end_headers()
                    self.wfile.write(b)
                else:
                    time.sleep(1.0)  # slows down guessing
                    self.send_json({"error": "Wrong username or password."}, 401)
                return
            if path == "/api/quit" and hmac.compare_digest(self.headers.get("X-Quit-Token") or "", app.auth.quit_token):
                pass
            elif not self.authed():
                if n:
                    self.rfile.read(n)
                self.send_json({"error": "Please sign in."}, 401)
                return
            try:
                if path == "/api/logout":
                    if n:
                        self.rfile.read(n)
                    app.auth.logout(self.token())
                    self.send_json({"ok": True})
                    return
                if path == "/api/auth/change":
                    data = json.loads(self.rfile.read(n) or b"{}") if n else {}
                    err = app.auth.change(data.get("current", ""), data.get("user", ""), data.get("password", ""))
                    if err:
                        self.send_json({"error": err}, 400)
                    else:
                        app.auth.logout()                      # sign out everywhere else …
                        token = app.auth.login()               # … but keep this window signed in
                        b = json.dumps(app.config_view(), ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(b)))
                        self.send_header("Set-Cookie", f"fa_session={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Strict")
                        self.end_headers()
                        self.wfile.write(b)
                    return
                if path == "/api/upload":
                    name = Path(unquote(self.headers.get("X-File-Name") or "photo.jpg")).name
                    mtime = float(self.headers.get("X-File-Mtime") or time.time() * 1000) / 1000
                    if Path(name).suffix.lower() not in IMAGE_EXT | VIDEO_EXT:
                        self.rfile.read(n)
                        self.send_json({"result": "skipped"})
                        return
                    tmp = lib.incoming / f"{uuid.uuid4().hex}{Path(name).suffix.lower()}"
                    left = n
                    with open(tmp, "wb") as f:
                        while left > 0:
                            chunk = self.rfile.read(min(1 << 20, left))
                            if not chunk:
                                break
                            f.write(chunk)
                            left -= len(chunk)
                    res, pid = lib.ingest_upload(tmp, name, mtime)
                    self.send_json({"result": res, "id": pid})
                    return
                data = json.loads(self.rfile.read(n) or b"{}") if n else {}
                parts = path.strip("/").split("/")
                ids = [int(i) for i in data.get("ids", []) if str(i).isdigit()]
                if path.startswith("/api/video/") and path.endswith("/prepare"):
                    pid = parts[2] if len(parts) > 2 else ""
                    self.send_json(lib.prepare_video(int(pid)) if pid.isdigit() else {"state": "none"})
                elif parts[:2] == ["api", "face"] and len(parts) == 4 and parts[2].isdigit() and parts[3] == "name":
                    n = lib.name_face(int(parts[2]), str(data.get("name", "")))
                    self.send_json({"ok": n is not None, "photos": n or 0})
                elif path == "/api/people/not":
                    self.send_json({"removed": lib.not_person(str(data.get("person", "")), ids)})
                elif path == "/api/people/rename":
                    lib.rename_person(str(data.get("old", "")), str(data.get("new", "")))
                    self.send_json({"ok": True})
                elif path == "/api/duplicates/ignore":
                    lib.ignore_duplicates(ids)
                    self.send_json({"ok": True})
                elif path == "/api/duplicates/remove-skipped":
                    done, failed = lib.remove_skipped([str(x) for x in data.get("paths", [])])
                    self.send_json({"removed": done, "failed": failed})
                elif path == "/api/scan":
                    app.wake.set()
                    self.send_json({"ok": True})
                elif path == "/api/config":
                    app.update_config(data)
                    self.send_json(app.config_view())
                elif path == "/api/pick-folder":
                    try:
                        self.send_json({"path": pick_folder_dialog(data.get("title", "Choose a folder"))})
                    except Exception as e:
                        self.send_json({"error": str(e)})
                elif parts[:2] == ["api", "photo"] and len(parts) == 3 and parts[2].isdigit():
                    lib.update_photo(int(parts[2]), data)
                    self.send_json({"ok": True})
                elif path == "/api/photos/update":
                    for pid in ids:
                        lib.update_photo(pid, {k: v for k, v in data.items() if k != "ids"})
                    self.send_json({"ok": True})
                elif path == "/api/photos/delete":
                    lib.delete_forever(ids)
                    self.send_json({"ok": True})
                elif path == "/api/albums":
                    name = str(data.get("name", "")).strip()[:80]
                    if not name:
                        self.send_json({"error": "Enter a name"}, 400)
                        return
                    self.send_json({"id": lib.create_album(name, str(data.get("color", "#2B5FD9"))[:9])})
                elif parts[:2] == ["api", "albums"] and len(parts) == 4 and parts[2].isdigit():
                    lib.album_op(int(parts[2]), parts[3], ids, str(data.get("name", "")).strip()[:80])
                    self.send_json({"ok": True})
                elif path == "/api/open-path":
                    target = Path(str(data.get("path", "")))
                    # only paths the database knows (originals and copies) are allowed, not arbitrary ones
                    with lib.db_lock:
                        known = lib.db.execute("SELECT 1 FROM photos WHERE orig=? UNION SELECT 1 FROM seen WHERE path=?",
                                               (str(target), str(target))).fetchone()
                    if not known:
                        self.send_json({"error": "unknown path"}, 400)
                        return
                    if target.exists():
                        open_path(target, select=True)
                    elif target.parent.exists():
                        open_path(target.parent)
                    else:
                        self.send_json({"error": "The original folder no longer exists"}, 404)
                        return
                    self.send_json({"ok": True})
                elif parts[:2] == ["api", "reveal"] and len(parts) == 3 and parts[2].isdigit():
                    f = lib.photo_path(int(parts[2]))
                    if f:
                        open_path(f, select=True)
                    self.send_json({"ok": True})
                elif parts[:2] == ["api", "open"] and len(parts) == 3 and parts[2].isdigit():
                    f = lib.photo_path(int(parts[2]))
                    if f:
                        open_path(f)
                    self.send_json({"ok": True})
                elif path == "/api/open-library":
                    open_path(lib.root)
                    self.send_json({"ok": True})
                elif path == "/api/ai/enable":
                    app.enable_ai()
                    self.send_json(app.ai.info())
                elif path == "/api/fix-group":
                    old = str(data.get("tag", "")).strip()
                    new = str(data.get("name", "")).strip()[:40]
                    if not old:
                        self.send_json({"error": "no group"}, 400)
                        return
                    n = app.fix_group(old, new, str(data.get("desc", "")).strip()[:200], ids)
                    self.send_json({"ok": True, "fixed": n})
                elif path == "/api/ai/model":
                    app.set_model(str(data.get("model", "")))
                    self.send_json(app.ai.info())
                elif path == "/api/ai/reanalyze":
                    lib.reanalyze()
                    self.send_json({"ok": True})
                elif path == "/api/system/shortcut":
                    self.send_json({"result": create_shortcuts()})
                elif path == "/api/system/autostart":
                    r = set_autostart(bool(data.get("on")))
                    app._autostart = autostart_on()
                    self.send_json({"result": r, "autostart": app._autostart})
                elif path == "/api/system/uninstall":
                    u = uninstaller()
                    if not u:
                        self.send_json({"error": "Uninstall is only available for the installed version"}, 400)
                        return
                    subprocess.Popen([str(u)])
                    self.send_json({"ok": True})
                    threading.Timer(1.0, lambda: os._exit(0)).start()
                elif path == "/api/quit":
                    self.send_json({"ok": True})
                    threading.Timer(0.5, app.shutdown).start()
                else:
                    self.send_error(404)
            except Exception as e:
                print("Error:", e)
                self.send_json({"error": str(e)}, 500)
    return H


def open_in_chrome(url):
    cands = []
    if IS_WIN:
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LocalAppData")):
            if base:
                cands += [Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
                          Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"]
    elif platform.system() == "Darwin":
        cands.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    else:
        cands += [Path(shutil.which(x) or "/nonexistent") for x in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")]
    for c in cands:
        if c.exists():
            try:
                subprocess.Popen([str(c), f"--app={url}", "--window-size=1320,880"])
                return
            except Exception:
                pass
    webbrowser.open(url)


def install_models():
    """Called by the installer: downloads the models right away so the app is complete.
    Prints plain ASCII text for the installer window."""
    cfg = load_config()
    a = AI(DATA_DIR / "models", cfg.get("model", DEFAULT_MODEL))
    v = LocalVLM(DATA_DIR / "models")
    ok = True
    f = Faces(DATA_DIR / "models")
    print("Face recognition (YuNet + SFace, ~39 MB): " + ("already installed" if f.downloaded() else "downloading..."), flush=True)
    if not f.downloaded():
        f.download()
        print("Face recognition: " + ("done" if f.downloaded() else "FAILED - " + f.message.encode("ascii", "replace").decode()), flush=True)
    for name, obj in (("Quick recognition (SigLIP 2, ~400 MB)", a), ("Photo describer (Qwen3-VL 4B, ~3 GB)", v)):
        if obj.state == "missing_deps":
            print(f"{name}: skipped (engine missing)", flush=True)
            continue
        if obj.downloaded():
            print(f"{name}: already installed", flush=True)
            continue
        print(f"{name}: downloading...", flush=True)
        t = threading.Thread(target=(lambda o=obj: o.download(then_load=False)) if obj is a else obj.download, daemon=True)
        t.start()
        last = -1
        while t.is_alive():
            pct = int(obj.progress * 100)
            if pct // 5 != last:
                last = pct // 5
                print(f"   {name}: {pct}%", flush=True)
            time.sleep(1)
        if obj.downloaded():
            print(f"{name}: done", flush=True)
        else:
            ok = False
            print(f"{name}: FAILED - {obj.message.encode('ascii', 'replace').decode()}", flush=True)
    cfg = load_config()
    cfg["ai_requested"] = True
    save_config(cfg)
    return 0 if ok else 2


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    cmd = args[0] if args else "run"
    no_browser = "--no-browser" in sys.argv
    if cmd == "shortcut":
        print(create_shortcuts())
        return
    if cmd == "install-models":
        sys.exit(install_models())
    cfg = load_config()
    port = int(cfg.get("port", 8765))
    url = f"http://127.0.0.1:{port}"
    if cmd == "organize":
        lib = Library(cfg, Places())
        print("Scanning…")
        lib.organize()
        s = lib.status
        print(f"Done: added {s['added']}, duplicates {s['duplicates']}, errors {s['errors']}.")
        return
    app = App()
    try:
        app.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    except OSError:
        print(f"Fotoarhiva is already running. Opening the window: {url}")
        if not no_browser:
            open_in_chrome(url)
        return
    app.httpd.daemon_threads = True
    app.background = no_browser
    print(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} · Fotoarhiva {VERSION} running at {url}{' (safe mode)' if SAFE else ''}")
    print(f"Library: {app.lib.root}")
    print("Fotoarhiva quits by itself when its window is closed" + (" (not now: it runs in the background)." if no_browser else "."))
    threading.Thread(target=app.watcher, daemon=True).start()
    threading.Thread(target=app.ai_worker, daemon=True).start()
    threading.Thread(target=app.maintenance, daemon=True).start()
    threading.Thread(target=app.cloud_worker, daemon=True).start()
    threading.Thread(target=app.idle_watch, daemon=True).start()
    if not no_browser:
        threading.Timer(0.6, lambda: open_in_chrome(url)).start()
    try:
        app.httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nGoodbye.")


if __name__ == "__main__":
    main()
