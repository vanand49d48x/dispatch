import asyncio, sys, tempfile, httpx, copy
from pathlib import Path
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from harness.config import Config
from harness.preflight import run_preflight, P_VIEW, P_SEND, P_HISTORY, P_PUBLIC_THREADS, P_SEND_THREADS, P_ADD_REACTIONS, P_MANAGE_MSG
GID, BOT, OWNER, CH = "111111111111111111", "222222222222222222", "333333333333333333", "444444444444444444"
ALL = P_VIEW|P_SEND|P_HISTORY|P_PUBLIC_THREADS|P_SEND_THREADS|P_ADD_REACTIONS|P_MANAGE_MSG
def handler(state):
    def h(req: httpx.Request):
        u = str(req.url)
        J = lambda d, code=200, hdr=None: httpx.Response(code, json=d, headers=hdr or {})
        if "anthropic.com/v1/models" in u: return J({"data": []})
        if "anthropic.com/v1/messages" in u:
            return J({"error": {"message": "Your credit balance is too low"}}, 400) if state.get("no_credit") else J({"content": []})
        if u.endswith("/users/@me"): return J({"id": BOT, "username": "forge"})
        if "applications/@me" in u: return J({"id": BOT, "flags": 0 if state.get("no_intent") else 1 << 19})
        if f"/channels/{CH}" in u: return J({"id": CH, "type": 0, "guild_id": GID, "name": "agentforge", "permission_overwrites": []})
        if f"/members/{OWNER}" in u: return J({"user": {"username": "vishal"}})
        if f"/members/{BOT}" in u: return J({"roles": ["555"]})
        if u.endswith("/roles"): return J([{"id": GID, "permissions": str(P_VIEW|P_SEND|P_HISTORY)},
                                           {"id": "555", "permissions": str(0 if state.get("few_perms") else ALL)}])
        if u.endswith(f"/guilds/{GID}"): return J({"owner_id": OWNER})
        if "/commands" in u: return J([])
        if "api.github.com/user" in u:
            return J({"login": "vishal"}, hdr={"x-oauth-scopes": state.get("scopes", "repo, workflow, read:org")})
        if "api.vercel.com/v2/user" in u: return J({"user": {"username": "v"}})
        if u.endswith("/v1/organizations"): return J([{"id": "orgslug", "name": "Mine"}])
        if "/v1/organizations/orgslug" in u: return J({"plan": "free"})
        if u.endswith("/v1/projects"): return J([{"organization_id": "orgslug", "status": "ACTIVE_HEALTHY"}] * state.get("projects", 0))
        return httpx.Response(404)
    return h
raw = {"anthropic": {"api_key": "sk-ant-abc"}, "discord": {"bot_token": "MTIz.abc.def", "channel_id": CH, "owner_user_id": OWNER},
       "github": {"token": "ghp_x", "owner": "vishal"}, "vercel": {"token": "v"}, "supabase": {"access_token": "sbp_x", "org_id": "orgslug"}, "git": {"author_email": "me@example.com"}}
async def run(state, raw=raw):
    d = Path(tempfile.mkdtemp()); cfg = Config(raw=copy.deepcopy(raw) | {"harness": {"workspace_dir": str(d)}}, path=d / "c.yaml")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler(state))) as c:
        return await run_preflight(cfg, client=c, check_local=False)
async def main():
    r = await run({}); print(r.text()); assert not r.failed and not r.warnings
    print("\n================ BAD SETUP")
    r = await run({"no_intent": 1, "few_perms": 1, "scopes": "repo", "projects": 2, "no_credit": 1}); print(r.text())
    names = {c.name for c in r.checks if c.status == "fail"}
    assert {"Message Content Intent", "permissions", "scope `workflow`", "room for a new project", "credits / billing"} <= names, names
    print("\n================ PLACEHOLDER")
    bad = copy.deepcopy(raw); bad["discord"]["channel_id"] = "your-channel-id"; bad["github"]["token"] = None
    r = await run({}, bad); print(r.text()); assert r.failed
    print("\nPREFLIGHT TESTS OK")
asyncio.run(main())
