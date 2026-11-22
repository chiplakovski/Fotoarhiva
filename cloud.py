"""The most accurate recognition, through Claude (Anthropic). Optional: needs an API key and sends a
smaller copy of the photo (at most 1024 px) to Anthropic. The originals stay on the computer.
"""
import base64, io, json, threading, time

CLOUD_MODELS = {
    # approximate cost per 1000 photos (photo ~1000 tokens, cached category list, short answer)
    "claude-opus-5-5": {"name": "Claude Opus 5.5 (most accurate)", "usd_per_1000": 10.0, "fallback": True},
    "claude-sonnet-5-5": {"name": "Claude Sonnet 5.5 (very good, cheaper)", "usd_per_1000": 5.0, "fallback": True},
    "claude-haiku-5-5": {"name": "Claude Haiku 5.5 (good, cheapest)", "usd_per_1000": 0.3, "fallback": False},
}
DEFAULT_CLOUD_MODEL = "claude-opus-5-5"

SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {"type": "array", "items": {"type": "string"}},
        "new_tags": {"type": "array", "items": {"type": "string"}},
        "caption_mk": {"type": "string"},
        "caption_en": {"type": "string"},
        "kind": {"type": "string", "enum": ["photo", "screenshot", "document"]},
    },
    "required": ["tags", "new_tags", "caption_mk", "caption_en", "kind"],
    "additionalProperties": False,
}


def system_prompt(labels):
    names = "\n".join(f"- {name}" + (f" ({desc})" if desc and desc != name else "") for name, desc in labels)
    return (
        "You describe the photos in a person's private photo archive so they can search them easily.\n\n"
        "For each photo return:\n"
        "- tags: categories from the list below that are clearly visible in the photo. Write them exactly as in the list. "
        "At most 5. If none fits, return an empty list. Do not guess: no tag is better than a wrong one.\n"
        "- new_tags: up to 3 specific English tags for important things that are not in the list "
        "(for example \"Stainless steel machines\", \"Bakery\", \"Tractor\"). Short, capitalized, no periods.\n"
        "- caption_mk: leave empty.\n"
        "- caption_en: one specific English sentence describing what is in the photo (objects, place, activity).\n"
        "- kind: \"screenshot\" for a screen capture, \"document\" if it is mainly paper/text, otherwise \"photo\".\n\n"
        f"Categories:\n{names}"
    )


class CloudAI:
    def __init__(self):
        self.key = ""
        self.model = DEFAULT_CLOUD_MODEL
        self.enabled = False
        self.state, self.message = "off", ""
        self.client = None
        self.lock = threading.Lock()
        self.spent_in = self.spent_out = 0

    def configure(self, enabled, key, model):
        self.enabled = bool(enabled)
        self.key = (key or "").strip()
        self.model = model if model in CLOUD_MODELS else DEFAULT_CLOUD_MODEL
        self.client = None
        if not self.enabled:
            self.state, self.message = "off", ""
            return
        try:
            import anthropic  # noqa: F401
        except Exception as e:
            self.state, self.message = "error", f"The Claude part is missing ({e}). Please reinstall Fotoarhiva."
            return
        if not self.key:
            self.state, self.message = "error", "Enter an API key in Settings."
            return
        self.state, self.message = "ready", ""

    def _client(self):
        if self.client is None:
            import anthropic
            self.client = anthropic.Anthropic(api_key=self.key, max_retries=4, timeout=120.0)
        return self.client

    def info(self):
        return {"state": self.state, "message": self.message, "model": self.model,
                "tokens_in": self.spent_in, "tokens_out": self.spent_out}

    @staticmethod
    def image_bytes(path, rot=0):
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            if rot:
                im = im.rotate(-rot, expand=True)
            im.thumbnail((1024, 1024))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=85)
        return buf.getvalue()

    def describe(self, jpeg, labels):
        """Returns a dict with tags/new_tags/caption_en/kind, or None if the request was declined."""
        import anthropic
        params = dict(
            model=self.model,
            max_tokens=2000,
            system=[{"type": "text", "text": system_prompt(labels), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.standard_b64encode(jpeg).decode("ascii")}},
                {"type": "text", "text": "Describe this photo."},
            ]}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
        try:
            if CLOUD_MODELS[self.model]["fallback"]:
                r = self._client().beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
            else:
                r = self._client().messages.create(**params)
        except anthropic.AuthenticationError:
            self.state, self.message = "error", "The API key is not valid. Check it in Settings."
            raise
        except anthropic.PermissionDeniedError:
            self.state, self.message = "error", "The API key has no access to this model."
            raise
        except anthropic.RateLimitError as e:
            wait = int(e.response.headers.get("retry-after", "30") or 30)
            time.sleep(min(wait, 120))
            raise
        u = getattr(r, "usage", None)
        if u:
            self.spent_in += (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
            self.spent_out += u.output_tokens or 0
        if r.stop_reason == "refusal":
            return None
        text = next((b.text for b in r.content if b.type == "text"), "")
        if not text:
            return None
        return json.loads(text)
