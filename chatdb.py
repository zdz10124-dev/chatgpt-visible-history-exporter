#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ChatGPT visible-history local database + AI-friendly CLI/API server.

Only stores user/assistant visible text supplied by the browser extension.
No cookies, passwords, access tokens, tool traces or reasoning are persisted.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import time
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "chatgpt_history.sqlite3"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17891

# AI 主动同步时的进程内状态。只存在于本地临时服务内，不保存认证信息。
SYNC_STATE = {
    "requested": False,
    "started_at": None,
    "completed": False,
    "result": None,
}
SYNC_EVENT = threading.Event()

SCHEMA = r"""
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  create_time REAL,
  update_time REAL,
  archived INTEGER NOT NULL DEFAULT 0,
  project_id TEXT,
  project_title TEXT,
  synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('user','assistant')),
  create_time REAL,
  ordinal INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL,
  synced_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_time
  ON messages(conversation_id, create_time, ordinal);
CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(create_time);
CREATE INDEX IF NOT EXISTS idx_conversations_update ON conversations(update_time);

CREATE TABLE IF NOT EXISTS message_reads (
  consumer TEXT NOT NULL,
  message_id TEXT NOT NULL,
  read_at TEXT NOT NULL,
  PRIMARY KEY (consumer, message_id),
  FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_reads_consumer ON message_reads(consumer, read_at);

CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  discovered INTEGER DEFAULT 0,
  synced_conversations INTEGER DEFAULT 0,
  synced_messages INTEGER DEFAULT 0,
  failed INTEGER DEFAULT 0,
  note TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db = sqlite3.connect(str(db_path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(SCHEMA)
    return db


def upsert_batch(payload: dict, db_path: Path = DB_PATH) -> dict:
    conversations = payload.get("conversations") or []
    ts = now_iso()
    conv_count = msg_count = 0
    with connect(db_path) as db:
        for c in conversations:
            cid = str(c.get("id") or "").strip()
            if not cid:
                continue
            proj = c.get("project") or {}
            db.execute(
                """INSERT INTO conversations
                (id,title,create_time,update_time,archived,project_id,project_title,synced_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  title=excluded.title,
                  create_time=COALESCE(excluded.create_time, conversations.create_time),
                  update_time=COALESCE(excluded.update_time, conversations.update_time),
                  archived=excluded.archived,
                  project_id=excluded.project_id,
                  project_title=excluded.project_title,
                  synced_at=excluded.synced_at""",
                (cid, c.get("title") or "", c.get("create_time"), c.get("update_time"),
                 1 if c.get("archived") else 0, proj.get("id"), proj.get("title"), ts),
            )
            conv_count += 1
            for i, m in enumerate(c.get("messages") or []):
                role = m.get("role")
                text = m.get("text")
                mid = str(m.get("id") or "").strip()
                if role not in ("user", "assistant") or not mid or not isinstance(text, str) or not text.strip():
                    continue
                db.execute(
                    """INSERT INTO messages(id,conversation_id,role,create_time,ordinal,text,synced_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      conversation_id=excluded.conversation_id,
                      role=excluded.role,
                      create_time=excluded.create_time,
                      ordinal=excluded.ordinal,
                      text=excluded.text,
                      synced_at=excluded.synced_at""",
                    (mid, cid, role, m.get("time"), i, text, ts),
                )
                msg_count += 1
    return {"ok": True, "conversations": conv_count, "messages": msg_count}


def _to_epoch(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def query_messages(params: dict, db_path: Path = DB_PATH) -> dict:
    where = ["1=1"]
    args: list = []

    def add(expr, val):
        where.append(expr)
        args.append(val)

    if params.get("after"):
        add("COALESCE(m.create_time,0) >= ?", _to_epoch(params["after"]))
    if params.get("before"):
        add("COALESCE(m.create_time,0) <= ?", _to_epoch(params["before"]))
    if params.get("role") in ("user", "assistant"):
        add("m.role = ?", params["role"])
    if params.get("conversation_id"):
        add("m.conversation_id = ?", params["conversation_id"])
    if params.get("project"):
        where.append("(c.project_id = ? OR c.project_title LIKE ?)")
        args += [params["project"], f"%{params['project']}%"]
    if params.get("title"):
        add("c.title LIKE ?", f"%{params['title']}%")
    if params.get("keyword"):
        add("m.text LIKE ?", f"%{params['keyword']}%")
    if str(params.get("archived", "")).lower() in ("0", "1", "true", "false"):
        val = str(params["archived"]).lower() in ("1", "true")
        add("c.archived = ?", 1 if val else 0)

    consumer = params.get("consumer") or params.get("unread_for")
    unread_only = bool(params.get("unread_for")) or str(params.get("unread", "")).lower() in ("1", "true", "yes")
    if unread_only:
        if not consumer:
            raise ValueError("unread=true requires consumer or unread_for")
        where.append("NOT EXISTS (SELECT 1 FROM message_reads r WHERE r.message_id=m.id AND r.consumer=?)")
        args.append(consumer)

    limit = max(1, min(int(params.get("limit") or 200), 5000))
    offset = max(0, int(params.get("offset") or 0))
    order = "DESC" if str(params.get("order", "asc")).lower() == "desc" else "ASC"

    sql = f"""
    SELECT m.id,m.conversation_id,m.role,m.create_time,m.ordinal,m.text,
           c.title,c.archived,c.project_id,c.project_title,c.create_time AS conversation_create_time,
           c.update_time AS conversation_update_time
    FROM messages m JOIN conversations c ON c.id=m.conversation_id
    WHERE {' AND '.join(where)}
    ORDER BY COALESCE(m.create_time,0) {order}, m.conversation_id {order}, m.ordinal {order}
    LIMIT ? OFFSET ?
    """
    args2 = args + [limit, offset]
    with connect(db_path) as db:
        rows = [dict(r) for r in db.execute(sql, args2).fetchall()]
        count_sql = f"SELECT COUNT(*) FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE {' AND '.join(where)}"
        total = db.execute(count_sql, args).fetchone()[0]
        for row in rows:
            if consumer:
                row["read_by_consumer"] = bool(db.execute(
                    "SELECT 1 FROM message_reads WHERE consumer=? AND message_id=?", (consumer, row["id"])
                ).fetchone())
    return {"ok": True, "total": total, "limit": limit, "offset": offset, "messages": rows}


def list_conversations(params: dict, db_path: Path = DB_PATH) -> dict:
    where = ["1=1"]
    args = []
    if params.get("after"):
        where.append("COALESCE(update_time,create_time,0) >= ?"); args.append(_to_epoch(params["after"]))
    if params.get("before"):
        where.append("COALESCE(update_time,create_time,0) <= ?"); args.append(_to_epoch(params["before"]))
    if params.get("title"):
        where.append("title LIKE ?"); args.append(f"%{params['title']}%")
    if params.get("project"):
        where.append("(project_id=? OR project_title LIKE ?)"); args += [params["project"], f"%{params['project']}%"]
    limit = max(1, min(int(params.get("limit") or 200), 5000))
    offset = max(0, int(params.get("offset") or 0))
    sql = f"""SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count
              FROM conversations c WHERE {' AND '.join(where)}
              ORDER BY COALESCE(c.update_time,c.create_time,0) DESC LIMIT ? OFFSET ?"""
    with connect(db_path) as db:
        rows = [dict(r) for r in db.execute(sql, args + [limit, offset]).fetchall()]
        total = db.execute(f"SELECT COUNT(*) FROM conversations WHERE {' AND '.join(where)}", args).fetchone()[0]
    return {"ok": True, "total": total, "conversations": rows}


def mark_read(consumer: str, message_ids: list[str], db_path: Path = DB_PATH) -> dict:
    consumer = (consumer or "").strip()
    if not consumer:
        raise ValueError("consumer is required")
    ids = [str(x).strip() for x in message_ids if str(x).strip()]
    ts = now_iso()
    with connect(db_path) as db:
        db.executemany(
            "INSERT INTO message_reads(consumer,message_id,read_at) VALUES(?,?,?) ON CONFLICT(consumer,message_id) DO UPDATE SET read_at=excluded.read_at",
            [(consumer, mid, ts) for mid in ids],
        )
    return {"ok": True, "consumer": consumer, "marked": len(ids), "read_at": ts}


def status(db_path: Path = DB_PATH) -> dict:
    with connect(db_path) as db:
        conv = db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        msg = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        latest = db.execute("SELECT MAX(synced_at) FROM conversations").fetchone()[0]
        consumers = [dict(r) for r in db.execute(
            """SELECT consumer, COUNT(*) AS read_messages, MAX(read_at) AS last_read_at
               FROM message_reads GROUP BY consumer ORDER BY consumer""").fetchall()]
    return {"ok": True, "database": str(db_path), "conversations": conv, "messages": msg, "last_sync_at": latest, "consumers": consumers}


class Handler(BaseHTTPRequestHandler):
    server_version = "ChatGPTLocalHistory/1.0"

    def _json(self, code: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._json(200, {"ok": True})

    def do_GET(self):
        try:
            u = urlparse(self.path)
            q0 = parse_qs(u.query)
            q = {k: v[-1] for k, v in q0.items() if v}
            if u.path == "/api/status":
                return self._json(200, status())
            if u.path == "/api/messages":
                return self._json(200, query_messages(q))
            if u.path == "/api/conversations":
                return self._json(200, list_conversations(q))
            if u.path == "/api/help":
                return self._json(200, HELP_JSON)
            if u.path == "/api/sync/poll":
                return self._json(200, {
                    "ok": True,
                    "requested": bool(SYNC_STATE["requested"] and not SYNC_STATE["completed"]),
                    "started_at": SYNC_STATE["started_at"],
                })
            self._json(404, {"ok": False, "error": "not found"})
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
            u = urlparse(self.path)
            if u.path == "/api/sync/batch":
                return self._json(200, upsert_batch(body))
            if u.path == "/api/sync/complete":
                SYNC_STATE["completed"] = True
                SYNC_STATE["result"] = body
                SYNC_EVENT.set()
                return self._json(200, {"ok": True})
            if u.path.startswith("/api/consumers/") and u.path.endswith("/mark"): 
                consumer = u.path[len("/api/consumers/"):-len("/mark")].strip("/")
                return self._json(200, mark_read(consumer, body.get("message_ids") or []))
            self._json(404, {"ok": False, "error": "not found"})
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})

    def log_message(self, fmt, *args):
        sys.stderr.write("[chatdb] " + (fmt % args) + "\n")


HELP_JSON = {
    "purpose": "Local searchable database of visible ChatGPT user/assistant messages",
    "endpoints": {
        "GET /api/status": "database status and consumers",
        "GET /api/conversations": "filters: after,before,title,project,limit,offset",
        "GET /api/messages": "filters: after,before,role,conversation_id,project,title,keyword,archived,consumer,unread,unread_for,limit,offset,order",
        "POST /api/consumers/{consumer}/mark": {"body": {"message_ids": ["..."]}},
        "POST /api/sync/batch": "used by browser extension",
        "GET /api/sync/poll": "browser content script checks whether an AI-triggered sync is requested",
        "POST /api/sync/complete": "browser reports AI-triggered sync completion"
    },
    "recommended_ai_flow": [
        "query with unread_for=<consumer>",
        "process returned messages",
        "mark exactly the processed message_ids",
        "repeat until total/returned is exhausted"
    ]
}


def cli():
    p = argparse.ArgumentParser(description="ChatGPT visible-history local DB")
    p.add_argument("--db", default=str(DB_PATH))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run local HTTP API")
    s.add_argument("--host", default=DEFAULT_HOST)
    s.add_argument("--port", type=int, default=DEFAULT_PORT)

    sub.add_parser("status", help="show database status")

    sy = sub.add_parser("sync", help="AI-friendly browser-assisted sync; no manual click required")
    sy.add_argument("--timeout", type=int, default=900, help="seconds to wait for the installed Edge extension; first full sync may take several minutes")
    sy.add_argument("--no-open-browser", action="store_true", help="do not automatically open a ChatGPT tab")

    q = sub.add_parser("query", help="query messages")
    for name in ("after","before","role","conversation_id","project","title","keyword","archived","consumer","unread_for","order"):
        q.add_argument("--" + name.replace("_", "-"), dest=name)
    q.add_argument("--unread", action="store_true")
    q.add_argument("--limit", type=int, default=200)
    q.add_argument("--offset", type=int, default=0)

    c = sub.add_parser("conversations", help="list conversations")
    for name in ("after","before","title","project"):
        c.add_argument("--" + name)
    c.add_argument("--limit", type=int, default=200)
    c.add_argument("--offset", type=int, default=0)

    m = sub.add_parser("mark", help="mark messages processed by a consumer")
    m.add_argument("--consumer", required=True)
    m.add_argument("message_ids", nargs="+")

    args = p.parse_args()
    db_path = Path(args.db)
    globals()["DB_PATH"] = db_path

    if args.cmd == "serve":
        connect(db_path).close()
        print(json.dumps({"ok": True, "url": f"http://{args.host}:{args.port}", "db": str(db_path)}, ensure_ascii=False))
        ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    elif args.cmd == "status":
        print(json.dumps(status(db_path), ensure_ascii=False, indent=2))
    elif args.cmd == "sync":
        connect(db_path).close()
        SYNC_STATE.update({"requested": True, "started_at": now_iso(), "completed": False, "result": None})
        SYNC_EVENT.clear()
        server = ThreadingHTTPServer((DEFAULT_HOST, DEFAULT_PORT), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            if not args.no_open_browser:
                edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
                if not edge.exists():
                    edge = Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
                if not edge.exists():
                    raise RuntimeError("Microsoft Edge executable not found")
                subprocess.Popen([str(edge), "https://chatgpt.com/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not SYNC_EVENT.wait(timeout=max(10, args.timeout)):
                raise RuntimeError("同步等待超时。已成功写入的批次会保留，可再次执行 sync 继续；无需手动点击同步按钮。")
            result = SYNC_STATE.get("result") or {}
            out = {"ok": bool(result.get("ok", True)), "sync": result, "database": status(db_path)}
            print(json.dumps(out, ensure_ascii=False, indent=2))
        finally:
            server.shutdown()
            server.server_close()
    elif args.cmd == "query":
        params = {k: v for k, v in vars(args).items() if k not in ("cmd", "db") and v not in (None, False)}
        print(json.dumps(query_messages(params, db_path), ensure_ascii=False, indent=2))
    elif args.cmd == "conversations":
        params = {k: v for k, v in vars(args).items() if k not in ("cmd", "db") and v is not None}
        print(json.dumps(list_conversations(params, db_path), ensure_ascii=False, indent=2))
    elif args.cmd == "mark":
        print(json.dumps(mark_read(args.consumer, args.message_ids, db_path), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()
