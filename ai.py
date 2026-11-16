"""Паметно препознавање на содржината на сликите. Работи локално, сликите не се праќаат никаде.

Модели (се симнуваат еднаш од HuggingFace):
  * siglip2 – Google SigLIP 2: прецизен и повеќејазичен (разбира македонски и англиски). Препорачан.
  * clip    – OpenAI CLIP ViT-B/32: помал и побрз, само англиски.

Потребни пакети: onnxruntime, numpy, tokenizers (вклучени во инсталерот).
"""
import hashlib, json, os, threading, urllib.request
from pathlib import Path

HF = os.environ.get("FOTOARHIVA_HF", "https://huggingface.co")
UA = {"User-Agent": "Fotoarhiva/2.1"}

MODELS = {
    "siglip2": {
        "name": "Напреден (Google SigLIP 2)", "name_en": "Advanced (Google SigLIP 2)",
        "about": "Најдобро препознавање. Разбира македонски и англиски. Околу 400 MB.",
        "repos": ["onnx-community/siglip2-base-patch16-224-ONNX", "onnx-community/siglip2-base-patch16-256-ONNX",
                  "onnx-community/siglip2-base-patch16-384-ONNX"],
        "kind": "siglip", "ctx": 64, "multilingual": True, "approx_mb": 400,
    },
    "siglip2-large": {
        "name": "Голем (SigLIP 2 Large)", "name_en": "Large (SigLIP 2 Large)",
        "about": "Попрецизен од Напредниот, македонски и англиски. Околу 900 MB, побавен.",
        "repos": ["onnx-community/siglip2-large-patch16-256-ONNX", "onnx-community/siglip2-large-patch16-384-ONNX",
                  "onnx-community/siglip2-large-patch16-512-ONNX"],
        "kind": "siglip", "ctx": 64, "multilingual": True, "approx_mb": 900,
    },
    "siglip2-so400m": {
        "name": "Максимален (SigLIP 2 So400m)", "name_en": "Maximum (SigLIP 2 So400m)",
        "about": "Најпрецизен локален модел. Околу 1,5 GB, бавен на постари компјутери.",
        "repos": ["onnx-community/siglip2-so400m-patch14-384-ONNX", "onnx-community/siglip2-so400m-patch16-384-ONNX",
                  "onnx-community/siglip2-so400m-patch14-224-ONNX", "onnx-community/siglip2-so400m-patch16-256-ONNX"],
        "kind": "siglip", "ctx": 64, "multilingual": True, "approx_mb": 1500,
    },
    "clip": {
        "name": "Стандарден (OpenAI CLIP)", "name_en": "Standard (OpenAI CLIP)",
        "about": "Помал и побрз, послабо препознавање, само англиски. Околу 155 MB.",
        "repos": ["Xenova/clip-vit-base-patch32"],
        "kind": "clip", "ctx": 77, "multilingual": False, "approx_mb": 155,
    },
}
DEFAULT_MODEL = "siglip2"
PREFER = {
    "vision": ["onnx/vision_model_quantized.onnx", "onnx/vision_model_int8.onnx", "onnx/vision_model_uint8.onnx",
               "onnx/vision_model_q8.onnx", "onnx/vision_model_fp16.onnx", "onnx/vision_model.onnx"],
    "text": ["onnx/text_model_quantized.onnx", "onnx/text_model_int8.onnx", "onnx/text_model_uint8.onnx",
             "onnx/text_model_q8.onnx", "onnx/text_model_fp16.onnx", "onnx/text_model.onnx"],
}

# (македонски, англиски, група, англиски описи за моделот). Ознаките што почнуваат со "_" служат само за споредба.
LABELS = [
    ("Природа", "Nature", "Природа", ["a photo of nature", "a landscape with trees and fields"]),
    ("Планини", "Mountains", "Природа", ["a photo of mountains", "a mountain peak", "a mountain landscape"]),
    ("Шума", "Forest", "Природа", ["a photo of a forest", "trees in a forest", "a path in the woods"]),
    ("Море", "Sea", "Природа", ["a photo of the sea", "ocean waves", "the sea seen from the shore"]),
    ("Плажа", "Beach", "Природа", ["a photo of a beach", "people on a sandy beach", "beach umbrellas and sand"]),
    ("Езеро", "Lake", "Природа", ["a photo of a lake", "a calm lake with mountains", "a lake shore"]),
    ("Река", "River", "Природа", ["a photo of a river", "a stream in nature"]),
    ("Водопад", "Waterfall", "Природа", ["a photo of a waterfall"]),
    ("Пештера", "Cave", "Природа", ["a photo inside a cave"]),
    ("Поле и ливада", "Meadow", "Природа", ["a green meadow", "a field of grass", "farm fields"]),
    ("Цвеќиња", "Flowers", "Природа", ["a photo of flowers", "a close-up of a flower", "a flower garden"]),
    ("Дрвја", "Trees", "Природа", ["a photo of a tree", "autumn trees with colorful leaves"]),
    ("Небо и облаци", "Sky", "Природа", ["a photo of the sky with clouds", "blue sky"]),
    ("Зајдисонце", "Sunset", "Природа", ["a photo of a sunset", "a sunrise", "an orange sky at dusk"]),
    ("Ноќ", "Night", "Природа", ["a photo taken at night", "a dark night scene"]),
    ("Ѕвезди и месечина", "Stars & moon", "Природа", ["the moon in the night sky", "stars in the night sky"]),
    ("Снег и зима", "Snow & winter", "Природа", ["a photo of snow", "a winter landscape with snow"]),
    ("Дожд и магла", "Rain & fog", "Природа", ["a rainy day", "fog over the landscape"]),
    ("Есен", "Autumn", "Природа", ["autumn leaves", "a fall landscape with yellow trees"]),
    ("Пролет", "Spring", "Природа", ["spring blossoms on trees", "a spring garden"]),
    ("Град", "City", "Места", ["a photo of a city street", "buildings in a city", "a city skyline"]),
    ("Улица", "Street", "Места", ["a street with shops", "a busy street", "an old town street"]),
    ("Село", "Village", "Места", ["a small village with old houses", "a rural village"]),
    ("Црква и манастир", "Church", "Места", ["a church", "an orthodox monastery", "a church interior with icons"]),
    ("Џамија", "Mosque", "Места", ["a mosque with a minaret"]),
    ("Тврдина и споменик", "Monument", "Места", ["an old fortress", "a historic monument", "ancient ruins", "a statue"]),
    ("Мост", "Bridge", "Места", ["a photo of a bridge"]),
    ("Музеј", "Museum", "Места", ["inside a museum", "an art gallery exhibition"]),
    ("Парк", "Park", "Места", ["a city park", "a park with benches and trees"]),
    ("Базен", "Pool", "Места", ["a swimming pool", "people in a swimming pool"]),
    ("Хотел", "Hotel", "Места", ["a hotel room", "a hotel lobby", "a resort"]),
    ("Аеродром и авион", "Airport & plane", "Места", ["an airport", "an airplane", "a view from an airplane window"]),
    ("Ресторан и кафе", "Restaurant & cafe", "Места", ["a restaurant interior", "a cafe terrace", "people eating in a restaurant"]),
    ("Продавница", "Shop", "Места", ["a supermarket", "a shop interior", "shopping mall"]),
    ("Плоштад", "Square", "Места", ["a city square", "a town square with a fountain"]),
    ("Внатре дома", "Indoors at home", "Дом", ["a living room", "inside a house", "a cozy home interior"]),
    ("Кујна", "Kitchen", "Дом", ["a kitchen", "cooking in a kitchen"]),
    ("Спална", "Bedroom", "Дом", ["a bedroom with a bed"]),
    ("Двор и градина", "Garden & yard", "Дом", ["a backyard garden", "a vegetable garden", "a yard of a house"]),
    ("Канцеларија", "Office", "Работа", ["an office", "a desk with a computer", "people working in an office"]),
    ("Состанок", "Meeting", "Работа", ["a business meeting", "a conference room", "a presentation"]),
    ("Училиште", "School", "Работа", ["a classroom", "school children in class", "a school building"]),
    ("Градилиште", "Construction", "Работа", ["a construction site", "workers building a house"]),
    ("Машини", "Machines", "Работа", ["industrial machines", "a machine in a factory", "metal machinery equipment", "a production machine"]),
    ("Инокс опрема", "Stainless steel", "Работа", ["stainless steel equipment", "stainless steel tanks", "polished stainless steel machine", "stainless steel kitchen equipment"]),
    ("Фабрика и производство", "Factory", "Работа", ["a factory production line", "an industrial workshop", "a manufacturing plant interior"]),
    ("Магацин", "Warehouse", "Работа", ["a warehouse with shelves", "pallets and boxes in a warehouse"]),
    ("Производи", "Products", "Работа", ["a product photo on a plain background", "products on display", "packaged products"]),
    ("Метал и заварување", "Metalwork", "Работа", ["welding metal", "metal pipes and sheets", "a metal workshop"]),
    ("Работни возила", "Work vehicles", "Работа", ["a truck", "a forklift", "a tractor", "an excavator"]),
    ("Електрика и инсталации", "Electrical", "Работа", ["an electrical panel with cables", "electrical installation", "a circuit board"]),
    ("Медицина", "Medical", "Работа", ["a hospital room", "medical equipment", "a doctor"]),
    ("Апарати за дома", "Appliances", "Дом", ["a washing machine", "a refrigerator", "home appliances", "an oven"]),
    ("Луѓе", "People", "Луѓе", ["a photo of a person", "a portrait of a person"]),
    ("Селфи", "Selfie", "Луѓе", ["a selfie", "a close-up selfie of a face"]),
    ("Друштво", "Friends", "Луѓе", ["a group photo of friends", "a group of people smiling"]),
    ("Семејство", "Family", "Луѓе", ["a family photo", "parents with children"]),
    ("Деца", "Children", "Луѓе", ["young children", "kids playing"]),
    ("Бебе", "Baby", "Луѓе", ["a baby", "a newborn baby"]),
    ("Пар", "Couple", "Луѓе", ["a couple together", "a romantic couple"]),
    ("Постари луѓе", "Elderly", "Луѓе", ["an elderly person", "grandparents"]),
    ("Толпа", "Crowd", "Луѓе", ["a crowd of people", "people at a concert"]),
    ("Роденден", "Birthday", "Настани", ["a birthday party", "a birthday cake with candles"]),
    ("Свадба", "Wedding", "Настани", ["a wedding", "a bride in a white dress", "a wedding ceremony"]),
    ("Забава", "Party", "Настани", ["people at a party", "a celebration with friends"]),
    ("Нова година", "New Year", "Настани", ["new year celebration", "fireworks", "a christmas tree"]),
    ("Велигден", "Easter", "Настани", ["painted easter eggs"]),
    ("Концерт", "Concert", "Настани", ["a concert", "a band playing on stage"]),
    ("Дипломирање", "Graduation", "Настани", ["a graduation ceremony"]),
    ("Крштевка", "Christening", "Настани", ["a christening in a church", "a baptism"]),
    ("Патување", "Travel", "Настани", ["a tourist on vacation", "a travel photo of a tourist destination", "luggage and suitcases"]),
    ("Камп", "Camping", "Настани", ["a tent at a campsite", "camping in nature"]),
    ("Пешачење", "Hiking", "Настани", ["people hiking on a trail", "a hiker in the mountains"]),
    ("Спорт", "Sport", "Спорт", ["people playing sports"]),
    ("Фудбал", "Football", "Спорт", ["a football match", "a soccer ball", "a football stadium"]),
    ("Кошарка", "Basketball", "Спорт", ["a basketball game"]),
    ("Пливање", "Swimming", "Спорт", ["people swimming"]),
    ("Скијање", "Skiing", "Спорт", ["skiing", "a ski slope"]),
    ("Велосипед", "Bicycle", "Спорт", ["a bicycle", "people cycling"]),
    ("Теретана", "Gym", "Спорт", ["a gym workout", "fitness equipment"]),
    ("Риболов", "Fishing", "Спорт", ["fishing with a rod", "a fish caught"]),
    ("Храна", "Food", "Храна", ["a plate of food", "a meal on a table"]),
    ("Скара и месо", "Grill & meat", "Храна", ["grilled meat", "a barbecue"]),
    ("Пица", "Pizza", "Храна", ["a pizza"]),
    ("Салата", "Salad", "Храна", ["a salad"]),
    ("Десерт и торта", "Dessert & cake", "Храна", ["a cake", "a dessert", "pastries and sweets"]),
    ("Овошје", "Fruit", "Храна", ["fresh fruit", "a bowl of fruit"]),
    ("Зеленчук", "Vegetables", "Храна", ["fresh vegetables", "peppers and tomatoes"]),
    ("Леб и печива", "Bread & pastry", "Храна", ["bread", "burek pastry", "baked pastries"]),
    ("Кафе", "Coffee", "Храна", ["a cup of coffee", "espresso"]),
    ("Пијалаци", "Drinks", "Храна", ["a glass of wine", "beer", "cocktails", "a drink"]),
    ("Семејна трпеза", "Family dinner", "Храна", ["a family dinner table with many dishes"]),
    ("Кучиња", "Dogs", "Животни", ["a dog", "a puppy"]),
    ("Мачки", "Cats", "Животни", ["a cat", "a kitten"]),
    ("Птици", "Birds", "Животни", ["a bird", "birds flying"]),
    ("Коњи", "Horses", "Животни", ["a horse"]),
    ("Домашни животни", "Farm animals", "Животни", ["cows", "sheep", "chickens", "farm animals"]),
    ("Риби", "Fish", "Животни", ["fish in water", "an aquarium"]),
    ("Инсекти", "Insects", "Животни", ["a butterfly", "a bee on a flower", "an insect"]),
    ("Диви животни", "Wildlife", "Животни", ["a wild animal", "a deer in the forest", "a bear"]),
    ("Автомобили", "Cars", "Предмети", ["a car", "a car on the road"]),
    ("Мотор", "Motorcycle", "Предмети", ["a motorcycle"]),
    ("Брод", "Boat", "Предмети", ["a boat", "a ship", "a sailboat"]),
    ("Воз и автобус", "Train & bus", "Предмети", ["a train", "a bus"]),
    ("Телефон и компјутер", "Devices", "Предмети", ["a smartphone", "a laptop computer"]),
    ("Книги", "Books", "Предмети", ["books", "a bookshelf"]),
    ("Облека и чевли", "Clothes & shoes", "Предмети", ["clothes", "shoes", "a fashion outfit"]),
    ("Играчки", "Toys", "Предмети", ["toys", "a teddy bear"]),
    ("Музички инструменти", "Instruments", "Предмети", ["a guitar", "a piano", "a musical instrument"]),
    ("Мебел", "Furniture", "Предмети", ["furniture", "a sofa", "a table and chairs"]),
    ("Алат", "Tools", "Предмети", ["tools", "a workshop with tools"]),
    ("Документи", "Documents", "Документи", ["a document with text", "a printed page", "a scanned paper document"]),
    ("Сметки и фискални", "Receipts", "Документи", ["a receipt", "an invoice", "a bill"]),
    ("Лична карта и пасош", "ID & passport", "Документи", ["an identity card", "a passport", "a driver license"]),
    ("Белешки и табла", "Notes", "Документи", ["handwritten notes", "a whiteboard with writing"]),
    ("Скриншот", "Screenshot", "Документи", ["a screenshot of a phone screen", "a screenshot of a chat app", "a screenshot of a website"]),
    ("Мемиња и графики", "Memes & graphics", "Документи", ["a meme with text", "a poster", "a cartoon or illustration"]),
    ("Мапа", "Map", "Документи", ["a map"]),
    ("Цртеж", "Drawing", "Документи", ["a child's drawing", "a painting"]),
    ("Портрет", "Portrait", "Вид", ["a professional portrait photo"]),
    ("Панорама", "Panorama", "Вид", ["a wide panoramic landscape photo"]),
    ("Макро", "Close-up", "Вид", ["a macro close-up photo"]),
    ("Црно-бело", "Black & white", "Вид", ["a black and white photo"]),
    ("Стара фотографија", "Old photo", "Вид", ["an old vintage photograph", "a scanned old family photo"]),
    ("_мутна", "_blurry", "", ["a blurry photo", "a dark underexposed photo"]),
]
MK2EN = {mk: en for mk, en, _, _ in LABELS}
MAX_TAGS = 4
# Општи описи: ознаката се дава само ако е посилна од „слика од нешто“ (иначе нема ознака, наместо погрешна).
BACKGROUND = ["a photo", "a picture", "an image", "a photo of something", "a photo of an object", "a snapshot"]
Z_MIN = 3.0          # колку ознаката мора да се издвојува од останатите (во стандардни девијации)


def deps_ok():
    try:
        import numpy, onnxruntime, tokenizers  # noqa: F401
        return True, ""
    except Exception as e:
        return False, str(e)


def labels_public():
    return [{"mk": mk, "en": en, "group": g} for mk, en, g, _ in LABELS if not mk.startswith("_")]


class AI:
    def __init__(self, data_dir: Path, model_id=DEFAULT_MODEL):
        self.model_id = model_id if model_id in MODELS else DEFAULT_MODEL
        self.cfg = MODELS[self.model_id]
        self.dir = Path(data_dir) / self.model_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state, self.message, self.progress = "off", "", 0.0
        self.lock = threading.Lock()
        self.vis = self.txt = self.tok = self.L = None
        self.names, self.pre, self.custom, self.B = [], {}, [], None
        ok, err = deps_ok()
        if not ok:
            self.state, self.message = "missing_deps", f"Делот за препознавање не можеше да се вчита ({err[:160]}). Инсталирај ја Фотоархива повторно."
        elif not self.downloaded():
            self.state = "not_downloaded"

    def downloaded(self):
        return all((self.dir / f).exists() for f in ("vision.onnx", "text.onnx", "tokenizer.json"))

    def info(self):
        return {"state": self.state, "message": self.message, "progress": round(self.progress, 3), "model": self.model_id,
                "model_name": self.cfg["name"], "multilingual": self.cfg["multilingual"]}

    # ---------- download
    @staticmethod
    def _get_json(url):
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def _plan(self):
        """Ги наоѓа точните датотеки на моделот преку HuggingFace API (имињата се разликуваат меѓу верзии)."""
        last = None
        for repo in self.cfg["repos"]:
            try:
                info = self._get_json(f"{HF}/api/models/{repo}")
            except Exception as e:
                last = e
                continue
            files = {s["rfilename"] for s in info.get("siblings", [])}
            v = next((f for f in PREFER["vision"] if f in files), None)
            t = next((f for f in PREFER["text"] if f in files), None)
            if v and t and "tokenizer.json" in files:
                plan = {"vision.onnx": v, "text.onnx": t, "tokenizer.json": "tokenizer.json"}
                if "preprocessor_config.json" in files:
                    plan["preprocessor_config.json"] = "preprocessor_config.json"
                return repo, plan
            last = RuntimeError(f"{repo}: нема потребни ONNX датотеки")
        raise RuntimeError(f"моделот не е најден ({last})")

    def download(self):
        with self.lock:
            if self.state in ("downloading", "loading", "ready", "missing_deps"):
                return
            self.state, self.progress, self.message = "downloading", 0.0, ""
        try:
            repo, plan = self._plan()
            todo = {k: v for k, v in plan.items() if not (self.dir / k).exists()}
            total, done, known = self.cfg["approx_mb"] * 1_000_000, 0, 0
            for local, remote in todo.items():
                part = self.dir / (local + ".part")
                url = f"{HF}/{repo}/resolve/main/{remote}"
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r, open(part, "wb") as out:
                    known += int(r.headers.get("Content-Length") or 0)
                    total = max(total, known)
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        self.progress = min(0.99, done / max(total, done + 1))
                part.replace(self.dir / local)
            (self.dir / "source.json").write_text(json.dumps({"repo": repo, "files": plan}), encoding="utf-8")
            self.progress = 1.0
            self.load()
        except Exception as e:
            self.state, self.message = "error", f"Симнувањето не успеа: {e}. Провери интернет и пробај повторно."

    # ---------- load
    def load(self):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.state = "loading"
        try:
            pre = {}
            pc = self.dir / "preprocessor_config.json"
            if pc.exists():
                pre = json.loads(pc.read_text(encoding="utf-8"))
            if self.cfg["kind"] == "siglip":
                size = pre.get("size") or {}
                side = size.get("height") or size.get("shortest_edge") or 224
                self.pre = {"side": int(side), "crop": False, "mean": pre.get("image_mean", [0.5] * 3), "std": pre.get("image_std", [0.5] * 3)}
            else:
                self.pre = {"side": 224, "crop": True, "mean": pre.get("image_mean", [0.48145466, 0.4578275, 0.40821073]),
                            "std": pre.get("image_std", [0.26862954, 0.26130258, 0.27577711])}
            so = ort.SessionOptions()
            so.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
            prov = ["CPUExecutionProvider"]
            self.vis = ort.InferenceSession(str(self.dir / "vision.onnx"), so, providers=prov)
            self.txt = ort.InferenceSession(str(self.dir / "text.onnx"), so, providers=prov)
            self.tok = Tokenizer.from_file(str(self.dir / "tokenizer.json"))
            pad = None
            for t in ("<pad>", "<|endoftext|>", "[PAD]"):
                pad = self.tok.token_to_id(t)
                if pad is not None:
                    break
            self.tok.enable_truncation(self.cfg["ctx"])
            self.tok.enable_padding(length=self.cfg["ctx"], pad_id=pad if pad is not None else 0)
            self.build_labels()
            self.state, self.message = "ready", ""
        except Exception as e:
            self.state, self.message = "error", f"Моделот не можеше да се вчита: {e}"

    def build_labels(self):
        """Векторите за сите категории: вградените + твоите („Мои категории“). Се кешираат на диск."""
        import numpy as np
        allx = [(l[0], l[3]) for l in LABELS] + [(n, p) for n, p in self.custom]
        sig = hashlib.sha1(json.dumps([self.model_id, allx, BACKGROUND], ensure_ascii=False).encode()).hexdigest()[:12]
        cache = self.dir / f"labels-{sig}.npz"
        if cache.exists():
            z = np.load(cache)
            L, B = z["L"], z["B"]
        else:
            rows = []
            for _, prompts in allx:
                m = self.encode_text(prompts).mean(0)
                rows.append(m / np.linalg.norm(m))
            L = np.stack(rows).astype(np.float32)
            B = self.encode_text(BACKGROUND).astype(np.float32)
            np.savez(cache, L=L, B=B)
            for old in self.dir.glob("labels-*"):
                if old != cache:
                    try:
                        old.unlink()
                    except OSError:
                        pass
        self.L, self.B = L, B
        self.names = [a[0] for a in allx]

    def set_custom(self, cats):
        """cats: [{"name":..., "desc":...}] → и македонски и англиски описи работат со SigLIP 2."""
        out = []
        for c in cats or []:
            name = str(c.get("name", "")).strip()
            if not name:
                continue
            desc = str(c.get("desc", "")).strip()
            prompts = [p for p in (desc, name, f"a photo of {desc or name}") if p]
            out.append((name, prompts))
        self.custom = out
        if self.state == "ready":
            self.build_labels()

    @staticmethod
    def _pick(sess, outs, kind):
        names = [o.name for o in sess.get_outputs()]
        for pref in ("embeds", "pooler_output"):
            for n, o in zip(names, outs):
                if pref in n and getattr(o, "ndim", 0) == 2:
                    return o
        for o in outs:
            if getattr(o, "ndim", 0) == 2:
                return o
        raise RuntimeError(f"неочекуван {kind} модел: излези {names}")

    # ---------- inference
    def encode_text(self, texts):
        import numpy as np
        if self.cfg["kind"] == "siglip":
            texts = [t.lower() for t in texts]
        enc = self.tok.encode_batch(list(texts))
        feeds = {}
        for i in self.txt.get_inputs():
            if "mask" in i.name:
                feeds[i.name] = np.array([e.attention_mask for e in enc], dtype=np.int64)
            elif "type" in i.name:
                feeds[i.name] = np.zeros((len(enc), self.cfg["ctx"]), dtype=np.int64)
            else:
                feeds[i.name] = np.array([e.ids for e in enc], dtype=np.int64)
        e = self._pick(self.txt, self.txt.run(None, feeds), "text").astype(np.float32)
        return e / np.linalg.norm(e, axis=1, keepdims=True)

    def preprocess(self, im):
        import numpy as np
        from PIL import Image
        side, p = self.pre["side"], self.pre
        im = im.convert("RGB")
        if p["crop"]:
            w, h = im.size
            s = side / min(w, h)
            im = im.resize((max(side, round(w * s)), max(side, round(h * s))), Image.BICUBIC)
            w, h = im.size
            l, t = (w - side) // 2, (h - side) // 2
            im = im.crop((l, t, l + side, t + side))
        else:
            im = im.resize((side, side), Image.BILINEAR)
        a = np.asarray(im, dtype=np.float32) / 255.0
        a = (a - np.array(p["mean"], dtype=np.float32)) / np.array(p["std"], dtype=np.float32)
        return a.transpose(2, 0, 1)

    def embed(self, images):
        import numpy as np
        x = np.stack([self.preprocess(im) for im in images]).astype(np.float32)
        i = self.vis.get_inputs()[0]
        if "float16" in i.type:
            x = x.astype(np.float16)
        e = self._pick(self.vis, self.vis.run(None, {i.name: x}), "vision").astype(np.float32)
        return e / np.linalg.norm(e, axis=1, keepdims=True)

    def classify(self, E):
        """За секоја слика [(ознака, z-оценка)].
        Ознака се дава само ако (1) јасно се издвојува од сите други категории (z ≥ Z_MIN)
        и (2) е посилна од општиот опис „слика од нешто“. Ако ништо не одговара, сликата нема ознака."""
        import numpy as np
        S = E @ self.L.T                         # сличност со секоја категорија
        bg = (E @ self.B.T).max(1)               # сличност со општо „слика“
        mu = S.mean(1, keepdims=True)
        sd = S.std(1, keepdims=True) + 1e-6
        Z = (S - mu) / sd
        out = []
        for k in range(len(E)):
            idx = np.argsort(-Z[k])[:MAX_TAGS + 2]
            tags = [(self.names[i], round(float(Z[k, i]), 2)) for i in idx
                    if Z[k, i] >= Z_MIN and S[k, i] >= bg[k]]
            out.append(tags[:MAX_TAGS])
        return out

    def query_vector(self, q):
        """Вектор за слободно пребарување. SigLIP 2 ги разбира и македонски и англиски.
        CLIP разбира само англиски, па македонските зборови се преведуваат преку ознаките."""
        q = q.strip()
        if not self.cfg["multilingual"] and any(ord(c) > 127 for c in q):
            low = q.lower()
            words = [en for mk, en, _, _ in LABELS if not mk.startswith("_") and mk.lower() in low]
            words += [en for mk, en, _, _ in LABELS if not mk.startswith("_") and any(w and w in low for w in mk.lower().split(" и ") if len(w) > 3)]
            if words:
                q = "a photo of " + " and ".join(dict.fromkeys(words))
        return self.encode_text([q if len(q.split()) > 1 else "a photo of " + q])[0]
