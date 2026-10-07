import faulthandler; faulthandler.dump_traceback_later(15, exit=True)
import asyncio, sys, tempfile, json
from pathlib import Path
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import harness.orchestrator as O
from harness.tools import make_tools
from harness.config import Config
from harness.db import DB
from harness.notify import Notifier
from harness.agents import RunResult

O.build_server = lambda ctx, read_only=False: {"ctx": ctx, "tools": {t.name: t for t in make_tools(ctx, read_only)}}

class TestNotifier(Notifier):
    def __init__(self, answer): super().__init__(); self.log=[]; self.answer=answer
    async def create_thread(self, t, i): return 1
    async def _send(self, tid, text):
        self.log.append(text)
        for p in list(self.pending.values()):
            if not p.future.done():
                a = self.answer(p.question)
                if a is not None: asyncio.get_running_loop().call_soon(self.deliver, tid, a)

attempts = {}
class FakeRunner:
    async def run(self, role, prompt, cwd, server, cb, read_only=False, max_turns=None, budget_usd=None, **kw):
        ctx, T = server["ctx"], server["tools"]
        call = lambda n, a: T[n].handler(a)
        k = ctx.task_key; attempts[(k, role)] = attempts.get((k, role), 0) + 1
        await call("get_context", {})
        if role == "researcher":
            assert read_only and "research_lookup" in T and "sk_" not in prompt
            return RunResult(text="ROOT CAUSE: x. FIX: y. SOURCES:\nhttps://docs.example.com/fix", cost=0.03)
        if role == "consultant":
            assert read_only and set(T) == {"get_context", "research_lookup"}
            return RunResult(text=f"advice from {ctx.role}: check env var", cost=0.05)
        if k == "plan":
            r = await call("submit_plan", {"spec_markdown": "# spec", "name": "Demo", "slug": "demo", "summary": "s", "features": ["a"],
                "tasks": [{"key": "setup", "role": "devops", "title": "Setup", "description": "d"},
                          {"key": "api", "role": "backend", "title": "API", "description": "d", "depends_on": ["setup"]},
                          {"key": "ui", "role": "frontend", "title": "UI", "description": "d", "depends_on": ["api"]},
                          {"key": "doomed", "role": "backend", "title": "Doomed", "description": "d", "depends_on": ["setup"]}]})
            assert "accepted" in r["content"][0]["text"], r
            return RunResult(cost=0.2)
        if k == "api":
            n = attempts[(k, role)]
            if n == 1: return RunResult(text="gave up", cost=0.1)          # no report -> L1
            if n == 2:
                await call("report_blocked", {"reason": "auth 401", "tried": "x"}); return RunResult(cost=0.1)  # -> L2
            assert "ADVICE FROM YOUR PEERS" in prompt and "WEB RESEARCH" in prompt and "docs.example.com" in prompt
            await call("complete_task", {"summary": "api done"}); return RunResult(cost=0.1)
        if k == "ui":
            if role != "debugger": return RunResult(error="boom", cost=0.1)
            await call("complete_task", {"summary": "ui fixed by debugger"}); return RunResult(cost=0.1)
        if k == "doomed":
            if role == "architect" and "REVISED" in prompt:
                await call("create_task", {"role": "backend", "title": "Doomed part A", "description": "x"})
                await call("complete_task", {"summary": "SPLIT"}); return RunResult(cost=0.1)
            return RunResult(error="always fails", cost=0.1)
        if k.startswith("qa"):
            if k == "qa-1": await call("create_task", {"role": "frontend", "title": "Fix button", "description": "bug"})
            await call("complete_task", {"summary": f"{k} ok"}); return RunResult(cost=0.1)
        if k == "release":
            await call("set_resource", {"key": "prod_url", "value": "https://demo.vercel.app"})
        await call("complete_task", {"summary": f"{k} done"}); return RunResult(cost=0.1)

async def main():
    d = Path(tempfile.mkdtemp())
    cfg = Config(raw={"harness": {"designer": False, "release_approval": False, "auto_approve_plan": True, "workspace_dir": str(d/"ws"), "db_path": str(d/"db")}}, path=d)
    cfg.workspace_dir.mkdir(parents=True)
    db = await DB(cfg.db_path).open()
    n = TestNotifier(lambda q: None)
    o = O.Orchestrator(cfg, db, n, runner=FakeRunner())
    pid = await o.new_project("demo idea")
    await asyncio.wait_for(o.loops[pid], 60)
    print(await o.status_text(pid)); print("----"); print(n.log[-1])
    p = await db.project(pid); assert p["status"] == "completed", p["status"]
    ts = {t["key"]: t for t in await db.tasks(pid)}
    assert ts["doomed"]["status"] == "skipped" and ts["doomed-part-a"]["status"] == "done"
    assert ts["ui"]["result"] == "ui fixed by debugger"
    assert "qa-2" in ts and "fix-button" in ts["release"]["depends_on"]
    lib = o.research_library().lookup("auth 401")
    assert any(h["fix"] == "api done" and h["error"] == "auth 401" and h["sources"] for h in lib), lib
    assert any("research" in l and "docs.example.com" in l for l in n.log), "no research announcement"
    print("ASSERTS OK, cost", p["cost_usd"])

    # owner escalation: a task that never succeeds; owner says skip
    attempts.clear()
    n2 = TestNotifier(lambda q: "skip" if "still blocked" in q else None)
    o2 = O.Orchestrator(cfg, db, n2, runner=FakeRunner())
    pid2 = await o2.new_project("demo two")

    # make 'ui' impossible even for debugger by patching
    orig = FakeRunner.run
    async def run2(self, role, prompt, cwd, server, cb, **kw):
        if server["ctx"].task_key == "ui": return RunResult(error="nope", cost=0.01)
        return await orig(self, role, prompt, cwd, server, cb, **kw)
    FakeRunner.run = run2
    await asyncio.wait_for(o2.loops[pid2], 60)
    ts = {t["key"]: t for t in await db.tasks(pid2)}
    print("ui:", ts["ui"]["status"], "| project:", (await db.project(pid2))["status"])
    assert ts["ui"]["status"] == "skipped"
    print("ESCALATION OK"); print(n2.log[-1][:300]); await db.close()
asyncio.run(main())
