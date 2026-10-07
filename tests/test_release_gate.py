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
gate_asks = []
class N(Notifier):
    def __init__(s): super().__init__(); s.log=[]
    async def create_thread(s,a,b): return 7
    async def _send(s,t,x):
        s.log.append(x)
        for p in list(s.pending.values()):
            if p.future.done(): continue
            if "Ready for production" in p.question:
                gate_asks.append(p.question)
                ans = "make the signup button green" if len(gate_asks) == 1 else "ship"
                asyncio.get_running_loop().call_soon(s.deliver, t, ans)
ran = []
class R:
    async def run(self, role, prompt, cwd, server, cb, **kw):
        ctx, T = server["ctx"], server["tools"]; k = ctx.task_key
        if k == "plan":
            await T["submit_plan"].handler({"spec_markdown": "# spec", "name":"x","summary":"s","features":[],"tasks":[{"key":"setup","role":"devops","title":"s","description":"d"}]})
            return RunResult()
        if k == "change-request":
            await T["create_task"].handler({"role":"frontend","title":"Green signup button","description":"make it green"})
            await T["complete_task"].handler({"summary":"queued: green button"}); return RunResult()
        if k == "setup": await T["set_resource"].handler({"key":"preview_url","value":"https://x-preview.vercel.app"})
        ran.append(k); await T["complete_task"].handler({"summary": f"{k} ok"}); return RunResult()
async def main():
    d=Path(tempfile.mkdtemp()); (d/"c.yaml").write_text("{}")
    cfg=Config(raw={"harness":{"designer":False,"workspace_dir":str(d/"ws"),"db_path":str(d/"db"),"auto_approve_plan":True}},path=d/"c.yaml"); cfg.env=_build_env(cfg); cfg.workspace_dir.mkdir()
    db=await DB(cfg.db_path).open(); o=O.Orchestrator(cfg,db,N(),runner=R())
    pid=await o.new_project("gate demo idea"); await asyncio.wait_for(o.loops[pid], 30)
    print("order:", ran)
    assert "preview.vercel.app" in gate_asks[0] and "qa-1 ok" in gate_asks[0]
    assert len(gate_asks) == 2 and ran.index("green-signup-button") < ran.index("qa-2") < ran.index("release")
    assert (await db.project(pid))["status"] == "completed"
    print("RELEASE GATE OK"); await db.close()
asyncio.run(main())
