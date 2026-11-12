"""Thin, stdlib-only client for model providers.

Apps call ctx.ai(capability, prompt, file=...). Capabilities map to real
models in config.toml, so swapping models never touches app code.
Add a provider by writing one function and registering it in PROVIDERS.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path


class AIError(Exception):
    pass


class EgressBlocked(AIError):
    """A strict app tried to send something the gate can't make safe."""


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_BASE = "https://api.openai.com/v1"
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
TEXT_TYPES = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".xml", ".html", ".ofx", ".qfx"}
AUDIO_TYPES = {".m4a", ".mp3", ".wav", ".mp4", ".webm", ".mpeg", ".mpga", ".ogg", ".flac"}


class AIClient:
    def __init__(self, cfg, allowed: set | None = None, on_call=None, models: dict | None = None,
                 gate=None, audit: Path | None = None, app_id: str | None = None):
        self.cfg = cfg
        self.allowed = allowed
        self.on_call = on_call
        self.models = models or cfg.models
        self.gate = gate      # a Redactor: set for strict apps
        self.audit = audit    # folder for the log of what was actually sent
        self.app_id = app_id

    def __call__(self, capability: str, prompt: str | None = None, *, file=None,
                 system: str | None = None, max_tokens: int = 1024, as_json: bool = False):
        if self.allowed is not None and capability not in self.allowed:
            raise AIError(f"Capability '{capability}' is not declared in [ai].uses")
        spec = self.models.get(capability)
        if not spec:
            raise AIError(f"No model mapped for '{capability}' in config.toml")
        provider = PROVIDERS.get(spec.get("provider"))
        if not provider:
            raise AIError(f"Unknown provider '{spec.get('provider')}'")

        file = Path(file) if file else None
        if self.gate is not None:
            prompt, system, file = self._gated(capability, spec, prompt, system, file)

        start = time.monotonic()
        text = provider(self.cfg, spec, capability=capability, prompt=prompt,
                        file=file, system=system, max_tokens=max_tokens)
        if self.on_call:
            self.on_call({"capability": capability, "provider": spec.get("provider"),
                          "model": spec.get("model"), "ms": int((time.monotonic() - start) * 1000)})
        result = parse_json(text) if as_json else text
        return self.gate.restore(result) if self.gate is not None else result

    def _gated(self, capability, spec, prompt, system, file):
        """Egress gate: redact, verify, record. Raises instead of sending anything doubtful."""
        if file is not None:
            if file.suffix.lower() not in TEXT_TYPES:
                raise EgressBlocked(
                    f"A strict app can't send a {file.suffix or 'binary'} file to a model, because it "
                    "can't be redacted. Extract the text on the device and send that.")
            prompt = file.read_text(encoding="utf-8", errors="replace") + "\n\n" + (prompt or "")
        prompt = self.gate.redact(prompt or "")
        system = self.gate.redact(system) if system else system
        leaked = self.gate.leaks(prompt + "\n" + (system or ""))
        if leaked:
            raise EgressBlocked("Not sent: your " + ", ".join(leaked) +
                                " would still be readable after redaction.")
        if self.audit is not None:
            self.audit.mkdir(parents=True, exist_ok=True)
            entry = {"ts": datetime.now().isoformat(timespec="seconds"), "app": self.app_id,
                     "capability": capability, "provider": spec.get("provider"),
                     "model": spec.get("model"), "system": system, "prompt": prompt}
            with open(self.audit / f"{datetime.now():%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return prompt, system, None


def parse_json(text: str):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise AIError(f"Model did not return valid JSON: {text[:200]}")


def _post(url: str, headers: dict, body: bytes, timeout: int = 120, retries: int = 2) -> dict:
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code in (429, 500, 502, 503, 529) and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise AIError(f"HTTP {e.code} from {url}: {detail}") from None
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise AIError(f"Network error: {e.reason}") from None
    raise AIError("unreachable")


def _multipart(fields: dict, file_field: str, path: Path):
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in fields.items()]
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                 f'filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _anthropic(cfg, spec, *, prompt, file, system, max_tokens, **_):
    content = []
    if file:
        ext = file.suffix.lower()
        if ext in IMAGE_TYPES or ext == ".pdf":
            data = base64.b64encode(file.read_bytes()).decode()
            kind, media = ("document", "application/pdf") if ext == ".pdf" else ("image", IMAGE_TYPES[ext])
            content.append({"type": kind, "source": {"type": "base64", "media_type": media, "data": data}})
        else:
            content.append({"type": "text", "text": file.read_text(encoding="utf-8", errors="replace")})
    content.append({"type": "text", "text": prompt or "Describe this."})

    body = {"model": spec["model"], "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}]}
    if system:
        body["system"] = system
    headers = {"x-api-key": cfg.secret("ANTHROPIC_API_KEY"),
               "anthropic-version": "2023-06-01", "content-type": "application/json"}
    data = _post(ANTHROPIC_URL, headers, json.dumps(body).encode())
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def _openai(cfg, spec, *, prompt, file, system, max_tokens, **_):
    key = cfg.secret("OPENAI_API_KEY")
    if file and file.suffix.lower() in AUDIO_TYPES:
        fields = {"model": spec["model"]}
        if prompt:
            fields["prompt"] = prompt
        body, ctype = _multipart(fields, "file", file)
        data = _post(f"{OPENAI_BASE}/audio/transcriptions",
                     {"Authorization": f"Bearer {key}", "Content-Type": ctype}, body, timeout=300)
        return data.get("text", "")

    user = [{"type": "text", "text": prompt or "Describe this."}]
    if file:
        ext = file.suffix.lower()
        if ext in IMAGE_TYPES:
            b64 = base64.b64encode(file.read_bytes()).decode()
            user.append({"type": "image_url", "image_url": {"url": f"data:{IMAGE_TYPES[ext]};base64,{b64}"}})
        else:
            user.append({"type": "text", "text": file.read_text(encoding="utf-8", errors="replace")})
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
    body = {"model": spec["model"], "messages": messages, "max_completion_tokens": max_tokens}
    data = _post(f"{OPENAI_BASE}/chat/completions",
                 {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                 json.dumps(body).encode())
    return data["choices"][0]["message"].get("content") or ""


def _mock(cfg, spec, *, capability, prompt, file, **_):
    """Offline provider for smoke tests: no network, no cost."""
    if "reply" in spec:  # a list is used up one reply per call
        return spec["reply"].pop(0) if isinstance(spec["reply"], list) else spec["reply"]
    source = prompt or (file.name if file else "")
    return f"[mock {capability}] {source[:200]}"


PROVIDERS = {"anthropic": _anthropic, "openai": _openai, "mock": _mock}
