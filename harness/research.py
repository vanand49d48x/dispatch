"""Research support: secret scrubbing for anything sent to the web, and a persistent library of solved errors."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

# Token-like strings that must never leave the machine in a search query or be stored in the library.
_SECRET_PATTERNS = [
    re.compile(r"\b(sk|rk|pk)_(live|test)_[A-Za-z0-9]{8,}"),       # Stripe
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"),                     # Anthropic
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),                     # GitHub
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsbp_[A-Za-z0-9]{20,}"),                           # Supabase access token
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),  # JWTs (anon/service keys)
    re.compile(r"\b(re|xoxb|xoxp|AKIA)[_A-Za-z0-9]{16,}"),           # Resend, Slack, AWS key ids
    re.compile(r"postgres(ql)?://[^\s'\"]+"),                        # DB URLs with passwords
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),                              # long hex secrets
    re.compile(r"[A-Za-z0-9+/_\-]{40,}={0,2}"),                       # long base64-ish blobs
]
_SECRET_ENV_HINT = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASS|DSN|URL)$", re.I)


def scrub(text: str, env: dict[str, str] | None = None) -> str:
    """Remove secrets: exact values of secret-looking env vars, then token-shaped strings."""
    out = text or ""
    for k, v in (env or {}).items():
        if v and len(v) >= 8 and (_SECRET_ENV_HINT.search(k) or len(v) >= 24):
            out = out.replace(v, f"<{k}>")
    for p in _SECRET_PATTERNS:
        out = p.sub("<redacted>", out)
    return out


_NOISE = [
    (re.compile(r"(/[\w.\-@]+)+"), " "),            # file paths
    (re.compile(r"https?://\S+"), " "),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), " "),
    (re.compile(r"\b\d+(\.\d+)*\b"), " "),          # numbers, versions, line:col
    (re.compile(r"[`'\"<>()\[\]{}:;,=|*]"), " "),
]
_STOP = {"the", "a", "an", "of", "to", "in", "at", "is", "for", "and", "or", "on", "with", "error", "failed",
         "file", "line", "from", "by", "be", "not", "was", "this", "it"}


def signature(error: str) -> set[str]:
    s = (error or "").lower()
    for rx, rep in _NOISE:
        s = rx.sub(rep, s)
    return {w for w in s.split() if len(w) > 2 and w not in _STOP}


class Library:
    """Append-only JSONL of solved problems; lookup by word overlap of the normalized error."""

    def __init__(self, path: Path, env: dict[str, str] | None = None):
        self.path = Path(path)
        self.env = env or {}

    def _entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def record(self, error: str, fix: str, sources: list[str] | None = None, project: str = "",
               context: str = "") -> bool:
        error, fix = scrub(error, self.env)[:1500], scrub(fix, self.env)[:3000]
        if not error.strip() or not fix.strip():
            return False
        sig = signature(error)
        for e in self._entries():                      # skip near-duplicates
            if _jaccard(sig, set(e.get("sig", []))) > 0.85:
                return False
        entry = {"ts": time.time(), "project": project, "context": scrub(context, self.env)[:300],
                 "error": error, "fix": fix, "sources": [s for s in (sources or []) if s.startswith("http")][:6],
                 "sig": sorted(sig)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        return True

    def lookup(self, error: str, k: int = 3, min_score: float = 0.25) -> list[dict]:
        sig = signature(error)
        if not sig:
            return []
        scored = [(_jaccard(sig, set(e.get("sig", []))), e) for e in self._entries()]
        scored = [x for x in scored if x[0] >= min_score]
        scored.sort(key=lambda x: (-x[0], -x[1].get("ts", 0)))
        return [dict(e, score=round(s, 2)) for s, e in scored[:k]]

    def format(self, hits: list[dict]) -> str:
        if not hits:
            return "No similar problems in the research library."
        parts = []
        for h in hits:
            src = ("\n  sources: " + ", ".join(h["sources"])) if h.get("sources") else ""
            parts.append(f"- (match {h['score']}) error: {h['error'][:300]}\n  fix: {h['fix'][:800]}{src}")
        return "Similar problems solved before:\n" + "\n".join(parts)


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


URL_RX = re.compile(r"https?://[^\s)\]>'\"]+")


def extract_urls(text: str) -> list[str]:
    seen, out = set(), []
    for u in URL_RX.findall(text or ""):
        u = u.rstrip(".,")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out
