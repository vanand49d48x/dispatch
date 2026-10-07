import faulthandler; faulthandler.dump_traceback_later(50, exit=True)
import asyncio, sys, tempfile
from pathlib import Path
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import harness.orchestrator as O
from harness.tools import make_tools
from harness.config import Config, _build_env
from harness.db import DB
from harness.notify import Notifier
from harness.agents import RunResult

O.build_server = lambda ctx, read_only=False: {"ctx": ctx, "tools": {t.name: t for t in make_tools(ctx, read_only)}}
key_asks = []
def answer(q):
    if "Reply **go**" in q: return "go"
    if "API keys aren't configured" in q:
        key_asks.append(q)
        return "STRIPE_SECRET_KEY=sk_test_123" if len(key_asks) == 1 else "skip"
    return None
class TN(Notifier):
    def __init__(s): super().__init__(); s.log=[]
    async def create_thread(s,a,b): return 1
    async def _send(s, tid, text):
        s.log.append(text)
        for p in list(s.pending.values()):
            if not p.future.done():
                a = answer(p.question)
                if a: asyncio.get_running_loop().call_soon(s.deliver, tid, a)
seen = {}
class Fake:
    async def run(self, role, prompt, cwd, server, cb, read_only=False, **kw):
        ctx, T = server["ctx"], server["tools"]; k = ctx.task_key
        seen[(k, role)] = seen.get((k, role), 0) + 1
        call = lambda n, a: T[n].handler(a)
        if role == "consultant":
            if "GENERAL lessons" in prompt: return RunResult(text="- Run db migrations before the first deploy\n- Verify env vars exist in Vercel preview", cost=0.02)
            return RunResult(text="peer advice", cost=0.02)
        if k == "plan":
            if "SPEC:" in prompt:
                assert "Teachloop" in prompt and (cwd / "SPEC.md").exists() is False
                r = await call("submit_plan", {"spec_markdown": "# spec", "name": "Teachloop", "slug": "teachloop", "summary": "s", "features": ["f"],
                    "milestones": ["Skeleton", "Sell"], "milestone_title": "Skeleton",
                    "required_env": [{"name": "STRIPE_SECRET_KEY", "purpose": "payments"}, {"name": "RESEND_API_KEY", "purpose": "email"}],
                    "tasks": [{"key": "setup", "role": "devops", "title": "Setup", "description": "d"},
                              {"key": "api", "role": "backend", "title": "API", "description": "d", "depends_on": ["setup"]}]})
            else:
                assert "Plan milestone 2/2" in prompt and (cwd / "SPEC.md").exists(), prompt[:200]
                r = await call("submit_plan", {"spec_markdown": "# spec", "name": "Teachloop", "summary": "s", "features": ["f"], "milestone_title": "Sell",
                    "tasks": [{"key": "checkout", "role": "backend", "title": "Checkout", "description": "d"}]})
            assert "accepted" in r["content"][0]["text"], r
            return RunResult(cost=0.1)
        if k == "m1-setup":
            await call("set_resource", {"key": "repo_url", "value": "https://github.com/x/teachloop"})
        if k == "m1-api" and seen[(k, role)] == 1:
            assert "API KEYS missing" in prompt and "CURRENT MILESTONE 1/2" in prompt
            return RunResult(error="tsc failed", cost=0.1)
        if k == "release":
            await call("set_resource", {"key": "prod_url", "value": "https://teachloop.vercel.app"})
        await call("complete_task", {"summary": f"{k} ok"}); return RunResult(cost=0.1)

async def main():
    d = Path(tempfile.mkdtemp()); (d/"config.yaml").write_text("extra_env: {}\n")
    cfg = Config(raw={"harness": {"designer": False, "release_approval": False, "workspace_dir": str(d/"ws"), "db_path": str(d/"data"/"db")}}, path=d/"config.yaml")
    cfg.workspace_dir.mkdir(parents=True); cfg.db_path.parent.mkdir(parents=True); cfg.env = _build_env(cfg)
    db = await DB(cfg.db_path).open(); n = TN()
    o = O.Orchestrator(cfg, db, n, runner=Fake())
    pid = await o.new_project("Teachloop", spec_doc="# Teachloop spec\n...milestones...")
    try:
        await asyncio.wait_for(asyncio.shield(o.loops[pid]), 10)
    except TimeoutError:
        print("\n=====LOG\n" + "\n---\n".join(x[:300] for x in n.log[-8:])); print(await o.status_text(pid), flush=True); import os; os._exit(1)
    await asyncio.sleep(0.5)
    while o.loops[pid] and not o.loops[pid].done(): await asyncio.sleep(0.2)
    p = await db.project(pid); print("after m1:", p["status"], "milestone", p["milestone"])
    ts = {t["key"]: t for t in await db.tasks(pid)}
    print(sorted(ts))
    assert p["status"] == "milestone_done"
    assert cfg.has_env("STRIPE_SECRET_KEY") and (cfg.db_path.parent/"secrets.env").read_text().startswith("STRIPE")
    wk = [k for k in ts if k.startswith("switch-to-real")]
    assert wk, "wiring task missing"
    print("wiring deps:", ts[wk[0]]["depends_on"])
    assert {"m1-setup", "m1-api"} & set(ts[wk[0]]["depends_on"]), "wiring task must wait for the build"
    assert (cfg.db_path.parent/"playbook.md").read_text().count("- ") == 2
    assert (cfg.db_path.parent/"retros"/"teachloop-m1.md").exists()
    board = [m["body"] for m in await db.board(pid, 100)]
    assert not any("sk_test_123" in b for b in board), "secret leaked to blackboard"
    print(n.log[[i for i,l in enumerate(n.log) if "LIVE" in l][0]])
    assert await o.handle_owner_message(1, "next")
    await asyncio.sleep(0.3)
    await asyncio.wait_for(o.loops[pid], 30)
    p = await db.project(pid); print("after m2:", p["status"], "milestone", p["milestone"])
    ts = {t["key"]: t for t in await db.tasks(pid)}
    assert p["status"] == "completed" and ts["m2-checkout"]["milestone"] == 2
    print(await o.status_text(pid))
    print("V2 OK"); await db.close()
asyncio.run(main())
