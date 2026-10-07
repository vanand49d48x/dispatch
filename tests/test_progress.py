import asyncio, sys, tempfile
from pathlib import Path
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import harness.orchestrator as O
from harness.tools import make_tools
from harness.config import Config, _build_env
from harness.db import DB
from harness.notify import Notifier
from harness.agents import RunResult, describe_tool
O.build_server = lambda ctx, read_only=False: {"ctx": ctx, "tools": {t.name: t for t in make_tools(ctx, read_only)}}
class N(Notifier):
    def __init__(s): super().__init__(); s.log=[]
    async def create_thread(s,a,b): return 7
    async def _send(s,t,x): s.log.append(x)
class R:
    async def run(self, role, prompt, cwd, server, cb, on_event=None, **kw):
        ctx, T = server["ctx"], server["tools"]
        if ctx.task_key == "plan":
            await T["submit_plan"].handler({"spec_markdown": "# spec", "name":"x","summary":"s","features":[],"tasks":[
                {"key":"setup","role":"devops","title":"Setup","description":"d"},
                {"key":"auth","role":"backend","title":"Auth","description":"d","depends_on":["setup"]},
                {"key":"ui","role":"frontend","title":"UI","description":"d","depends_on":["setup"]}]})
            return RunResult()
        delay = 0.3 if ctx.task_key == "setup" else 1.5
        on_event(describe_tool("Bash", {"command": "npm run build"}))
        await asyncio.sleep(delay)
        on_event(describe_tool("Edit", {"file_path": "/repo/app/page.tsx"}))
        await T["complete_task"].handler({"summary": "ok"}); return RunResult(cost=0.1)
async def main():
    d=Path(tempfile.mkdtemp()); (d/"c.yaml").write_text("{}")
    cfg=Config(raw={"harness":{"designer":False,"release_approval":False,"workspace_dir":str(d/"ws"),"db_path":str(d/"db"),"auto_approve_plan":True}},path=d/"c.yaml"); cfg.env=_build_env(cfg); cfg.workspace_dir.mkdir()
    db=await DB(cfg.db_path).open(); n=N(); o=O.Orchestrator(cfg,db,n,runner=R())
    pid=await o.new_project("progress demo idea")
    await asyncio.sleep(1.0)
    mid = await o.status_text(pid); print(mid, "\n----")
    assert "%" in mid and "Now:" in mid and "npm run build" in mid
    assert await o.handle_owner_message(7, "status?")      # natural-language status, no architect call
    assert "%" in n.log[-1]
    await asyncio.wait_for(o.loops[pid], 30)
    print(await o.status_text(pid, compact=True)); print("PROGRESS OK"); await db.close()
asyncio.run(main())
