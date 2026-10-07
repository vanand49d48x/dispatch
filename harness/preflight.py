"""Preflight: verify config.yaml and every account BEFORE the harness starts, so builds never stall
on a bad key or missing permission later. Each check returns ok / warn / fail with a concrete fix."""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field

import httpx

OK, WARN, FAIL = "ok", "warn", "fail"
ICON = {OK: "✔", WARN: "⚠", FAIL: "✘"}

DISCORD = "https://discord.com/api/v10"
# Discord permission bits
P_ADMIN, P_ADD_REACTIONS, P_VIEW, P_SEND = 1 << 3, 1 << 6, 1 << 10, 1 << 11
P_MANAGE_MSG, P_HISTORY, P_PUBLIC_THREADS, P_SEND_THREADS = 1 << 13, 1 << 16, 1 << 35, 1 << 38
NEEDED_PERMS = {
    "View Channel": P_VIEW, "Send Messages": P_SEND, "Read Message History": P_HISTORY,
    "Create Public Threads": P_PUBLIC_THREADS, "Send Messages in Threads": P_SEND_THREADS,
    "Add Reactions": P_ADD_REACTIONS, "Manage Messages": P_MANAGE_MSG,
}
INTENT_MESSAGE_CONTENT = (1 << 18) | (1 << 19)
PLACEHOLDER = re.compile(r"(your[-_ ]|xxx|\.\.\.|<|changeme|todo)", re.I)


@dataclass
class Check:
    area: str
    name: str
    status: str
    detail: str = ""
    fix: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, area, name, status, detail="", fix=""):
        self.checks.append(Check(area, name, status, detail, fix))

    @property
    def failed(self) -> bool:
        return any(c.status == FAIL for c in self.checks)

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    def text(self, color: bool = True) -> str:
        out, area = [], None
        for c in self.checks:
            if c.area != area:
                area = c.area
                out.append(f"\n{area}")
            line = f"  {ICON[c.status]} {c.name}" + (f" - {c.detail}" if c.detail else "")
            out.append(line)
            if c.fix and c.status != OK:
                out.append(f"      → {c.fix}")
        n_fail = sum(c.status == FAIL for c in self.checks)
        n_warn = len(self.warnings)
        if n_fail:
            out.append(f"\n✘ {n_fail} problem(s) must be fixed before starting. Edit config.yaml and rerun.")
        elif n_warn:
            out.append(f"\n✔ Ready to start ({n_warn} warning(s) - the harness will work, but read them).")
        else:
            out.append("\n✔ Everything checks out. Ready to start.")
        return "\n".join(out)


# ---------------------------------------------------------------- config shape (no network)
def check_config(cfg, r: Report) -> None:
    A = "Config file"
    fields = {
        "anthropic.api_key|anthropic.oauth_token": None,
        "discord.bot_token": None, "discord.channel_id": r"^\d{17,20}$", "discord.owner_user_id": r"^\d{17,20}$",
        "github.token": None, "github.owner": r"^[A-Za-z0-9-]{1,39}$",
        "vercel.token": None, "supabase.access_token": None, "supabase.org_id": r"^[A-Za-z0-9_-]{3,64}$",
    }
    for key, pattern in fields.items():
        alts = key.split("|")
        val = next((str(cfg.get(k)) for k in alts if cfg.get(k) is not None), None)
        label = " or ".join(alts)
        if val is None:
            r.add(A, label, FAIL, "missing", f"fill in `{alts[0]}` in config.yaml")
        elif PLACEHOLDER.search(val):
            r.add(A, label, FAIL, "still a placeholder", f"replace it with the real value")
        elif pattern and not re.match(pattern, val.strip()):
            hint = "should be a long number - copy it with Developer Mode on" if "discord" in key else "check the value"
            r.add(A, label, FAIL, f"unexpected format: {val[:12]}…", hint)
        else:
            r.add(A, label, OK)
    tok = str(cfg.get("discord.bot_token") or "")
    if tok and tok.count(".") < 2:
        r.add(A, "discord.bot_token format", FAIL, "doesn't look like a bot token",
              "copy it from Developer Portal → Bot → Reset Token (not the Client Secret / Public Key)")
    ak = str(cfg.get("anthropic.api_key") or "")
    if ak and not ak.startswith("sk-ant-"):
        r.add(A, "anthropic.api_key format", WARN, "doesn't start with sk-ant-", "make sure it's an Anthropic API key")
    sk = str(cfg.get("stripe.secret_key") or (cfg.get("extra_env") or {}).get("STRIPE_SECRET_KEY") or "")
    if sk.startswith(("sk_live_", "rk_live_")):
        r.add(A, "Stripe key", WARN, "LIVE key - agents could create real charges", "use an sk_test_ key while building")
    budget, task_budget = float(cfg.h("project_budget_usd", 50)), float(cfg.h("task_budget_usd", 8))
    if task_budget > budget:
        r.add(A, "budgets", WARN, "task_budget_usd is larger than project_budget_usd")


# ---------------------------------------------------------------- services
async def check_anthropic(cfg, c: httpx.AsyncClient, r: Report) -> None:
    A = "Anthropic"
    key = cfg.get("anthropic.api_key")
    if not key:
        r.add(A, "OAuth token", WARN, "can't be verified ahead of time",
              "if agents fail to start, regenerate with `claude setup-token`")
        return
    h = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    try:
        m = await c.get("https://api.anthropic.com/v1/models", headers=h)
    except httpx.HTTPError as e:
        r.add(A, "API reachable", FAIL, str(e), "check your internet connection")
        return
    if m.status_code == 401:
        r.add(A, "API key", FAIL, "rejected (401)", "create a new key at console.anthropic.com → API Keys")
        return
    r.add(A, "API key", OK if m.status_code < 300 else WARN, f"HTTP {m.status_code}")
    # 1-token call proves billing/credits work (costs a fraction of a cent).
    p = await c.post("https://api.anthropic.com/v1/messages", headers=h, json={
        "model": "claude-haiku-4-5-20251001", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]})
    body = p.text.lower()
    if p.status_code < 300:
        r.add(A, "credits / billing", OK)
    elif "credit" in body or "billing" in body:
        r.add(A, "credits / billing", FAIL, "no credit on the account",
              "console.anthropic.com → Billing → add credit (set a monthly limit too)")
    elif p.status_code == 429:
        r.add(A, "credits / billing", WARN, "rate limited right now", "fine to continue; retry the check later")
    else:
        r.add(A, "credits / billing", WARN, f"test call returned HTTP {p.status_code}: {p.text[:120]}")


async def check_discord(cfg, c: httpx.AsyncClient, r: Report) -> None:
    A = "Discord"
    h = {"Authorization": f"Bot {cfg.get('discord.bot_token')}"}
    cid, owner = str(cfg.get("discord.channel_id")), str(cfg.get("discord.owner_user_id"))
    me = await c.get(f"{DISCORD}/users/@me", headers=h)
    if me.status_code == 401:
        r.add(A, "bot token", FAIL, "rejected (401)", "Developer Portal → Bot → Reset Token, paste the new one")
        return
    if me.status_code >= 300:
        r.add(A, "bot token", FAIL, f"HTTP {me.status_code}")
        return
    bot = me.json()
    r.add(A, "bot token", OK, f"logged in as {bot.get('username')}")

    app = await c.get(f"{DISCORD}/oauth2/applications/@me", headers=h)
    app_id = bot["id"]
    if app.status_code < 300:
        app_id = app.json().get("id", app_id)
        if app.json().get("flags", 0) & INTENT_MESSAGE_CONTENT:
            r.add(A, "Message Content Intent", OK)
        else:
            r.add(A, "Message Content Intent", FAIL, "off - the bot can't read your replies",
                  "Developer Portal → Bot → Privileged Gateway Intents → Message Content Intent → Save")

    ch = await c.get(f"{DISCORD}/channels/{cid}", headers=h)
    if ch.status_code in (403, 404):
        r.add(A, "channel", FAIL, "bot can't see this channel",
              "invite the bot to your server (OAuth2 → URL Generator) and check channel_id")
        return
    chan = ch.json()
    if chan.get("type") not in (0, 5):
        r.add(A, "channel", FAIL, "not a text channel", "use a normal text channel's ID")
        return
    gid = chan["guild_id"]
    r.add(A, "channel", OK, f"#{chan.get('name')}")

    om = await c.get(f"{DISCORD}/guilds/{gid}/members/{owner}", headers=h)
    if om.status_code < 300:
        r.add(A, "owner user", OK, (om.json().get("user") or {}).get("username", ""))
    else:
        r.add(A, "owner user", FAIL, "that user isn't in this server",
              "right-click YOUR name → Copy User ID (not the bot's or server's ID)")

    perms = await _channel_permissions(c, h, gid, bot["id"], chan)
    if perms is None:
        r.add(A, "permissions", WARN, "couldn't compute")
    else:
        missing = [n for n, bit in NEEDED_PERMS.items() if not (perms & P_ADMIN or perms & bit)]
        if missing:
            sev = FAIL if any(m != "Manage Messages" for m in missing) else WARN
            r.add(A, "permissions", sev, "missing: " + ", ".join(missing),
                  "Server Settings → Roles → the bot's role (or the channel's permissions) → enable them")
        else:
            r.add(A, "permissions", OK)

    cmds = await c.get(f"{DISCORD}/applications/{app_id}/guilds/{gid}/commands", headers=h)
    if cmds.status_code < 300:
        r.add(A, "slash commands", OK)
    else:
        r.add(A, "slash commands", WARN, "applications.commands scope not granted (plain messages still work)",
              "re-invite with scopes `bot` + `applications.commands` to get /status, /next, /budget")


async def _channel_permissions(c, h, gid, bot_id, chan) -> int | None:
    g = await c.get(f"{DISCORD}/guilds/{gid}", headers=h)
    roles = await c.get(f"{DISCORD}/guilds/{gid}/roles", headers=h)
    mem = await c.get(f"{DISCORD}/guilds/{gid}/members/{bot_id}", headers=h)
    if any(x.status_code >= 300 for x in (g, roles, mem)):
        return None
    if g.json().get("owner_id") == bot_id:
        return P_ADMIN
    by_id = {x["id"]: int(x["permissions"]) for x in roles.json()}
    my_roles = mem.json().get("roles", [])
    perms = by_id.get(gid, 0)
    for rid in my_roles:
        perms |= by_id.get(rid, 0)
    if perms & P_ADMIN:
        return perms
    ow = {o["id"]: o for o in chan.get("permission_overwrites", [])}
    if gid in ow:
        perms = (perms & ~int(ow[gid]["deny"])) | int(ow[gid]["allow"])
    allow = deny = 0
    for rid in my_roles:
        if rid in ow:
            allow |= int(ow[rid]["allow"])
            deny |= int(ow[rid]["deny"])
    perms = (perms & ~deny) | allow
    if bot_id in ow:
        perms = (perms & ~int(ow[bot_id]["deny"])) | int(ow[bot_id]["allow"])
    return perms


async def check_github(cfg, c: httpx.AsyncClient, r: Report) -> None:
    A = "GitHub"
    h = {"Authorization": f"Bearer {cfg.get('github.token')}", "Accept": "application/vnd.github+json"}
    u = await c.get("https://api.github.com/user", headers=h)
    if u.status_code == 401:
        r.add(A, "token", FAIL, "rejected (401) - wrong or expired", "generate a new classic token")
        return
    if u.status_code >= 300:
        r.add(A, "token", FAIL, f"HTTP {u.status_code}")
        return
    login = u.json().get("login", "")
    r.add(A, "token", OK, f"user {login}")
    scopes_hdr = u.headers.get("x-oauth-scopes")
    if scopes_hdr is None:
        r.add(A, "scopes", WARN, "fine-grained token: scopes can't be verified",
              "it needs Administration + Contents + Workflows (read/write) on all repos; a classic token is simpler")
    else:
        scopes = {s.strip() for s in scopes_hdr.split(",") if s.strip()}
        for need, why in (("repo", "create private repos and push"), ("workflow", "push GitHub Actions files")):
            r.add(A, f"scope `{need}`", OK if need in scopes else FAIL, "" if need in scopes else f"needed to {why}",
                  "regenerate the token with this scope ticked")
        if not scopes & {"read:org", "admin:org", "write:org"}:
            r.add(A, "scope `read:org`", WARN, "gh CLI may print warnings", "tick read:org when regenerating")
        if "delete_repo" in scopes:
            r.add(A, "scope `delete_repo`", WARN, "granted - agents could delete repos if you approve it",
                  "consider a token without delete_repo")
    owner = str(cfg.get("github.owner"))
    if owner.lower() != login.lower():
        o = await c.get(f"https://api.github.com/orgs/{owner}", headers=h)
        if o.status_code >= 300:
            r.add(A, "owner", FAIL, f"'{owner}' isn't your username or an org you can see",
                  f"set github.owner to {login} (or the org name)")
        else:
            r.add(A, "owner", OK, f"org {owner}")
    else:
        r.add(A, "owner", OK)


async def check_vercel(cfg, c: httpx.AsyncClient, r: Report) -> None:
    A = "Vercel"
    h = {"Authorization": f"Bearer {cfg.get('vercel.token')}"}
    u = await c.get("https://api.vercel.com/v2/user", headers=h)
    if u.status_code >= 300:
        r.add(A, "token", FAIL, f"rejected (HTTP {u.status_code})", "create a new token at vercel.com/account/tokens")
        return
    r.add(A, "token", OK, (u.json().get("user") or {}).get("username", ""))
    team = cfg.get("vercel.team_id")
    if team:
        t = await c.get(f"https://api.vercel.com/v2/teams/{team}", headers=h)
        r.add(A, "team", OK if t.status_code < 300 else FAIL, "" if t.status_code < 300 else "team not found / no access",
              "use the team ID or slug from Team Settings, or leave team_id blank for a personal account")


async def check_supabase(cfg, c: httpx.AsyncClient, r: Report) -> None:
    A = "Supabase"
    h = {"Authorization": f"Bearer {cfg.get('supabase.access_token')}"}
    o = await c.get("https://api.supabase.com/v1/organizations", headers=h)
    if o.status_code >= 300:
        r.add(A, "access token", FAIL, f"rejected (HTTP {o.status_code})",
              "Account → Access Tokens → generate a new token")
        return
    r.add(A, "access token", OK)
    org = str(cfg.get("supabase.org_id"))
    orgs = o.json()
    match = next((x for x in orgs if org in (str(x.get("id")), str(x.get("slug")))), None)
    if not match:
        names = ", ".join(f"{x.get('name')} ({x.get('id')})" for x in orgs[:5])
        r.add(A, "organization", FAIL, "org_id not found for this token", f"use one of: {names}")
        return
    r.add(A, "organization", OK, match.get("name", ""))
    plan = None
    d = await c.get(f"https://api.supabase.com/v1/organizations/{match.get('id')}", headers=h)
    if d.status_code < 300:
        plan = (d.json().get("plan") or "").lower() or None
    p = await c.get("https://api.supabase.com/v1/projects", headers=h)
    if p.status_code < 300:
        active = [x for x in p.json() if str(x.get("organization_id")) == str(match.get("id"))
                  and str(x.get("status", "")).upper() not in ("INACTIVE", "PAUSED", "REMOVED", "GOING_DOWN")]
        detail = f"{len(active)} active project(s)" + (f", {plan} plan" if plan else "")
        if len(active) >= 2 and plan in (None, "free"):
            sev = FAIL if plan == "free" else WARN
            r.add(A, "room for a new project", sev, detail + " - the free plan allows 2",
                  "pause or delete an old project in the dashboard, or upgrade the org")
        else:
            r.add(A, "room for a new project", OK, detail)


# ---------------------------------------------------------------- local machine
def check_machine(cfg, r: Report) -> None:
    A = "This machine"
    for b, why in (("claude", "runs the agents"), ("git", "version control"), ("gh", "GitHub CLI"),
                   ("vercel", "deploys"), ("node", "builds apps"), ("npm", "installs packages")):
        found = shutil.which(b)
        r.add(A, b, OK if found else FAIL, "" if found else f"not found ({why})",
              "run inside the Docker image (docker compose run …) or install it")
    try:
        free_gb = shutil.disk_usage(cfg.workspace_dir).free / 1e9
        r.add(A, "disk space", OK if free_gb >= 10 else (WARN if free_gb >= 5 else FAIL), f"{free_gb:.0f} GB free",
              "free up disk space (each project uses ~1-2 GB with node_modules)")
    except OSError:
        pass
    try:
        with open("/proc/meminfo") as f:
            kb = int(next(l for l in f if l.startswith("MemTotal")).split()[1])
        gb = kb / 1e6
        r.add(A, "memory", OK if gb >= 6 else WARN, f"{gb:.0f} GB",
              "give Docker 8 GB+ (Docker Desktop → Settings → Resources) or set max_parallel_agents: 1")
    except (OSError, StopIteration):
        pass
    from . import capacity
    setting = str(cfg.h("max_parallel_agents", "auto")).lower()
    r.add(A, "parallel agents", OK, capacity.describe() if setting in ("auto", "", "0") else f"fixed at {setting}")
    if os.access(cfg.workspace_dir, os.W_OK):
        r.add(A, "workspace writable", OK)
    else:
        r.add(A, "workspace writable", FAIL, str(cfg.workspace_dir), "check folder permissions for ./data")


def check_git_identity(cfg, r: Report) -> None:
    A = "Commit author (Vercel)"
    if cfg.get("git.author_email"):
        r.add(A, "git.author_email", OK, str(cfg.get("git.author_email")))
    else:
        r.add(A, "git.author_email", WARN, "not set - commits will use your GitHub noreply address",
              "if Vercel blocks deploys ('not a member of the team'), set git.author_email to your Vercel login email")


def summarize_optional(cfg, r: Report) -> None:
    A = "Optional product keys"
    extra = {k: v for k, v in (cfg.get("extra_env") or {}).items() if v}
    if extra:
        r.add(A, "extra_env", OK, ", ".join(sorted(extra)))
    else:
        r.add(A, "extra_env", OK, "none - products will use mock providers until you add keys")


async def run_preflight(cfg, client: httpx.AsyncClient | None = None, check_local: bool = True) -> Report:
    r = Report()
    check_config(cfg, r)
    if r.failed:
        return r  # don't hit APIs with a broken config
    own = client is None
    c = client or httpx.AsyncClient(timeout=25)
    try:
        for fn in (check_anthropic, check_discord, check_github, check_vercel, check_supabase):
            try:
                await fn(cfg, c, r)
            except httpx.HTTPError as e:
                r.add(fn.__name__.replace("check_", "").title(), "reachable", FAIL, str(e)[:120],
                      "check your internet connection and retry")
    finally:
        if own:
            await c.aclose()
    if check_local:
        check_machine(cfg, r)
    check_git_identity(cfg, r)
    summarize_optional(cfg, r)
    return r
