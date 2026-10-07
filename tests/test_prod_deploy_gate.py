import asyncio, sys, tempfile
from pathlib import Path
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import harness.orchestrator as O
from harness.tools import TaskCtx
from harness.config import Config, _build_env
from harness.db import DB
from harness.notify import Notifier
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
class N(Notifier):
    async def create_thread(s,a,b): return 7
    async def _send(s,t,x): pass
async def main():
    d=Path(tempfile.mkdtemp()); (d/"c.yaml").write_text("{}")
    cfg=Config(raw={"harness":{"workspace_dir":str(d/"ws"),"db_path":str(d/"db")}},path=d/"c.yaml"); cfg.env=_build_env(cfg); cfg.workspace_dir.mkdir()
    db=await DB(cfg.db_path).open(); o=O.Orchestrator(cfg,db,N())
    pid=await db.create_project("x","idea",10); await db.update_project(pid, thread_id=7, status="running")
    cmds = ['vercel deploy --prod --token "$VERCEL_TOKEN" --yes', 'npx vercel --prod', 'vercel promote https://x.vercel.app']
    for c in cmds:
        r = await o._permission_cb(TaskCtx(o,pid,"deploy-preview","devops",d))("Bash",{"command":c},None)
        assert isinstance(r, PermissionResultDeny), c
    r = await o._permission_cb(TaskCtx(o,pid,"deploy-preview","devops",d))("Bash",{"command":"vercel deploy --yes"},None)
    assert isinstance(r, PermissionResultAllow)
    r = await o._permission_cb(TaskCtx(o,pid,"release","devops",d))("Bash",{"command":cmds[0]},None)
    assert isinstance(r, PermissionResultAllow)
    # waiting-on-owner status text
    await db.add_task(pid,"setup","devops","s","d"); await db.update_task(pid,"setup",status="done")
    await db.add_task(pid,"release","devops","r","d"); await db.update_task(pid,"release",status="waiting_owner")
    st = await o.status_text(pid, compact=True); print(st)
    assert "waiting on your reply" in st and "left" not in st
    print("PROD GATE + WAITING OK"); await db.close()
asyncio.run(main())
