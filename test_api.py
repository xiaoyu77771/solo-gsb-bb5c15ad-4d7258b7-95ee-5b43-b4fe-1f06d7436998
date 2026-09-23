"""API 层集成测试：以最小桩替代 fastapi/pydantic，直接调用 main.py 的端点函数。

覆盖：模板 CRUD、校验、预览、实例化、模板实例导出、普通导出、场景保存。
运行：python test_api.py
"""
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, ".")

# ---------------------------------------------------------------------------
# 最小桩：fastapi / pydantic（仅满足 main.py 导入与调用）
# ---------------------------------------------------------------------------

fastapi = types.ModuleType("fastapi")


class HTTPException(Exception):
    def __init__(self, status_code, detail=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class FastAPI:
    def __init__(self, **kw):
        self.routes = {}

    def _reg(self, method):
        def deco(path, **kw):
            def wrap(fn):
                self.routes[(method, path)] = fn
                return fn
            return wrap
        return deco

    def get(self, path, **kw):
        return self._reg("GET")(path, **kw)

    def post(self, path, **kw):
        return self._reg("POST")(path, **kw)

    def put(self, path, **kw):
        return self._reg("PUT")(path, **kw)

    def delete(self, path, **kw):
        return self._reg("DELETE")(path, **kw)

    def mount(self, *a, **kw):
        pass

    def on_event(self, *a, **kw):
        return lambda f: f


fastapi.FastAPI = FastAPI
fastapi.HTTPException = HTTPException

responses = types.ModuleType("fastapi.responses")


class FileResponse:
    def __init__(self, path, media_type=None, filename=None):
        self.path = str(path)
        self.filename = filename


class JSONResponse(dict):
    pass


class RedirectResponse:
    def __init__(self, url, **kw):
        self.url = url


responses.FileResponse = FileResponse
responses.JSONResponse = JSONResponse
responses.RedirectResponse = RedirectResponse

staticfiles = types.ModuleType("fastapi.staticfiles")


class StaticFiles:
    def __init__(self, **kw):
        pass


staticfiles.StaticFiles = StaticFiles

pydantic = types.ModuleType("pydantic")


class BaseModel:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def Field(default=None, default_factory=None, **kw):
    return default if default_factory is None else default_factory()


pydantic.BaseModel = BaseModel
pydantic.Field = Field

sys.modules["fastapi"] = fastapi
sys.modules["fastapi.responses"] = responses
sys.modules["fastapi.staticfiles"] = staticfiles
sys.modules["pydantic"] = pydantic

# ---------------------------------------------------------------------------
# 隔离数据目录后导入应用
# ---------------------------------------------------------------------------

tmpdir = tempfile.mkdtemp(prefix="mwtest-")
os.environ["MW_DATA_DIR"] = tmpdir

from app import main  # noqa: E402
from app.models import TemplateIn, TemplateApplyIn, ScenarioIn  # noqa: E402

main.init_db()

passed = 0


def check(name, cond, extra=""):
    global passed
    assert cond, f"FAIL: {name} {extra}"
    passed += 1
    print(f"  ok - {name}")


TPL = {
    "name": "夜间发布模板",
    "description": "测试模板",
    "variables": [
        {"name": "base", "type": "base_date", "label": "基准日期",
         "default": "2026-09-23T22:00"},
        {"name": "upg", "type": "duration", "label": "升级时长", "default": 60},
        {"name": "gap", "type": "offset", "label": "切换偏移", "default": 15},
    ],
    "scenario": {
        "window_start": "base",
        "window_end": "base + 8h",
        "resources": {"DBA": 1},
        "nodes": [
            {"id": "a", "name": "切换", "duration_minutes": 30,
             "resources": {"DBA": 1}},
            {"id": "b", "name": "升级", "duration_minutes": "upg",
             "resources": {"DBA": 1}, "fixed_start": "a.end + gap"},
            {"id": "c", "name": "验证", "duration_minutes": "upg + 30m",
             "resources": {}},
        ],
        "edges": [{"source": "b", "target": "c"}],
        "blackouts": [
            {"name": "计费", "start": "base + 2h", "end": "base + 2h + 30m"}],
    },
}


print("1. 模板 CRUD")
r = main.save_template(TemplateIn(**TPL))
check("新建模板", r["id"] and r["updated"] is False, r)
tid = r["id"]
r2 = main.save_template(TemplateIn(**TPL))
check("同名 upsert", r2["id"] == tid and r2["updated"] is True, r2)
lst = main.list_templates()
check("列表 1 条且含变量数", len(lst) == 1 and lst[0]["var_count"] == 3, lst)
got = main.get_template(tid)
check("取回定义", got["name"] == "夜间发布模板" and len(got["variables"]) == 3)

upd = dict(TPL, description="改")
r3 = main.update_template(tid, TemplateIn(**upd))
check("PUT 更新", r3["updated"] is True)
check("描述已更新", main.get_template(tid)["description"] == "改")

print("2. 非法模板保存被拒")
bad = json.loads(json.dumps(TPL))
bad["scenario"]["edges"].append({"source": "b", "target": "a"})
try:
    main.save_template(TemplateIn(**bad))
    raise AssertionError("应 422")
except HTTPException as e:
    check("422 + dependency_cycle", e.status_code == 422
          and e.detail["code"] == "dependency_cycle", e.detail)

print("3. 校验接口")
v = main.validate_template(TemplateIn(**TPL))
check("valid", v["valid"] is True and v["variables"] == 3, v)
v2 = main.validate_template(TemplateIn(**bad))
check("invalid 返回错误结构", v2["valid"] is False
      and v2["error"]["code"] == "dependency_cycle", v2)

print("4. 预览（含排程结果）")
pv = main.preview_template(TemplateApplyIn(
    template_id=tid, params={"upg": 30, "gap": 5}, instance_name="预演A"))
check("实例名", pv["instance_name"] == "预演A")
check("场景窗口", pv["scenario"]["window_start"] == "2026-09-23T22:00")
check("解析变量 3 个", len(pv["resolved"]["variables"]) == 3)
check("排程结果可行", pv["result"]["feasible"] is True, pv["result"]["conflicts"])
check("隐含边记录", len(pv["resolved"]["implicit_edges"]) == 1)

print("5. 预览：解析期错误 422")
try:
    main.preview_template(TemplateApplyIn(
        template_id=tid, params={"upg": 9999}))
    raise AssertionError("应 422")
except HTTPException as e:
    check("越界 422", e.status_code == 422
          and e.detail["code"] == "fixed_out_of_window", e.detail)

print("6. 实例化（内联模板定义）")
inline = {k: TPL[k] for k in ("name", "description", "variables", "scenario")}
inst = main.instantiate_template(TemplateApplyIn(
    template=inline,
    params={"upg": 45}, instance_name="实例B"))
check("实例名", inst["instance_name"] == "实例B")
nodes = {n["id"]: n for n in inst["scenario"]["nodes"]}
check("时长 45", nodes["b"]["duration_minutes"] == 45)
check("模板回传", inst["template"]["name"] == "夜间发布模板")

print("7. 实例化：未知模板 404")
try:
    main.instantiate_template(TemplateApplyIn(template_id=999))
    raise AssertionError("应 404")
except HTTPException as e:
    check("404", e.status_code == 404)

print("8. 模板实例导出（封包）")
bundle = {
    "scenario": inst["scenario"],
    "resolved": inst["resolved"],
    "template": inst["template"],
    "instance_name": "实例B",
}
resp = main.export_schedule(bundle)
check("返回文件", resp.filename.startswith("maintenance-template-instance-"))
data = json.loads(open(resp.path, encoding="utf-8").read())
check("kind=template_instance", data["kind"] == "template_instance")
check("含模板", data["template"]["name"] == "夜间发布模板")
check("含解析参数", len(data["resolved"]["variables"]) == 3)
check("含场景与结果", data["scenario"]["window_start"] and "items" in data["result"])

print("9. 普通场景导出（向后兼容）")
plain = {
    "window_start": "2026-09-23T22:00", "window_end": "2026-09-24T06:00",
    "resources": {"DBA": 1},
    "nodes": [{"id": "a", "name": "A", "duration_minutes": 30,
               "resources": {"DBA": 1}}],
    "edges": [], "blackouts": [],
}
resp = main.export_schedule(plain)
check("普通文件名", resp.filename.startswith("maintenance-schedule-"))
data = json.loads(open(resp.path, encoding="utf-8").read())
check("kind=scenario", data["kind"] == "scenario")
check("无 template 键", "template" not in data)

print("10. 场景保存（含 templateContext 透传）")
sc = main.save_scenario(ScenarioIn(name="实例场景", data={
    "windowStart": "2026-09-23T22:00", "windowEnd": "2026-09-24T06:00",
    "templateContext": {"template": inst["template"], "instance_name": "实例B",
                        "resolved": inst["resolved"]},
}))
got = main.get_scenario(sc["id"])
check("templateContext 持久化",
      got["data"]["templateContext"]["instance_name"] == "实例B")

print("11. 模板删除")
main.delete_template(tid)
check("删除后列表为空", main.list_templates() == [])
try:
    main.get_template(tid)
    raise AssertionError("应 404")
except HTTPException as e:
    check("删除后 404", e.status_code == 404)

print(f"\n全部 {passed} 项检查通过 ✔（数据目录 {tmpdir}）")
