import asyncio, sys, tempfile
from pathlib import Path
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import harness.orchestrator as O, harness.capacity as C
from harness.tools import make_tools
from harness.config import Config, _build_env
from harness.db import DB
from harness.notify import Notifier
from harness.agents import RunResult
O.build_server = lambda ctx, read_only=False: {"ctx": ctx, "tools": {t.name: t for t in make_tools(ctx, read_only)}}
class N(Notifier):
    async def create_thread(s,a,b): return 7
    async def _send(s,t,x): pass
live=0; peak=0
class R:
    async def run(self, role, prompt, cwd, server, cb, **kw):
        global live, peak
        ctx, T = server["ctx"], server["tools"]
        if ctx.task_key=="plan":
            await T["submit_plan"].handler({"name":"x","summary":"s","features":[],"spec_markdown":"# x",
              "tasks":[{"key":"setup","role":"devops","title":"s","description":"d"}]+[{"key":f"t{i}","role":"backend","title":f"t{i}","description":"d","depends_on":["setup"]} for i in range(10)]})
            return RunResult()
        live+=1; peak=max(peak,live); await asyncio.sleep(0.3); live-=1
        await T["complete_task"].handler({"summary":"ok"}); return RunResult()
async def run(limit_fn, free_gb):
    global peak; peak=0
    C.auto_limit = limit_fn; C.available_memory = lambda: int(free_gb*2**30)
    d=Path(tempfile.mkdtemp()); (d/"c.yaml").write_text("{}")
    cfg=Config(raw={"harness":{"designer":False,"workspace_dir":str(d/"ws"),"db_path":str(d/"db"),"autonomy":"full"}},path=d/"c.yaml"); cfg.env=_build_env(cfg); cfg.workspace_dir.mkdir()
    db=await DB(cfg.db_path).open(); o=O.Orchestrator(cfg,db,N(),runner=R())
    pid=await o.new_project("capacity test idea here"); await asyncio.wait_for(o.loops[pid], 60); await db.close()
    return peak
async def main():
    a = await run(lambda: 6, 20); b = await run(lambda: 6, 1)
    print("big machine peak:", a, "| low free memory peak:", b)
    assert a == 6 and b == 1
    print("AUTO CAPACITY OK")
asyncio.run(main())
