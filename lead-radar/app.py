#!/usr/bin/env python3
"""Panel web del radar de leads.

    uvicorn app:app --host 0.0.0.0 --port 8000

Variables de entorno (todas opcionales salvo la clave de Claude para usar IA):
  ANTHROPIC_API_KEY        clave de la API de Claude (sin ella: modo palabras clave)
  REDDIT_CLIENT_ID/SECRET  app "script" de Reddit para leer con la API oficial
  TELEGRAM_BOT_TOKEN/CHAT_ID  avisos al móvil
  APP_PASSWORD             protege el panel con usuario "admin" y esta contraseña
  APP_NAME                 nombre de la marca (por defecto "Rastro")
  DATA_DIR                 dónde guardar la base de datos (por defecto ./data)
  DEMO_MODE=1              usa posts de ejemplo, sin internet ni IA
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

import radar

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", HERE / "data"))
DB_PATH = DATA_DIR / "radar.db"
APP_NAME = os.environ.get("APP_NAME", "Rastro")
DEMO = os.environ.get("DEMO_MODE") == "1"

DEFAULT_SETTINGS = {
    "scan_interval_min": "30",
    "lookback_hours": "24",
    "model": "claude-opus-5-5",
    "effort": "low",
    "include_comments": "1",
    "notify_telegram": "1",
}

# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  url TEXT DEFAULT '',
  description TEXT DEFAULT '',
  ideal_customer TEXT DEFAULT '',
  not_a_fit TEXT DEFAULT '',
  keywords TEXT DEFAULT '[]',
  negative_keywords TEXT DEFAULT '[]',
  subreddits TEXT DEFAULT '[]',
  search_queries TEXT DEFAULT '[]',
  min_score INTEGER DEFAULT 7,
  active INTEGER DEFAULT 1,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS leads (
  id TEXT PRIMARY KEY,
  project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
  post_id TEXT, source TEXT, where_ TEXT, title TEXT, body TEXT, url TEXT, author TEXT,
  created_utc REAL, score INTEGER, reason TEXT, suggested_reply TEXT,
  status TEXT DEFAULT 'new', found_at REAL
);
CREATE INDEX IF NOT EXISTS leads_found ON leads(found_at DESC);
CREATE TABLE IF NOT EXISTS seen (post_id TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL, finished_at REAL,
  fetched INTEGER, candidates INTEGER, leads INTEGER, mode TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

JSON_FIELDS = ("keywords", "negative_keywords", "subreddits", "search_queries")


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        if DEMO and not c.execute("SELECT 1 FROM projects").fetchone():
            seed = json.loads((HERE / "tests" / "demo_projects.json").read_text(encoding="utf-8"))
            for p in seed:
                save_project_row(c, p)


def get_settings() -> dict:
    with db() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings")}


def project_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    for f in JSON_FIELDS:
        d[f] = json.loads(d[f] or "[]")
    return d


def save_project_row(c: sqlite3.Connection, p: dict, pid: int | None = None) -> int:
    vals = {
        "name": p["name"].strip(),
        "url": p.get("url", ""),
        "description": p.get("description", ""),
        "ideal_customer": p.get("ideal_customer", ""),
        "not_a_fit": p.get("not_a_fit", ""),
        **{f: json.dumps([s.strip().removeprefix("r/") if f == "subreddits" else s.strip()
                          for s in p.get(f, []) if s.strip()], ensure_ascii=False)
           for f in JSON_FIELDS},
        "min_score": int(p.get("min_score", 7)),
        "active": 1 if p.get("active", True) else 0,
    }
    if pid is None:
        cur = c.execute(
            f"INSERT INTO projects ({', '.join(vals)}, created_at) VALUES ({', '.join('?' * len(vals))}, ?)",
            (*vals.values(), time.time()),
        )
        return cur.lastrowid
    c.execute(f"UPDATE projects SET {', '.join(f'{k} = ?' for k in vals)} WHERE id = ?", (*vals.values(), pid))
    return pid


# ---------------------------------------------------------------------------
# Escaneo
# ---------------------------------------------------------------------------

scan_lock = threading.Lock()
scan_state = {"running": False, "last_error": None}


def use_ai() -> bool:
    return not DEMO and bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def run_scan() -> dict:
    if not scan_lock.acquire(blocking=False):
        return {"skipped": "ya hay un escaneo en marcha"}
    scan_state["running"] = True
    started = time.time()
    fetched = candidates_n = new_leads = 0
    mode = "demo" if DEMO else ("ia" if use_ai() else "palabras clave")
    error = None
    try:
        s = get_settings()
        with db() as c:
            projects = [project_dict(r) for r in c.execute("SELECT * FROM projects WHERE active = 1")]
            seen = {r["post_id"]: r["ts"] for r in c.execute("SELECT post_id, ts FROM seen")}
        if not projects:
            return {"skipped": "no hay proyectos activos"}

        cfg = {
            "products": projects,
            "settings": {
                "model": s["model"], "effort": s["effort"],
                "include_comments": s["include_comments"] == "1",
                "max_llm_posts_per_run": 150,
            },
        }
        since = started - float(s["lookback_hours"]) * 3600
        if DEMO:
            raw = json.loads((HERE / "tests" / "sample_posts.json").read_text(encoding="utf-8"))
            posts = []
            for d in raw:
                d = dict(d)
                age = d.pop("age_min", 30)
                posts.append(radar.Post(**{**d, "created_utc": started - age * 60}))
        else:
            posts = radar.fetch_all(cfg, since)
        fetched = len(posts)

        cands = radar.prefilter(posts, cfg, seen)
        candidates_n = len(cands)
        verdicts = (radar.classify(cands, cfg) if use_ai() else radar.keyword_only(cands)) if cands else []

        by_name = {p["name"]: p for p in projects}
        to_notify = []
        with db() as c:
            for post, v in verdicts:
                c.execute("INSERT OR REPLACE INTO seen(post_id, ts) VALUES (?, ?)", (post.id, post.created_utc))
                proj = by_name.get(v.product) or by_name.get(post.matched_products[0])
                if not (v.is_lead and proj) or v.score < 5:
                    continue
                cur = c.execute(
                    "INSERT OR IGNORE INTO leads (id, project_id, post_id, source, where_, title, body, url,"
                    " author, created_utc, score, reason, suggested_reply, found_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{proj['id']}:{post.id}", proj["id"], post.id, post.source, post.where, post.title,
                     post.body, post.url, post.author, post.created_utc, v.score, v.reason,
                     v.suggested_reply, time.time()),
                )
                if cur.rowcount:
                    new_leads += 1
                    if v.score >= proj["min_score"]:
                        to_notify.append((post, v))
            c.execute("DELETE FROM seen WHERE ts < ?", (started - 14 * 86400,))
        if to_notify and s["notify_telegram"] == "1" and not DEMO:
            radar.notify_telegram(to_notify)
        return {"fetched": fetched, "candidates": candidates_n, "new_leads": new_leads, "mode": mode}
    except Exception as e:  # el escaneo nunca debe tumbar el servidor
        error = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        return {"error": error}
    finally:
        with db() as c:
            c.execute(
                "INSERT INTO scans (started_at, finished_at, fetched, candidates, leads, mode, error)"
                " VALUES (?,?,?,?,?,?,?)",
                (started, time.time(), fetched, candidates_n, new_leads, mode, error),
            )
        scan_state.update(running=False, last_error=error)
        scan_lock.release()


def scheduler() -> None:
    time.sleep(3)
    while True:
        run_scan()
        try:
            minutes = max(5, int(get_settings()["scan_interval_min"]))
        except (KeyError, ValueError):
            minutes = 30
        time.sleep(minutes * 60)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

app = FastAPI(title=APP_NAME)
security = HTTPBasic(auto_error=False)


def auth(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
    pw = os.environ.get("APP_PASSWORD")
    if not pw:
        return
    ok = creds and secrets.compare_digest(creds.username, "admin") and secrets.compare_digest(creds.password, pw)
    if not ok:
        raise HTTPException(401, "Necesitas iniciar sesión", headers={"WWW-Authenticate": "Basic"})


@app.on_event("startup")
def startup() -> None:
    init_db()
    if os.environ.get("DISABLE_SCHEDULER") != "1":
        threading.Thread(target=scheduler, daemon=True).start()


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
def index() -> str:
    html = (HERE / "static" / "index.html").read_text(encoding="utf-8")
    return html.replace("{{APP_NAME}}", APP_NAME)


@app.get("/api/status", dependencies=[Depends(auth)])
def status() -> dict:
    with db() as c:
        scans = [dict(r) for r in c.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 10")]
        counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM leads GROUP BY status")}
    return {
        "app_name": APP_NAME, "demo": DEMO, "ai": use_ai(),
        "reddit_oauth": bool(os.environ.get("REDDIT_CLIENT_ID")),
        "telegram": bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
        "running": scan_state["running"], "scans": scans, "counts": counts,
    }


@app.post("/api/scan", dependencies=[Depends(auth)])
def scan_now() -> dict:
    if scan_state["running"]:
        return {"started": False, "reason": "ya hay un escaneo en marcha"}
    threading.Thread(target=run_scan, daemon=True).start()
    return {"started": True}


@app.get("/api/settings", dependencies=[Depends(auth)])
def read_settings() -> dict:
    return get_settings()


@app.put("/api/settings", dependencies=[Depends(auth)])
def write_settings(body: dict) -> dict:
    with db() as c:
        for k, v in body.items():
            if k in DEFAULT_SETTINGS:
                c.execute("UPDATE settings SET value = ? WHERE key = ?", (str(v), k))
    return get_settings()


class ProjectIn(BaseModel):
    name: str
    url: str = ""
    description: str = ""
    ideal_customer: str = ""
    not_a_fit: str = ""
    keywords: list[str] = []
    negative_keywords: list[str] = []
    subreddits: list[str] = []
    search_queries: list[str] = []
    min_score: int = 7
    active: bool = True


@app.get("/api/projects", dependencies=[Depends(auth)])
def list_projects() -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM leads l WHERE l.project_id = p.id AND l.status = 'new'"
            " AND l.score >= p.min_score) AS new_leads FROM projects p ORDER BY p.name"
        )
        return [project_dict(r) for r in rows]


@app.post("/api/projects", dependencies=[Depends(auth)])
def create_project(p: ProjectIn) -> dict:
    with db() as c:
        try:
            pid = save_project_row(c, p.model_dump())
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Ya existe un proyecto con ese nombre")
    return {"id": pid}


@app.put("/api/projects/{pid}", dependencies=[Depends(auth)])
def update_project(pid: int, p: ProjectIn) -> dict:
    with db() as c:
        if not c.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone():
            raise HTTPException(404, "Proyecto no encontrado")
        try:
            save_project_row(c, p.model_dump(), pid)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Ya existe un proyecto con ese nombre")
    return {"id": pid}


@app.delete("/api/projects/{pid}", dependencies=[Depends(auth)])
def delete_project(pid: int) -> dict:
    with db() as c:
        c.execute("DELETE FROM projects WHERE id = ?", (pid,))
    return {"deleted": pid}


class SetupIn(BaseModel):
    url: str = ""
    description: str = ""


@app.post("/api/projects/suggest", dependencies=[Depends(auth)])
def suggest(body: SetupIn) -> dict:
    if not use_ai():
        raise HTTPException(400, "La autoconfiguración necesita la clave de la API de Claude (ANTHROPIC_API_KEY)")
    if not (body.url or body.description):
        raise HTTPException(400, "Pon la web del producto o una descripción")
    try:
        return radar.suggest_setup(get_settings()["model"], body.url, body.description).model_dump()
    except Exception as e:
        raise HTTPException(502, f"No se pudo generar la configuración: {e}")


@app.get("/api/leads", dependencies=[Depends(auth)])
def list_leads(project_id: int | None = None, status: str = "new", min_score: int | None = None,
               source: str = "", q: str = "", limit: int = 100, offset: int = 0) -> dict:
    where, args = [], []
    if project_id:
        where.append("l.project_id = ?"); args.append(project_id)
    if status != "all":
        where.append("l.status = ?"); args.append(status)
    if min_score is None:
        where.append("l.score >= p.min_score")
    else:
        where.append("l.score >= ?"); args.append(min_score)
    if source:
        where.append("l.source = ?"); args.append(source)
    if q:
        where.append("(l.title LIKE ? OR l.body LIKE ?)"); args += [f"%{q}%", f"%{q}%"]
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    with db() as c:
        total = c.execute(f"SELECT COUNT(*) FROM leads l JOIN projects p ON p.id = l.project_id {sql_where}",
                          args).fetchone()[0]
        rows = c.execute(
            f"SELECT l.*, p.name AS project FROM leads l JOIN projects p ON p.id = l.project_id {sql_where}"
            " ORDER BY l.found_at DESC, l.score DESC LIMIT ? OFFSET ?", (*args, limit, offset)
        )
        return {"total": total, "items": [dict(r) for r in rows]}


class LeadPatch(BaseModel):
    status: str | None = None
    suggested_reply: str | None = None


@app.patch("/api/leads/{lead_id:path}", dependencies=[Depends(auth)])
def patch_lead(lead_id: str, body: LeadPatch) -> dict:
    with db() as c:
        if body.status:
            if body.status not in ("new", "saved", "replied", "dismissed"):
                raise HTTPException(400, "Estado no válido")
            c.execute("UPDATE leads SET status = ? WHERE id = ?", (body.status, lead_id))
        if body.suggested_reply is not None:
            c.execute("UPDATE leads SET suggested_reply = ? WHERE id = ?", (body.suggested_reply, lead_id))
    return {"ok": True}


class ReplyIn(BaseModel):
    tone: str = ""


@app.post("/api/leads/{lead_id:path}/reply", dependencies=[Depends(auth)])
def regenerate_reply(lead_id: str, body: ReplyIn) -> dict:
    if not use_ai():
        raise HTTPException(400, "Generar respuestas necesita la clave de la API de Claude (ANTHROPIC_API_KEY)")
    with db() as c:
        lead = c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not lead:
            raise HTTPException(404, "Lead no encontrado")
        proj = project_dict(c.execute("SELECT * FROM projects WHERE id = ?", (lead["project_id"],)).fetchone())
    try:
        reply = radar.write_reply(get_settings()["model"], proj, dict(lead), body.tone)
    except Exception as e:
        raise HTTPException(502, f"No se pudo generar la respuesta: {e}")
    with db() as c:
        c.execute("UPDATE leads SET suggested_reply = ? WHERE id = ?", (reply, lead_id))
    return {"suggested_reply": reply}
