"""Find and replace personal identifiers before text leaves the device or reaches a log.

Two passes: the user's own identifiers from secrets.toml ([identifiers]),
then patterns (emails, SSNs, card and account numbers, phones). Matches become
tokens like [[ACCOUNT_1]] that can be restored in a model's reply.

This lowers exposure; it does not make text anonymous. Merchant names,
amounts and dates pass through.
"""
from __future__ import annotations

import re
import traceback

TOKEN_RE = re.compile(r"\[\[([A-Z]+)_(\d+)\]\]")
EDGE_L, EDGE_R = r"(?<![^\W_])", r"(?![^\W_])"  # not inside a word or number
CANARY_MIN = 4  # shorter identifiers are redacted but not used to block a send


def _norm(text: str) -> str:
    return re.sub(r"[\W_]", "", text.casefold())


def _luhn(match: str) -> bool:
    digits = [int(c) for c in match if c.isdigit()]
    total = sum(digits[-1::-2]) + sum(sum(divmod(2 * d, 10)) for d in digits[-2::-2])
    return total % 10 == 0


def _aba(match: str) -> bool:
    d = [int(c) for c in match]
    return sum(d) > 0 and (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + d[2] + d[5] + d[8]) % 10 == 0


def _iban(match: str) -> bool:
    s = match[4:] + match[:4]
    return int("".join(str(int(c, 36)) for c in s)) % 97 == 1


# Order matters: specific shapes first, the catch-all digit run last.
PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), None),
    ("SSN", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"), None),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), _iban),
    ("CARD", re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"), _luhn),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"), None),
    ("TAXID", re.compile(r"(?<!\d)\d{2}-\d{7}(?!\d)"), None),
    ("ROUTING", re.compile(r"(?<!\d)\d{9}(?!\d)"), _aba),
    ("NUMBER", re.compile(r"(?<!\d)\d{8,}(?!\d)"), None),
]


def _known_pattern(value: str):
    """Match an identifier however a statement spaces or punctuates it."""
    parts = re.findall(r"[^\W_]+", value)
    if not parts:
        return None
    if all(p.isdigit() for p in parts):
        body = r"[\s.-]?".join("".join(parts))
    else:
        body = r"[\s.,/-]*".join(re.escape(p) for p in parts)
    return re.compile(EDGE_L + body + EDGE_R, re.IGNORECASE)


class Redactor:
    def __init__(self, identifiers: dict | None = None):
        known = []
        for label, values in (identifiers or {}).items():
            for value in [values] if isinstance(values, str) else values:
                rx = _known_pattern(str(value))
                if rx:
                    known.append((len(_norm(str(value))), re.sub(r"[^A-Z]", "", label.upper()) or "ID", rx, _norm(str(value))))
        known.sort(key=lambda k: -k[0])  # longest first: full name before surname
        self._known = [(label, rx) for _, label, rx, _ in known]
        self._canaries = [(label, norm) for size, label, _, norm in known if size >= CANARY_MIN]
        self._tokens: dict = {}   # (label, normalized value) -> token
        self._originals: dict = {}  # token -> first text seen

    def _token(self, label: str, text: str) -> str:
        key = (label, _norm(text))
        if key not in self._tokens:
            n = sum(1 for k in self._tokens if k[0] == label) + 1
            self._tokens[key] = f"[[{label}_{n}]]"
            self._originals[self._tokens[key]] = text
        return self._tokens[key]

    def redact(self, text):
        if not text or not isinstance(text, str):
            return text
        for label, rx in self._known:
            text = rx.sub(lambda m, label=label: self._token(label, m.group(0)), text)
        for label, rx, valid in PATTERNS:
            text = rx.sub(lambda m, label=label, valid=valid: self._token(label, m.group(0))
                          if valid is None or valid(re.sub(r"[ -]", "", m.group(0))) else m.group(0), text)
        return text

    def restore(self, value):
        """Put original text back into a model reply (str, or parsed JSON)."""
        if isinstance(value, str):
            return TOKEN_RE.sub(lambda m: self._originals.get(m.group(0), m.group(0)), value)
        if isinstance(value, list):
            return [self.restore(v) for v in value]
        if isinstance(value, dict):
            return {self.restore(k): self.restore(v) for k, v in value.items()}
        return value

    def leaks(self, text: str) -> list:
        """Labels of known identifiers still present, ignoring case, spacing and punctuation."""
        flat = _norm(text or "")
        return sorted({label for label, norm in self._canaries if norm in flat})


def safe_traceback(exc: BaseException, redactor: Redactor, withhold: bool = False) -> str:
    """A traceback fit to store and send to a model: frames and types, never raw messages.

    Exception messages often quote the data that broke the code, so strict
    apps drop them (withhold=True) and other apps get them redacted.
    """
    chunks, seen = [], set()
    while exc is not None and id(exc) not in seen and len(seen) < 4:
        seen.add(id(exc))
        frames = "".join(traceback.format_tb(exc.__traceback__))
        message = str(exc)
        if withhold:
            message = f"<message withheld, {len(message)} chars>" if message else ""
        else:
            message = redactor.redact(message)
        chunks.append(f"Traceback (most recent call last):\n{frames}{type(exc).__name__}: {message}".rstrip(": "))
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return "\n\nCaused by an error raised while handling:\n\n".join(chunks) + "\n"
