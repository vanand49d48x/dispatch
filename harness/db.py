"""SQLite state store: projects, task graph, blackboard messages, shared resources."""
from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT UNIQUE, idea TEXT, status TEXT, thread_id INTEGER,
  spec TEXT, cost_usd REAL DEFAULT 0, budget_usd REAL,
  qa_round INTEGER DEFAULT 0,
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER, key TEXT, role TEXT, title TEXT, description TEXT,
  depends_on TEXT DEFAULT '[]', status TEXT DEFAULT 'pending',
  level INTEGER DEFAULT 0, result TEXT DEFAULT '', history TEXT DEFAULT '[]',
  created_at REAL, updated_at REAL,
  UNIQUE(project_id, key)
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER, task_key TEXT, sender TEXT, kind TEXT, body TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS resources (
  project_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(project_id, key)
);
"""

# Columns added after v0.1 (applied in place on existing databases).
MIGRATIONS = [
    ("projects", "spec_doc", "TEXT DEFAULT ''"),
    ("projects", "milestone", "INTEGER DEFAULT 0"),
    ("projects", "milestones", "TEXT DEFAULT '[]'"),
    ("projects", "required_env", "TEXT DEFAULT '[]'"),
    ("tasks", "milestone", "INTEGER DEFAULT 0"),
    ("tasks", "cost", "REAL DEFAULT 0"),
    ("tasks", "started_at", "REAL"),
    ("tasks", "finished_at", "REAL"),
]
JSON_PROJECT_FIELDS = ("spec", "milestones", "required_env")

# task statuses: pending | running | done | failed | skipped | waiting_owner
TERMINAL = {"done", "failed", "skipped"}


class DB:
    def __init__(self, path: str):
        self.path = str(path)
        self.conn: aiosqlite.Connection | None = None

    async def open(self) -> "DB":
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        for table, col, ddl in MIGRATIONS:
            cur = await self.conn.execute(f"PRAGMA table_info({table})")
            if col not in {r[1] for r in await cur.fetchall()}:
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        await self.conn.commit()
        return self

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def _exec(self, sql: str, args: tuple = ()) -> aiosqlite.Cursor:
        cur = await self.conn.execute(sql, args)
        await self.conn.commit()
        return cur

    async def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        cur = await self.conn.execute(sql, args)
        return [dict(r) for r in await cur.fetchall()]

    async def _one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = await self._all(sql, args)
        return rows[0] if rows else None

    # ---- projects ------------------------------------------------------
    async def create_project(self, slug: str, idea: str, budget: float, spec_doc: str = "") -> int:
        now = time.time()
        cur = await self._exec(
            "INSERT INTO projects(slug, idea, status, budget_usd, spec_doc, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (slug, idea, "planning", budget, spec_doc, now, now),
        )
        return cur.lastrowid

    async def project(self, pid: int) -> dict | None:
        p = await self._one("SELECT * FROM projects WHERE id=?", (pid,))
        if p:
            for k in JSON_PROJECT_FIELDS:
                p[k] = json.loads(p[k]) if p.get(k) else ({} if k == "spec" else [])
        return p

    async def project_by_thread(self, thread_id: int) -> dict | None:
        p = await self._one("SELECT id FROM projects WHERE thread_id=?", (thread_id,))
        return await self.project(p["id"]) if p else None

    async def projects(self, statuses: tuple[str, ...] | None = None) -> list[dict]:
        if statuses:
            q = ",".join("?" * len(statuses))
            return await self._all(f"SELECT * FROM projects WHERE status IN ({q}) ORDER BY id", statuses)
        return await self._all("SELECT * FROM projects ORDER BY id DESC LIMIT 20")

    async def update_project(self, pid: int, **fields: Any) -> None:
        for k in JSON_PROJECT_FIELDS:
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        fields["updated_at"] = time.time()
        sets = ",".join(f"{k}=?" for k in fields)
        await self._exec(f"UPDATE projects SET {sets} WHERE id=?", (*fields.values(), pid))

    async def add_cost(self, pid: int, usd: float) -> float:
        await self._exec("UPDATE projects SET cost_usd=cost_usd+? WHERE id=?", (usd or 0.0, pid))
        p = await self._one("SELECT cost_usd FROM projects WHERE id=?", (pid,))
        return p["cost_usd"]

    # ---- tasks ---------------------------------------------------------
    async def add_task(self, pid: int, key: str, role: str, title: str, description: str,
                       depends_on: list[str] | None = None, milestone: int = 0) -> str:
        now = time.time()
        base, n = key, 1
        while await self._one("SELECT 1 FROM tasks WHERE project_id=? AND key=?", (pid, key)):
            n += 1
            key = f"{base}-{n}"
        await self._exec(
            "INSERT INTO tasks(project_id,key,role,title,description,depends_on,milestone,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (pid, key, role, title, description, json.dumps(depends_on or []), milestone, now, now),
        )
        return key

    async def add_task_cost(self, pid: int, key: str, usd: float) -> None:
        await self._exec("UPDATE tasks SET cost=cost+? WHERE project_id=? AND key=?", (usd or 0.0, pid, key))

    async def tasks(self, pid: int) -> list[dict]:
        rows = await self._all("SELECT * FROM tasks WHERE project_id=? ORDER BY id", (pid,))
        for r in rows:
            r["depends_on"] = json.loads(r["depends_on"])
            r["history"] = json.loads(r["history"])
        return rows

    async def task(self, pid: int, key: str) -> dict | None:
        for t in await self.tasks(pid):
            if t["key"] == key:
                return t
        return None

    async def update_task(self, pid: int, key: str, **fields: Any) -> None:
        for k in ("depends_on", "history"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        fields["updated_at"] = time.time()
        sets = ",".join(f"{k}=?" for k in fields)
        await self._exec(f"UPDATE tasks SET {sets} WHERE project_id=? AND key=?", (*fields.values(), pid, key))
        st = fields.get("status")
        if st == "running":
            await self._exec("UPDATE tasks SET started_at=COALESCE(started_at, ?), finished_at=NULL "
                             "WHERE project_id=? AND key=?", (time.time(), pid, key))
        elif st in TERMINAL:
            await self._exec("UPDATE tasks SET finished_at=? WHERE project_id=? AND key=?", (time.time(), pid, key))

    async def append_history(self, pid: int, key: str, entry: str) -> None:
        t = await self.task(pid, key)
        hist = (t["history"] if t else []) + [entry[:4000]]
        await self.update_task(pid, key, history=hist[-12:])

    # ---- blackboard ----------------------------------------------------
    async def post(self, pid: int, sender: str, kind: str, body: str, task_key: str = "") -> None:
        await self._exec(
            "INSERT INTO messages(project_id,task_key,sender,kind,body,created_at) VALUES(?,?,?,?,?,?)",
            (pid, task_key, sender, kind, body, time.time()),
        )

    async def board(self, pid: int, limit: int = 30, kinds: tuple[str, ...] | None = None) -> list[dict]:
        if kinds:
            q = ",".join("?" * len(kinds))
            rows = await self._all(
                f"SELECT * FROM messages WHERE project_id=? AND kind IN ({q}) ORDER BY id DESC LIMIT ?",
                (pid, *kinds, limit))
        else:
            rows = await self._all("SELECT * FROM messages WHERE project_id=? ORDER BY id DESC LIMIT ?", (pid, limit))
        return list(reversed(rows))

    # ---- shared resources (urls, project refs, non-secret ids) ---------
    async def set_resource(self, pid: int, key: str, value: str) -> None:
        await self._exec("INSERT OR REPLACE INTO resources(project_id,key,value) VALUES(?,?,?)", (pid, key, value))

    async def resources(self, pid: int) -> dict[str, str]:
        rows = await self._all("SELECT key, value FROM resources WHERE project_id=?", (pid,))
        return {r["key"]: r["value"] for r in rows}
