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
    async def create_thread(s,a,b): return 7
    async def _send(s,t,x): pass
prompts = []
class R:
    async def run(self, role, prompt, cwd, server, cb, **kw):
        T = server["tools"]
        if server["ctx"].task_key == "plan":
            await T["submit_plan"].handler({"spec_markdown": "# spec", "name":"x","summary":"s","features":[],"tasks":[{"key":"setup","role":"devops","title":"s","description":"d"}]})
        else:
            prompts.append(prompt); await T["complete_task"].handler({"summary":"ok"})
        return RunResult()
async def main():
    d=Path(tempfile.mkdtemp()); cfgp=d/"c.yaml"; cfgp.write_text("extra_env: {}\n")
    cfg=Config(raw={"stripe":{"secret_key":"sk_test_x"},"harness":{"designer":False,"release_approval":False,"workspace_dir":str(d/"ws"),"db_path":str(d/"db"),"auto_approve_plan":True}},path=cfgp); cfg.env=_build_env(cfg); cfg.workspace_dir.mkdir()
    cfgp.write_text('extra_env:\n  ADMIN_EMAILS: "owner@example.com"\n')   # edited after start -> hot reload
    db=await DB(cfg.db_path).open(); o=O.Orchestrator(cfg,db,N(),runner=R())
    pid=await o.new_project("owner var demo idea"); await asyncio.wait_for(o.loops[pid], 20)
    line=[l for l in prompts[0].splitlines() if "OWNER-PROVIDED" in l][0]; print(line)
    assert "ADMIN_EMAILS" in line and "STRIPE_SECRET_KEY" in line and "owner@example" not in prompts[0]
    print("OWNER VARS OK"); await db.close()
asyncio.run(main())
