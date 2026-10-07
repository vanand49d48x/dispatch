import asyncio, sys, tempfile
from pathlib import Path
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import harness.orchestrator as O
from harness.tools import make_tools
from harness.config import Config, _build_env
from harness.db import DB
from harness.notify import Notifier
from harness.agents import RunResult, ROLES
O.build_server = lambda ctx, read_only=False: {"ctx": ctx, "tools": {t.name: t for t in make_tools(ctx, read_only)}}
sent_files = []
class N(Notifier):
    async def create_thread(s,a,b): return 7
    async def send_files(s, t, paths, text=""): sent_files.extend(paths)
    async def _send(s,t,x):
        for p in list(s.pending.values()):
            if not p.future.done() and "Ready for production" in p.question:
                asyncio.get_running_loop().call_soon(s.deliver, t, "ship")
order = []; plan_err = []
class R:
    async def run(self, role, prompt, cwd, server, cb, **kw):
        ctx, T = server["ctx"], server["tools"]; k = ctx.task_key
        if k == "plan":
            base = {"name":"x","summary":"s","features":[],"spec_markdown":"# x"}
            r = await T["submit_plan"].handler(base | {"tasks":[{"key":"setup","role":"devops","title":"s","description":"d"},
                {"key":"ui","role":"frontend","title":"UI","description":"d","depends_on":["setup"]}]})
            plan_err.append(r["content"][0]["text"])
            await T["submit_plan"].handler(base | {"tasks":[{"key":"setup","role":"devops","title":"s","description":"d"},
                {"key":"design-system","role":"designer","title":"Design system","description":"d","depends_on":["setup"]},
                {"key":"ui","role":"frontend","title":"UI","description":"d","depends_on":["design-system"]}]})
            return RunResult()
        order.append((k, role))
        if k.startswith("design-review"):
            (cwd/"design/screens").mkdir(parents=True, exist_ok=True); (cwd/"design/screens/home-390.png").write_bytes(b"png")
        await T["complete_task"].handler({"summary": f"{k} ok"}); return RunResult()
async def main():
    d=Path(tempfile.mkdtemp()); (d/"c.yaml").write_text("{}")
    cfg=Config(raw={"harness":{"workspace_dir":str(d/"ws"),"db_path":str(d/"db"),"auto_approve_plan":True}},path=d/"c.yaml"); cfg.env=_build_env(cfg); cfg.workspace_dir.mkdir()
    db=await DB(cfg.db_path).open(); o=O.Orchestrator(cfg,db,N(),runner=R())
    pid=await o.new_project("design flow test idea")
    await o.add_design_refs(pid, [("linear shot.png", b"img")])
    await asyncio.wait_for(o.loops[pid], 30)
    keys=[k for k,_ in order]; print("order:", keys)
    assert "no designer task" in plan_err[0]
    assert keys.index("design-system") < keys.index("ui") < keys.index("design-review-1") < keys.index("qa-1") < keys.index("release")
    assert dict(order)["design-review-1"] == "designer" and sent_files and sent_files[0].endswith("home-390.png")
    assert any("reference image" in m["body"] for m in await db.board(pid, 50, kinds=("owner",)))
    assert (await db.resources(pid)).get("design_system") and "INSPIRATION" in ROLES["designer"] and "CREDITS.md" in ROLES["designer"]
    print("DESIGNER FLOW OK"); await db.close()
asyncio.run(main())
