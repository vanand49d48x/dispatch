"""Loads the single user-facing config file (config.yaml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_GATED = [
    r"\brm\s+-rf\s+/(?!tmp)",            # wiping outside the workspace
    r"vercel\s+(remove|rm)\b",
    r"vercel\s+domains\s+(add|buy)",
    r"gh\s+repo\s+delete",
    r"supabase\s+projects\s+delete",
    r"\bdrop\s+(database|schema)\b",
    r"aws\s+.*\b(delete|terminate)",
]


AUTONOMY_FULL = {
    "auto_approve_plan": True,
    "auto_continue_milestones": True,
    "release_approval": False,
    "start_if_no_plan_reply": True,
}


class ConfigError(Exception):
    pass


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path
    env: dict[str, str] = field(default_factory=dict)

    # ---- helpers -------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node in (None, "") else node

    def h(self, key: str, default: Any = None) -> Any:
        # autonomy: full -> run end to end without waiting on the owner (overrides the individual switches)
        if key in AUTONOMY_FULL and str(self.get("harness.autonomy", "")).lower() == "full":
            return AUTONOMY_FULL[key]
        val = self.get(f"harness.{key}")
        return default if val is None else val

    # ---- frequently used ----------------------------------------------
    @property
    def workspace_dir(self) -> Path:
        return Path(self.h("workspace_dir", "./data/workspaces")).resolve()

    @property
    def db_path(self) -> Path:
        return Path(self.h("db_path", "./data/harness.db")).resolve()

    @property
    def gated_patterns(self) -> list[str]:
        return DEFAULT_GATED + list(self.h("extra_gated_patterns", []) or [])

    @property
    def secrets_path(self) -> Path:
        return self.db_path.parent / "secrets.env"

    @property
    def playbook_path(self) -> Path:
        return self.db_path.parent / "playbook.md"

    def refresh_env(self) -> None:
        """Re-read config.yaml extra_env and data/secrets.env so new keys apply without a restart."""
        try:
            fresh = yaml.safe_load(self.path.read_text()) or {}
            self.raw["extra_env"] = fresh.get("extra_env") or {}
        except Exception:
            pass
        self.env = _build_env(self)

    def save_secrets(self, pairs: dict[str, str]) -> None:
        current = _read_env_file(self.secrets_path)
        current.update(pairs)
        self.secrets_path.write_text("".join(f"{k}={v}\n" for k, v in current.items()))
        try:
            self.secrets_path.chmod(0o600)
        except OSError:
            pass
        self.env = _build_env(self)

    # Commit identity: Vercel (Hobby) only deploys commits authored by a member of the account.
    git_name: str = ""
    git_email: str = ""

    def set_git_identity(self, name: str, email: str) -> None:
        self.git_name, self.git_email = name, email
        self.env = _build_env(self)

    def owner_env_names(self) -> list[str]:
        """Names (never values) of product vars the owner supplied via extra_env, secrets.env or stripe/aws."""
        names = set((self.get("extra_env") or {}).keys()) | set(_read_env_file(self.secrets_path).keys())
        for k in ("STRIPE_SECRET_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"):
            if self.env.get(k):
                names.add(k)
        return sorted(n for n in names if self.env.get(n))

    def has_env(self, name: str) -> bool:
        return bool(self.env.get(name))

    def model_for(self, role: str) -> str:
        models = self.h("models", {}) or {}
        return models.get(role) or models.get("default") or "sonnet"


REQUIRED = [
    "discord.bot_token",
    "discord.channel_id",
    "discord.owner_user_id",
    "github.token",
    "github.owner",
    "vercel.token",
    "supabase.access_token",
    "supabase.org_id",
]


def load_config(path: str | os.PathLike = "config.yaml", require_discord: bool = True,
                strict: bool = True) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"{p} not found. Copy config.example.yaml to config.yaml and fill it in.")
    raw = yaml.safe_load(p.read_text()) or {}
    cfg = Config(raw=raw, path=p)

    missing = [k for k in REQUIRED if cfg.get(k) is None and (require_discord or not k.startswith("discord"))]
    if not (cfg.get("anthropic.api_key") or cfg.get("anthropic.oauth_token")):
        missing.append("anthropic.api_key (or anthropic.oauth_token)")
    if missing and strict:
        raise ConfigError("Missing required config values:\n  - " + "\n  - ".join(missing))

    cfg.workspace_dir.mkdir(parents=True, exist_ok=True)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.env = _build_env(cfg)
    return cfg


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def _build_env(cfg: "Config") -> dict[str, str]:
    env = {
        "GH_TOKEN": cfg.get("github.token"),
        "GITHUB_TOKEN": cfg.get("github.token"),
        "GITHUB_OWNER": cfg.get("github.owner"),
        "VERCEL_TOKEN": cfg.get("vercel.token"),
        "VERCEL_TEAM_ID": cfg.get("vercel.team_id", ""),
        "VERCEL_ORG_ID": cfg.get("vercel.team_id", ""),
        "SUPABASE_ACCESS_TOKEN": cfg.get("supabase.access_token"),
        "SUPABASE_ORG_ID": cfg.get("supabase.org_id"),
        "SUPABASE_REGION": cfg.get("supabase.region", "us-east-1"),
        "STRIPE_SECRET_KEY": cfg.get("stripe.secret_key", ""),
        "AWS_ACCESS_KEY_ID": cfg.get("aws.access_key_id", ""),
        "AWS_SECRET_ACCESS_KEY": cfg.get("aws.secret_access_key", ""),
        "AWS_DEFAULT_REGION": cfg.get("aws.region", ""),
        "CI": "1",  # non-interactive CLIs
    }
    if cfg.get("anthropic.api_key"):
        env["ANTHROPIC_API_KEY"] = cfg.get("anthropic.api_key")
    if cfg.get("anthropic.oauth_token"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = cfg.get("anthropic.oauth_token")
    for k, v in (cfg.get("extra_env", {}) or {}).items():
        env[str(k)] = str(v)
    env.update(_read_env_file(cfg.secrets_path))
    name = cfg.get("git.author_name") or cfg.git_name
    email = cfg.get("git.author_email") or cfg.git_email
    if name and email:
        env.update(GIT_AUTHOR_NAME=name, GIT_COMMITTER_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_EMAIL=email)
    return {k: str(v) for k, v in env.items() if v not in (None, "")}
