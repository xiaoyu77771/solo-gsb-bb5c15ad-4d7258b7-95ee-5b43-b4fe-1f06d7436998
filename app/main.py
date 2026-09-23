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

from .models import ScenarioIn, TemplateIn, TemplatePreviewIn, TemplateInstantiateIn
from . import scheduler, templating

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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS templates (
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


# ---------------------------------------------------------------------------
# 场景模板（含变量；预览解析、实例化、模板导出）
# ---------------------------------------------------------------------------

def _resolve_or_422(template_data: dict, params: dict) -> dict:
    try:
        outcome = templating.resolve(template_data, params)
    except templating.TemplateError as exc:
        raise HTTPException(status_code=422, detail=f"模板结构有误：{exc}")
    if outcome["scenario"] is None:
        raise HTTPException(status_code=422, detail={
            "message": "模板解析失败，请检查变量参数与表达式",
            "issues": outcome["issues"],
        })
    return outcome


@app.get("/api/templates")
def list_templates() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, updated_at FROM templates ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/templates/{template_id}")
def get_template(template_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return {
        "id": row["id"],
        "name": row["name"],
        "data": json.loads(row["data"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.post("/api/templates", status_code=201)
def save_template(tpl: TemplateIn) -> dict:
    # 保存前先做一次结构校验
    try:
        templating.normalize_template(tpl.data)
    except templating.TemplateError as exc:
        raise HTTPException(status_code=422, detail=f"模板结构有误：{exc}")
    name = tpl.name.strip()
    payload = json.dumps(tpl.data, ensure_ascii=False)
    now = _now()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM templates WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE templates SET data = ?, updated_at = ? WHERE id = ?",
                (payload, now, existing["id"]),
            )
            return {"id": existing["id"], "name": name, "updated": True}
        cur = conn.execute(
            "INSERT INTO templates (name, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, payload, now, now),
        )
        return {"id": cur.lastrowid, "name": name, "updated": False}


@app.put("/api/templates/{template_id}")
def update_template(template_id: int, tpl: TemplateIn) -> dict:
    try:
        templating.normalize_template(tpl.data)
    except templating.TemplateError as exc:
        raise HTTPException(status_code=422, detail=f"模板结构有误：{exc}")
    with get_db() as conn:
        row = conn.execute("SELECT id FROM templates WHERE id = ?", (template_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="模板不存在")
        conn.execute(
            "UPDATE templates SET name = ?, data = ?, updated_at = ? WHERE id = ?",
            (tpl.name.strip(), json.dumps(tpl.data, ensure_ascii=False),
             _now(), template_id),
        )
    return {"id": template_id, "updated": True}


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: int) -> dict:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="模板不存在")
    return {"deleted": template_id}


@app.post("/api/templates/preview")
def preview_template(req: TemplatePreviewIn) -> dict:
    """用给定参数解析模板并立即排程（不落库）。error 以 422 返回问题明细。"""
    outcome = _resolve_or_422(req.data, req.params)
    try:
        result = scheduler.solve(outcome["scenario"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"实例校验失败：{exc}")
    return {
        "issues": outcome["issues"],
        "resolved": outcome["resolved"],
        "scenario": outcome["scenario"],
        "result": result,
    }


def _state_from_scenario(scenario: dict, uid_seed: int = 1) -> dict:
    """把解析后的具体场景转换成前端编辑器 state 格式。"""
    return {
        "windowStart": scenario["window_start"],
        "windowEnd": scenario["window_end"],
        "resources": [
            {"name": name, "capacity": cap}
            for name, cap in scenario.get("resources", {}).items()
        ],
        "nodes": [
            {
                "id": n["id"], "name": n.get("name", n["id"]),
                "duration_minutes": n["duration_minutes"],
                "resources": n.get("resources", {}),
                "fixed_start": n.get("fixed_start"),
            }
            for n in scenario["nodes"]
        ],
        "edges": [dict(e) for e in scenario.get("edges", [])],
        "blackouts": [
            {"id": f"b{idx + 1}", "name": b.get("name", f"禁排期{idx + 1}"),
             "start": b["start"], "end": b["end"]}
            for idx, b in enumerate(scenario.get("blackouts", []))
        ],
        "uid": uid_seed,
    }


def _unique_scenario_name(conn: sqlite3.Connection, name: str) -> str:
    base_name = name.strip() or "模板实例"
    candidate = base_name
    k = 2
    while conn.execute(
        "SELECT 1 FROM scenarios WHERE name = ?", (candidate,)
    ).fetchone():
        candidate = f"{base_name} ({k})"
        k += 1
    return candidate


@app.post("/api/templates/instantiate", status_code=201)
def instantiate_template(req: TemplateInstantiateIn) -> dict:
    """解析模板 → 排程 → 保存为独立场景。返回完整编辑器 state 与排程结果。"""
    if req.template_id is not None:
        with get_db() as conn:
            row = conn.execute(
                "SELECT data FROM templates WHERE id = ?", (req.template_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="模板不存在")
        template_data = json.loads(row["data"])
        template_ref = {"id": req.template_id}
    elif req.data is not None:
        template_data = req.data
        template_ref = {"id": None}
    else:
        raise HTTPException(status_code=422, detail="必须提供 template_id 或内联模板 data")

    outcome = _resolve_or_422(template_data, req.params)
    try:
        result = scheduler.solve(outcome["scenario"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"实例校验失败：{exc}")

    state = _state_from_scenario(outcome["scenario"])
    state["templateRef"] = {
        "id": template_ref["id"],
        "name": req.name,
        "template": template_data,
        "params": outcome["params"],
        "resolved": outcome["resolved"],
        "issues": outcome["issues"],
    }

    now = _now()
    with get_db() as conn:
        final_name = _unique_scenario_name(conn, req.name)
        cur = conn.execute(
            "INSERT INTO scenarios (name, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (final_name, json.dumps(state, ensure_ascii=False), now, now),
        )
        scenario_id = cur.lastrowid

    return {
        "id": scenario_id,
        "name": final_name,
        "data": state,
        "result": result,
        "resolved": outcome["resolved"],
        "issues": outcome["issues"],
    }


@app.post("/api/templates/export")
def export_template(req: TemplatePreviewIn) -> FileResponse:
    """解析模板并导出 JSON：模板定义 + 参数 + 解析结果 + 实例场景 + 排程结果。"""
    outcome = _resolve_or_422(req.data, req.params)
    try:
        result = scheduler.solve(outcome["scenario"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"实例校验失败：{exc}")

    export = {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": "template",
        "template": req.data,
        "params": outcome["params"],
        "resolved": outcome["resolved"],
        "issues": outcome["issues"],
        "scenario": outcome["scenario"],
        "result": result,
    }
    stamp = f"{datetime.now():%Y%m%d%H%M%S}"
    tmp = Path("/tmp") / f"maintenance-template-{stamp}.json"
    tmp.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    return FileResponse(
        tmp, media_type="application/json",
        filename=f"maintenance-template-{stamp}.json",
    )


@app.post("/api/export")
def export_schedule(payload: dict) -> FileResponse:
    """计算排程并以 JSON 文件下载（同时包含输入场景与求解结果）。

    若载荷携带 ``__template__ / __params__ / __resolved__ / __issues__``（模板实例），
    导出文件中额外包含模板定义、输入参数与解析后的参数值。
    """
    template_meta = {
        "template": payload.pop("__template__", None),
        "params": payload.pop("__params__", None),
        "resolved": payload.pop("__resolved__", None),
        "issues": payload.pop("__issues__", None),
    }
    try:
        result = scheduler.solve(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    export = {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scenario": payload,
        "result": result,
    }
    if template_meta["template"] is not None:
        export["kind"] = "template_instance"
        export.update({k: v for k, v in template_meta.items() if v is not None})

    stamp = f"{datetime.now():%Y%m%d%H%M%S}"
    prefix = "maintenance-template-instance" if "kind" in export else "maintenance-schedule"
    tmp = Path("/tmp") / f"{prefix}-{stamp}.json"
    tmp.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    return FileResponse(
        tmp, media_type="application/json",
        filename=f"{prefix}-{stamp}.json",
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
