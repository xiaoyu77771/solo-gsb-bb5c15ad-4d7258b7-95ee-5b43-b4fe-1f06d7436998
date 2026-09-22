"""FastAPI 入口：排程接口、场景存取（SQLite）、JSON 导出、静态页面。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .models import ScenarioIn
from . import scheduler

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MW_DATA_DIR", "/data"))
DB_PATH = Path(os.environ.get("MW_DB_PATH", DATA_DIR / "maintenance.db"))

app = FastAPI(title="维护窗口排程", version="1.0.0")


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenarios (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                data        TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
            """
        )


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "db": str(DB_PATH)}


@app.post("/api/schedule")
def schedule(payload: dict) -> dict:
    """根据当前编辑中的 DAG/约束立即计算排程。"""
    try:
        return scheduler.solve(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/api/scenarios")
def list_scenarios() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, updated_at FROM scenarios ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/scenarios/{scenario_id}")
def get_scenario(scenario_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM scenarios WHERE id = ?", (scenario_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="场景不存在")
    return {
        "id": row["id"],
        "name": row["name"],
        "data": json.loads(row["data"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.post("/api/scenarios", status_code=201)
def save_scenario(scenario: ScenarioIn) -> dict:
    name = scenario.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="场景名称不能为空")
    payload = json.dumps(scenario.data, ensure_ascii=False)
    now = _now()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM scenarios WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE scenarios SET data = ?, updated_at = ? WHERE id = ?",
                (payload, now, existing["id"]),
            )
            return {"id": existing["id"], "name": name, "updated": True}
        cur = conn.execute(
            "INSERT INTO scenarios (name, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, payload, now, now),
        )
        return {"id": cur.lastrowid, "name": name, "updated": False}


@app.put("/api/scenarios/{scenario_id}")
def update_scenario(scenario_id: int, scenario: ScenarioIn) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT id FROM scenarios WHERE id = ?", (scenario_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="场景不存在")
        conn.execute(
            "UPDATE scenarios SET name = ?, data = ?, updated_at = ? WHERE id = ?",
            (scenario.name.strip(), json.dumps(scenario.data, ensure_ascii=False),
             _now(), scenario_id),
        )
    return {"id": scenario_id, "updated": True}


@app.delete("/api/scenarios/{scenario_id}")
def delete_scenario(scenario_id: int) -> dict:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM scenarios WHERE id = ?", (scenario_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="场景不存在")
    return {"deleted": scenario_id}


@app.post("/api/export")
def export_schedule(payload: dict) -> FileResponse:
    """计算排程并以 JSON 文件下载（同时包含输入场景与求解结果）。"""
    try:
        result = scheduler.solve(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    export = {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scenario": payload,
        "result": result,
    }
    tmp = Path("/tmp") / f"maintenance-schedule-{datetime.now():%Y%m%d%H%M%S}.json"
    tmp.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    return FileResponse(
        tmp, media_type="application/json",
        filename=f"maintenance-schedule-{datetime.now():%Y%m%d%H%M%S}.json",
    )


# ---------------------------------------------------------------------------
# 静态页面
# ---------------------------------------------------------------------------

@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/static/index.html")


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    init_db()
