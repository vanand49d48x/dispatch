"""Project lifecycle, task scheduling, and the stuck-agent escalation ladder."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from . import capacity
from .research import Library, extract_urls, scrub
from .agents import AgentRunner, RunResult
from .db import DB, TERMINAL
from .notify import Notifier
from .tools import READ_ONLY_TOOL_NAMES, TOOL_NAMES, TaskCtx, build_server

log = logging.getLogger("forge.orch")

# Escalation ladder for a stuck task
L_SELF, L_RETRY, L_PEERS, L_DEBUGGER, L_REPLAN, L_OWNER = range(6)
LEVEL_NAMES = ["first attempt", "retry with new approach", "peer consult", "debugger takeover",
               "architect re-plan", "owner escalation"]
PEERS = {
    "frontend": ["backend", "designer"],
    "designer": ["frontend", "architect"],
    "backend": ["architect", "devops"],
    "devops": ["backend", "architect"],
    "qa": ["frontend", "backend"],
    "debugger": ["architect", "devops"],
}
STATUS_ICON = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌", "skipped": "⏭️",
               "waiting_owner": "🙋"}
GO_WORDS = ("go", "yes", "y", "approve", "approved", "ok", "okay", "ship it", "start", "lgtm", "next",
            "continue", "proceed")
STATUS_Q = re.compile(r"^(status|progress|update|eta|how far|how long|where are (we|you)|what('?s| is) "
                      r"(happening|going on|the status)|what are you (doing|working on)|are you done)\b[\s?!.]*", re.I)
SECRET_LINE = re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})\s*=\s*(\S.*?)\s*$", re.M)

TASK_RULES = """Task graph rules:
- Roles only: devops, backend, frontend, designer.
- If there is any UI: a designer task "design-system" (DESIGN.md + tokens + base components + one reference
  screen) right after setup; every frontend task depends on it. A design review before QA is added automatically. QA and the production release are added automatically - don't add them.
- Each task: a precise description with acceptance criteria an agent can verify alone (exact pages, routes,
  tables, env vars, commands that must pass). Put shared contracts (tables, routes) in data_model.
- Use depends_on so independent work runs in parallel. Last task: devops "deploy-preview" depending on all
  build tasks, which deploys a preview and records preview_url.
"""

SPEC_PLAN_PROMPT = """Plan the build of this product. The owner attached notes/a spec (below). Its product
decisions are binding: same features, stack, schema, routes, pricing and rules. Exceptions: the database is
always the team's Supabase project (as Postgres), and any process instructions in it (work loops, "never ask",
PROGRESS/NEEDS_HUMAN/DONE files, local-only commands) are ignored. Also write `spec_markdown`: a clean version
of the spec the team will follow (saved as SPEC.md).
{feedback}
- If the spec defines milestones/phases: put ALL their titles in `milestones` (ordered) and plan ONLY
  milestone 1 in `tasks` (5-14 tasks), deliverable end to end: built, deployed to preview, testable.
  Otherwise plan the whole product in one pass (5-14 tasks) and leave `milestones` empty.
- First task: devops "setup" - scaffold the app per the spec, GitHub repo, Supabase project (as the Postgres
  database; enable pgvector if the spec needs it), Vercel project, env vars, first preview deploy.
- required_env: every external API key the WHOLE product needs (payments, email, video, transcription, LLM,
  storage, OAuth), with where the owner gets it. Not keys the team provisions (Supabase, Vercel, GitHub) or
  generates (auth/session secrets).
{rules}
SPEC:
{spec}
"""

MILESTONE_PLAN_PROMPT = """Plan milestone {n}/{total}: "{title}" for the existing project.
Already shipped: {done}
The repo is your working directory and SPEC.md is the binding spec. Inspect both before planning and build on
what exists (no re-scaffolding).
{feedback}
- 3-14 tasks that deliver exactly this milestone's scope end to end. Set milestone_title. Leave milestones empty.
- required_env: only NEW external API keys this milestone needs.
{rules}"""

PLAN_PROMPT = """Plan this product for the team to build and deploy.

OWNER'S IDEA:
{idea}
{feedback}
Explore nothing yet (the repo is empty). The owner only gives ideas in chat - YOU write the spec.

1. Write `spec_markdown`: the product spec the whole team will build from (saved as SPEC.md in the repo).
   Cover: what it is and for whom, core user flows, pages/routes, data model (tables + key columns),
   integrations, pricing/billing if relevant, rules (security, data scoping), and the milestone list.
   Default stack unless the idea names one: Next.js (App Router, TS) + Tailwind, Supabase (Postgres, Auth, RLS,
   Storage, pgvector), Vercel. External services sit behind real/mock providers so missing keys never block.
2. Size it. If the idea fits in ~14 tasks, ship it in one pass (leave `milestones` empty). If it's bigger,
   split it into 2-10 ordered milestones, each a usable, deployable increment (milestone 1 = skeleton + auth +
   the smallest end-to-end slice of the core flow). Put all titles in `milestones` and plan ONLY milestone 1.
3. required_env: every external API key the WHOLE product needs, with where the owner gets it (not keys the
   team provisions - Supabase, Vercel, GitHub - or generates itself).
4. Task graph for this pass/milestone 1:
- 5 to 14 tasks. First task: devops "setup" - scaffold the app, GitHub repo, Supabase project, Vercel project,
  env vars, first preview deploy. Everything else depends on it directly or indirectly.
- Include auth and a polished landing page in the first pass/milestone.
{rules}
Then call `submit_plan`.
"""


def _error_of(entry: str) -> str:
    """'[peer consult · backend] blocked: <error> | tried: ...' -> '<error>'"""
    e = entry.split("]", 1)[-1].strip()
    e = re.sub(r"^(blocked|no-report|complete)\s*:\s*", "", e)
    return e.split(" | ", 1)[0].strip()


def slugify(text: str, n: int = 32) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:n].strip("-") or "app")


class Orchestrator:
    def __init__(self, cfg, db: DB, notifier: Notifier, runner: AgentRunner | None = None):
        self.cfg, self.db, self.notifier = cfg, db, notifier
        self.runner = runner or AgentRunner(cfg, TOOL_NAMES)
        self.loops: dict[int, asyncio.Task] = {}
        self.paused: set[int] = set()
        self.wake: dict[int, asyncio.Event] = {}
        self.running: dict[int, dict[str, asyncio.Task]] = {}
        self.research_notes: dict[tuple[int, str], str] = {}   # (pid, task) -> researcher findings
        # live view of what each agent is doing: (pid, task_key) -> {role, since, last, at, steps}
        self.activity: dict[tuple[int, str], dict] = {}
        self._bg: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ owner I/O
    async def _thread(self, pid: int) -> int:
        return (await self.db.project(pid))["thread_id"]

    async def say(self, pid: int, text: str) -> None:
        try:
            await self.notifier.say(await self._thread(pid), text)
        except Exception:
            log.exception("notify failed")

    async def ask_owner(self, pid: int, question: str, timeout_h: float | None = None) -> str | None:
        timeout = (timeout_h or float(self.cfg.h("owner_reply_timeout_hours", 12))) * 3600
        ans = await self.notifier.ask(await self._thread(pid), question, timeout)
        if ans:
            saved, ans = self._extract_secrets(ans)
            if saved:
                ans = (ans + f"\n(saved keys: {', '.join(saved)})").strip()
                self._spawn(self._on_new_keys(pid, saved))
            await self.db.post(pid, "owner", "owner", f"(answer) Q: {question[:300]} A: {ans}")
        return ans

    def _extract_secrets(self, text: str) -> tuple[list[str], str]:
        """Pull NAME=value lines out of an owner message, save them, return (names, remaining text)."""
        pairs = dict(SECRET_LINE.findall(text or ""))
        if not pairs:
            return [], text
        self.cfg.save_secrets(pairs)
        return list(pairs), SECRET_LINE.sub("", text).strip()

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    def _ev(self, pid: int) -> asyncio.Event:
        return self.wake.setdefault(pid, asyncio.Event())

    # ------------------------------------------------------------------ public API (bot commands)
    def refs_dir(self, pid: int) -> Path:
        return self.cfg.db_path.parent / "refs" / str(pid)

    async def add_design_refs(self, pid: int, files: list[tuple[str, bytes]]) -> list[str]:
        """Save owner-provided reference images (screenshots of apps/sites they like) for the designer."""
        d = self.refs_dir(pid)
        d.mkdir(parents=True, exist_ok=True)
        saved = []
        for name, data in files:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[-80:] or "ref.png"
            path = d / f"{int(time.time())}-{safe}"
            path.write_bytes(data)
            saved.append(str(path))
        if saved:
            await self.db.post(pid, "owner", "owner", "Design reference images from the owner (match the mood, "
                               "don't copy): " + ", ".join(saved))
            await self.say(pid, f"🎨 Saved {len(saved)} reference image(s) for the designer.")
        return saved

    def research_library(self) -> Library:
        return Library(self.cfg.db_path.parent / "research.jsonl", env=self.cfg.env)

    def parallel_limit(self) -> int:
        v = str(self.cfg.h("max_parallel_agents", "auto")).strip().lower()
        if v in ("", "auto", "0"):
            return capacity.auto_limit()
        try:
            return max(1, int(v))
        except ValueError:
            return capacity.auto_limit()

    async def ensure_git_identity(self) -> None:
        """Commit as the owner (config git.author_*, else the GitHub account's noreply address)."""
        if self.cfg.get("git.author_email"):
            self.cfg.set_git_identity(self.cfg.get("git.author_name") or self.cfg.get("github.owner"),
                                      self.cfg.get("git.author_email"))
            return
        if self.cfg.git_email:
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get("https://api.github.com/user",
                                headers={"Authorization": f"Bearer {self.cfg.get('github.token')}"})
            u = r.json() if r.status_code < 300 else {}
            login = u.get("login") or self.cfg.get("github.owner")
            email = u.get("email") or (f"{u['id']}+{login}@users.noreply.github.com" if u.get("id") else "")
            if login and email:
                self.cfg.set_git_identity(u.get("name") or login, email)
        except Exception:
            log.warning("could not resolve git identity from GitHub; set git.author_email in config.yaml")

    async def start(self) -> None:
        """Resume unfinished projects after a restart."""
        await self.ensure_git_identity()
        for p in await self.db.projects(("planning", "running")):
            tasks = await self.db.tasks(p["id"])
            for t in tasks:
                if t["status"] in ("running", "waiting_owner"):
                    await self.db.update_task(p["id"], t["key"], status="pending")
            # Repair key-wiring tasks created before v0.2.1 without dependencies: make them wait for the
            # build and restart their escalation from the beginning.
            build = [t["key"] for t in tasks if t["milestone"] == p["milestone"] and t["status"] not in TERMINAL
                     and t["role"] != "qa" and not t["key"].startswith(("release", "switch-to-real"))]
            for t in tasks:
                if t["key"].startswith("switch-to-real") and t["status"] not in TERMINAL and not t["depends_on"]:
                    await self.db.update_task(p["id"], t["key"], depends_on=build, level=0, status="pending")
            await self.say(p["id"], "♻️ Harness restarted - resuming this project.")
            self._start_loop(p["id"])

    async def new_project(self, idea: str, spec_doc: str = "", budget: float | None = None) -> int:
        await self.ensure_git_identity()
        slug = f"{slugify(idea, 24)}-{secrets.token_hex(2)}"
        pid = await self.db.create_project(slug, idea, float(budget or self.cfg.h("project_budget_usd", 50)),
                                           spec_doc=spec_doc or "")
        src = f"from your spec ({len(spec_doc):,} chars)" if spec_doc else "from your idea"
        thread_id = await self.notifier.create_thread(
            f"🚀 {idea[:80]}",
            f"New project **#{pid}**. Architect is drafting the plan {src}…\n"
            f"Reply in this thread any time - I'll route it to the team.")
        await self.db.update_project(pid, thread_id=thread_id)
        self._start_loop(pid)
        return pid

    async def pause(self, pid: int) -> None:
        self.paused.add(pid)
        await self.say(pid, "⏸️ Paused. Running agents finish their current step; nothing new starts. `/resume` to continue.")

    async def cancel(self, pid: int) -> None:
        """Stop a project for good: kill its loop and running agents, skip remaining tasks."""
        p = await self.db.project(pid)
        if not p:
            return
        for t in list(self.running.pop(pid, {}).values()):
            t.cancel()
        loop = self.loops.pop(pid, None)
        if loop and not loop.done():
            loop.cancel()
        for q in self.notifier.open_questions(p["thread_id"]):
            if not q.future.done():
                q.future.cancel()
        self.paused.discard(pid)
        for t in await self.db.tasks(pid):
            if t["status"] not in TERMINAL:
                await self.db.update_task(pid, t["key"], status="skipped", result="cancelled by owner")
        await self.db.update_project(pid, status="cancelled")
        await self.say(pid, "🛑 Project cancelled. No agents are running for it. Repos/Supabase/Vercel projects it "
                            "created (if any) are left in your accounts.")

    async def resume(self, pid: int) -> None:
        self.paused.discard(pid)
        self._ev(pid).set()
        p = await self.db.project(pid)
        if p["status"] == "cancelled":
            await self.say(pid, "This project was cancelled; start a new one instead.")
            return
        if p["status"] in ("stalled", "failed", "over_budget"):
            for t in await self.db.tasks(pid):
                if t["status"] == "failed":
                    await self.db.update_task(pid, t["key"], status="pending", level=L_RETRY)
            await self.db.update_project(pid, status="running")
        self._start_loop(pid)
        await self.say(pid, "▶️ Resumed.")

    async def set_budget(self, pid: int, usd: float) -> None:
        await self.db.update_project(pid, budget_usd=usd)
        p = await self.db.project(pid)
        if p["status"] == "over_budget":
            await self.db.update_project(pid, status="running")
            self._start_loop(pid)
        self._ev(pid).set()
        await self.say(pid, f"💰 Budget set to ${usd:.2f} (spent so far ${p['cost_usd']:.2f}).")

    def _progress(self, p: dict, tasks: list[dict]) -> dict:
        """Percent complete, elapsed and a rough ETA for the current milestone."""
        now = time.time()
        total = len(tasks) or 1
        done = [t for t in tasks if t["status"] in TERMINAL]
        running = [t for t in tasks if t["status"] in ("running", "waiting_owner")]
        partial = sum(min(0.9, self.activity.get((p["id"], t["key"]), {}).get("steps", 0) / 60) for t in running)
        pct = int(100 * (len(done) + partial) / total)
        durations = [t["finished_at"] - t["started_at"] for t in done
                     if t.get("started_at") and t.get("finished_at") and t["status"] == "done"]
        avg = (sum(durations) / len(durations)) if durations else 8 * 60   # assume ~8 min/task until measured
        remaining = (total - len(done)) - partial
        lanes = max(1, min(self.parallel_limit(), total - len(done)))
        eta = max(0.0, remaining * avg / lanes)
        starts = [t["started_at"] for t in tasks if t.get("started_at")]
        elapsed = now - (min(starts) if starts else p["created_at"])
        return {"pct": pct, "eta_min": eta / 60, "elapsed_min": elapsed / 60, "done": len(done), "total": total,
                "measured": bool(durations)}

    @staticmethod
    def _bar(pct: int, width: int = 12) -> str:
        filled = round(width * pct / 100)
        return "▰" * filled + "▱" * (width - filled)

    def _fmt_min(self, m: float) -> str:
        return f"{m:.0f} min" if m < 90 else f"{m / 60:.1f} h"

    async def status_text(self, pid: int, compact: bool = False) -> str:
        p = await self.db.project(pid)
        if not p:
            return "Project not found."
        tasks = [t for t in await self.db.tasks(pid) if t["milestone"] == p["milestone"]]
        lines = [f"**#{pid} {p['slug']}** - {p['status']}  ·  ${p['cost_usd']:.2f} / ${p['budget_usd']:.0f}"]
        if p["milestones"]:
            lines.append(f"Milestone {p['milestone']}/{len(p['milestones'])}: "
                         f"{p['milestones'][max(p['milestone'], 1) - 1]}")
        if tasks:
            pr = self._progress(p, tasks)
            waiting = any(t["status"] == "waiting_owner" for t in tasks) and not any(
                t["status"] == "running" for t in tasks)
            eta = ("done" if pr["pct"] >= 100 or p["status"] in ("completed", "milestone_done", "cancelled") else
                   "⏸ waiting on your reply" if waiting else
                   f"~{self._fmt_min(pr['eta_min'])} left" + ("" if pr["measured"] else " (rough until tasks finish)"))
            lines.append(f"{self._bar(pr['pct'])} **{pr['pct']}%** · {pr['done']}/{pr['total']} tasks · "
                         f"{self._fmt_min(pr['elapsed_min'])} elapsed · {eta}")
        now_lines = []
        for t in tasks:
            a = self.activity.get((pid, t["key"]))
            if t["status"] == "running" and a:
                mins = (time.time() - (t.get("started_at") or a["since"])) / 60
                who = a.get("role", t["role"])
                now_lines.append(f"🔄 `{t['key']}` [{who}] {mins:.0f}m - {a.get('last', '')}")
        if now_lines:
            lines.append("**Now:**")
            lines += now_lines
        if not compact:
            lines.append("**Tasks:**")
            for t in tasks:
                lvl = f" (⚠️ {LEVEL_NAMES[t['level']]})" if t["level"] and t["status"] not in TERMINAL else ""
                lines.append(f"{STATUS_ICON.get(t['status'], '•')} `{t['key']}` [{t['role']}] {t['title']}{lvl}")
            res = await self.db.resources(pid)
            for k in ("prod_url", "preview_url", "repo_url"):
                if res.get(k):
                    lines.append(f"{k}: {res[k]}")
        qs = self.notifier.open_questions(p["thread_id"])
        if qs:
            lines.append(f"🙋 Waiting on you: " + ", ".join(f"#{q.qid}" for q in qs))
        return "\n".join(lines)

    async def handle_owner_message(self, thread_id: int, text: str) -> bool:
        """Free-form owner message in a project thread (not an answer to a pending question)."""
        p = await self.db.project_by_thread(thread_id)
        if not p:
            return False
        pid = p["id"]
        saved, text = self._extract_secrets(text)
        if saved:
            await self.say(pid, f"🔒 Saved keys: {', '.join(f'`{k}`' for k in saved)}. They apply to the next "
                                "agent run (no restart needed).")
            self._spawn(self._on_new_keys(pid, saved))
            if not text:
                return True
        if STATUS_Q.search(text.strip()):
            await self.say(pid, await self.status_text(pid))
            return True
        if text.lower().startswith("lesson:"):
            self._add_lessons([text.split(":", 1)[1].strip()])
            await self.say(pid, "📚 Added to the team playbook.")
            return True
        if p["status"] == "milestone_done" and text.strip().lower().rstrip("!. ") in GO_WORDS:
            await self._start_next_milestone(pid)
            return True
        await self.db.post(pid, "owner", "owner", text)
        if p["status"] == "planning":
            await self.say(pid, "📝 Noted - the architect will include this in the plan.")
            return True
        await self.say(pid, "📨 Got it - routing to the architect to decide what changes.")
        self._spawn(self._change_request(pid, text))
        return True

    # ------------------------------------------------------------------ lifecycle
    def _start_loop(self, pid: int) -> None:
        if pid in self.loops and not self.loops[pid].done():
            self._ev(pid).set()
            return
        self.loops[pid] = self._spawn(self._lifecycle(pid))

    async def _lifecycle(self, pid: int) -> None:
        try:
            while True:
                p = await self.db.project(pid)
                if p["status"] == "planning":
                    if not await self._plan(pid):
                        return
                p = await self.db.project(pid)
                if p["status"] == "running":
                    await self._execute(pid)
                if (await self.db.project(pid))["status"] != "planning":  # planning = next milestone queued
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("lifecycle crashed")
            await self.db.update_project(pid, status="stalled")
            await self.say(pid, f"💥 Harness error: `{type(e).__name__}: {e}`. Fix/redeploy and `/resume`.")

    async def _plan(self, pid: int) -> bool:
        p = await self.db.project(pid)
        feedback, plan = "", None
        for round_ in range(5):
            owner_notes = [m["body"] for m in await self.db.board(pid, 15, kinds=("owner",))]
            fb = ""
            if owner_notes or feedback:
                fb = "\nOWNER FEEDBACK / NOTES (must be reflected):\n" + "\n".join(f"- {n}" for n in owner_notes)
            if plan:
                fb += f"\nPREVIOUS PLAN (revise it):\n{json.dumps(plan)[:6000]}\n"
            prompt, cwd = await self._plan_prompt(p, fb)
            ctx = TaskCtx(self, pid, "plan", "architect", cwd)
            res = await self._run(ctx, "architect", prompt,
                                  budget=float(self.cfg.h("plan_budget_usd", 4)), max_turns=40)
            if not ctx.plan:
                if round_ >= 1:
                    await self.db.update_project(pid, status="stalled")
                    await self.say(pid, f"❌ Planning failed: {res.error or 'no plan submitted'}. "
                                        "Rephrase the idea in this thread, then `/resume`.")
                    return False
                continue
            plan = ctx.plan
            await self.say(pid, self._plan_summary(plan))
            if self.cfg.h("auto_approve_plan", False):
                break
            ans = await self.ask_owner(pid, "Reply **go** to build and deploy this, or tell me what to change.",
                                       timeout_h=float(self.cfg.h("plan_approval_timeout_hours", 12)))
            if ans is None:
                if self.cfg.h("start_if_no_plan_reply", True):
                    await self.say(pid, "No reply - starting the build with this plan.")
                    break
                await self.db.update_project(pid, status="stalled")
                return False
            if ans.strip().lower().rstrip("!. ") in GO_WORDS:
                break
            feedback = ans  # owner answer is already on the board as kind=owner
        else:
            await self.say(pid, "Too many plan revisions; building the latest version.")

        await self._materialize_plan(pid, plan)
        await self.say(pid, "🏗️ Build started. I'll message you on milestones, blockers and when it's live.")
        await self._preflight_env(pid)
        return True

    async def _plan_prompt(self, p: dict, feedback: str) -> tuple[str, Path]:
        ms, n = p["milestones"], p["milestone"]
        if ms and n >= 1:
            done = "; ".join(f"{i + 1}. {t}" for i, t in enumerate(ms[:n - 1])) or "nothing yet"
            return (MILESTONE_PLAN_PROMPT.format(n=n, total=len(ms), title=ms[n - 1], done=done,
                                                 feedback=feedback, rules=TASK_RULES), await self._cwd(p["id"]))
        plan_dir = self.cfg.workspace_dir / ".planning" / str(p["id"])
        plan_dir.mkdir(parents=True, exist_ok=True)
        if p["spec_doc"]:
            return (SPEC_PLAN_PROMPT.format(feedback=feedback, rules=TASK_RULES,
                                            spec=p["spec_doc"][:60000]), plan_dir)
        return PLAN_PROMPT.format(idea=p["idea"], feedback=feedback, rules=TASK_RULES), plan_dir

    def _plan_summary(self, plan: dict) -> str:
        lines = [f"📋 **{plan['name']}** - {plan['summary']}"]
        if plan.get("milestones"):
            lines += ["", "**Milestones** (building one at a time)"]
            lines += [f"{i + 1}. {m}" for i, m in enumerate(plan["milestones"])]
        if plan.get("milestone_title"):
            lines += ["", f"**This plan: {plan['milestone_title']}**"]
        lines += ["", "**Features**"]
        lines += [f"• {f}" for f in plan.get("features", [])]
        lines += ["", f"**Tasks** ({len(plan['tasks'])} + QA + release)"]
        lines += [f"• `{t['key']}` [{t['role']}] {t['title']}" for t in plan["tasks"]]
        if plan.get("required_env"):
            lines += ["", "**API keys this needs** (mocks used until provided)"]
            lines += [f"• `{e['name']}` - {e.get('purpose', '')}" for e in plan["required_env"]]
        return "\n".join(lines)

    async def _materialize_plan(self, pid: int, plan: dict) -> None:
        p = await self.db.project(pid)
        first = not p["milestones"] and p["milestone"] == 0 and not await self.db.tasks(pid)
        if first:
            slug = slugify(plan.get("slug") or plan["name"])
            existing = {x["slug"] for x in await self.db.projects()}
            if slug in existing and slug != p["slug"]:
                slug = f"{slug}-{pid}"
            milestones = [m for m in plan.get("milestones") or [] if m.strip()]
            await self.db.update_project(pid, slug=slug, spec=plan, milestones=milestones,
                                         milestone=1 if milestones else 0)
            ws = self._init_workspace(slug)
            if not (ws / "SPEC.md").exists():
                doc = p["spec_doc"] or plan.get("spec_markdown") or ""
                if p["spec_doc"] and plan.get("spec_markdown"):
                    # Owner pasted notes/docs: keep them as background under the architect's binding spec.
                    doc = plan["spec_markdown"] + "\n\n---\n## Owner's original notes (background)\n\n" + p["spec_doc"]
                if doc:
                    (ws / "SPEC.md").write_text(doc)
                    subprocess.run(["git", "add", "SPEC.md"], cwd=ws, capture_output=True)
                    subprocess.run(["git", "commit", "-m", "Add product spec"], cwd=ws, capture_output=True)
        else:
            prev = p["spec"] or {}
            merged = {**prev, "milestone_title": plan.get("milestone_title", ""),
                      "data_model": (plan.get("data_model") or prev.get("data_model", ""))}
            await self.db.update_project(pid, spec=merged)
        p = await self.db.project(pid)
        req = {e["name"]: e for e in p["required_env"]}
        for e in plan.get("required_env") or []:
            req.setdefault(e["name"], e)
        await self.db.update_project(pid, required_env=list(req.values()), status="running")

        m = p["milestone"]
        prefix = f"m{m}-" if m else ""
        keys = []
        for t in plan["tasks"]:
            keys.append(await self.db.add_task(pid, prefix + t["key"], t["role"], t["title"], t["description"],
                                               [prefix + d for d in t.get("depends_on") or []], milestone=m))
        await self._add_verification(pid, keys)

    async def _add_verification(self, pid: int, after_keys: list[str], first: bool = False) -> None:
        """Append QA + production release after the given tasks."""
        p = await self.db.project(pid)
        rnd = p["qa_round"] + 1
        await self.db.update_project(pid, qa_round=rnd)
        m = p["milestone"]
        has_ui = any(t["role"] in ("frontend", "designer") and t["milestone"] == m for t in await self.db.tasks(pid))
        if has_ui and self.cfg.h("designer", True):
            dr = await self.db.add_task(pid, f"design-review-{rnd}", "designer", "Design review on the preview",
                                        "Design review task: screenshot every key page at 390px and 1440px, compare "
                                        "with DESIGN.md, fix visual/UX issues directly, commit screenshots to "
                                        "design/screens/.", after_keys, milestone=m)
            after_keys = after_keys + [dr]
        qa = await self.db.add_task(pid, f"qa-{rnd}", "qa", "End-to-end QA on the preview deployment",
                                    "Test the latest preview deployment end to end (resources.preview_url; if "
                                    "missing, ask devops). Cover everything shipped so far, not only this "
                                    "milestone. Flows that depend on a mocked provider count as passing if the "
                                    "mock path works. File a create_task for every defect.", after_keys,
                                    milestone=m)
        await self.db.add_task(pid, "release", "devops", "Production release + smoke test",
                               "Deploy to production (`vercel deploy --prod`), make sure env vars exist for "
                               "production, run migrations if pending, smoke test with curl and a quick "
                               "Playwright signup/login check, record prod_url and repo_url with set_resource.",
                               [qa], milestone=m)

    def _init_workspace(self, slug: str) -> Path:
        d = self.cfg.workspace_dir / slug
        d.mkdir(parents=True, exist_ok=True)
        gi = d / ".gitignore"
        if not gi.exists():
            gi.write_text("node_modules/\n.next/\n.vercel/\n.env*.local\n.env\ncoverage/\nplaywright-report/\n")
        if not (d / ".git").exists():
            subprocess.run(["git", "init", "-b", "main"], cwd=d, capture_output=True)
        # Always (re)apply the owner's identity, so existing repos are fixed on restart too.
        name = self.cfg.git_name or self.cfg.get("git.author_name") or self.cfg.get("github.owner") or "owner"
        email = self.cfg.git_email or self.cfg.get("git.author_email")
        if email:
            subprocess.run(["git", "config", "user.name", str(name)], cwd=d, capture_output=True)
            subprocess.run(["git", "config", "user.email", str(email)], cwd=d, capture_output=True)
        return d

    async def _cwd(self, pid: int) -> Path:
        return self._init_workspace((await self.db.project(pid))["slug"])

    async def add_task(self, pid: int, role: str, title: str, description: str, depends_on: list[str],
                       created_by: str = "") -> str:
        m = (await self.db.project(pid))["milestone"]
        key = await self.db.add_task(pid, slugify(title, 28), role, title,
                                     description + (f"\n\n(created by {created_by})" if created_by else ""),
                                     depends_on, milestone=m)
        # Anything new must land before the pending release / QA re-check.
        for t in await self.db.tasks(pid):
            if t["key"] != key and t["status"] == "pending" and (t["key"].startswith("release")):
                await self.db.update_task(pid, t["key"], depends_on=t["depends_on"] + [key])
        await self.db.post(pid, created_by or "harness", "note", f"New task `{key}` [{role}]: {title}")
        self._ev(pid).set()
        return key

    # ------------------------------------------------------------------ scheduler
    async def _execute(self, pid: int) -> None:
        limit = self.parallel_limit()
        running: dict[str, asyncio.Task] = self.running.setdefault(pid, {})
        warned_budget = False
        every = float(self.cfg.h("progress_update_minutes", 20)) * 60
        last_beat = time.time()
        while True:
            ev = self._ev(pid)
            ev.clear()
            p = await self.db.project(pid)
            tasks = await self.db.tasks(pid)
            by_key = {t["key"]: t for t in tasks}
            for k in [k for k, t in running.items() if t.done()]:
                running.pop(k)

            if p["cost_usd"] >= p["budget_usd"]:
                if not running:
                    await self.db.update_project(pid, status="over_budget")
                    await self.say(pid, f"💸 Budget reached (${p['cost_usd']:.2f}). Raise it with "
                                        f"`/budget <usd>` to continue.")
                    return
            elif p["cost_usd"] >= 0.8 * p["budget_usd"] and not warned_budget:
                warned_budget = True
                await self.say(pid, f"💸 80% of budget used (${p['cost_usd']:.2f}/${p['budget_usd']:.0f}).")

            can_dispatch = pid not in self.paused and p["cost_usd"] < p["budget_usd"]
            if can_dispatch:
                limit = self.parallel_limit()
                for t in tasks:
                    if len(running) >= limit or not capacity.can_start_another(len(running)):
                        break
                    if t["status"] != "pending" or t["key"] in running:
                        continue
                    deps = [by_key.get(d) for d in t["depends_on"]]
                    if all(d is None or d["status"] in ("done", "skipped") for d in deps):
                        await self.db.update_task(pid, t["key"], status="running")
                        running[t["key"]] = asyncio.create_task(self._run_task(pid, t["key"]))

            if not running:
                pending = [t for t in tasks if t["status"] == "pending"]
                if not pending:
                    break
                if pid in self.paused:
                    await ev.wait()
                    continue
                # Deadlock: pending tasks blocked by failed dependencies.
                failed = [t for t in tasks if t["status"] == "failed"]
                if not await self._resolve_deadlock(pid, failed):
                    await self.db.update_project(pid, status="stalled")
                    await self.say(pid, "🛑 Project stalled on failed tasks. Send instructions here and `/resume`.")
                    return
                continue

            busy = [t for t in tasks if t["status"] == "running"]
            if every > 0 and busy and time.time() - last_beat >= every:
                last_beat = time.time()
                await self.say(pid, "⏱️ Progress update\n" + await self.status_text(pid, compact=True))

            waiter = asyncio.create_task(ev.wait())
            await asyncio.wait([*running.values(), waiter], timeout=60, return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()

        await self._finish(pid)

    async def _resolve_deadlock(self, pid: int, failed: list[dict]) -> bool:
        if not failed:
            return False
        names = "\n".join(f"• `{t['key']}`: {t['result'][:300]}" for t in failed)
        ans = await self.ask_owner(pid, f"These tasks failed after every escalation step and block the rest:\n"
                                        f"{names}\nReply **retry** (optionally with guidance), **skip**, or **stop**.")
        if not ans or ans.lower().startswith("stop"):
            return False
        for t in failed:
            if ans.lower().startswith("skip"):
                await self.db.update_task(pid, t["key"], status="skipped")
            else:
                await self.db.post(pid, "owner", "owner", f"Guidance for {t['key']}: {ans}")
                await self.db.update_task(pid, t["key"], status="pending", level=L_RETRY)
        return True

    # ------------------------------------------------------------------ one task + escalation ladder
    async def _run_task(self, pid: int, key: str) -> None:
        try:
            await self._run_task_inner(pid, key)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("task crashed")
            await self.db.append_history(pid, key, f"harness error {e}")
            await self.db.update_task(pid, key, status="failed", result=f"harness error: {e}")
        finally:
            self.activity.pop((pid, key), None)
            self._ev(pid).set()

    async def _run_task_inner(self, pid: int, key: str) -> None:
        cwd = await self._cwd(pid)
        if key.startswith("release") and self.cfg.h("release_approval", True):
            if not await self._release_gate(pid, key):
                return
        while True:
            t = await self.db.task(pid, key)
            level = t["level"]
            agent_role, extra = t["role"], ""
            hist = "\n".join(f"- {h}" for h in t["history"][-6:])

            if level == L_RETRY:
                hits = self.research_library().lookup(t["history"][-1] if t["history"] else "")
                extra = (f"A previous attempt FAILED:\n{hist}\nTake a different approach. Use ask_peer if "
                         f"another role's work is involved. If the error isn't obvious, research it before "
                         f"retrying (research_lookup, then the web).")
                if hits:
                    extra += "\n\n" + self.research_library().format(hits)
            elif level == L_PEERS:
                peers = ', '.join(PEERS.get(t['role'], ['architect']))
                err = scrub((t["history"][-1] if t["history"] else ""), self.cfg.env)
                err = _error_of(err)[:160]
                await self.say(pid, f"🤝 `{key}` is stuck - consulting peers ({peers}) and 🔎 researching: `{err}`")
                advice, research = await asyncio.gather(self._peer_round(pid, t, cwd), self._research(pid, t, cwd))
                extra = (f"Earlier attempts FAILED:\n{hist}\n\nADVICE FROM YOUR PEERS (apply it):\n{advice}"
                         f"\n\nWEB RESEARCH (with sources - prefer this when it conflicts with memory):\n{research}")
            elif level == L_DEBUGGER:
                await self.say(pid, f"🐛 `{key}`: debugger agent taking over.")
                agent_role = "debugger"
                advice = "\n".join(m["body"] for m in await self.db.board(pid, 10, kinds=("advice",))
                                   if m["task_key"] == key)
                extra = (f"You are taking over a task originally assigned to {t['role']} that failed repeatedly.\n"
                         f"HISTORY:\n{hist}\nPEER ADVICE:\n{advice[:4000]}\n"
                         f"WEB RESEARCH:\n{self.research_notes.get((pid, key), '(none)')[:3000]}\n"
                         f"If the research didn't settle it, research further yourself before changing code.")
            elif level == L_REPLAN:
                await self.say(pid, f"📐 `{key}`: architect re-planning this part.")
                outcome = await self._replan_task(pid, t, cwd)
                if outcome == "split":
                    return
                t = await self.db.task(pid, key)
                extra = f"The architect REVISED this task after repeated failures:\n{hist}"
            elif level >= L_OWNER:
                await self.db.update_task(pid, key, status="waiting_owner")
                ans = await self.ask_owner(pid,
                    f"🧱 `{key}` ({t['title']}) is still blocked after peers, debugger and re-plan.\n"
                    f"Latest:\n{hist[-1500:]}\n\nReply with guidance (e.g. a credential, a decision, a different "
                    f"approach), **skip**, or **fail**.")
                if not ans or ans.lower().startswith("fail"):
                    await self.db.update_task(pid, key, status="failed", result=t["history"][-1] if t["history"] else "")
                    return
                if ans.lower().startswith("skip"):
                    await self.db.update_task(pid, key, status="skipped", result="skipped by owner")
                    return
                await self.db.update_task(pid, key, status="running", level=L_RETRY)
                await self.db.post(pid, "owner", "owner", f"Guidance for {key}: {ans}", key)
                continue

            ctx = TaskCtx(self, pid, key, agent_role, cwd)
            res = await self._run(ctx, agent_role, await self._task_prompt(pid, t, extra))

            if ctx.outcome == "complete":
                await self.db.update_task(pid, key, status="done", result=ctx.summary[:4000])
                if level > L_SELF:
                    await self.db.post(pid, agent_role, "resolved",
                                       f"{key} unblocked at '{LEVEL_NAMES[level]}': {ctx.summary[:500]}", key)
                    notes = self.research_notes.pop((pid, key), "")
                    urls = extract_urls(notes)[:3]
                    if level >= L_PEERS:
                        src = ("\nSources: " + " · ".join(f"<{u}>" for u in urls)) if urls else ""
                        await self.say(pid, f"✅ `{key}` unblocked via {LEVEL_NAMES[level]}"
                                            f"{' + research' if urls else ''}.{src}")
                    if t["history"]:
                        p_ = await self.db.project(pid)
                        self.research_library().record(_error_of(t["history"][-1]), ctx.summary, urls,
                                                       project=p_["slug"], context=f"{key} ({t['role']})")
                await self._after_complete(pid, t, ctx)
                return

            why = ctx.summary or res.error or "ended without complete_task/report_blocked"
            entry = (f"[{LEVEL_NAMES[level]} · {agent_role}] {ctx.outcome or 'no-report'}: {why[:800]}"
                     + (f" | tried: {ctx.tried[:500]}" if ctx.tried else "")
                     + (f" | last output: {res.text[-700:]}" if not ctx.summary else ""))
            await self.db.append_history(pid, key, entry)
            await self.db.post(pid, agent_role, "help", f"{key} failed: {why[:600]}", key)
            # A self-reported blocker means the agent already retried internally -> go straight to peers.
            nxt = max(level + 1, L_PEERS) if ctx.outcome == "blocked" and level < L_PEERS else level + 1
            await self.db.update_task(pid, key, level=min(nxt, L_OWNER))

    async def _task_prompt(self, pid: int, t: dict, extra: str) -> str:
        p = await self.db.project(pid)
        deps = []
        for d in t["depends_on"]:
            dt = await self.db.task(pid, d)
            if dt:
                deps.append(f"- {d} ({dt['status']}): {dt['result'][:600]}")
        owner = [m["body"] for m in await self.db.board(pid, 20, kinds=("owner",))]
        ms = ""
        if p["milestones"]:
            ms = (f"CURRENT MILESTONE {p['milestone']}/{len(p['milestones'])}: "
                  f"{p['milestones'][p['milestone'] - 1]} - build only this milestone's scope (see SPEC.md).")
        names = [e["name"] for e in p["required_env"]]
        owner_vars = self.cfg.owner_env_names()
        have = sorted(set(n for n in names if self.cfg.has_env(n)) | set(owner_vars))
        miss = [n for n in names if not self.cfg.has_env(n)]
        keys = (f"OWNER-PROVIDED ENV VARS already set (read them from the environment; never ask the owner for "
                f"these): {', '.join(have) or 'none'}")
        if names:
            keys += f"\nAPI KEYS missing (use mock providers + post NEEDS KEY notes): {', '.join(miss) or 'none'}"
        return "\n".join(filter(None, [
            f"PROJECT: {p['slug']}\nIDEA: {p['idea'][:500]}",
            ms, keys,
            f"SPEC SUMMARY: {(p.get('spec') or {}).get('summary', '')}",
            f"DATA MODEL / CONTRACTS: {(p.get('spec') or {}).get('data_model', '')[:3000]}",
            f"\nYOUR TASK `{t['key']}`: {t['title']}\n{t['description']}",
            ("\nCOMPLETED DEPENDENCIES:\n" + "\n".join(deps)) if deps else "",
            ("\nOWNER INSTRUCTIONS:\n" + "\n".join(f"- {o}" for o in owner[-10:])) if owner else "",
            f"\n{extra}" if extra else "",
            "\nStart by calling get_context. Finish with complete_task or report_blocked.",
        ]))

    async def _peer_round(self, pid: int, t: dict, cwd: Path) -> str:
        hist = "\n".join(t["history"][-4:])
        q = (f"Teammate ({t['role']}) is stuck on task `{t['key']}`: {t['title']}\n{t['description'][:1500]}\n"
             f"Failure history:\n{hist}\nWhat is the root cause and the exact fix?")
        peers = PEERS.get(t["role"], ["architect"])
        answers = await asyncio.gather(*[
            self.consult(pid, from_role="harness", to_role=r, question=q, task_key=t["key"], cwd=cwd)
            for r in peers])
        return "\n\n".join(f"[{r}]\n{a}" for r, a in zip(peers, answers))

    async def _research(self, pid: int, t: dict, cwd: Path) -> str:
        """A researcher agent looks the failure up on the web (scrubbed of secrets) and returns a cited fix."""
        if not self.cfg.h("web_research", True):
            return "(web research disabled)"
        hist = scrub("\n".join(t["history"][-3:]), self.cfg.env)
        ctx = TaskCtx(self, pid, t["key"], "researcher", cwd, consult_depth=1)
        prompt = (f"Task `{t['key']}` ({t['role']}): {t['title']}\n{scrub(t['description'][:1200], self.cfg.env)}\n\n"
                  f"It failed with:\n{hist}\n\nFind the authoritative fix.")
        res = await self._run(ctx, "researcher", prompt, read_only=True,
                              budget=float(self.cfg.h("research_budget_usd", 1.0)), max_turns=30)
        notes = res.text[-4000:] or f"(research produced no answer: {res.error})"
        self.research_notes[(pid, t["key"])] = notes
        urls = extract_urls(notes)
        await self.db.post(pid, "researcher", "advice", f"re {t['key']}: {notes[:2000]}", t["key"])
        if urls:
            await self.say(pid, f"🔎 Research for `{t['key']}` found {len(urls)} source(s), e.g. <{urls[0]}>")
        return notes

    async def consult(self, pid: int, from_role: str, to_role: str, question: str, task_key: str,
                      cwd: Path) -> str:
        """Agent-to-agent help: a read-only expert session in the same repo answers a question."""
        from .agents import ROLES
        await self.db.post(pid, from_role, "help", f"→ {to_role}: {question[:1000]}", task_key)
        ctx = TaskCtx(self, pid, task_key, to_role, cwd, consult_depth=1)
        prompt = (f"You are acting as the team's {to_role} specialist.\n{ROLES.get(to_role, '')}\n\n"
                  f"A teammate ({from_role}) asks:\n{question}\n\n"
                  "Use get_context and read the repo as needed, then answer directly.")
        res = await self._run(ctx, "consultant", prompt, read_only=True,
                              budget=float(self.cfg.h("consult_budget_usd", 1.5)), max_turns=25)
        answer = res.text[-3500:] or f"(no answer: {res.error})"
        await self.db.post(pid, to_role, "advice", f"re {task_key}: {answer[:2000]}", task_key)
        return answer

    async def _replan_task(self, pid: int, t: dict, cwd: Path) -> str:
        ctx = TaskCtx(self, pid, t["key"], "architect", cwd)
        prompt = (f"Task `{t['key']}` ({t['role']}) keeps failing despite retries, peer advice and a debugger.\n"
                  f"Description:\n{t['description']}\nHistory:\n" + "\n".join(t["history"][-6:]) +
                  "\n\nInspect the repo. Decide whether the task is badly specified, too large, or the design is "
                  "wrong. Then EITHER:\n(a) call complete_task with summary starting 'REVISED:' followed by the new "
                  "full task description (simpler/different approach), OR\n(b) split it: create_task for each "
                  "smaller task, then complete_task with summary 'SPLIT'.\nIf the design must change, post_note the "
                  "new contract first.")
        await self._run(ctx, "architect", prompt, budget=3, max_turns=30)
        if ctx.outcome == "complete" and ctx.summary.strip().upper().startswith("SPLIT") and ctx.created:
            for other in await self.db.tasks(pid):
                if t["key"] in other["depends_on"]:
                    await self.db.update_task(pid, other["key"], depends_on=[d for d in other["depends_on"]
                                                                              if d != t["key"]] + ctx.created)
            await self.db.update_task(pid, t["key"], status="skipped",
                                      result=f"replaced by {', '.join(ctx.created)}")
            return "split"
        if ctx.outcome == "complete" and ctx.summary.strip().upper().startswith("REVISED:"):
            await self.db.update_task(pid, t["key"], description=ctx.summary.split(":", 1)[1].strip())
        return "revised"

    async def _after_complete(self, pid: int, t: dict, ctx: TaskCtx) -> None:
        if t["role"] == "designer" and "design-review" not in t["key"]:
            await self.db.set_resource(pid, "design_system", "DESIGN.md")
        if t["role"] == "qa":
            p = await self.db.project(pid)
            if ctx.created:
                if p["qa_round"] < int(self.cfg.h("max_qa_rounds", 3)):
                    rnd = p["qa_round"] + 1
                    await self.db.update_project(pid, qa_round=rnd)
                    qa = await self.db.add_task(pid, f"qa-{rnd}", "qa", f"QA re-check round {rnd}",
                                                "Redeploy preview if needed (ask devops), then re-test the fixed "
                                                "defects and the core flows. File create_task for remaining defects.",
                                                ctx.created, milestone=p["milestone"])
                    for x in await self.db.tasks(pid):
                        if x["key"].startswith("release") and x["status"] == "pending":
                            await self.db.update_task(pid, x["key"], depends_on=x["depends_on"] + [qa])
                    await self.say(pid, f"🧪 QA found {len(ctx.created)} issue(s); fixes queued, then re-test.")
                else:
                    await self.say(pid, f"🧪 QA still finding issues after {p['qa_round']} rounds; fixing and "
                                        "releasing - remaining items will be listed in the report.")
            else:
                await self.say(pid, f"🧪 QA passed: {ctx.summary[:600]}")

    # ------------------------------------------------------------------ change requests & finish
    async def _change_request(self, pid: int, text: str, queue: bool = True) -> list[str]:
        cwd = await self._cwd(pid)
        ctx = TaskCtx(self, pid, "change-request", "architect", cwd)
        await self._run(ctx, "architect",
                        f"The owner sent this message about the project:\n\"{text}\"\n\n"
                        "Use get_context. If it's information or a question, answer briefly in complete_task. If it "
                        "requires work, create_task for each change (precise acceptance criteria, depends_on where "
                        "needed) then complete_task summarising what you queued.", budget=2, max_turns=25)
        if ctx.summary:
            await self.say(pid, f"🏛️ Architect: {ctx.summary[:1500]}")
        if ctx.created and queue:
            await self._queue_work(pid, ctx.created)
        return ctx.created

    async def _release_gate(self, pid: int, key: str) -> bool:
        """Ask the owner to approve the production release, showing the preview link and QA result.
        Returns True to proceed; False if the release was deferred behind requested changes."""
        p = await self.db.project(pid)
        res = await self.db.resources(pid)
        if res.get(f"approved:{key}"):
            return True
        tasks = [t for t in await self.db.tasks(pid) if t["milestone"] == p["milestone"]]
        qa = [t for t in tasks if t["role"] == "qa" and t["status"] == "done"]
        lines = [f"🚦 **Ready for production** - {p['slug']}"]
        if res.get("preview_url"):
            lines.append(f"**Preview:** {res['preview_url']}  (try it on your phone)")
        if qa:
            lines.append(f"**QA:** {qa[-1]['result'][:900]}")
        miss = [e["name"] for e in p["required_env"] if not self.cfg.has_env(e["name"])]
        if miss:
            lines.append(f"**Still on mocks:** {', '.join(miss)}")
        lines.append("\nReply **ship** to release to production, or describe what to change first.")
        try:
            shots = sorted((await self._cwd(pid)).glob("design/screens/*.png"), key=lambda f: f.stat().st_mtime)[-4:]
            if shots:
                await self.notifier.send_files(p["thread_id"], [str(x) for x in shots],
                                               "📱 Latest screens from the design review:")
        except Exception:
            log.exception("could not attach screenshots")
        await self.db.update_task(pid, key, status="waiting_owner")
        while True:
            ans = await self.ask_owner(pid, "\n".join(lines),
                                       timeout_h=float(self.cfg.h("release_approval_timeout_hours", 24)))
            if ans is None:
                if self.cfg.h("release_if_no_reply", False):
                    await self.say(pid, "No reply - releasing as configured (release_if_no_reply).")
                    break
                lines = [f"⏰ Reminder: {p['slug']} is still waiting for release approval."] + lines[1:]
                continue
            word = ans.strip().lower().rstrip("!. ")
            if word in GO_WORDS + ("ship", "ship it", "release", "deploy", "approve", "lgtm"):
                break
            if word in ("no", "wait", "hold", "not yet"):
                lines = ["OK, holding. Reply **ship** when ready, or describe changes."]
                continue
            await self.say(pid, "🛠️ Holding the release; sending your changes to the architect.")
            created = await self._change_request(pid, ans, queue=False)
            if not created:
                lines = ["Nothing to change was queued. Reply **ship**, or describe the change more specifically."]
                continue
            p = await self.db.project(pid)
            rnd = p["qa_round"] + 1
            await self.db.update_project(pid, qa_round=rnd)
            qa_key = await self.db.add_task(pid, f"qa-{rnd}", "qa", f"QA re-check round {rnd}",
                                            "Redeploy the preview with the requested changes (ask devops if needed), "
                                            "verify them and re-test the core flows. File create_task for defects.",
                                            created, milestone=p["milestone"])
            t = await self.db.task(pid, key)
            await self.db.update_task(pid, key, status="pending", depends_on=t["depends_on"] + created + [qa_key])
            return False
        await self.db.set_resource(pid, f"approved:{key}", "yes")
        await self.db.update_task(pid, key, status="running")
        await self.say(pid, "🚀 Approved - releasing to production.")
        return True

    async def _queue_work(self, pid: int, keys: list[str]) -> None:
        """New tasks arrived outside the plan: make sure QA + release follow, and the loop runs."""
        p = await self.db.project(pid)
        if p["status"] == "planning":
            return  # the plan being drafted adds its own QA + release; the loop is already running
        release_pending = any(t["key"].startswith("release") and t["status"] == "pending"
                              for t in await self.db.tasks(pid))
        if not release_pending:
            await self._add_verification(pid, keys)
        if p["status"] not in ("running", "planning"):
            await self.db.update_project(pid, status="running")
        self._start_loop(pid)

    async def _finish(self, pid: int) -> None:
        p = await self.db.project(pid)
        cur = p["milestone"]
        all_tasks = await self.db.tasks(pid)
        tasks = [t for t in all_tasks if t["milestone"] == cur]
        res = await self.db.resources(pid)
        released = any(t["key"].startswith("release") and t["status"] == "done" for t in tasks)
        total_ms = len(p["milestones"])
        more = bool(total_ms) and cur < total_ms
        status = ("milestone_done" if more else "completed") if released else "stalled"
        await self.db.update_project(pid, status=status)

        resolved = [m for m in await self.db.board(pid, 80, kinds=("resolved",))
                    if any(m["task_key"] == t["key"] for t in tasks)]
        failed = [t for t in tasks if t["status"] in ("failed", "skipped") and not t["result"].startswith("replaced")]
        ms_cost = sum(t["cost"] for t in tasks)
        mins = (time.time() - p["created_at"]) / 60
        title = f"Milestone {cur}/{total_ms}: {p['milestones'][cur - 1]}" if total_ms else p["slug"]
        head = "🎉 **LIVE** - " if released else "⚠️ **Finished with gaps** - "
        lines = [head + title]
        for k, label in (("prod_url", "App"), ("repo_url", "Repo"), ("supabase_url", "Supabase"),
                         ("admin_login", "Admin")):
            if res.get(k):
                lines.append(f"**{label}:** {res[k]}")
        lines.append(f"**Tasks:** {sum(t['status'] == 'done' for t in tasks)}/{len(tasks)} done · "
                     f"**Cost:** ${ms_cost:.2f} this round, ${p['cost_usd']:.2f} total · **Elapsed:** {mins:.0f} min")
        qa = [t for t in tasks if t["role"] == "qa" and t["status"] == "done"]
        if qa:
            lines.append(f"**QA:** {qa[-1]['result'][:700]}")
        if resolved:
            lines.append("**Blockers the team solved:**")
            lines += [f"• {m['body'][:250]}" for m in resolved[-8:]]
        if failed:
            lines.append("**Not done:**")
            lines += [f"• `{t['key']}` {t['status']}: {t['result'][:200]}" for t in failed]
        missing = [e["name"] for e in p["required_env"] if not self.cfg.has_env(e["name"])]
        if missing:
            lines.append(f"**Running on mocks until you add keys:** {', '.join(f'`{k}`' for k in missing)}")
        if more and released:
            nxt = p["milestones"][cur]
            if self.cfg.h("auto_continue_milestones", False):
                lines.append(f"\n➡️ Starting milestone {cur + 1}: {nxt}")
            else:
                lines.append(f"\nReply **next** to start milestone {cur + 1}: *{nxt}* - or send changes first.")
        else:
            lines.append("\nReply here with changes or new features any time - the team will pick them up.")
        await self.say(pid, "\n".join(lines))
        await self._retro(pid, cur)
        if more and released and self.cfg.h("auto_continue_milestones", False):
            await self._start_next_milestone(pid, start_loop=False)

    async def _start_next_milestone(self, pid: int, start_loop: bool = True) -> None:
        p = await self.db.project(pid)
        if not p["milestones"] or p["milestone"] >= len(p["milestones"]):
            await self.say(pid, "All milestones are done.")
            return
        n = p["milestone"] + 1
        await self.db.update_project(pid, milestone=n, status="planning")
        await self.say(pid, f"🧭 Planning milestone {n}/{len(p['milestones'])}: {p['milestones'][n - 1]}…")
        if start_loop:
            self._start_loop(pid)

    # ------------------------------------------------------------------ API keys
    async def _preflight_env(self, pid: int) -> None:
        self.cfg.refresh_env()
        p = await self.db.project(pid)
        missing = [e for e in p["required_env"] if not self.cfg.has_env(e["name"])]
        if missing:
            self._spawn(self._collect_keys(pid, missing))

    async def _collect_keys(self, pid: int, missing: list[dict]) -> None:
        for _ in range(3):
            lst = "\n".join(f"• `{e['name']}` - {e.get('purpose', '')}"
                            + (f" ({e['where']})" if e.get("where") else "") for e in missing)
            ans = await self.ask_owner(pid,
                f"🔑 These API keys aren't configured yet. The team is building with **mocks** meanwhile, so "
                f"nothing is blocked.\n{lst}\n\nEither add them under `extra_env:` in config.yaml and reply "
                f"**done**, or reply with `NAME=value` lines (I'll delete the message after saving). "
                f"Reply **skip** to stay on mocks for now.")
            if not ans or ans.lower().startswith("skip"):
                return
            self.cfg.refresh_env()
            missing = [e for e in missing if not self.cfg.has_env(e["name"])]
            if not missing:
                return
        # _on_new_keys queues the wiring work for whatever did arrive

    async def _on_new_keys(self, pid: int, names: list[str]) -> None:
        self.cfg.refresh_env()
        p = await self.db.project(pid)
        relevant = [n for n in names if any(e["name"] == n for e in p["required_env"])] or names
        tasks = await self.db.tasks(pid)
        if not tasks or p["status"] == "planning":
            return  # still planning; keys are simply available to the build
        # Wire real providers only after the build work in flight is done (repo, Vercel project and the
        # provider code must exist first). QA/release tasks then wait on this one via add_task().
        deps = [t["key"] for t in tasks if t["milestone"] == p["milestone"] and t["status"] not in TERMINAL
                and t["role"] != "qa" and not t["key"].startswith("release")]
        key = await self.add_task(
            pid, "backend", f"Switch to real providers: {', '.join(relevant)}"[:80],
            f"The owner added real credentials for: {', '.join(relevant)} (now in the environment). "
            "Add them to Vercel (production + preview) with `vercel env add`, make sure the provider factory picks "
            "the real implementation when the var is set, and verify the real integration works (test mode for "
            "payments). Keep the mock for local/e2e tests. Remove the matching NEEDS KEY notes from your report.",
            deps, created_by="owner")
        await self._queue_work(pid, [key])

    # ------------------------------------------------------------------ retro + playbook (self-hardening)
    async def _retro(self, pid: int, milestone: int) -> None:
        p = await self.db.project(pid)
        tasks = [t for t in await self.db.tasks(pid) if t["milestone"] == milestone]
        rdir = self.cfg.db_path.parent / "retros"
        rdir.mkdir(exist_ok=True)
        rows = ["| task | role | status | max level | cost |", "|---|---|---|---|---|"]
        rows += [f"| {t['key']} | {t['role']} | {t['status']} | {LEVEL_NAMES[t['level']]} | ${t['cost']:.2f} |"
                 for t in tasks]
        trouble = [t for t in tasks if t["level"] > 0 or t["status"] in ("failed", "skipped")]
        detail = "\n\n".join(f"### {t['key']} ({t['role']}, {t['status']})\n{t['description'][:600]}\n"
                              + "\n".join(f"- {h}" for h in t["history"]) + f"\nResult: {t['result'][:400]}"
                              for t in trouble)
        (rdir / f"{p['slug']}-m{milestone}.md").write_text(
            f"# Retro: {p['slug']} milestone {milestone}\n\n" + "\n".join(rows) + "\n\n" + detail + "\n")
        if not trouble or not self.cfg.h("playbook_learning", True):
            return
        ctx = TaskCtx(self, pid, "retro", "architect", await self._cwd(pid), consult_depth=1)
        res = await self._run(ctx, "consultant",
                              "These tasks got stuck during a build, with how they were (or weren't) resolved:\n\n"
                              f"{detail[:12000]}\n\nWrite 1-5 GENERAL lessons that would have prevented these "
                              "failures on any future project (tooling quirks, ordering, config, verification "
                              "steps) - not project trivia. One line each, starting with '- '. Output only the list.",
                              read_only=True, budget=0.6, max_turns=4)
        lessons = [l.strip()[2:].strip() for l in res.text.splitlines() if l.strip().startswith("- ")][:5]
        added = self._add_lessons(lessons)
        if added:
            await self.say(pid, f"📚 Retro: added {added} lesson(s) to the team playbook for future builds.")

    def _add_lessons(self, lessons: list[str]) -> int:
        path = self.cfg.playbook_path
        existing = path.read_text().splitlines() if path.exists() else []
        have = {l.strip().lower() for l in existing}
        new = [f"- {l}" for l in lessons if l and f"- {l}".lower() not in have]
        if new:
            lines = (existing + new)[-int(self.cfg.h("playbook_max_lessons", 60)):]
            path.write_text("\n".join(lines) + "\n")
        return len(new)

    # ------------------------------------------------------------------ running an agent
    async def _run(self, ctx: TaskCtx, role: str, prompt: str, read_only: bool = False,
                   budget: float | None = None, max_turns: int | None = None) -> RunResult:
        self.cfg.refresh_env()
        server = build_server(ctx, read_only=read_only)
        akey = (ctx.pid, ctx.task_key)
        act = self.activity.setdefault(akey, {"since": time.time(), "steps": 0})
        act.update(role=role, last="starting", at=time.time())

        def on_event(text: str) -> None:
            act.update(last=text, at=time.time(), steps=act.get("steps", 0) + 1)

        res = await self.runner.run(role, prompt, ctx.cwd, server, self._permission_cb(ctx),
                                    read_only=read_only, budget_usd=budget, max_turns=max_turns,
                                    on_event=on_event)
        total = await self.db.add_cost(ctx.pid, res.cost)
        await self.db.add_task_cost(ctx.pid, ctx.task_key, res.cost)
        log.info("run %s/%s outcome=%s cost=%.3f total=%.2f err=%s", ctx.task_key, role, ctx.outcome,
                 res.cost, total, res.error)
        return res

    def _permission_cb(self, ctx: TaskCtx):
        patterns = [re.compile(p, re.I) for p in self.cfg.gated_patterns]
        prod_gate = bool(self.cfg.h("require_approval_for_production_deploy", False))

        async def cb(tool_name: str, tool_input: dict[str, Any], _context: Any):
            if tool_name == "Bash":
                cmd = str(tool_input.get("command", ""))
                gated = any(p.search(cmd) for p in patterns)
                is_prod = bool(re.search(r"\bvercel\b[^\n]*(--prod\b|\bpromote\b)", cmd))
                if is_prod and self.cfg.h("release_approval", True) and not ctx.task_key.startswith("release"):
                    return PermissionResultDeny(message=(
                        "Production deploys happen only in the release task, after the owner approves the "
                        "release. Deploy a preview instead (`vercel deploy` without --prod) and record preview_url."))
                if prod_gate and is_prod:
                    res = await self.db.resources(ctx.pid)
                    gated = gated or not res.get("prod_approved")
                if gated:
                    ok = await self.notifier.ask_yes_no(
                        await self._thread(ctx.pid),
                        f"⚠️ **{ctx.role}** wants to run a gated command:\n```\n{cmd[:1500]}\n```",
                        float(self.cfg.h("owner_reply_timeout_hours", 12)) * 3600)
                    if not ok:
                        return PermissionResultDeny(message="Owner did not approve this command. Find a safer "
                                                    "alternative or report_blocked.")
                    if "--prod" in cmd:
                        await self.db.set_resource(ctx.pid, "prod_approved", "yes")
            return PermissionResultAllow(updated_input=tool_input)

        return cb
