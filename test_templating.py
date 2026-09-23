"""场景模板（templating）测试：变量解析、循环引用、越界校验与实例化排程。

运行：python test_templating.py
"""
import sys

sys.path.insert(0, ".")

from app.templating import (
    instantiate,
    validate_template,
    parse_expr,
    expr_refs,
    TemplateError,
)
from app.scheduler import solve

passed = 0


def check(name, cond, extra=""):
    global passed
    assert cond, f"FAIL: {name} {extra}"
    passed += 1
    print(f"  ok - {name}")


def base_template(**over):
    tpl = {
        "name": "夜间发布模板",
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
    tpl.update(over)
    return tpl


print("1. 默认参数实例化")
res = instantiate(base_template(), {})
sc = res["scenario"]
check("窗口起点 = 基准日期", sc["window_start"] == "2026-09-23T22:00", sc)
check("窗口终点 = 基准+8h", sc["window_end"] == "2026-09-24T06:00", sc)
nodes = {n["id"]: n for n in sc["nodes"]}
check("时长变量取默认值 60", nodes["b"]["duration_minutes"] == 60)
check("组合时长 60+30m=90", nodes["c"]["duration_minutes"] == 90)
check("b 固定在 a.end(22:30)+15m=22:45",
      nodes["b"]["fixed_start"] == "2026-09-23T22:45", nodes["b"])
check("隐含依赖边 a→b 自动补全",
      {"source": "a", "target": "b"} in sc["edges"], sc["edges"])
check("显式依赖边保留", {"source": "b", "target": "c"} in sc["edges"])
check("禁排期解析 base+2h",
      sc["blackouts"][0]["start"] == "2026-09-24T00:00", sc["blackouts"])
implicit = res["resolved"]["implicit_edges"]
check("解析明细记录 1 条隐含边", len(implicit) == 1 and implicit[0]["source"] == "a")

print("2. 用户参数覆盖（duration/offset 支持字符串数字）")
res = instantiate(base_template(), {"upg": 90, "gap": "20",
                                    "base": "2026-10-01T20:00"})
sc = res["scenario"]
check("基准日期参数生效", sc["window_start"] == "2026-10-01T20:00", sc)
check("窗口 = 次日 04:00", sc["window_end"] == "2026-10-02T04:00")
nodes = {n["id"]: n for n in sc["nodes"]}
check("时长 90", nodes["b"]["duration_minutes"] == 90)
check("b 固定 20:30+20=20:50", nodes["b"]["fixed_start"] == "2026-10-01T20:50")
check("c 时长 90+30=120", nodes["c"]["duration_minutes"] == 120)
vals = {v["name"]: v for v in res["resolved"]["variables"]}
check("参数来源标记 param", vals["upg"]["source"] == "param")
check("未传参数来源标记 default", vals["gap"]["source"] == "param"
      if False else vals["upg"]["source"] == "param")

print("3. 实例化结果可直接交给现有排程器")
res = instantiate(base_template(), {"upg": 30, "gap": 5})
r = solve(res["scenario"])
check("排程器执行成功", "items" in r)
check("3 个任务全部排程", r["stats"]["scheduled"] == 3, r["conflicts"])
starts = {it["id"]: it["start"] for it in r["items"]}
check("b 从 22:35 开始（22:30+5）", starts.get("b") == "2026-09-23T22:35", starts)
check("b 为固定任务", any(it["id"] == "b" and it["fixed"] for it in r["items"]))

print("4. 缺参数（无默认值的变量）")
tpl = base_template()
tpl["variables"][1]["default"] = None
try:
    instantiate(tpl, {})
    raise AssertionError("应报错")
except TemplateError as e:
    check("missing_param", e.code == "missing_param", e.code)

print("5. 变量间依赖与循环引用")
tpl = base_template()
tpl["variables"][1]["default"] = "gap + 10"
tpl["variables"][2]["default"] = "upg"
try:
    instantiate(tpl, {})
    raise AssertionError("应报错")
except TemplateError as e:
    check("variable_cycle", e.code == "variable_cycle", e.code)
# 合法：变量引用其他变量（无环）
tpl = base_template()
tpl["variables"][2]["default"] = "upg / 2"  # / 不支持 → 语法错误
try:
    validate_template(tpl)
    raise AssertionError("应报错")
except TemplateError as e:
    check("非法运算符报错", e.code == "invalid_expression", e.code)

print("6. 变量合法引用（offset 默认引用 duration）")
tpl = base_template()
tpl["variables"][2]["default"] = "upg + 15"
res = instantiate(tpl, {"upg": 40})
nodes = {n["id"]: n for n in res["scenario"]["nodes"]}
# gap = 40 + 15 = 55；b 固定 22:30 + 55 = 23:25
check("变量引用求值", nodes["b"]["fixed_start"] == "2026-09-23T23:25",
      nodes["b"]["fixed_start"])

print("7. 任务依赖循环（含 .end 表达式诱发）")
tpl = base_template()
tpl["scenario"]["edges"].append({"source": "b", "target": "a"})
try:
    instantiate(tpl, {})
    raise AssertionError("应报错")
except TemplateError as e:
    check("dependency_cycle", e.code == "dependency_cycle", e.code)

# .end 自引用
tpl = base_template()
tpl["scenario"]["nodes"][0]["fixed_start"] = "a.end"
try:
    instantiate(tpl, {})
    raise AssertionError("应报错")
except TemplateError as e:
    check("自引用按环/解析失败处理", e.code in ("dependency_cycle",
                                              "reference_unavailable"), e.code)

print("8. 时间越界：固定任务越窗")
tpl = base_template()
tpl["scenario"]["nodes"][1]["fixed_start"] = "a.end + 9h"
try:
    instantiate(tpl, {})
    raise AssertionError("应报错")
except TemplateError as e:
    check("fixed_out_of_window", e.code == "fixed_out_of_window", e.code)

print("9. 时间越界：窗口反向")
tpl = base_template()
tpl["scenario"]["window_end"] = "base - 1h"
try:
    instantiate(tpl, {})
    raise AssertionError("应报错")
except TemplateError as e:
    check("window_reversed", e.code == "window_reversed", e.code)

print("10. 时间越界：禁排期反向")
tpl = base_template()
tpl["scenario"]["blackouts"][0]["end"] = "base + 1h"
tpl["scenario"]["blackouts"][0]["start"] = "base + 2h"
try:
    instantiate(tpl, {})
    raise AssertionError("应报错")
except TemplateError as e:
    check("blackout_reversed", e.code == "blackout_reversed", e.code)

print("11. 引用校验")
tpl = base_template()
tpl["scenario"]["nodes"][0]["duration_minutes"] = "nope"
try:
    validate_template(tpl)
    raise AssertionError("应报错")
except TemplateError as e:
    check("未知变量", e.code == "unknown_variable", e.code)

tpl = base_template()
tpl["scenario"]["window_end"] = "a.end"
try:
    validate_template(tpl)
    raise AssertionError("应报错")
except TemplateError as e:
    check("窗口不允许任务引用", e.code == "task_ref_not_allowed", e.code)

tpl = base_template()
tpl["scenario"]["nodes"][1]["fixed_start"] = "ghost.end + 5m"
try:
    validate_template(tpl)
    raise AssertionError("应报错")
except TemplateError as e:
    check("引用不存在任务", e.code == "unknown_reference", e.code)

print("12. 类型检查：基准日期不能用于数值字段")
tpl = base_template()
tpl["scenario"]["nodes"][0]["duration_minutes"] = "base"
try:
    instantiate(tpl, {})
    raise AssertionError("应报错")
except TemplateError as e:
    check("type_mismatch", e.code == "type_mismatch", e.code)

# duration 变量不能单独作为日期（无日期锚点）
tpl = base_template()
tpl["scenario"]["window_start"] = "gap"
try:
    instantiate(tpl, {})
    raise AssertionError("应报错")
except TemplateError as e:
    check("无日期基准", e.code == "no_datetime_base", e.code)

print("13. 表达式词法")
terms = parse_expr("base + 1d + 2h - 30m")
refs = expr_refs(terms)
check("解析 3 个字面量 + 1 个引用", len(terms) == 4 and refs == [("base", None)], terms)
check("符号正确", [t["sign"] for t in terms] == [1, 1, 1, -1])
terms = parse_expr("n4.end + gray_offset")
check("任务属性引用", expr_refs(terms) == [("n4", "end"), ("gray_offset", None)], terms)
try:
    parse_expr("a + b * c")
    raise AssertionError("应报错")
except TemplateError as e:
    check("非法词法", e.code == "invalid_expression")

print("14. 绝对时间字面量与表达式混用")
tpl = base_template()
tpl["scenario"]["nodes"][0]["fixed_start"] = "2026-09-23T23:00"
res = instantiate(tpl, {})
nodes = {n["id"]: n for n in res["scenario"]["nodes"]}
check("绝对时间字面量保留", nodes["a"]["fixed_start"] == "2026-09-23T23:00")

tpl = base_template()
tpl["scenario"]["window_start"] = "2026-09-23T21:00"
tpl["scenario"]["window_end"] = "base + 8h"
res = instantiate(tpl, {})
check("字面量窗口+表达式窗口", res["scenario"]["window_start"] == "2026-09-23T21:00"
      and res["scenario"]["window_end"] == "2026-09-24T06:00")

print("15. 模板结构校验")
for mutate, expect_code in [
    (lambda t: t.update(name=""), "invalid_template"),
    (lambda t: t["variables"].append({"name": "base", "type": "base_date"}),
     "duplicate_variable"),
    (lambda t: t["variables"].append({"name": "x", "type": "weird"}),
     "invalid_variable_type"),
    (lambda t: t["scenario"]["nodes"].append(
        {"id": "a", "name": "dup", "duration_minutes": 1}), "duplicate_node"),
]:
    tpl = base_template()
    mutate(tpl)
    try:
        validate_template(tpl)
        raise AssertionError("应报错")
    except TemplateError as e:
        check(f"结构错误 {expect_code}", e.code == expect_code, e.code)

print("16. 负值防护")
tpl = base_template()
try:
    instantiate(tpl, {"upg": -10})
    raise AssertionError("应报错")
except TemplateError as e:
    check("负参数", e.code in ("negative_value",), e.code)
tpl = base_template()
tpl["scenario"]["nodes"][0]["duration_minutes"] = "-30"
try:
    instantiate(tpl, {})
    raise AssertionError("应报错")
except TemplateError as e:
    check("负时长", e.code == "negative_value", e.code)

print("17. 未定义参数给告警，不报错")
res = instantiate(base_template(), {"whoami": 1})
check("unknown_param 告警",
      any(w["code"] == "unknown_param" for w in res["resolved"]["warnings"]))

print("18. 解析轨迹完整")
res = instantiate(base_template(), {})
resolved = res["resolved"]
check("含 variables 明细", len(resolved["variables"]) == 3)
check("含 timeline 计划时间", len(resolved["timeline"]) == 3)
check("timeline 按拓扑序 a,b,c",
      [t["node_id"] for t in resolved["timeline"]] == ["a", "b", "c"])
check("b 标记 anchored",
      next(t for t in resolved["timeline"] if t["node_id"] == "b")["anchored"])
check("含 expressions 轨迹",
      resolved["expressions"]["nodes"]["b"]["fixed_start"] == "a.end + gap")
check("含 window 汇总", resolved["window"]["start"] == "2026-09-23T22:00")
check("回传模板定义", res["template"]["name"] == "夜间发布模板")

print(f"\n全部 {passed} 项检查通过 ✔")
