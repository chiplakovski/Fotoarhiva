"""Вграден визуелен јазичен модел (Qwen3-VL-4B, 4-битен) што ја „гледа“ сликата и ја опишува.
Работи на компјутерот: на NVIDIA/AMD/Intel графичка картичка преку DirectML, или на процесорот.
Моделот (~2,9 GB) се симнува еднаш од HuggingFace. Сликите никогаш не излегуваат од компјутерот.
"""
import json, os, queue, re, subprocess, sys, tempfile, threading, urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}

HF = os.environ.get("FOTOARHIVA_HF", "https://huggingface.co")
REPO = "onnx-community/Qwen3-VL-4B-Instruct-ONNX"
FOLDER = "onnxruntime/cpu_and_mobile/cpu-int4-rtn-block-32"
APPROX_BYTES = 2_950_000_000
UA = {"User-Agent": "Fotoarhiva/2.3"}
MAX_SIDE = 768


def deps_ok():
    # Само проверка дека пакетот постои. Самиот мотор се вчитува во посебен процес,
    # за грешка во драјверот на картичката да не ја сруши апликацијата.
    import importlib.util
    if importlib.util.find_spec("onnxruntime_genai") is None:
        return False, "onnxruntime_genai"
    return True, ""


class ChildDied(RuntimeError):
    pass


class _Child:
    """Моделот работи во посебен процес; разговараме преку JSON редови."""

    def __init__(self, model_dir, device, log_path):
        exe = sys.executable
        if exe.lower().endswith("pythonw.exe"):
            exe = exe[:-5] + ".exe"
        self.log = open(log_path, "ab") if log_path else subprocess.DEVNULL
        self.p = subprocess.Popen([exe, str(APP_DIR / "vlm.py"), "--worker", str(model_dir), device],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, **NOWIN)
        self.q = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.p.stdout:
            if line.startswith(b"@@"):
                self.q.put(json.loads(line[2:].decode("utf-8")))
        self.q.put(None)  # процесот заврши

    def call(self, req, timeout):
        if self.p.poll() is not None:
            raise ChildDied(f"процесот заврши (код {self.p.returncode})")
        try:
            self.p.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
            self.p.stdin.flush()
        except OSError:
            raise ChildDied("процесот не одговара")
        try:
            r = self.q.get(timeout=timeout)
        except queue.Empty:
            self.kill()
            raise ChildDied("истече времето")
        if r is None:
            raise ChildDied(f"процесот падна (код {self.p.poll()})")
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "грешка"))
        return r

    def kill(self):
        try:
            self.p.kill()
        except Exception:
            pass


class LocalVLM:
    name = "Qwen3-VL 4B (вграден)"

    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "qwen3-vl-4b"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state, self.message, self.progress = "off", "", 0.0
        self.device = ""
        self.enabled = False
        self.child = None
        self.log_path = os.environ.get("FOTOARHIVA_LOG", "")
        self.lock = threading.Lock()      # еден опис во исто време (графичката меморија е ограничена)
        ok, err = deps_ok()
        if not ok:
            self.state, self.message = "missing_deps", f"Моторот за опишување не е инсталиран ({err[:120]})."

    def downloaded(self):
        return (self.dir / "genai_config.json").exists() and (self.dir / "text.onnx.data").exists() and not list(self.dir.glob("*.part"))

    def info(self):
        return {"state": self.state, "message": self.message, "progress": round(self.progress, 3), "device": self.device,
                "model": self.name, "enabled": self.enabled}

    def configure(self, enabled):
        self.enabled = bool(enabled)
        if not self.enabled:
            if self.state not in ("missing_deps",):
                self.state = "off"
            if self.child:
                self.child.kill()
                self.child = None
            return
        if self.state == "missing_deps":
            return
        if self.state in ("ready", "downloading", "loading"):
            return
        threading.Thread(target=self._start, daemon=True).start()

    def _start(self):
        if not self.downloaded():
            self.download()
        if self.downloaded() and self.state != "error":
            self.load()

    # ---------- download
    def download(self):
        self.state, self.progress, self.message = "downloading", 0.0, ""
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{HF}/api/models/{REPO}", headers=UA), timeout=30) as r:
                info = json.loads(r.read().decode("utf-8"))
            files = [s["rfilename"] for s in info.get("siblings", []) if s["rfilename"].startswith(FOLDER + "/")]
            if not files:
                raise RuntimeError("моделот не е најден на HuggingFace")
            done = 0
            for f in sorted(files, key=lambda x: x.endswith(".data")):
                local = self.dir / Path(f).name
                if local.exists():
                    done += local.stat().st_size
                    continue
                part = local.with_name(local.name + ".part")
                url = f"{HF}/{REPO}/resolve/main/{f}"
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r, open(part, "wb") as out:
                    while True:
                        chunk = r.read(4 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        self.progress = min(0.99, done / APPROX_BYTES)
                part.replace(local)
            self.progress = 1.0
        except Exception as e:
            self.state, self.message = "error", f"Симнувањето не успеа: {e}. Провери интернет и пробај повторно."

    # ---------- load (во посебен процес)
    def load(self):
        self.state, self.message = "loading", ""
        errors = []
        bad = self.dir / "gpu_failed.txt"   # картичката веќе еднаш паднала → директно на процесорот
        devices = ["cpu"] if bad.exists() else os.environ.get("FOTOARHIVA_VLM_DEVICES", "dml,cpu").split(",")
        for dev in devices:
            child = None
            try:
                child = _Child(self.dir, dev, self.log_path)
                r = child.call({"op": "load"}, timeout=600)
                if r.get("skip"):
                    child.kill()
                    continue
                child.call({"op": "test"}, timeout=600)
                self.child = child
                self.device = "графичка картичка" if dev == "dml" else "процесор (побавно)"
                self.state = "ready"
                return
            except Exception as e:
                errors.append(f"{dev}: {e}")
                if child:
                    child.kill()
                if dev == "dml" and isinstance(e, ChildDied):
                    try:
                        bad.write_text(str(e), encoding="utf-8")
                    except OSError:
                        pass
        self.state, self.message = "error", "Моделот не можеше да се вклучи: " + " | ".join(errors)[:300]

    def _generate(self, image_path, question, max_new):
        try:
            return self.child.call({"op": "gen", "image": str(image_path), "q": question, "max": max_new}, timeout=900)["text"]
        except ChildDied as e:
            # процесот падна при опишување: следниот пат на процесорот
            if self.device.startswith("графичка"):
                try:
                    (self.dir / "gpu_failed.txt").write_text(str(e), encoding="utf-8")
                except OSError:
                    pass
            self.child = None
            self.state, self.message = "loading", ""
            threading.Thread(target=self.load, daemon=True).start()
            raise

    def describe(self, jpeg, labels):
        """labels: [(македонски, англиски)]. Враќа ист облик како Claude: tags (мк), new_tags, caption_mk, caption_en, kind.
        Моделот слободно наведува англиски ознаки; тие се пресликуваат во нашите категории преку речник.
        Твоите категории (Мои категории) се даваат на моделот по број, бидејќи се малку."""
        from PIL import Image
        import io
        from ai import MK2EN
        mine = [(mk, en) for mk, en in labels if mk not in MK2EN]
        q = ("Look at this photo. Reply with JSON only, no other text:\n"
             '{"caption_en": "one specific English sentence describing the photo", '
             '"caption_mk": "the same sentence in Macedonian (Cyrillic)", '
             '"tags": ["3 to 6 short English tags: the main subject, objects, the place/setting, the activity"], '
             + ('"mine": [numbers from MY CATEGORIES below that clearly match, [] if none], ' if mine else '')
             + '"kind": "photo or screenshot or document"}')
        if mine:
            q += "\nMY CATEGORIES: " + "; ".join(f"{i + 1} {mk} ({en})" for i, (mk, en) in enumerate(mine))
        with self.lock, tempfile.TemporaryDirectory() as td:
            p = Path(td) / "img.jpg"
            with Image.open(io.BytesIO(jpeg)) as im:
                im = im.convert("RGB")
                im.thumbnail((MAX_SIDE, MAX_SIDE))
                im.save(p, "JPEG", quality=90)
            raw = self._generate(p, q, 260)
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except ValueError:
            return None
        free = [str(t).strip() for t in (d.get("tags") or []) if str(t).strip()][:6]
        text = " ".join(free + [str(d.get("caption_en", ""))])
        tags = map_to_categories(text, [l for l in labels if l[0] in MK2EN])
        for c in d.get("mine") or []:
            try:
                i = int(c) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(mine):
                tags.insert(0, mine[i][0])
        extra = [t[:1].upper() + t[1:] for t in free if not map_to_categories(t, [l for l in labels if l[0] in MK2EN])][:3]
        return {"tags": list(dict.fromkeys(tags))[:5], "new_tags": extra, "caption_mk": str(d.get("caption_mk", "")).strip(),
                "caption_en": str(d.get("caption_en", "")).strip(), "kind": d.get("kind", "photo")}


# Англиски зборови → наши категории (македонско име). Покрај англиското име на категоријата.
SYNONYMS = {
    "Луѓе": ["person", "people", "man", "woman", "men", "women", "girl", "boy", "lady", "guy"],
    "Деца": ["child", "children", "kid", "kids", "toddler"], "Бебе": ["baby", "infant", "newborn"],
    "Друштво": ["friends", "group of people", "group photo"], "Семејство": ["family", "parents"],
    "Селфи": ["selfie"], "Пар": ["couple"], "Постари луѓе": ["elderly", "old man", "old woman", "grandmother", "grandfather"],
    "Толпа": ["crowd", "audience"],
    "Кучиња": ["dog", "puppy"], "Мачки": ["cat", "kitten", "manul"], "Птици": ["bird", "pigeon", "duck", "swan"],
    "Коњи": ["horse"], "Домашни животни": ["cow", "sheep", "goat", "chicken", "pig", "livestock"],
    "Диви животни": ["wildlife", "wild animal", "deer", "bear", "fox", "wolf"], "Инсекти": ["insect", "butterfly", "bee"],
    "Риби": ["fish", "aquarium"],
    "Планини": ["mountain", "peak", "hill"], "Шума": ["forest", "woods"], "Море": ["sea", "ocean"],
    "Плажа": ["beach", "sand", "shore"], "Езеро": ["lake"], "Река": ["river", "stream"], "Водопад": ["waterfall"],
    "Цвеќиња": ["flower", "blossom", "rose", "tulip"], "Дрвја": ["tree"], "Небо и облаци": ["sky", "cloud"],
    "Зајдисонце": ["sunset", "sunrise", "dusk"], "Ноќ": ["night"], "Снег и зима": ["snow", "winter", "ski"],
    "Поле и ливада": ["meadow", "field", "grass"], "Есен": ["autumn", "fall foliage"],
    "Град": ["city", "urban", "skyline"], "Улица": ["street", "road"], "Село": ["village", "rural"],
    "Црква и манастир": ["church", "monastery", "chapel", "icon"], "Џамија": ["mosque"],
    "Тврдина и споменик": ["fortress", "monument", "ruins", "statue", "castle"], "Мост": ["bridge"],
    "Ресторан и кафе": ["restaurant", "cafe", "bar"], "Продавница": ["shop", "store", "supermarket", "market"],
    "Базен": ["pool"], "Хотел": ["hotel", "resort"], "Аеродром и авион": ["airport", "airplane", "plane", "aircraft"],
    "Внатре дома": ["living room", "sofa", "couch", "bedroom", "home interior", "indoor"], "Кујна": ["kitchen"],
    "Двор и градина": ["garden", "yard", "backyard"],
    "Канцеларија": ["office", "desk", "computer"], "Состанок": ["meeting", "conference", "presentation"],
    "Училиште": ["school", "classroom"], "Градилиште": ["construction"],
    "Машини": ["machine", "machinery", "industrial equipment"], "Инокс опрема": ["stainless steel", "stainless", "inox"],
    "Фабрика и производство": ["factory", "production", "manufacturing", "industrial", "plant floor"],
    "Магацин": ["warehouse", "pallet"], "Метал и заварување": ["welding", "metalwork", "metal pipe", "sheet metal"],
    "Работни возила": ["truck", "forklift", "tractor", "excavator"], "Електрика и инсталации": ["electrical", "cable", "wiring"],
    "Роденден": ["birthday"], "Свадба": ["wedding", "bride", "groom"], "Забава": ["party", "celebration"],
    "Нова година": ["new year", "christmas", "fireworks"], "Концерт": ["concert", "stage"],
    "Храна": ["food", "meal", "dish", "plate"], "Пица": ["pizza"], "Десерт и торта": ["cake", "dessert", "pastry"],
    "Кафе": ["coffee", "espresso"], "Пијалаци": ["wine", "beer", "drink", "cocktail"], "Овошје": ["fruit"],
    "Зеленчук": ["vegetable"], "Скара и месо": ["grill", "barbecue", "meat"],
    "Автомобили": ["car", "vehicle"], "Мотор": ["motorcycle"], "Брод": ["boat", "ship", "yacht"],
    "Воз и автобус": ["train", "bus"], "Велосипед": ["bicycle", "bike", "cycling"],
    "Фудбал": ["football", "soccer"], "Кошарка": ["basketball"], "Пливање": ["swimming"], "Скијање": ["skiing"],
    "Документи": ["document", "paper", "text page"], "Сметки и фискални": ["receipt", "invoice", "bill"],
    "Лична карта и пасош": ["passport", "id card", "identity card"], "Мапа": ["map"],
    "Мемиња и графики": ["meme", "poster", "cartoon", "illustration"], "Цртеж": ["drawing", "painting"],
    "Телефон и компјутер": ["smartphone", "phone", "laptop"], "Книги": ["book"], "Играчки": ["toy"],
    "Мебел": ["furniture", "chair", "table"], "Алат": ["tool", "hammer", "drill", "wrench"],
    "Црно-бело": ["black and white"], "Стара фотографија": ["vintage", "old photo"],
}


def _norm(w):
    w = w.lower().strip()
    for suf in ("ies", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def map_to_categories(text, labels):
    words = " " + " ".join(_norm(w) for w in re.findall(r"[a-zA-Z']+", text)) + " "
    out = []
    for mk, en in labels:
        keys = SYNONYMS.get(mk, []) + [k for k in re.split(r"\s*(?:&|,| and )\s*", en.lower()) if k]
        for k in keys:
            kn = " ".join(_norm(w) for w in k.split())
            if f" {kn} " in words:
                out.append(mk)
                break
    return out


# ---------- процесот што го извршува моделот
def _worker(model_dir, device):
    import onnxruntime_genai as og
    out = sys.stdout.buffer

    def send(obj):
        out.write(b"@@" + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
        out.flush()

    model = proc = tok = None
    for line in sys.stdin.buffer:
        try:
            req = json.loads(line.decode("utf-8"))
            op = req.get("op")
            if op == "load":
                if device == "dml" and not getattr(og, "is_dml_available", lambda: False)():
                    send({"ok": True, "skip": True})
                    continue
                cfg = og.Config(model_dir)
                cfg.clear_providers()
                if device == "dml":
                    cfg.append_provider("dml")
                model = og.Model(cfg)
                proc, tok = model.create_multimodal_processor(), og.Tokenizer(model)
                send({"ok": True})
            elif op in ("test", "gen"):
                if op == "test":
                    from PIL import Image
                    td = tempfile.mkdtemp()
                    path = os.path.join(td, "t.jpg")
                    Image.new("RGB", (64, 64), (120, 160, 200)).save(path)
                    q, mx = "What color is this image? One word.", 5
                else:
                    path, q, mx = req["image"], req["q"], int(req.get("max", 256))
                messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": q}]}]
                prompt = tok.apply_chat_template(json.dumps(messages, ensure_ascii=False), add_generation_prompt=True)
                inputs = proc(prompt, images=og.Images.open(path))
                params = og.GeneratorParams(model)
                params.set_search_options(max_length=8192, do_sample=False)
                gen = og.Generator(model, params)
                gen.set_inputs(inputs)
                stream = proc.create_stream()
                text, n = "", 0
                while not gen.is_done() and n < mx:
                    gen.generate_next_token()
                    text += stream.decode(gen.get_next_tokens()[0])
                    n += 1
                send({"ok": True, "text": text})
            else:
                send({"ok": False, "error": f"непознато: {op}"})
        except Exception as e:
            send({"ok": False, "error": str(e)[:500]})


if __name__ == "__main__" and len(sys.argv) >= 4 and sys.argv[1] == "--worker":
    if os.environ.get("FOTOARHIVA_VLM_CRASH") == sys.argv[3]:
        os._exit(3)  # само за тестирање: симулира пад на драјверот
    import faulthandler
    faulthandler.enable()
    _worker(sys.argv[2], sys.argv[3])
