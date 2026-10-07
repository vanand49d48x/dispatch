import asyncio, sys, tempfile
from pathlib import Path
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import harness.orchestrator as O
from harness.config import Config, _build_env
from harness.db import DB
from harness.notify import Notifier
class N(Notifier):
    async def create_thread(s,a,b): return 1
    async def _send(s,t,x): pass
async def main():
    d = Path(tempfile.mkdtemp()); (d/"c.yaml").write_text("{}")
    cfg = Config(raw={"harness": {"workspace_dir": str(d/"ws"), "db_path": str(d/"db")}}, path=d/"c.yaml"); cfg.env=_build_env(cfg)
    cfg.workspace_dir.mkdir()
    db = await DB(cfg.db_path).open()
    pid = await db.create_project("x", "idea", 10); await db.update_project(pid, status="running", thread_id=1)
    await db.add_task(pid, "setup", "devops", "s", "d"); await db.update_task(pid, "setup", status="running")
    await db.add_task(pid, "auth", "backend", "a", "d", ["setup"])
    await db.add_task(pid, "switch-to-real-providers-adm", "backend", "w", "d", []); await db.update_task(pid, "switch-to-real-providers-adm", level=3, status="running")
    o = O.Orchestrator(cfg, db, N()); o._start_loop = lambda pid: None
    await o.start()
    t = await db.task(pid, "switch-to-real-providers-adm"); print(t["status"], t["level"], t["depends_on"])
    assert t["depends_on"] == ["setup", "auth"] and t["level"] == 0 and t["status"] == "pending"
    print("REPAIR OK"); await db.close()
asyncio.run(main())
