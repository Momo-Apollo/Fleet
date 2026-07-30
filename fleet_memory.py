#!/usr/bin/env python3
"""Fleet Memory Server — SQLite+FTS5 knowledge base over MCP Streamable HTTP.

Standalone:  python fleet_memory.py
Migrate:     python fleet_memory.py --migrate
Auto-start:  imported by fleet_app.py via start_server()
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

FLEET_DIR = Path.home() / ".fleet"
DB_PATH   = FLEET_DIR / "memory.db"
HOST      = "127.0.0.1"
PORT      = 54321

_server: HTTPServer = None
_running = False
_db_lock = threading.Lock()


# ── DB ──────────────────────────────────────────────────────────────────

def _init_db() -> None:
    FLEET_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories USING fts5(
                name      UNINDEXED,
                type      UNINDEXED,
                description,
                content,
                tokenize='porter ascii'
            );
            CREATE TABLE IF NOT EXISTS memories_meta (
                name    TEXT PRIMARY KEY,
                type    TEXT,
                created REAL,
                updated REAL
            );
        """)
        conn.commit()
    finally:
        conn.close()


def _search(query: str, limit: int = 10) -> list:
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT name, type, description, content "
                "FROM memories WHERE memories MATCH ? "
                "ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            return [{"error": str(e)}]
        finally:
            conn.close()


def _write(name: str, type_: str, description: str, content: str) -> str:
    now = time.time()
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            row = conn.execute(
                "SELECT created FROM memories_meta WHERE name=?", (name,)
            ).fetchone()
            created = row[0] if row else now
            conn.execute("DELETE FROM memories WHERE name=?", (name,))
            conn.execute("DELETE FROM memories_meta WHERE name=?", (name,))
            conn.execute(
                "INSERT INTO memories(name, type, description, content) VALUES (?,?,?,?)",
                (name, type_, description, content),
            )
            conn.execute(
                "INSERT INTO memories_meta(name, type, created, updated) VALUES (?,?,?,?)",
                (name, type_, created, now),
            )
            conn.commit()
            return "Memory '{}' written.".format(name)
        except Exception as e:
            conn.rollback()
            return "Error: {}".format(e)
        finally:
            conn.close()


def _delete(name: str) -> str:
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            c = conn.execute("DELETE FROM memories WHERE name=?", (name,))
            conn.execute("DELETE FROM memories_meta WHERE name=?", (name,))
            conn.commit()
            if c.rowcount:
                return "Memory '{}' deleted.".format(name)
            return "No memory named '{}'.".format(name)
        except Exception as e:
            conn.rollback()
            return "Error: {}".format(e)
        finally:
            conn.close()


def _list_all() -> list:
    """Return all memories (for migration/export)."""
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(
                "SELECT name, type, description, content FROM memories"
            ).fetchall()]
        finally:
            conn.close()


# ── Tools ────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "memory_search",
        "description": (
            "Full-text search the Fleet knowledge base. Returns matching memories "
            "sorted by relevance. Call at the start of tasks where past context "
            "helps (user preferences, decisions, project state)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Search terms"},
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10)",
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "memory_write",
        "description": (
            "Write or update a memory in the Fleet knowledge base. "
            "Use for cross-session knowledge: user preferences, decisions, "
            "project state, reference pointers. Overwrites same-name entries."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["name", "type", "description", "content"],
            "properties": {
                "name":        {"type": "string", "description": "Unique kebab-case slug"},
                "type":        {"type": "string", "description": "user | feedback | project | reference"},
                "description": {"type": "string", "description": "One-line summary"},
                "content":     {"type": "string", "description": "Full memory body"},
            },
        },
    },
    {
        "name": "memory_delete",
        "description": "Delete a memory entry by name.",
        "inputSchema": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "description": "Name of the memory to delete"},
            },
        },
    },
]


# ── MCP dispatch ──────────────────────────────────────────────────────────

def _dispatch(method: str, params: dict, req_id) -> dict:
    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fleet-memory", "version": "1.0.0"},
            },
        }
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        try:
            if name == "memory_search":
                results = _search(args["query"], int(args.get("limit", 10)))
                text = json.dumps(results, indent=2) if results else "No results found."
            elif name == "memory_write":
                text = _write(
                    args["name"], args["type"], args["description"], args["content"]
                )
            elif name == "memory_delete":
                text = _delete(args["name"])
            else:
                text = "Unknown tool: {}".format(name)
        except Exception as e:
            text = "Error: {}".format(e)
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": text}]},
        }
    if req_id is not None:
        return {
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": "Unknown method: {}".format(method)},
        }
    return None


# ── HTTP handler ──────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress access log noise

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Mcp-Session-Id")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self._cors()
            self.end_headers()
            self.wfile.write(b"fleet-memory ok\n")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path not in ("/mcp", "/", ""):
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            req = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        method = req.get("method", "")
        params = req.get("params", {})
        req_id = req.get("id")
        resp   = _dispatch(method, params, req_id)

        if resp is None:
            # Notification — acknowledge with no body
            self.send_response(202)
            self._cors()
            self.end_headers()
            return

        out = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self._cors()
        self.end_headers()
        self.wfile.write(out)


# ── Public API ────────────────────────────────────────────────────────────

def start_server(host: str = HOST, port: int = PORT) -> HTTPServer:
    """Init DB, start the HTTP server in a daemon thread, return the server."""
    global _server, _running
    if _running:
        return _server
    try:
        _init_db()
        srv = HTTPServer((host, port), _Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        _server = srv
        _running = True
        return srv
    except Exception as e:
        sys.stderr.write("[fleet-memory] Failed to start on {}:{} — {}\n".format(host, port, e))
        return None


def stop_server() -> None:
    global _server, _running
    if _server:
        try:
            _server.shutdown()
        except Exception:
            pass
        _server = None
    _running = False


def is_running() -> bool:
    return _running


# ── Migration ─────────────────────────────────────────────────────────────

def _parse_frontmatter(text: str) -> tuple:
    """Return (frontmatter_dict, body) from a YAML frontmatter markdown file."""
    if not text.startswith("---"):
        return {}, text.strip()
    try:
        end = text.index("---", 3)
        header = text[3:end]
        body   = text[end + 3:].strip()
        fm = {}
        for line in header.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip().strip("\"'")
        return fm, body
    except Exception:
        return {}, text.strip()


def migrate_from_files(dry_run: bool = False) -> int:
    """Scan ~/.claude/projects/*/memory/*.md and import into DB. Returns count."""
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        print("No ~/.claude/projects directory found.")
        return 0
    files = list(base.glob("*/memory/*.md"))
    if not files:
        print("No memory files found.")
        return 0
    count = 0
    for f in files:
        if f.name == "MEMORY.md":
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            fm, body = _parse_frontmatter(text)
            name = fm.get("name") or f.stem
            type_ = fm.get("metadata", {}).get("type", "project") if isinstance(
                fm.get("metadata"), dict
            ) else "project"
            description = fm.get("description", "Migrated from {}".format(f.name))
            if dry_run:
                print("  would import: {} ({})".format(name, type_))
            else:
                result = _write(name, type_, description, body)
                print("  {}".format(result))
            count += 1
        except Exception as e:
            print("  skip {} — {}".format(f.name, e))
    return count


# ── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--migrate" in sys.argv:
        dry = "--dry-run" in sys.argv
        _init_db()
        print("Migrating memory files{}...".format(" (dry run)" if dry else ""))
        n = migrate_from_files(dry_run=dry)
        print("Done — {} entries.".format(n))
        sys.exit(0)

    _init_db()

    import urllib.request as _ur
    def _port_has_fleet_memory():
        try:
            with _ur.urlopen("http://{}:{}/health".format(HOST, PORT), timeout=1) as r:
                return b"fleet-memory" in r.read()
        except Exception:
            return False

    # If another fleet-memory instance (e.g. Fleet GUI app) already holds the port,
    # wait in a polling loop until it releases, then take over.
    _logged_waiting = False
    while True:
        try:
            srv = HTTPServer((HOST, PORT), _Handler)
            break
        except OSError as e:
            if "Address already in use" not in str(e):
                raise
            if not _logged_waiting:
                label = "fleet-memory" if _port_has_fleet_memory() else "unknown process"
                sys.stderr.write(
                    "[fleet-memory] port {} held by {}; polling...\n".format(PORT, label)
                )
                sys.stderr.flush()
                _logged_waiting = True
            time.sleep(2)

    print("Fleet Memory Server on {}:{} (DB: {})".format(HOST, PORT, DB_PATH), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
