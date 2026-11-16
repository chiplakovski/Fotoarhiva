"""Паметно препознавање на содржината на сликите (CLIP модел, работи локално, без праќање слики на интернет).

Моделот (~155 MB) се симнува еднаш од HuggingFace во папката на библиотеката.
Потребни пакети: onnxruntime, numpy, tokenizers (start.bat ги инсталира).
"""
import hashlib, json, os, threading, time, urllib.request
from pathlib import Path

BASE = os.environ.get("FOTOARHIVA_MODEL_BASE", "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main")
FILES = {
    "vision.onnx": "/onnx/vision_model_quantized.onnx",
    "text.onnx": "/onnx/text_model_quantized.onnx",
    "tokenizer.json": "/tokenizer.json",
}
MEAN = (0.48145466, 0.4578275, 0.40821073)
STD = (0.26862954, 0.26130258, 0.27577711)

# Македонско име → англиски описи за моделот. Ознаките со "_" на почеток служат само за споредба и не се прикажуваат.
LABELS = {
    "Природа": ["a photo of nature", "a landscape photo with trees and fields", "a photo of a forest"],
    "Планини": ["a photo of mountains", "a mountain landscape", "a photo taken while hiking in the mountains"],
    "Море и плажа": ["a photo of a beach", "a photo of the sea", "people swimming at the seaside"],
    "Езеро и река": ["a photo of a lake", "a photo of a river", "a calm lake with a shore"],
    "Град": ["a photo of a city street", "a photo of buildings in a city", "a city skyline"],
    "Знаменитости": ["a photo of a church or monastery", "a photo of a historic monument", "a photo of an old fortress or ruins"],
    "Луѓе": ["a portrait photo of a person", "a selfie", "a close-up photo of a face"],
    "Друштво": ["a group photo of friends", "a photo of a group of people", "a family photo"],
    "Деца": ["a photo of a baby", "a photo of young children", "kids playing"],
    "Прослави": ["a birthday party", "a photo of a celebration with a cake", "people at a party"],
    "Свадби": ["a wedding photo", "a bride in a white dress", "a wedding celebration"],
    "Храна": ["a photo of food", "a plate of food on a table", "a photo of a meal at a restaurant"],
    "Пијалаци": ["a photo of a drink", "a cup of coffee", "glasses of wine or beer"],
    "Кучиња": ["a photo of a dog", "a puppy"],
    "Мачки": ["a photo of a cat", "a kitten"],
    "Животни": ["a photo of an animal", "a photo of a bird", "farm animals"],
    "Цвеќиња": ["a photo of flowers", "a close-up of a flower", "a garden with flowers"],
    "Зајдисонце": ["a photo of a sunset", "a sunrise over the horizon", "an orange sky at dusk"],
    "Зима и снег": ["a photo of snow", "a winter landscape", "people skiing"],
    "Ноќ": ["a photo taken at night", "city lights at night", "a dark night scene"],
    "Автомобили": ["a photo of a car", "a photo of a vehicle", "a motorcycle"],
    "Спорт": ["a photo of people playing sports", "a football match", "a gym workout"],
    "Дома": ["a photo of a living room", "a photo inside a house", "a kitchen interior"],
    "Документи": ["a photo of a document", "a photo of a paper with text", "a receipt or invoice", "a scanned page"],
    "Скриншот": ["a screenshot of a phone screen", "a screenshot of a computer screen", "a screenshot of a chat app"],
    "Мемиња и графики": ["a meme with text", "a graphic design poster", "a cartoon or illustration"],
    "_предмет": ["a photo of an object", "a photo of something"],
    "_мутна": ["a blurry photo", "a dark underexposed photo"],
}
THRESH, MAX_TAGS = 0.15, 3


def deps_ok():
    try:
        import numpy, onnxruntime, tokenizers  # noqa: F401
        return True, ""
    except Exception as e:
        return False, str(e)


class AI:
    def __init__(self, model_dir: Path):
        self.dir = model_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state = "off"        # off | missing_deps | not_downloaded | downloading | loading | ready | error
        self.message = ""
        self.progress = 0.0
        self.lock = threading.Lock()
        self.vis = None
        self.L = None
        self.names = []
        ok, err = deps_ok()
        if not ok:
            self.state, self.message = "missing_deps", f"Делот за препознавање не можеше да се вчита ({err[:160]}). Инсталирај ја Фотоархива повторно."
        elif not self.downloaded():
            self.state = "not_downloaded"

    def downloaded(self):
        return all((self.dir / f).exists() for f in FILES)

    def info(self):
        return {"state": self.state, "message": self.message, "progress": round(self.progress, 3)}

    # ---------- download
    def download(self):
        with self.lock:
            if self.state in ("downloading", "loading", "ready", "missing_deps"):
                return
            self.state, self.progress, self.message = "downloading", 0.0, ""
        try:
            expected = {"vision.onnx": 88_000_000, "text.onnx": 65_000_000, "tokenizer.json": 2_300_000}
            todo = [f for f in FILES if not (self.dir / f).exists()]
            total = sum(expected[f] for f in todo) or 1
            done = 0
            for f in todo:
                part = self.dir / (f + ".part")
                req = urllib.request.Request(BASE + FILES[f], headers={"User-Agent": "Fotoarhiva"})
                with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as out:
                    real = int(r.headers.get("Content-Length") or 0)
                    if real:
                        total += real - expected[f]
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        self.progress = min(0.99, done / max(total, 1))
                part.replace(self.dir / f)
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
            so = ort.SessionOptions()
            so.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
            self.vis = ort.InferenceSession(str(self.dir / "vision.onnx"), so, providers=["CPUExecutionProvider"])
            sig = hashlib.sha1(json.dumps(LABELS, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
            cache = self.dir / f"labels-{sig}.npy"
            if cache.exists():
                L = np.load(cache)
            else:
                txt = ort.InferenceSession(str(self.dir / "text.onnx"), so, providers=["CPUExecutionProvider"])
                tok = Tokenizer.from_file(str(self.dir / "tokenizer.json"))
                pad = tok.token_to_id("<|endoftext|>")
                tok.enable_truncation(77)
                tok.enable_padding(length=77, pad_id=pad if pad is not None else 0)
                rows = []
                for name, prompts in LABELS.items():
                    enc = tok.encode_batch(prompts)
                    feeds = {}
                    for i in txt.get_inputs():
                        if "mask" in i.name:
                            feeds[i.name] = np.array([e.attention_mask for e in enc], dtype=np.int64)
                        else:
                            feeds[i.name] = np.array([e.ids for e in enc], dtype=np.int64)
                    e = self._pick(txt, txt.run(None, feeds), "text")
                    e = e / np.linalg.norm(e, axis=1, keepdims=True)
                    m = e.mean(0)
                    rows.append(m / np.linalg.norm(m))
                L = np.stack(rows).astype(np.float32)
                np.save(cache, L)
                del txt
            self.L = L
            self.names = list(LABELS)
            self.state, self.message = "ready", ""
        except Exception as e:
            self.state, self.message = "error", f"Моделот не можеше да се вчита: {e}"

    @staticmethod
    def _pick(sess, outs, kind):
        names = [o.name for o in sess.get_outputs()]
        for n, o in zip(names, outs):
            if "embed" in n:
                return o
        for o in outs:
            if getattr(o, "ndim", 0) == 2:
                return o
        raise RuntimeError(f"Неочекуван {kind} модел: излези {names}")

    # ---------- inference
    def preprocess(self, im):
        import numpy as np
        from PIL import Image
        im = im.convert("RGB")
        w, h = im.size
        s = 224 / min(w, h)
        im = im.resize((max(224, round(w * s)), max(224, round(h * s))), Image.BICUBIC)
        w, h = im.size
        l, t = (w - 224) // 2, (h - 224) // 2
        im = im.crop((l, t, l + 224, t + 224))
        a = np.asarray(im, dtype=np.float32) / 255.0
        a = (a - np.array(MEAN, dtype=np.float32)) / np.array(STD, dtype=np.float32)
        return a.transpose(2, 0, 1)

    def embed(self, images):
        """images: листа PIL слики → нормализирани вектори (N, D)."""
        import numpy as np
        x = np.stack([self.preprocess(im) for im in images]).astype(np.float32)
        name = self.vis.get_inputs()[0].name
        e = self._pick(self.vis, self.vis.run(None, {name: x}), "vision")
        return e / np.linalg.norm(e, axis=1, keepdims=True)

    def classify(self, E):
        """E: (N, D) → за секоја слика листа [(ознака, веројатност)], подредена."""
        import numpy as np
        logits = 100.0 * E @ self.L.T
        logits -= logits.max(1, keepdims=True)
        p = np.exp(logits)
        p /= p.sum(1, keepdims=True)
        out = []
        for row in p:
            idx = np.argsort(-row)
            tags = []
            for k, i in enumerate(idx[:MAX_TAGS + 2]):
                n = self.names[i]
                if row[i] >= THRESH or (k == 0 and row[i] >= 0.10):
                    tags.append((n, float(row[i])))
            out.append(tags)
        return out
