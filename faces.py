"""People: finds faces in photos and recognizes the same person in other photos. Runs locally.

Models (downloaded once from HuggingFace, ~39 MB, from the OpenCV model zoo):
  * YuNet (MIT license): finds faces and 5 points on each face (eyes, nose, mouth corners)
  * SFace (Apache 2.0 license): turns a face into 128 numbers; the same person gives similar numbers
"""
import os, threading, urllib.request
from pathlib import Path

HF = os.environ.get("FOTOARHIVA_HF", "https://huggingface.co")
UA = {"User-Agent": "Fotoarhiva/2.8"}
FILES = {
    "yunet.onnx": "opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx",
    "sface.onnx": "opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx",
}
SIZE = 640              # the detector looks at the photo at this size
MIN_SCORE = 0.82        # how sure the detector must be that it is a face
MIN_FACE = 28           # faces smaller than this (in pixels of the 640 picture) are too small to recognize
SAME_PERSON = 0.42      # cosine similarity above which two faces are the same person (OpenCV suggests 0.363)
# where the 5 face points must end up in the 112×112 face picture that SFace expects
TEMPLATE = [(38.2946, 51.6963), (73.5318, 51.5014), (56.0252, 71.7366), (41.5493, 92.3655), (70.7299, 92.2041)]


class Faces:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "faces"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.det = self.rec = None
        self.state, self.message = "off", ""
        self.lock = threading.Lock()

    def downloaded(self):
        return all((self.dir / f).exists() for f in FILES)

    def download(self):
        self.state, self.message = "downloading", ""
        try:
            for name, path in FILES.items():
                dest = self.dir / name
                if dest.exists():
                    continue
                part = dest.with_name(name + ".part")
                with urllib.request.urlopen(urllib.request.Request(f"{HF}/{path}", headers=UA), timeout=60) as r, open(part, "wb") as out:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                part.replace(dest)
            self.state = "downloaded"
        except Exception as e:
            self.state, self.message = "error", f"Download failed: {e}"

    def load(self):
        with self.lock:
            if self.det is not None:
                return True
            if not self.downloaded():
                self.download()
                if not self.downloaded():
                    return False
            try:
                import onnxruntime as ort
                o = ort.SessionOptions()
                o.log_severity_level = 3
                o.intra_op_num_threads = 2
                cpu = ["CPUExecutionProvider"]
                self.det = ort.InferenceSession(str(self.dir / "yunet.onnx"), o, providers=cpu)
                self.rec = ort.InferenceSession(str(self.dir / "sface.onnx"), o, providers=cpu)
                self.state = "ready"
                return True
            except Exception as e:
                self.state, self.message = "error", f"The face models could not be loaded: {e}"
                return False

    # ---------- finding faces
    def detect(self, im):
        """[(box (x, y, w, h) as fractions of the photo, 5 points in photo pixels, score)]"""
        import numpy as np
        rgb = im.convert("RGB")
        W, H = rgb.size
        scale = SIZE / max(W, H)
        small = rgb.resize((max(1, round(W * scale)), max(1, round(H * scale))))
        canvas = np.zeros((SIZE, SIZE, 3), dtype=np.float32)
        a = np.asarray(small, dtype=np.float32)
        canvas[:a.shape[0], :a.shape[1]] = a
        blob = canvas[:, :, ::-1].transpose(2, 0, 1)[None].copy()   # BGR, as YuNet expects
        out = dict(zip([o.name for o in self.det.get_outputs()], self.det.run(None, {self.det.get_inputs()[0].name: blob})))
        boxes, scores, kps = [], [], []
        for s in (8, 16, 32):
            cols = SIZE // s
            cls, obj = out[f"cls_{s}"][0, :, 0], out[f"obj_{s}"][0, :, 0]
            score = np.sqrt(np.clip(cls, 0, 1) * np.clip(obj, 0, 1))
            keep = np.nonzero(score >= MIN_SCORE)[0]
            if not len(keep):
                continue
            r, c = keep // cols, keep % cols
            bb, kp = out[f"bbox_{s}"][0, keep], out[f"kps_{s}"][0, keep]
            cx, cy = (c + bb[:, 0]) * s, (r + bb[:, 1]) * s
            w, h = np.exp(bb[:, 2]) * s, np.exp(bb[:, 3]) * s
            boxes.append(np.stack([cx - w / 2, cy - h / 2, w, h], 1))
            scores.append(score[keep])
            k = kp.reshape(-1, 5, 2).copy()
            k[:, :, 0] = (k[:, :, 0] + c[:, None]) * s
            k[:, :, 1] = (k[:, :, 1] + r[:, None]) * s
            kps.append(k)
        if not boxes:
            return []
        boxes, scores, kps = np.concatenate(boxes), np.concatenate(scores), np.concatenate(kps)
        found = []
        for i in self._nms(boxes, scores):
            x, y, w, h = boxes[i]
            if min(w, h) < MIN_FACE:
                continue
            x, y = max(0.0, x), max(0.0, y)
            w, h = min(w, small.size[0] - x), min(h, small.size[1] - y)
            found.append(((float(x / small.size[0]), float(y / small.size[1]), float(w / small.size[0]), float(h / small.size[1])),
                          kps[i] / scale, float(scores[i])))
        return found

    @staticmethod
    def _nms(boxes, scores, thr=0.3):
        import numpy as np
        order = np.argsort(-scores)
        keep = []
        while len(order):
            i = order[0]
            keep.append(i)
            x1 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
            y1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
            x2 = np.minimum(boxes[i, 0] + boxes[i, 2], boxes[order[1:], 0] + boxes[order[1:], 2])
            y2 = np.minimum(boxes[i, 1] + boxes[i, 3], boxes[order[1:], 1] + boxes[order[1:], 3])
            inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
            union = boxes[i, 2] * boxes[i, 3] + boxes[order[1:], 2] * boxes[order[1:], 3] - inter
            order = order[1:][inter / np.maximum(union, 1e-6) < thr]
        return keep

    # ---------- recognizing
    def embed(self, im, points):
        """128 numbers for one face (normalized), from the photo and the 5 face points."""
        import numpy as np
        from PIL import Image
        M = self._similarity(np.asarray(points, dtype=np.float64), np.asarray(TEMPLATE, dtype=np.float64))
        # PIL wants the inverse mapping (from the 112×112 output back into the photo)
        inv = np.linalg.inv(np.vstack([M, [0, 0, 1]]))[:2]
        face = im.convert("RGB").transform((112, 112), Image.AFFINE, tuple(inv.flatten()), resample=Image.BILINEAR)
        blob = np.asarray(face, dtype=np.float32).transpose(2, 0, 1)[None].copy()   # RGB, 0..255
        v = self.rec.run(None, {self.rec.get_inputs()[0].name: blob})[0][0]
        return (v / (np.linalg.norm(v) + 1e-9)).astype(np.float32)

    @staticmethod
    def _similarity(src, dst):
        """Rotation + uniform scale + shift that maps src points onto dst points (Umeyama)."""
        import numpy as np
        ms, md = src.mean(0), dst.mean(0)
        s0, d0 = src - ms, dst - md
        cov = d0.T @ s0 / len(src)
        U, S, Vt = np.linalg.svd(cov)
        D = np.eye(2)
        if np.linalg.det(U) * np.linalg.det(Vt) < 0:
            D[1, 1] = -1
        R = U @ D @ Vt
        scale = (S * np.diag(D)).sum() / s0.var(0).sum()
        t = md - scale * R @ ms
        return np.hstack([scale * R, t[:, None]])

    def analyze(self, im):
        """All faces in a photo: [(box, score, vector)]."""
        out = []
        for box, pts, score in self.detect(im):
            try:
                out.append((box, score, self.embed(im, pts)))
            except Exception:
                pass
        return out
