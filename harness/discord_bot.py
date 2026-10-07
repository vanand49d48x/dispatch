"""Discord front-end: one thread per project, slash commands, two-way owner messaging."""
from __future__ import annotations

import logging
import re
from typing import Optional

import discord
from discord import app_commands

from .notify import Notifier
from .orchestrator import GO_WORDS

log = logging.getLogger("forge.discord")
SPEC_EXT = (".md", ".txt", ".markdown")
SECRET_RE = re.compile(r"^\s*[A-Z][A-Z0-9_]{2,}\s*=\s*\S", re.M)


async def read_spec(attachments) -> str:
    """Concatenate text attachments (spec files) into one document."""
    parts = []
    for a in attachments or []:
        if a and a.filename.lower().endswith(SPEC_EXT) and a.size <= 400_000:
            parts.append((await a.read()).decode("utf-8", "replace"))
    return "\n\n".join(parts)

HELP = ("**Dispatch** - post an idea in this channel (or `/build`), optionally with a spec file attached "
        "(.md/.txt). The team plans, builds, tests and deploys it; specs with milestones ship one milestone at a "
        "time. Each project gets a thread: reply there to answer questions, send `NAME=value` API keys, request "
        "changes, `lesson: ...` to teach the team, or attach screenshots of apps you like as design references.\n"
        "Commands: `/build` `/status` `/projects` `/next` `/pause` `/resume` `/cancel` `/budget`")


class DiscordNotifier(Notifier):
    def __init__(self, client: discord.Client, channel_id: int):
        super().__init__()
        self.client, self.channel_id = client, channel_id

    async def _chan(self, cid: int):
        return self.client.get_channel(cid) or await self.client.fetch_channel(cid)

    async def create_thread(self, title: str, intro: str) -> int:
        ch = await self._chan(self.channel_id)
        msg = await ch.send(f"**{title}**")
        thread = await msg.create_thread(name=title[:95], auto_archive_duration=10080)
        await thread.send(intro)
        return thread.id

    async def send_files(self, thread_id: int, paths: list[str], text: str = "") -> None:
        ch = await self._chan(thread_id)
        files = [discord.File(p) for p in paths[:10]]
        await ch.send(text or None, files=files)

    async def _send(self, thread_id: int, text: str) -> None:
        ch = await self._chan(thread_id)
        if isinstance(ch, discord.Thread) and ch.archived:
            await ch.edit(archived=False)
        await ch.send(text, suppress_embeds=True)


class ForgeBot(discord.Client):
    def __init__(self, cfg, make_orchestrator, warnings: list[str] | None = None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.cfg = cfg
        self.channel_id = int(cfg.get("discord.channel_id"))
        self.owner_id = int(cfg.get("discord.owner_user_id"))
        self.notifier = DiscordNotifier(self, self.channel_id)
        self.orch = None
        self._make_orch = make_orchestrator
        self.tree = app_commands.CommandTree(self)
        self._started = False
        self.preflight_warnings = warnings or []
        self._register()

    # ------------------------------------------------------------------ setup
    async def setup_hook(self) -> None:
        self.orch = await self._make_orch(self.notifier)
        ch = await self.fetch_channel(self.channel_id)
        guild = discord.Object(id=ch.guild.id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self) -> None:
        log.info("Discord connected as %s", self.user)
        if not self._started:
            self._started = True
            await self.orch.start()
            ch = await self.fetch_channel(self.channel_id)
            from . import capacity
            msg = (f"🟢 Dispatch online - all preflight checks passed. Capacity: {capacity.describe()}.\n"
                   + HELP)
            if self.preflight_warnings:
                msg += "\n\n⚠ Preflight warnings:\n" + "\n".join(f"• {w}" for w in self.preflight_warnings)
            await ch.send(msg[:1990])

    # ------------------------------------------------------------------ helpers
    def _in_scope(self, channel) -> bool:
        if getattr(channel, "id", None) == self.channel_id:
            return True
        return isinstance(channel, discord.Thread) and channel.parent_id == self.channel_id

    async def _project_for(self, inter: discord.Interaction, project: Optional[int]) -> Optional[int]:
        if project:
            return project
        if isinstance(inter.channel, discord.Thread):
            p = await self.orch.db.project_by_thread(inter.channel.id)
            if p:
                return p["id"]
        rows = await self.orch.db.projects(("planning", "running", "over_budget", "stalled", "milestone_done"))
        return rows[-1]["id"] if rows else None

    # ------------------------------------------------------------------ owner messages
    async def on_message(self, m: discord.Message) -> None:
        if m.author.bot or m.author.id != self.owner_id or not self._in_scope(m.channel):
            return
        text = m.content.strip()
        images = [a for a in m.attachments if (a.content_type or "").startswith("image/") and a.size <= 8_000_000]
        spec = "" if isinstance(m.channel, discord.Thread) else await read_spec(m.attachments)
        if images and isinstance(m.channel, discord.Thread):
            p = await self.orch.db.project_by_thread(m.channel.id)
            if p:
                await self.orch.add_design_refs(p["id"], [(a.filename, await a.read()) for a in images])
                if not text:
                    await m.add_reaction("🎨")
                    return
        if not text and not spec:
            return
        if isinstance(m.channel, discord.Thread):
            has_secret = bool(SECRET_RE.search(text))
            if self.notifier.deliver(m.channel.id, text):
                handled = True
                if not has_secret:
                    await m.add_reaction("✅")
            else:
                handled = await self.orch.handle_owner_message(m.channel.id, text)
                if handled and not has_secret:
                    await m.add_reaction("👀")
            if handled and has_secret:
                try:
                    await m.delete()
                    await m.channel.send("🔒 Keys received; your message was deleted so they don't sit in chat.")
                except discord.HTTPException:
                    await m.channel.send("🔒 Keys received. Delete your message manually (I lack Manage Messages).")
            return
        # Top-level message in the control channel = a new build request.
        if text.lower() in ("help", "?"):
            await m.reply(HELP)
            return
        # Short replies like "go"/"yes" belong in a project thread, not as new project ideas.
        if not spec and (text.lower().rstrip("!. ") in GO_WORDS + ("skip", "done", "no", "stop")
                         or len(text.split()) < 3):
            await m.reply("That looks like a reply - send it inside the project's thread. To start a new "
                          "project, describe the idea in a full sentence (or use `/build`).")
            return
        idea = text or spec.strip().splitlines()[0].lstrip("# ")[:200]
        pid = await self.orch.new_project(idea, spec_doc=spec)
        if images:
            await self.orch.add_design_refs(pid, [(a.filename, await a.read()) for a in images])
        await m.add_reaction("🚀")
        log.info("new project %s from channel message", pid)

    # ------------------------------------------------------------------ slash commands
    def _register(self) -> None:
        tree, bot = self.tree, self

        def owner_only(inter: discord.Interaction) -> bool:
            return inter.user.id == bot.owner_id

        @tree.command(name="build", description="Plan, build and deploy a new SaaS idea")
        @app_commands.check(owner_only)
        @app_commands.describe(idea="What to build", spec="Optional spec file (.md/.txt)",
                               budget="Optional USD cap for this project")
        async def build(inter: discord.Interaction, idea: str, spec: Optional[discord.Attachment] = None,
                        budget: Optional[float] = None):
            await inter.response.defer(thinking=True)
            await inter.followup.send(f"🚀 On it: {idea[:300]}" + (" (with spec)" if spec else ""))
            await bot.orch.new_project(idea, spec_doc=await read_spec([spec]), budget=budget)

        @tree.command(name="next", description="Start the next milestone of a project")
        @app_commands.check(owner_only)
        async def next_(inter: discord.Interaction, project: Optional[int] = None):
            await inter.response.defer(thinking=True)
            pid = await bot._project_for(inter, project)
            await inter.followup.send(f"➡️ Next milestone for #{pid}")
            await bot.orch._start_next_milestone(pid)

        @tree.command(name="status", description="Progress of a project (defaults to this thread / latest)")
        @app_commands.check(owner_only)
        async def status(inter: discord.Interaction, project: Optional[int] = None):
            await inter.response.defer(thinking=True)
            pid = await bot._project_for(inter, project)
            text = await bot.orch.status_text(pid) if pid else "No projects yet."
            await inter.followup.send(text[:1990])

        @tree.command(name="projects", description="List recent projects")
        @app_commands.check(owner_only)
        async def projects(inter: discord.Interaction):
            await inter.response.defer(thinking=True)
            rows = await bot.orch.db.projects()
            text = "\n".join(f"#{p['id']} **{p['slug']}** - {p['status']} (${p['cost_usd']:.2f})" for p in rows)
            await inter.followup.send(text[:1990] or "No projects yet.")

        @tree.command(name="pause", description="Pause a project")
        @app_commands.check(owner_only)
        async def pause(inter: discord.Interaction, project: Optional[int] = None):
            await inter.response.defer(thinking=True)
            pid = await bot._project_for(inter, project)
            await inter.followup.send(f"Pausing #{pid}…")
            await bot.orch.pause(pid)

        @tree.command(name="resume", description="Resume a paused/stalled project")
        @app_commands.check(owner_only)
        async def resume(inter: discord.Interaction, project: Optional[int] = None):
            await inter.response.defer(thinking=True)
            pid = await bot._project_for(inter, project)
            await inter.followup.send(f"Resuming #{pid}…")
            await bot.orch.resume(pid)

        @tree.command(name="cancel", description="Permanently stop a project and all its agents")
        @app_commands.check(owner_only)
        async def cancel(inter: discord.Interaction, project: Optional[int] = None):
            await inter.response.defer(thinking=True)
            if project is None and not isinstance(inter.channel, discord.Thread):
                await inter.followup.send("Run this inside the project's thread, or pass project:<number>.")
                return
            pid = await bot._project_for(inter, project)
            await inter.followup.send(f"🛑 Cancelling #{pid}…")
            await bot.orch.cancel(pid)

        @tree.command(name="budget", description="Set a project's budget in USD")
        @app_commands.check(owner_only)
        async def budget(inter: discord.Interaction, usd: float, project: Optional[int] = None):
            await inter.response.defer(thinking=True)
            pid = await bot._project_for(inter, project)
            await inter.followup.send(f"Setting budget for #{pid} to ${usd:.2f}")
            await bot.orch.set_budget(pid, usd)

        @tree.error
        async def on_error(inter: discord.Interaction, error: app_commands.AppCommandError):
            msg = "Only the owner can use this." if isinstance(error, app_commands.CheckFailure) else f"Error: {error}"
            if inter.response.is_done():
                await inter.followup.send(msg, ephemeral=True)
            else:
                await inter.response.send_message(msg, ephemeral=True)
