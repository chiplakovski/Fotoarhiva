#!/usr/bin/env python3
"""Fotoarhiva — a local app that gathers all your photos in one place.

Start: the installer (Windows) or ./start.sh. Everything else is set up inside the app.
Commands for advanced users:
  python fotoarhiva.py run [--no-browser]   # server + automatic scanning (default)
  python fotoarhiva.py organize             # a single scan, no interface
  python fotoarhiva.py shortcut             # create a Desktop shortcut (Windows)
"""
import hashlib, hmac, json, secrets, math, os, re, shutil, sqlite3, struct, sys, threading, time, uuid, webbrowser, mimetypes, subprocess, platform
from datetime import datetime
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
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
import urllib.request, urllib.parse  # noqa: E402

VERSION = "2.6"
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
        self.trash_dir = self.root / "Trash"
        for p in (self.root, self.meta, self.thumbs, self.previews, self.incoming):
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
            CREATE INDEX IF NOT EXISTS idx_photos_taken ON photos(taken);
            CREATE INDEX IF NOT EXISTS idx_tags_photo ON tags(photo_id);
            """)
            cols = {r["name"] for r in self.db.execute("PRAGMA table_info(photos)")}
            for c, t in (("app", "TEXT"), ("orig", "TEXT"), ("place", "TEXT"), ("country", "TEXT"),
                         ("rot", "INTEGER DEFAULT 0"), ("ai_done", "INTEGER DEFAULT 0"), ("how", "TEXT"), ("caption", "TEXT"), ("cloud_done", "INTEGER DEFAULT 0")):
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

    def make_thumb(self, pid, f: Path, kind, rot=0):
        out = self.thumbs / f"{pid}.jpg"
        try:
            if kind == "video":
                if not FFMPEG:
                    return False
                for ss in (["-ss", "1"], []):
                    subprocess.run([FFMPEG, "-v", "error", "-y", *ss, "-i", str(f), "-frames:v", "1",
                                    "-vf", f"scale='min({THUMB},iw)':-2", str(out)], timeout=60,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **NOWIN)
                    if out.exists():
                        break
                if rot and out.exists():
                    with Image.open(out) as im:
                        im.rotate(-rot, expand=True).save(out, "JPEG", quality=82)
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
        elif ext in (".mp4", ".mov", ".m4v", ".3gp"):
            taken = video_date(src)
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
                "INSERT INTO photos(path,name,hash,taken,size,w,h,cam,kind,lat,lon,source,added,app,orig,place,country,ai_done,how) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.rel(dest), dest.name, h, taken.isoformat(timespec="seconds"), st.st_size, w, h_, cam, kind, lat, lon,
                 root_src, datetime.now().isoformat(timespec="seconds"), app, orig, place, country, 2 if kind == "video" else 0,
                 "upload" if not record_seen else mode))
            pid = cur.lastrowid
            if record_seen:
                self.db.execute("INSERT OR REPLACE INTO seen VALUES(?,?,?,?)", (str(src), st.st_mtime, "added", pid))
            self.db.commit()
        self.make_thumb(pid, dest, kind)
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
            return self.db.execute("SELECT COUNT(*) c FROM photos WHERE cloud_done=0 AND trash=0 AND kind!='video'").fetchone()["c"]

    def cloud_step(self, cloud, labels, workers=3):
        """Describing with a describer: Claude (cloud) or the built-in Qwen3-VL. Same result shape."""
        with self.db_lock:
            rows = self.db.execute("SELECT id,path,kind,cam,app,taken,rot FROM photos WHERE cloud_done=0 AND trash=0 AND kind!='video' LIMIT ?",
                                   (workers * 2,)).fetchall()
        if not rows:
            return False
        known = {mk for mk, _ in labels}
        by = "vlm" if isinstance(cloud, LocalVLM) else "cloud"

        def one(r):
            f = self.abs(r["path"])
            try:
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
            rows = self.db.execute("SELECT id,path,name,taken,size,w,h,cam,kind,lat,lon,fav,trash,source,app,place,country,rot,ai_done,caption,cloud_done FROM photos ORDER BY taken DESC").fetchall()
            ap, tg = {}, {}
            for r in self.db.execute("SELECT album_id, photo_id FROM album_photos"):
                ap.setdefault(r["photo_id"], []).append(r["album_id"])
            tb = {}
            for r in self.db.execute("SELECT photo_id, tag, by FROM tags ORDER BY by='user' DESC, score DESC"):
                tg.setdefault(r["photo_id"], []).append(r["tag"])
                tb.setdefault(r["photo_id"], {})[r["tag"]] = r["by"]
            albums = [dict(r) for r in self.db.execute("SELECT * FROM albums ORDER BY name")]
            dups = self.db.execute("SELECT COUNT(*) c FROM seen WHERE result='duplicate'").fetchone()["c"]
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
        photos = [dict(r, albums=ap.get(r["id"], []), tags=tg.get(r["id"], []), tagby=tb.get(r["id"], {}), myplace=myplace(r["lat"], r["lon"])) for r in rows]
        try:
            total = shutil.disk_usage(self.root)[0]
        except Exception:
            total = 0
        return {"photos": photos, "albums": albums, "duplicates": dups, "library": str(self.root), "disk_total": total}

    def duplicates(self):
        with self.db_lock:
            rows = self.db.execute("""SELECT s.path AS src, s.dup_of AS of_id, p.path AS of_path FROM seen s
                                      LEFT JOIN photos p ON p.id = s.dup_of WHERE s.result='duplicate' ORDER BY s.path""").fetchall()
        return [dict(r) for r in rows]

    def update_photo(self, pid, data):
        with self.db_lock:
            p = self.db.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
            if not p:
                return
            if "fav" in data:
                self.db.execute("UPDATE photos SET fav=? WHERE id=?", (1 if data["fav"] else 0, pid))
            if "rotate" in data:
                rot = (int(p["rot"] or 0) + int(data["rotate"])) % 360
                self.db.execute("UPDATE photos SET rot=? WHERE id=?", (rot, pid))
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
                for t in (self.thumbs / f"{pid}.jpg", self.previews / f"{pid}.jpg"):
                    if t.exists():
                        t.unlink()
                for tbl in ("photos WHERE id", "album_photos WHERE photo_id", "tags WHERE photo_id", "emb WHERE photo_id", "hidden_tags WHERE photo_id"):
                    self.db.execute(f"DELETE FROM {tbl}=?", (pid,))
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
            self.db.execute("UPDATE photos SET ai_done=0 WHERE kind!='video'")
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
            self.db.execute("UPDATE photos SET ai_done=0 WHERE ai_done=1 OR (ai_done=2 AND kind!='video')")
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
                "cloud_models": [{"id": k, "name": v["name"], "usd_per_1000": v["usd_per_1000"]} for k, v in CLOUD_MODELS.items()], "can_uninstall": uninstaller() is not None,
                "models": [{"id": k, "name": v["name"], "name_en": v["name_en"], "about": v["about"], "multilingual": v["multilingual"]} for k, v in MODELS.items()]}

    def update_config(self, data):
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
<script>document.getElementById('f').onsubmit=async e=>{e.preventDefault();const err=document.getElementById('err');err.textContent='';
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
            try:
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
                self.send_json(lib.duplicates())
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
                    name = Path(unquote(self.headers.get("X-File-Name") or "slika.jpg")).name
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
                if path == "/api/scan":
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
                    threading.Timer(0.5, lambda: os._exit(0)).start()
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
    for name, obj in (("Brzo prepoznavanje (SigLIP 2, ~400 MB)", a), ("Opishuvac na sliki (Qwen3-VL 4B, ~3 GB)", v)):
        if obj.state == "missing_deps":
            print(f"{name}: preskoknato (nedostasuva motor)", flush=True)
            continue
        if obj.downloaded():
            print(f"{name}: vekje instalirano", flush=True)
            continue
        print(f"{name}: go simnuvam...", flush=True)
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
            print(f"{name}: gotovo", flush=True)
        else:
            ok = False
            print(f"{name}: NE USPEA - {obj.message.encode('ascii', 'replace').decode()}", flush=True)
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
    print(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} · Fotoarhiva {VERSION} running at {url}{' (safe mode)' if SAFE else ''}")
    print(f"Library: {app.lib.root}")
    print("This window can stay minimized. Close it to quit the app.")
    threading.Thread(target=app.watcher, daemon=True).start()
    threading.Thread(target=app.ai_worker, daemon=True).start()
    threading.Thread(target=app.cloud_worker, daemon=True).start()
    if not no_browser:
        threading.Timer(0.6, lambda: open_in_chrome(url)).start()
    try:
        app.httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nGoodbye.")


if __name__ == "__main__":
    main()
