import asyncio, sys, tempfile, subprocess
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
    d=Path(tempfile.mkdtemp()); (d/"c.yaml").write_text("{}")
    cfg=Config(raw={"github":{"owner":"octocat"},"git":{"author_email":"me@example.com"},
                    "harness":{"workspace_dir":str(d/"ws"),"db_path":str(d/"db")}},path=d/"c.yaml"); cfg.env=_build_env(cfg); cfg.workspace_dir.mkdir()
    o=O.Orchestrator(cfg, await DB(cfg.db_path).open(), N())
    old = cfg.workspace_dir/"old"; old.mkdir()
    subprocess.run(["git","init","-q"],cwd=old); subprocess.run(["git","config","user.email","agents@agentforge.local"],cwd=old)
    await o.ensure_git_identity(); ws = o._init_workspace("old")
    em = subprocess.run(["git","config","user.email"],cwd=ws,capture_output=True,text=True).stdout.strip()
    print("repo email:", em, "| env:", cfg.env.get("GIT_AUTHOR_EMAIL"), cfg.env.get("GIT_AUTHOR_NAME"))
    assert em == "me@example.com" and cfg.env["GIT_AUTHOR_EMAIL"] == "me@example.com"
    print("GIT IDENTITY OK")
asyncio.run(main())
