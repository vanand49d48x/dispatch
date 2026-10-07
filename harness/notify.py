"""Channel-agnostic owner messaging. Discord implements it; Console is for local testing."""
from __future__ import annotations

import asyncio
import itertools
import re
import sys
from dataclasses import dataclass


@dataclass
class Pending:
    qid: int
    thread_id: int
    question: str
    future: asyncio.Future


class Notifier:
    """Subclasses implement create_thread() and _send(). Questions/answers are handled here."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.pending: dict[int, Pending] = {}

    async def create_thread(self, title: str, intro: str) -> int:
        raise NotImplementedError

    async def _send(self, thread_id: int, text: str) -> None:
        raise NotImplementedError

    async def send_files(self, thread_id: int, paths: list[str], text: str = "") -> None:
        """Post files (e.g. screenshots). Channels without file support just list the paths."""
        await self.say(thread_id, (text + "\n" if text else "") + "\n".join(paths))

    async def say(self, thread_id: int, text: str) -> None:
        for chunk in _chunks(text, 1900):
            await self._send(thread_id, chunk)

    async def ask(self, thread_id: int, question: str, timeout_s: float) -> str | None:
        """Post a question and wait for the owner's reply. None on timeout."""
        qid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[qid] = Pending(qid, thread_id, question, fut)
        open_here = [p for p in self.pending.values() if p.thread_id == thread_id]
        tag = f"**Question #{qid}**" + (" (reply with `#%d your answer`)" % qid if len(open_here) > 1 else "")
        await self.say(thread_id, f"❓ {tag}\n{question}")
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            await self.say(thread_id, f"⏱️ No reply to question #{qid}; agents will proceed with their best judgement.")
            return None
        finally:
            self.pending.pop(qid, None)

    async def ask_yes_no(self, thread_id: int, question: str, timeout_s: float) -> bool:
        ans = await self.ask(thread_id, question + "\nReply `yes` or `no`.", timeout_s)
        return bool(ans) and ans.strip().lower().startswith(("y", "approve", "ok", "go"))

    def deliver(self, thread_id: int, text: str) -> bool:
        """Route an owner message to a waiting question. Returns True if it answered one."""
        open_here = [p for p in self.pending.values() if p.thread_id == thread_id and not p.future.done()]
        if not open_here:
            return False
        m = re.match(r"\s*#(\d+)\s*(.*)", text, re.S)
        if m and int(m.group(1)) in self.pending:
            target, body = self.pending[int(m.group(1))], m.group(2)
        elif len(open_here) == 1:
            target, body = open_here[0], text
        else:
            return False
        target.future.set_result(body.strip())
        return True

    def open_questions(self, thread_id: int) -> list[Pending]:
        return [p for p in self.pending.values() if p.thread_id == thread_id]


def _chunks(text: str, n: int):
    text = text or "(empty)"
    while text:
        if len(text) <= n:
            yield text
            return
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        yield text[:cut]
        text = text[cut:].lstrip("\n")


class ConsoleNotifier(Notifier):
    """Local mode: prints to stdout and reads answers from stdin."""

    def __init__(self) -> None:
        super().__init__()
        self._threads = itertools.count(1)
        self.last_thread = 0

    async def create_thread(self, title: str, intro: str) -> int:
        self.last_thread = next(self._threads)
        await self.say(self.last_thread, f"=== {title} ===\n{intro}")
        return self.last_thread

    async def _send(self, thread_id: int, text: str) -> None:
        print(f"\n[thread {thread_id}] {text}", flush=True)

    async def stdin_loop(self, on_message) -> None:
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                await asyncio.sleep(3600)
                continue
            line = line.strip()
            if line and not self.deliver(self.last_thread, line):
                await on_message(self.last_thread, line)
