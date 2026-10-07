"""In-process MCP tools the agents use to coordinate with each other, the owner and infrastructure."""
from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

SUPABASE_API = "https://api.supabase.com/v1"
ROLES = ["architect", "backend", "frontend", "devops", "designer", "qa", "debugger"]


@dataclass
class TaskCtx:
    orch: "Orchestrator"
    pid: int
    task_key: str
    role: str
    cwd: Path
    consult_depth: int = 0
    outcome: str | None = None          # "complete" | "blocked"
    summary: str = ""
    tried: str = ""
    created: list[str] = field(default_factory=list)
    plan: dict | None = None


def _ok(text: str | dict | list) -> dict[str, Any]:
    if not isinstance(text, str):
        text = json.dumps(text, indent=2)
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"ERROR: {text}"}], "is_error": True}


TOOL_NAMES = [
    "get_context", "post_note", "set_resource", "ask_peer", "ask_owner", "report_progress",
    "create_task", "complete_task", "report_blocked", "supabase_create_project", "supabase_sql",
    "submit_plan", "research_lookup", "record_fix",
]
READ_ONLY_TOOL_NAMES = ["get_context", "research_lookup"]


def make_tools(ctx: TaskCtx, read_only: bool = False) -> list:
    o, db, pid = ctx.orch, ctx.orch.db, ctx.pid

    @tool("get_context", "Project spec, shared resources, recent blackboard notes, owner instructions, task list.", {})
    async def get_context(_: dict) -> dict:
        p = await db.project(pid)
        tasks = await db.tasks(pid)
        board = await db.board(pid, 40, kinds=("note", "owner", "advice", "help", "resolved"))
        return _ok({
            "project": p["slug"], "idea": p["idea"], "spec": p.get("spec"),
            "resources": await db.resources(pid),
            "owner_instructions": [m["body"] for m in board if m["kind"] == "owner"],
            "blackboard": [f'[{m["kind"]}] {m["sender"]}: {m["body"][:800]}' for m in board if m["kind"] != "owner"],
            "tasks": [{"key": t["key"], "role": t["role"], "title": t["title"], "status": t["status"]} for t in tasks],
            "you": {"role": ctx.role, "task": ctx.task_key},
        })

    @tool("post_note", "Share a contract/decision with the whole team (API routes, tables, env var names).",
          {"note": str})
    async def post_note(a: dict) -> dict:
        await db.post(pid, ctx.role, "note", a["note"], ctx.task_key)
        return _ok("posted")

    @tool("set_resource", "Record a shared non-secret resource (url, project ref, id).", {"key": str, "value": str})
    async def set_resource(a: dict) -> dict:
        await db.set_resource(pid, a["key"], a["value"])
        return _ok(f"saved {a['key']}")

    @tool("ask_peer", "Ask another agent role for help/advice. Returns their answer. "
          "Roles: architect, backend, frontend, devops, designer, qa, debugger.", {"role": str, "question": str})
    async def ask_peer(a: dict) -> dict:
        role = a["role"].strip().lower()
        if role not in ROLES:
            return _err(f"unknown role {role}; use one of {ROLES}")
        if ctx.consult_depth >= 1:
            return _err("consultants cannot open nested consultations; answer with what you know")
        answer = await o.consult(pid, from_role=ctx.role, to_role=role, question=a["question"],
                                 task_key=ctx.task_key, cwd=ctx.cwd)
        return _ok(answer)

    @tool("ask_owner", "Ask the human owner a question (only for business/human decisions). Waits for reply.",
          {"question": str})
    async def ask_owner(a: dict) -> dict:
        ans = await o.ask_owner(pid, f"**{ctx.role}** asks: {a['question']}")
        return _ok(ans or "No reply from owner. Make the most reasonable choice, note it with post_note, continue.")

    @tool("report_progress", "Send a short milestone update to the owner.", {"message": str})
    async def report_progress(a: dict) -> dict:
        await o.say(pid, f"🔧 **{ctx.role}** · `{ctx.task_key}`: {a['message']}")
        return _ok("sent")

    @tool("create_task", "Add a task to the team's queue (e.g. a bug found by QA, or a missing piece).",
          {"type": "object", "properties": {
              "role": {"type": "string", "enum": ["backend", "frontend", "devops", "designer", "debugger"]},
              "title": {"type": "string"}, "description": {"type": "string"},
              "depends_on": {"type": "array", "items": {"type": "string"}}},
           "required": ["role", "title", "description"]})
    async def create_task(a: dict) -> dict:
        key = await o.add_task(pid, a["role"], a["title"], a["description"], a.get("depends_on") or [],
                               created_by=ctx.role)
        ctx.created.append(key)
        return _ok(f"created task {key}")

    @tool("complete_task", "Finish your task. Only after verifying acceptance criteria.", {"summary": str})
    async def complete_task(a: dict) -> dict:
        ctx.outcome, ctx.summary = "complete", a["summary"]
        return _ok("Task marked complete. Stop now.")

    @tool("report_blocked", "Report that you cannot finish. The team will escalate and help.",
          {"reason": str, "tried": str})
    async def report_blocked(a: dict) -> dict:
        ctx.outcome, ctx.summary, ctx.tried = "blocked", a["reason"], a.get("tried", "")
        return _ok("Blocker recorded; the team will escalate. Stop now.")

    @tool("supabase_create_project", "Create a Supabase project (or reuse the project's existing one). "
          "Writes URL/keys/db password to .env.local and returns the url + anon key.", {"name": str})
    async def supabase_create_project(a: dict) -> dict:
        res = await db.resources(pid)
        if res.get("supabase_ref"):
            return _ok(f"Project already exists: ref={res['supabase_ref']} url={res.get('supabase_url')}. "
                       "Keys are in .env.local.")
        try:
            info = await create_supabase_project(o.cfg, a["name"], ctx.cwd)
        except Exception as e:
            return _err(f"Supabase create failed: {e}")
        await db.set_resource(pid, "supabase_ref", info["ref"])
        await db.set_resource(pid, "supabase_url", info["url"])
        await db.post(pid, ctx.role, "note", f"Supabase project ready: {info['url']} (ref {info['ref']}). "
                      "Env: NEXT_PUBLIC_SUPABASE_URL, NEXT_PUBLIC_SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY "
                      "in .env.local", ctx.task_key)
        return _ok({"ref": info["ref"], "url": info["url"], "anon_key": info["anon_key"],
                    "note": "service role key + DB password written to .env.local (gitignored)"})

    @tool("supabase_sql", "Run SQL (migrations, RLS policies, queries) on the project's Supabase database.",
          {"sql": str})
    async def supabase_sql(a: dict) -> dict:
        ref = (await db.resources(pid)).get("supabase_ref")
        if not ref:
            return _err("No Supabase project yet. Ask devops or call supabase_create_project.")
        try:
            out = await run_supabase_sql(o.cfg, ref, a["sql"])
        except Exception as e:
            return _err(str(e))
        return _ok(out[:8000])

    @tool("submit_plan", "Architect only: submit the project spec and task graph.",
          {"type": "object", "properties": {
              "name": {"type": "string", "description": "short product name"},
              "slug": {"type": "string", "description": "kebab-case repo name"},
              "summary": {"type": "string"},
              "features": {"type": "array", "items": {"type": "string"}},
              "data_model": {"type": "string"},
              "spec_markdown": {"type": "string", "description": "FIRST PLAN ONLY: the full product spec in "
                                "markdown (saved as SPEC.md for the whole team)"},
              "milestones": {"type": "array", "items": {"type": "string"},
                             "description": "FIRST PLAN ONLY: ordered milestone titles "
                                            "(empty if the product ships in one pass)"},
              "milestone_title": {"type": "string", "description": "title of the milestone this plan builds"},
              "required_env": {"type": "array", "description": "external API keys the product needs "
                               "(NOT ones the team provisions or generates itself)",
                               "items": {"type": "object", "properties": {
                                   "name": {"type": "string"}, "purpose": {"type": "string"},
                                   "where": {"type": "string", "description": "where the owner gets it"}},
                                   "required": ["name", "purpose"]}},
              "tasks": {"type": "array", "items": {"type": "object", "properties": {
                  "key": {"type": "string"}, "role": {"type": "string",
                                                      "enum": ["backend", "frontend", "devops", "designer"]},
                  "title": {"type": "string"}, "description": {"type": "string"},
                  "depends_on": {"type": "array", "items": {"type": "string"}}},
                  "required": ["key", "role", "title", "description"]}}},
           "required": ["name", "summary", "features", "tasks"]})
    async def submit_plan(a: dict) -> dict:
        keys = {t["key"] for t in a["tasks"]}
        bad = [d for t in a["tasks"] for d in t.get("depends_on", []) if d not in keys]
        if bad:
            return _err(f"depends_on references unknown keys: {bad}")
        has_infra = bool((await db.resources(pid)).get("repo_url"))
        if not has_infra and not any(t["role"] == "devops" for t in a["tasks"]):
            return _err("plan needs at least one devops task (repo + supabase + vercel setup/deploy)")
        if not has_infra and not (a.get("spec_markdown") or "").strip():
            return _err("first plan needs spec_markdown (the full product spec the team will build from)")
        res_ = await db.resources(pid)
        if (o.cfg.h("designer", True) and any(t["role"] == "frontend" for t in a["tasks"])
                and not res_.get("design_system")
                and not any(t["role"] == "designer" for t in a["tasks"])):
            return _err("plan has UI work but no designer task: add a designer 'design-system' task right after "
                        "setup, and make the frontend tasks depend on it")
        if len(a["tasks"]) > 20:
            return _err("too many tasks (max 20); merge small ones or move work to a later milestone")
        ctx.plan = a
        ctx.outcome, ctx.summary = "complete", "plan submitted"
        return _ok("Plan accepted. Stop now.")

    lib = o.research_library()

    @tool("research_lookup", "Search the team's library of problems solved on earlier projects (check this BEFORE "
          "searching the web). Pass the error message or a short description.", {"error": str})
    async def research_lookup(a: dict) -> dict:
        return _ok(lib.format(lib.lookup(a["error"])))

    @tool("record_fix", "After you SOLVE a non-obvious problem, save it so future agents don't have to research it "
          "again. error = the exact error/symptom, fix = what fixed it (commands/code), sources = URLs you used.",
          {"type": "object", "properties": {"error": {"type": "string"}, "fix": {"type": "string"},
                                            "sources": {"type": "array", "items": {"type": "string"}}},
           "required": ["error", "fix"]})
    async def record_fix(a: dict) -> dict:
        p = await db.project(pid)
        ok = lib.record(a["error"], a["fix"], a.get("sources") or [], project=p["slug"], context=ctx.task_key)
        if ok:
            await db.post(pid, ctx.role, "note", f"Recorded fix in research library: {a['error'][:200]}", ctx.task_key)
        return _ok("saved" if ok else "already in the library (or empty)")

    tools = [get_context, research_lookup, record_fix, post_note, set_resource, ask_peer, ask_owner, report_progress, create_task,
             complete_task, report_blocked, supabase_create_project, supabase_sql, submit_plan]
    return [get_context, research_lookup] if read_only else tools


def build_server(ctx: TaskCtx, read_only: bool = False):
    return create_sdk_mcp_server(name="forge", tools=make_tools(ctx, read_only))


# ---------------------------------------------------------------- Supabase Management API
def _sb_headers(cfg) -> dict:
    return {"Authorization": f"Bearer {cfg.get('supabase.access_token')}", "Content-Type": "application/json"}


async def create_supabase_project(cfg, name: str, cwd: Path) -> dict:
    db_pass = secrets.token_urlsafe(24)
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{SUPABASE_API}/projects", headers=_sb_headers(cfg), json={
            "name": name[:60], "organization_id": cfg.get("supabase.org_id"),
            "region": cfg.get("supabase.region", "us-east-1"), "db_pass": db_pass})
        if r.status_code >= 300:
            raise RuntimeError(f"{r.status_code} {r.text[:500]}")
        ref = r.json().get("id") or r.json().get("ref")
        for _ in range(60):  # wait up to ~10 min for provisioning
            s = await c.get(f"{SUPABASE_API}/projects/{ref}", headers=_sb_headers(cfg))
            if s.status_code < 300 and s.json().get("status") == "ACTIVE_HEALTHY":
                break
            await asyncio.sleep(10)
        k = await c.get(f"{SUPABASE_API}/projects/{ref}/api-keys?reveal=true", headers=_sb_headers(cfg))
        keys = {x.get("name"): x.get("api_key") for x in (k.json() if k.status_code < 300 else [])}
        pooled, direct = await _pooler_urls(c, cfg, ref, db_pass)
    url = f"https://{ref}.supabase.co"
    env_file = cwd / ".env.local"
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    lines = [l for l in lines if not l.startswith(("NEXT_PUBLIC_SUPABASE_", "SUPABASE_", "DATABASE_URL=",
                                                    "DIRECT_URL="))]
    lines += [f"DATABASE_URL={pooled}", f"DIRECT_URL={direct}"]
    lines += [f"NEXT_PUBLIC_SUPABASE_URL={url}",
              f"NEXT_PUBLIC_SUPABASE_ANON_KEY={keys.get('anon', '')}",
              f"SUPABASE_SERVICE_ROLE_KEY={keys.get('service_role', '')}",
              f"SUPABASE_DB_PASSWORD={db_pass}",
              f"SUPABASE_PROJECT_REF={ref}"]
    env_file.write_text("\n".join(lines) + "\n")
    return {"ref": ref, "url": url, "anon_key": keys.get("anon", "")}


async def _pooler_urls(c: httpx.AsyncClient, cfg, ref: str, db_pass: str) -> tuple[str, str]:
    """(pooled transaction-mode URL for serverless, session-mode URL for migrations)."""
    region = cfg.get("supabase.region", "us-east-1")
    pooled = f"postgresql://postgres.{ref}:{db_pass}@aws-0-{region}.pooler.supabase.com:6543/postgres"
    try:
        r = await c.get(f"{SUPABASE_API}/projects/{ref}/config/database/pooler", headers=_sb_headers(cfg))
        if r.status_code < 300:
            items = r.json() if isinstance(r.json(), list) else [r.json()]
            for it in items:
                cs = it.get("connection_string") or it.get("connectionString") or ""
                if cs and ("6543" in cs or it.get("pool_mode") == "transaction"):
                    pooled = cs.replace("[YOUR-PASSWORD]", db_pass)
                    break
    except Exception:
        pass
    direct = pooled.replace(":6543/", ":5432/")
    return pooled, direct


async def run_supabase_sql(cfg, ref: str, sql: str) -> str:
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{SUPABASE_API}/projects/{ref}/database/query", headers=_sb_headers(cfg),
                         json={"query": sql})
    if r.status_code >= 300:
        raise RuntimeError(f"SQL failed ({r.status_code}): {r.text[:2000]}")
    return r.text
