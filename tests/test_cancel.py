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
class N(Notifier):
    def __init__(s): super().__init__(); s.log=[]
    async def create_thread(s,a,b): return 7
    async def _send(s,t,x):
        s.log.append(x)
        if "Reply **go**" in x:
            for p in list(s.pending.values()): asyncio.get_running_loop().call_soon(s.deliver, t, "go")
class Slow:
    async def run(self, role, prompt, cwd, server, cb, **kw):
        ctx, T = server["ctx"], server["tools"]
        if ctx.task_key == "plan":
            await T["submit_plan"].handler({"spec_markdown": "# spec", "name":"x","summary":"s","features":[],"tasks":[
                {"key":"setup","role":"devops","title":"s","description":"d"},
                {"key":"ui","role":"frontend","title":"u","description":"d","depends_on":["setup"]}]})
            return RunResult()
        await asyncio.sleep(3600)   # agent "working"
async def main():
    d=Path(tempfile.mkdtemp()); (d/"c.yaml").write_text("{}")
    cfg=Config(raw={"harness":{"designer":False,"release_approval":False,"workspace_dir":str(d/"ws"),"db_path":str(d/"db")}},path=d/"c.yaml"); cfg.env=_build_env(cfg); cfg.workspace_dir.mkdir()
    db=await DB(cfg.db_path).open(); n=N(); o=O.Orchestrator(cfg,db,n,runner=Slow())
    pid=await o.new_project("demo idea for cancel")
    await asyncio.sleep(1.0)
    print("before:", [(t["key"],t["status"]) for t in await db.tasks(pid)])
    loop = o.loops[pid]
    await o.cancel(pid); await asyncio.sleep(0.2)
    ts=await db.tasks(pid); p=await db.project(pid)
    print("after:", p["status"], [(t["key"],t["status"]) for t in ts], "loop done:", loop.done())
    assert p["status"]=="cancelled" and loop.done() and all(t["status"]=="skipped" for t in ts)
    await o.start()  # restart must not resume cancelled projects
    assert pid not in o.loops
    print("CANCEL OK"); await db.close()
asyncio.run(main())
