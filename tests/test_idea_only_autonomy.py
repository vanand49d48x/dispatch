import asyncio, sys, tempfile, subprocess
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
    def __init__(s): super().__init__(); s.asked=[]
    async def create_thread(s,a,b): return 7
    async def _send(s,t,x):
        if "❓" in x: s.asked.append(x)
plans = []
class R:
    async def run(self, role, prompt, cwd, server, cb, **kw):
        ctx, T = server["ctx"], server["tools"]; k = ctx.task_key
        if k == "plan":
            plans.append(prompt)
            if "Plan milestone 2" in prompt:
                assert (cwd / "SPEC.md").read_text().startswith("# Teachloop")
                await T["submit_plan"].handler({"name":"Teachloop","summary":"s","features":[],"milestone_title":"Sell",
                    "tasks":[{"key":"checkout","role":"backend","title":"Checkout","description":"d"}]})
            else:
                assert "YOU write the spec" in prompt and "ordered milestones" in prompt
                r = await T["submit_plan"].handler({"name":"Teachloop","summary":"s","features":[],"milestones":["Skeleton","Sell"],
                    "tasks":[{"key":"setup","role":"devops","title":"s","description":"d"}]})
                assert "spec_markdown" in r["content"][0]["text"], r
                await T["submit_plan"].handler({"name":"Teachloop","summary":"s","features":[],"milestones":["Skeleton","Sell"],
                    "spec_markdown":"# Teachloop\\nTalk-to-course platform",
                    "tasks":[{"key":"setup","role":"devops","title":"s","description":"d"}]})
            return RunResult()
        if k == "m1-setup": await T["set_resource"].handler({"key":"repo_url","value":"x"})
        await T["complete_task"].handler({"summary":"ok"}); return RunResult()
async def main():
    d=Path(tempfile.mkdtemp()); (d/"c.yaml").write_text("{}")
    cfg=Config(raw={"harness":{"designer":False,"workspace_dir":str(d/"ws"),"db_path":str(d/"db"),"autonomy":"full",
                               "auto_approve_plan":False,"release_approval":True}},path=d/"c.yaml"); cfg.env=_build_env(cfg); cfg.workspace_dir.mkdir()
    db=await DB(cfg.db_path).open(); n=N(); o=O.Orchestrator(cfg,db,n,runner=R())
    pid=await o.new_project("a platform where freelancers talk about a skill and AI turns it into a sellable course")
    for _ in range(100):
        await asyncio.sleep(0.2)
        if (await db.project(pid))["status"] == "completed": break
    p=await db.project(pid); ws=cfg.workspace_dir/p["slug"]
    log=subprocess.run(["git","log","--oneline"],cwd=ws,capture_output=True,text=True).stdout
    print("status:", p["status"], "| milestones:", p["milestones"], "| plans:", len(plans), "| owner questions:", len(n.asked), "| spec committed:", "Add product spec" in log)
    assert p["status"]=="completed" and len(plans)==2 and not n.asked and "Add product spec" in log
    print("IDEA-ONLY AUTONOMOUS OK"); await db.close()
asyncio.run(main())
