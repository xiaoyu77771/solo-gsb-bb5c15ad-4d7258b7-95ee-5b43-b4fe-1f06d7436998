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

from .models import ScenarioIn, TemplateIn, TemplateApplyIn
from . import scheduler
from . import templating
from .templating import TemplateError

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
                description TEXT NOT NULL DEFAULT '',
                definition  TEXT NOT NULL,
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
    """计算排程并以 JSON 文件下载。

    - 普通场景：{window_start,...}，导出 scenario + result；
    - 模板实例：{scenario, resolved, template, instance_name}
      （由 /api/templates/instantiate 返回），额外写入模板定义与解析后的参数。
    """
    bundle = _as_template_bundle(payload)
    if bundle is not None:
        scenario, resolved, template, instance_name = bundle
        try:
            result = scheduler.solve(scenario)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        export = {
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": "template_instance",
            "instance_name": instance_name,
            "scenario": scenario,
            "result": result,
            "resolved": resolved,
            "template": template,
        }
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        filename = f"maintenance-template-instance-{stamp}.json"
    else:
        try:
            result = scheduler.solve(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        export = {
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": "scenario",
            "scenario": payload,
            "result": result,
        }
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        filename = f"maintenance-schedule-{stamp}.json"

    tmp = Path("/tmp") / filename
    tmp.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    return FileResponse(tmp, media_type="application/json", filename=filename)


def _as_template_bundle(payload: dict):
    """识别模板实例封包；返回 (scenario, resolved, template, instance_name) 或 None。"""
    if not isinstance(payload, dict):
        return None
    scenario = payload.get("scenario")
    if not isinstance(scenario, dict) or "window_start" not in scenario:
        return None
    if "resolved" not in payload or "template" not in payload:
        return None
    return (
        scenario,
        payload.get("resolved") or {},
        payload.get("template") or {},
        str(payload.get("instance_name") or
            (payload.get("template") or {}).get("name") or "模板实例"),
    )


# ---------------------------------------------------------------------------
# 场景模板
# ---------------------------------------------------------------------------

def _template_row_to_definition(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        **json.loads(row["definition"]),
    }


def _load_template_definition(conn, template_id: int | None,
                              inline: dict | None) -> dict:
    if template_id is not None:
        row = conn.execute(
            "SELECT definition FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="模板不存在")
        return json.loads(row["definition"])
    if isinstance(inline, dict):
        return inline
    raise HTTPException(status_code=422,
                        detail="需要 template_id 或 template 定义之一")


@app.get("/api/templates")
def list_templates() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, description, definition, created_at, updated_at "
            "FROM templates ORDER BY updated_at DESC"
        ).fetchall()
    items = []
    for r in rows:
        definition = json.loads(r["definition"])
        items.append({
            "id": r["id"],
            "name": r["name"],
            "description": r["description"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "var_count": len(definition.get("variables", [])),
        })
    return items


@app.get("/api/templates/{template_id}")
def get_template(template_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return _template_row_to_definition(row)


@app.post("/api/templates", status_code=201)
def save_template(template: TemplateIn) -> dict:
    definition = {
        "name": template.name.strip(),
        "description": template.description.strip(),
        "variables": template.variables,
        "scenario": template.scenario,
    }
    # 保存前用默认参数干跑一次，保证模板结构合法、可解析
    try:
        templating.instantiate(definition, {})
    except TemplateError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "code": exc.code, "path": exc.path},
        )

    name = definition["name"]
    payload = json.dumps(definition, ensure_ascii=False)
    now = _now()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM templates WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE templates SET description = ?, definition = ?, updated_at = ? "
                "WHERE id = ?",
                (definition["description"], payload, now, existing["id"]),
            )
            return {"id": existing["id"], "name": name, "updated": True}
        cur = conn.execute(
            "INSERT INTO templates (name, description, definition, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, definition["description"], payload, now, now),
        )
        return {"id": cur.lastrowid, "name": name, "updated": False}


@app.put("/api/templates/{template_id}")
def update_template(template_id: int, template: TemplateIn) -> dict:
    definition = {
        "name": template.name.strip(),
        "description": template.description.strip(),
        "variables": template.variables,
        "scenario": template.scenario,
    }
    try:
        templating.instantiate(definition, {})
    except TemplateError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "code": exc.code, "path": exc.path},
        )
    with get_db() as conn:
        row = conn.execute("SELECT id FROM templates WHERE id = ?",
                           (template_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="模板不存在")
        conn.execute(
            "UPDATE templates SET name = ?, description = ?, definition = ?, "
            "updated_at = ? WHERE id = ?",
            (definition["name"], definition["description"],
             json.dumps(definition, ensure_ascii=False), _now(), template_id),
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
def preview_template(body: TemplateApplyIn) -> dict:
    """用给定参数解析模板（不落库），并立即交给排程器计算，用于实例化前预览。"""
    with get_db() as conn:
        definition = _load_template_definition(conn, body.template_id, body.template)
    try:
        bundle = templating.instantiate(definition, body.params, body.instance_name)
    except TemplateError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "code": exc.code, "path": exc.path},
        )
    result = scheduler.solve(bundle["scenario"])
    return {
        "instance_name": bundle["instance_name"],
        "scenario": bundle["scenario"],
        "resolved": bundle["resolved"],
        "template": bundle["template"],
        "result": result,
    }


@app.post("/api/templates/instantiate")
def instantiate_template(body: TemplateApplyIn) -> dict:
    """仅做变量解析与校验，返回可独立排程的具体场景（不自动保存场景）。"""
    with get_db() as conn:
        definition = _load_template_definition(conn, body.template_id, body.template)
    try:
        return templating.instantiate(definition, body.params, body.instance_name)
    except TemplateError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "code": exc.code, "path": exc.path},
        )


@app.post("/api/templates/validate")
def validate_template(body: TemplateIn) -> dict:
    """校验模板定义并用默认参数干跑（编辑器保存前的即时检查）。"""
    definition = {
        "name": body.name.strip(),
        "description": body.description.strip(),
        "variables": body.variables,
        "scenario": body.scenario,
    }
    try:
        bundle = templating.instantiate(definition, {})
    except TemplateError as exc:
        return {"valid": False, "error": {"message": str(exc),
                                         "code": exc.code, "path": exc.path}}
    return {"valid": True,
            "variables": len(bundle["template"]["variables"]),
            "window": bundle["resolved"]["window"]}



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
