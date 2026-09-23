"""场景模板：含变量表达式的模板 → 参数解析 → 实例化为具体排程场景。

支持三类变量（type）：
- base_date 基准日期：值为 datetime，可用于窗口 / 禁排期 / 任务固定开始时间，
  表达式形如 ``base_date + 22h``、``base_date + 1d + 22h``。
- duration  任务时长：值为分钟数，可用于节点 duration_minutes 与资源容量。
- offset    前置任务结束时间偏移：非负分钟数，用于 ``<task>.end + 30m`` 之类的
  固定开始时间表达式（解析后自动补上对应的依赖边）。

表达式文法（不走 eval，仅允许：标识符 / 整数 / 小数 / 加减号 / m|h|d 单位）：

    expr    := term (( '+' | '-' ) term)*
    term    := NUMBER UNIT? | NAME ('.' NAME)?
    UNIT    := 'm' | 'h' | 'd'        # 分钟 / 小时 / 天（天=24h，无 DST 换算）

解析在两种上下文中求值：
- 日期时间上下文（窗口、禁排期、fixed_start）：结果必须为 datetime；
- 数值上下文（时长、容量）：结果必须为数字（分钟）。

解析前会做模板变量依赖与任务引用依赖的循环引用检测；
日期时间越界（窗口反向、固定任务越窗、禁排期反向）以解析期错误返回。
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# 词法 / 表达式
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
      \s*(?:
        (?P<number>\d+(?:\.\d+)?)          # 整数 / 小数
      | (?P<name>[A-Za-z_一-鿿][\w一-鿿]*)  # 标识符（含中文）
      | (?P<op>[+\-.,])
      )
    """,
    re.VERBOSE,
)

_TERM_TYPES = {"base_date", "duration", "offset"}
_UNIT_MIN = {"m": 1.0, "h": 60.0, "d": 1440.0}
# 各单位允许出现在什么上下文：d/h/m 均可用于时间偏移；数值上下文只允许 m
_TEMPORAL_UNITS = {"m", "h", "d"}


class TemplateError(ValueError):
    """模板结构 / 解析错误；code 供前端区分错误类型。"""

    def __init__(self, message: str, code: str = "template_error", path: str = ""):
        super().__init__(message)
        self.code = code
        self.path = path


def _parse_dt(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip() if value is not None else ""
        if not text:
            raise TemplateError(f"缺少必填的{field}", "missing_value", field)
        if text.endswith("Z"):
            text = text[:-1]
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise TemplateError(f"{field}格式无效：{text}（应为 YYYY-MM-DDTHH:MM）",
                                "invalid_datetime", field)
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def tokenize(expr: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    text = str(expr)
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise TemplateError(f"表达式无法解析：{expr!r}（位置 {pos} 附近）",
                                "invalid_expression")
        pos = m.end()
        kind = m.lastgroup
        value = m.group().strip()
        if kind == "op" and value == ".":
            tokens.append(("dot", "."))
        elif kind == "op":
            tokens.append(("sign", value))
        else:
            tokens.append((kind, value))
    return tokens


def parse_expr(expr: str) -> list[dict[str, Any]]:
    """把表达式解析为带符号的 term 列表：[{sign, kind, ...}, ...]。"""
    tokens = tokenize(expr)
    if not tokens:
        raise TemplateError(f"表达式为空：{expr!r}", "invalid_expression")

    terms: list[dict[str, Any]] = []
    i = 0
    sign = 1.0
    expect_term = True
    while i < len(tokens):
        kind, value = tokens[i]
        if expect_term:
            if kind == "sign":
                if value == "-":
                    sign = -sign
                i += 1
                continue
            if kind == "number":
                unit = None
                if i + 1 < len(tokens) and tokens[i + 1][0] == "name" \
                        and tokens[i + 1][1] in _UNIT_MIN:
                    unit = tokens[i + 1][1]
                    i += 1
                terms.append({"sign": sign, "kind": "literal",
                              "value": float(value), "unit": unit})
            elif kind == "name":
                ref = value
                attr = None
                if i + 1 < len(tokens) and tokens[i + 1][0] == "dot":
                    if i + 2 >= len(tokens) or tokens[i + 2][0] != "name":
                        raise TemplateError(
                            f"表达式 {expr!r} 中点号后应为属性名（如 .end）",
                            "invalid_expression")
                    attr = tokens[i + 2][1]
                    i += 2
                terms.append({"sign": sign, "kind": "ref", "name": ref, "attr": attr})
            else:
                raise TemplateError(f"表达式 {expr!r} 缺少运算项", "invalid_expression")
            sign = 1.0
            expect_term = False
            i += 1
        else:
            if kind != "sign":
                raise TemplateError(
                    f"表达式 {expr!r} 中运算项之间缺少 + / -", "invalid_expression")
            sign = -1.0 if value == "-" else 1.0
            expect_term = True
            i += 1

    if expect_term:
        raise TemplateError(f"表达式 {expr!r} 末尾缺少运算项", "invalid_expression")
    return terms


def expr_refs(terms: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    """表达式中引用的 (名称, 属性)；属性为 None 表示变量，'end' 表示任务结束。"""
    return [(t["name"], t["attr"]) for t in terms if t["kind"] == "ref"]


def _is_pure_literal(terms: list[dict[str, Any]]) -> bool:
    return len(terms) == 1 and terms[0]["kind"] == "literal"


_DATETIME_LITERAL_RE = re.compile(
    r"^\s*\d{4}-\d{2}-\d{2}([Tt ]\d{1,2}(:\d{2})?)?\s*$"
)


def _looks_like_datetime_literal(text: str) -> bool:
    return bool(_DATETIME_LITERAL_RE.match(str(text)))


# ---------------------------------------------------------------------------
# 模板定义校验
# ---------------------------------------------------------------------------

def _as_vars(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TemplateError("模板 variables 必须是数组", "invalid_template", "variables")
    seen: set[str] = set()
    vars_: list[dict[str, Any]] = []
    for i, v in enumerate(raw):
        path = f"variables[{i}]"
        if not isinstance(v, dict):
            raise TemplateError(f"{path} 必须是对象", "invalid_template", path)
        name = str(v.get("name", "")).strip()
        if not name:
            raise TemplateError(f"{path} 缺少变量名", "invalid_template", path)
        if name in seen:
            raise TemplateError(f"变量名重复：{name}", "duplicate_variable", path)
        seen.add(name)
        vtype = v.get("type")
        if vtype not in _TERM_TYPES:
            raise TemplateError(
                f"变量 {name} 的 type 必须是 {sorted(_TERM_TYPES)} 之一",
                "invalid_variable_type", path)
        item = {"name": name, "type": vtype,
                "label": str(v.get("label") or name),
                "default": v.get("default"),
                "help": str(v.get("help") or "")}
        if vtype == "base_date" and item["default"] in (None, ""):
            raise TemplateError(f"基准日期变量 {name} 必须提供默认值",
                                "missing_default", path)
        if item["default"] not in (None, ""):
            if vtype == "base_date":
                _parse_dt(item["default"], f"变量 {name} 默认值")
            else:
                # 默认值可以是数字（分钟），也可以是引用其他变量的表达式
                try:
                    num = float(item["default"])
                except (TypeError, ValueError):
                    parse_expr(str(item["default"]))  # 仅做语法校验，引用稍后校验
                else:
                    if num < 0:
                        raise TemplateError(
                            f"变量 {name} 默认值不能为负", "invalid_default", path)
        vars_.append(item)
    return vars_


def validate_template(template: dict[str, Any]) -> dict[str, Any]:
    """结构校验 + 表达式语法 / 引用校验。返回规整后的模板定义。"""
    if not isinstance(template, dict):
        raise TemplateError("模板必须是 JSON 对象", "invalid_template")
    name = str(template.get("name", "")).strip()
    if not name:
        raise TemplateError("模板名称不能为空", "invalid_template", "name")

    vars_ = _as_vars(template.get("variables"))
    var_names = {v["name"] for v in vars_}

    scenario = template.get("scenario")
    if not isinstance(scenario, dict):
        raise TemplateError("模板 scenario 必须是对象（排程场景骨架）",
                            "invalid_template", "scenario")
    nodes_in = scenario.get("nodes")
    if not isinstance(nodes_in, list):
        raise TemplateError("模板 scenario.nodes 必须是数组", "invalid_template",
                            "scenario.nodes")
    node_ids: set[str] = set()
    for i, n in enumerate(nodes_in):
        path = f"scenario.nodes[{i}]"
        if not isinstance(n, dict):
            raise TemplateError(f"{path} 必须是对象", "invalid_template", path)
        nid = str(n.get("id", "")).strip()
        if not nid:
            raise TemplateError(f"{path} 缺少 id", "invalid_template", path)
        if nid in node_ids:
            raise TemplateError(f"任务 id 重复：{nid}", "duplicate_node", path)
        node_ids.add(nid)

    # 缓存解析结果同时做语法检查
    def check_expr(raw_expr: Any, path: str, numeric: bool,
                   allow_task_refs: bool) -> list[dict[str, Any]] | None:
        if raw_expr in (None, ""):
            return None
        text = str(raw_expr)
        # 纯日期字面量（如 2026-09-23T22:00）不走表达式解析
        if not numeric and _looks_like_datetime_literal(text):
            return None
        terms = parse_expr(text)
        for t in terms:
            if t["kind"] == "literal":
                if t["unit"] and t["unit"] not in (
                        {"m"} if numeric else _TEMPORAL_UNITS):
                    raise TemplateError(
                        f"{path} 的单位 {t['unit']} 在此处不允许"
                        + ("（数值字段仅支持 m）" if numeric else ""),
                        "invalid_unit", path)
            else:
                ref, attr = t["name"], t["attr"]
                if attr is not None:
                    if not allow_task_refs:
                        raise TemplateError(
                            f"{path} 不允许引用任务结束时间（{ref}.{attr}）；"
                            "只有任务固定开始时间可以",
                            "task_ref_not_allowed", path)
                    if attr != "end":
                        raise TemplateError(
                            f"{path} 仅支持 .end 引用，收到 {ref}.{attr}",
                            "invalid_reference", path)
                    if ref not in node_ids:
                        raise TemplateError(
                            f"{path} 引用了不存在的任务：{ref}",
                            "unknown_reference", path)
                elif ref not in var_names:
                    raise TemplateError(
                        f"{path} 引用了未定义的变量：{ref}",
                        "unknown_variable", path)
        return terms

    # 窗口（日期时间，禁止任务引用）
    check_expr(scenario.get("window_start"), "scenario.window_start", False, False)
    check_expr(scenario.get("window_end"), "scenario.window_end", False, False)

    # 依赖边：必须引用存在的任务（悬空边交给排程器只会成为告警，模板阶段直接拒绝）
    raw_edges = scenario.get("edges", [])
    if not isinstance(raw_edges, list):
        raise TemplateError("scenario.edges 必须是数组", "invalid_template",
                            "scenario.edges")
    for i, e in enumerate(raw_edges):
        if not isinstance(e, dict):
            raise TemplateError(f"scenario.edges[{i}] 必须是对象", "invalid_template",
                                f"scenario.edges[{i}]")
        s = str(e.get("source", "")).strip()
        t = str(e.get("target", "")).strip()
        if not s or not t:
            raise TemplateError(
                f"scenario.edges[{i}] 必须同时包含 source 和 target",
                "invalid_edge", f"scenario.edges[{i}]")
        if s not in node_ids:
            raise TemplateError(
                f"scenario.edges[{i}] 的前置任务不存在：{s}",
                "unknown_reference", f"scenario.edges[{i}].source")
        if t not in node_ids:
            raise TemplateError(
                f"scenario.edges[{i}] 的后继任务不存在：{t}",
                "unknown_reference", f"scenario.edges[{i}].target")

    # 资源容量（数值，禁止任务引用）
    raw_res = scenario.get("resources") or {}
    if not isinstance(raw_res, dict):
        raise TemplateError("scenario.resources 必须是对象", "invalid_template",
                            "scenario.resources")
    for rname, cap in raw_res.items():
        check_expr(cap, f"scenario.resources.{rname}", True, False)

    for i, n in enumerate(nodes_in):
        check_expr(n.get("duration_minutes"), f"scenario.nodes[{i}].duration_minutes",
                   True, False)
        if n.get("fixed_start") not in (None, ""):
            check_expr(n["fixed_start"], f"scenario.nodes[{i}].fixed_start",
                       False, True)
        for rname, amount in (n.get("resources") or {}).items():
            check_expr(amount, f"scenario.nodes[{i}].resources.{rname}", True, False)

    for i, b in enumerate(scenario.get("blackouts") or []):
        check_expr(b.get("start"), f"scenario.blackouts[{i}].start", False, False)
        check_expr(b.get("end"), f"scenario.blackouts[{i}].end", False, False)

    return {
        "name": name,
        "description": str(template.get("description") or ""),
        "variables": vars_,
        "scenario": scenario,
    }


# ---------------------------------------------------------------------------
# 变量解析
# ---------------------------------------------------------------------------

def _detect_cycles(nodes: set[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    """在 (变量 + 任务) 依赖图上找环；返回可读环路径列表。"""
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for s, t in edges:
        if s in adj and t in adj:
            adj[s].append(t)

    # Tarjan SCC（迭代版）
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    counter = 0
    sccs: list[list[str]] = []

    def strongconnect(root: str) -> None:
        nonlocal counter
        work: list[tuple[str, int]] = [(root, 0)]
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, pi = work[-1]
            if pi < len(adj[v]):
                w = adj[v][pi]
                work[-1] = (v, pi + 1)
                if w not in index_of:
                    index_of[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, 0))
                elif w in on_stack:
                    low[v] = min(low[v], index_of[w])
            else:
                work.pop()
                if low[v] == index_of[v]:
                    comp: list[str] = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == v:
                            break
                    sccs.append(comp)
                if work:
                    p = work[-1][0]
                    low[p] = min(low[p], low[v])

    for n in nodes:
        if n not in index_of:
            strongconnect(n)

    cycles: list[list[str]] = []
    for comp in sccs:
        if len(comp) == 1:
            v = comp[0]
            if v not in adj[v]:
                continue
            cycles.append([v])
            continue
        comp_set = set(comp)
        start = sorted(comp)[0]
        path = [start]
        visited = {start}
        cur = start
        while True:
            nxt = next((w for w in adj[cur] if w in comp_set and w not in visited), None)
            if nxt is None:
                break
            path.append(nxt)
            visited.add(nxt)
            cur = nxt
            if start in adj[cur]:
                break
        cycles.append(path)
    return cycles


def resolve_variables(vars_: list[dict[str, Any]],
                      params: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """用户参数 + 默认值 → 求值，得到 {name: {value, type, source}}。"""
    by_name = {v["name"]: v for v in vars_}

    # 1) 收集每个变量的原始输入（用户参数优先，其次默认值）
    raw: dict[str, Any] = {}
    source: dict[str, str] = {}
    for v in vars_:
        name = v["name"]
        if name in params and params[name] not in (None, ""):
            raw[name] = params[name]
            source[name] = "param"
        elif v["default"] not in (None, ""):
            raw[name] = v["default"]
            source[name] = "default"
        else:
            raise TemplateError(f"缺少变量值：{v['label']}（{name}）",
                                "missing_param", f"variables.{name}")

    # 2) 变量间依赖图（默认值可引用其他变量；任务引用不允许）
    dep_edges: list[tuple[str, str]] = []
    parsed: dict[str, list[dict[str, Any]]] = {}
    for name, text in raw.items():
        vtype = by_name[name]["type"]
        if vtype == "base_date":
            parsed[name] = None  # 日期字面量，无需算术解析
            continue
        terms = parse_expr(str(text))
        for ref, attr in expr_refs(terms):
            if attr is not None:
                raise TemplateError(
                    f"变量 {name} 的值不允许引用任务（{ref}.{attr}）",
                    "task_ref_not_allowed", f"variables.{name}")
            if ref not in by_name:
                raise TemplateError(
                    f"变量 {name} 引用了未定义变量：{ref}", "unknown_variable",
                    f"variables.{name}")
            dep_edges.append((ref, name))
        parsed[name] = terms

    cycles = _detect_cycles(set(by_name), dep_edges)
    if cycles:
        chain = " → ".join(cycles[0] + [cycles[0][0]])
        raise TemplateError(f"变量依赖存在循环引用：{chain}", "variable_cycle")

    # 3) 拓扑序求值（稳定：名字排序）
    indeg: dict[str, int] = {n: 0 for n in by_name}
    radj: dict[str, list[str]] = defaultdict(list)
    for s, t in dep_edges:
        indeg[t] += 1
        radj[s].append(t)
    ready = sorted(n for n, d in indeg.items() if d == 0)
    order: list[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for m in radj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
                ready.sort()

    values: dict[str, dict[str, Any]] = {}
    for name in order:
        v = by_name[name]
        if v["type"] == "base_date":
            values[name] = {
                "value": _parse_dt(raw[name], f"变量 {name}"),
                "type": "base_date", "source": source[name],
                "raw": str(raw[name]),
            }
            continue
        terms = parsed[name]
        num = _eval_numeric(terms, values)
        if num < 0:
            raise TemplateError(
                f"变量 {v['label']}（{name}）求值结果为负：{num:g} 分钟，"
                "时长 / 偏移不允许为负",
                "negative_value", f"variables.{name}")
        values[name] = {"value": num, "type": v["type"],
                        "source": source[name], "raw": str(raw[name])}

    return {name: values[name] for name in by_name}


def _eval_numeric(terms: list[dict[str, Any]],
                  values: dict[str, dict[str, Any]]) -> float:
    total = 0.0
    for t in terms:
        if t["kind"] == "literal":
            total += t["sign"] * t["value"] * (_UNIT_MIN[t["unit"]] if t["unit"] else 1.0)
        else:
            spec = values.get(t["name"])
            if spec is None:
                raise TemplateError(f"变量 {t['name']} 未提供值", "missing_param")
            if spec["type"] == "base_date":
                raise TemplateError(
                    f"基准日期变量 {t['name']} 不能用于数值字段", "type_mismatch")
            total += t["sign"] * spec["value"]
    return total


def _eval_datetime(terms: list[dict[str, Any]],
                   values: dict[str, dict[str, Any]],
                   ends: dict[str, datetime],
                   field: str) -> datetime:
    base: datetime | None = None
    delta = 0.0
    for t in terms:
        if t["kind"] == "literal":
            if not t["unit"]:
                raise TemplateError(
                    f"{field} 中的裸数字 {t['value']:g} 缺少单位（m/h/d）",
                    "invalid_unit", field)
            if t["unit"] not in _TEMPORAL_UNITS:
                raise TemplateError(f"{field} 使用了不支持的单位 {t['unit']}",
                                    "invalid_unit", field)
            delta += t["sign"] * _UNIT_MIN[t["unit"]] * t["value"]
        elif t["attr"]:  # <task>.end —— 日期锚点
            if t["name"] not in ends:
                raise TemplateError(
                    f"{field} 引用的任务 {t['name']} 结束时间尚不可用"
                    "（依赖链可能成环）", "reference_unavailable", field)
            when = ends[t["name"]]
            if base is None:
                base = when
            else:
                delta += t["sign"] * (when - base).total_seconds() / 60.0
                base = when
        else:
            spec = values.get(t["name"])
            if spec is None:
                raise TemplateError(f"变量 {t['name']} 未提供值", "missing_param")
            if spec["type"] == "base_date":
                when = spec["value"]
                if base is None:
                    base = when
                else:
                    delta += t["sign"] * (when - base).total_seconds() / 60.0
                    base = when
            else:
                # duration / offset 变量为分钟数，参与时间增量
                delta += t["sign"] * spec["value"]
    if base is None:
        raise TemplateError(f"{field} 的表达式必须包含一个日期基准（变量或任务 .end）",
                            "no_datetime_base", field)
    return base + timedelta(minutes=delta)


# ---------------------------------------------------------------------------
# 实例化
# ---------------------------------------------------------------------------

def _eval_datetime_field(raw_expr: Any, values: dict[str, dict[str, Any]],
                         ends: dict[str, datetime], field: str) -> datetime:
    text = str(raw_expr).strip()
    # 纯日期字面量（含 T、-、: 等）直接解析；否则走表达式
    if _looks_like_datetime_literal(text):
        return _parse_dt(text, field)
    terms = parse_expr(text)
    if _is_pure_literal(terms):
        return _parse_dt(text, field)
    return _eval_datetime(terms, values, ends, field)


def _eval_number_field(raw_expr: Any, values: dict[str, dict[str, Any]],
                       field: str, *, integer: bool) -> float:
    text = str(raw_expr).strip()
    if re.fullmatch(r"\s*\d+(\.\d+)?\s*", text):
        num = float(text)
    else:
        num = _eval_numeric(parse_expr(text), values)
    if num < 0:
        raise TemplateError(f"{field} 求值结果为负：{num:g}", "negative_value", field)
    if integer:
        return int(num)
    return int(num) if num.is_integer() else num


def instantiate(template: dict[str, Any], params: dict[str, Any],
                instance_name: str = "") -> dict[str, Any]:
    """模板 + 用户参数 → {scenario, resolved, template}。

    scenario：可直接交给 scheduler.solve 的具体场景；
    resolved：解析后的变量值、表达式、自动补全的依赖边与告警。
    """
    tpl = validate_template(template)
    scenario = tpl["scenario"]
    vars_ = tpl["variables"]
    if not isinstance(params, dict):
        raise TemplateError("params 必须是 JSON 对象", "invalid_params")

    values = resolve_variables(vars_, params)
    unknown_params = sorted(k for k in params if k not in values)

    resolved_vars = []
    for v in vars_:
        spec = values[v["name"]]
        if spec["type"] == "base_date":
            resolved_vars.append({
                "name": v["name"], "type": spec["type"], "label": v["label"],
                "input": spec["raw"], "value": _iso(spec["value"]),
                "source": spec["source"],
            })
        else:
            num_value = spec["value"]
            resolved_vars.append({
                "name": v["name"], "type": spec["type"], "label": v["label"],
                "input": spec["raw"],
                "value": int(num_value) if float(num_value).is_integer() else num_value,
                "unit": "minutes", "source": spec["source"],
            })

    warnings: list[dict[str, str]] = []
    for k in unknown_params:
        warnings.append({"code": "unknown_param",
                         "message": f"参数 {k} 未在模板变量中定义，已忽略"})

    # --- 节点：先求值时长 / 资源，再在依赖图上推结束时间 / fixed_start ---
    raw_nodes = scenario["nodes"]
    nodes: dict[str, dict[str, Any]] = {}
    expressions: dict[str, dict[str, Any]] = {}
    for n in raw_nodes:
        nid = str(n["id"]).strip()
        if n.get("duration_minutes") in (None, ""):
            raise TemplateError(f"任务 {nid} 缺少时长 duration_minutes",
                                "missing_value",
                                f"scenario.nodes[{nid}].duration_minutes")
        duration = _eval_number_field(n.get("duration_minutes"), values,
                                      f"任务 {nid} 时长", integer=True)
        if duration <= 0:
            raise TemplateError(f"任务 {nid} 时长必须大于 0（求值得到 {duration}）",
                                "nonpositive_duration",
                                f"scenario.nodes.{nid}.duration_minutes")
        resources: dict[str, float] = {}
        for rname, amount in (n.get("resources") or {}).items():
            val = _eval_number_field(amount, values,
                                     f"任务 {nid} 资源 {rname}", integer=False)
            if val > 0:
                resources[str(rname)] = val
        fs_raw = n.get("fixed_start")
        nodes[nid] = {
            "id": nid,
            "name": str(n.get("name") or nid),
            "duration_minutes": duration,
            "resources": resources,
            "fixed_raw": None if fs_raw in (None, "") else str(fs_raw),
            "fixed_dt": None,
        }

    # 资源容量
    capacities: dict[str, float] = {}
    for rname, cap in (scenario.get("resources") or {}).items():
        capacities[str(rname)] = _eval_number_field(
            cap, values, f"资源 {rname} 容量", integer=False)

    # --- 依赖边 + 表达式中引用 <task>.end 隐含的依赖边 ---
    explicit_edges: set[tuple[str, str]] = set()
    for e in scenario.get("edges", []):
        s, t = str(e.get("source", "")).strip(), str(e.get("target", "")).strip()
        if s and t:
            explicit_edges.add((s, t))
    all_edges: set[tuple[str, str]] = set(explicit_edges)
    implicit_edges: list[dict[str, str]] = []
    for nid, node in nodes.items():
        if not node["fixed_raw"]:
            continue
        raw_fs = node["fixed_raw"]
        if _looks_like_datetime_literal(raw_fs):
            continue
        terms = parse_expr(raw_fs)
        for ref, attr in expr_refs(terms):
            if attr == "end" and ref in nodes and ref != nid:
                if (ref, nid) not in all_edges:
                    all_edges.add((ref, nid))
                    implicit_edges.append({
                        "source": ref, "target": nid,
                        "reason": f"固定开始表达式引用了 {ref}.end",
                    })
        # 记录表达式轨迹
        expressions.setdefault(nid, {})["fixed_start"] = raw_fs

    # --- 循环引用检测（显式边 + 隐含边）---
    cycles = _detect_cycles(set(nodes), list(all_edges))
    if cycles:
        chain = " → ".join(cycles[0] + [cycles[0][0]])
        raise TemplateError(
            f"任务依赖存在循环引用：{chain}，无法解析前置任务结束时间",
            "dependency_cycle")

    # 稳定拓扑序（id 字典序）
    indeg: dict[str, int] = {n: 0 for n in nodes}
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for s, t in all_edges:
        if s in nodes and t in nodes:
            adj[s].append(t)
            indeg[t] += 1
    ready = sorted(n for n, d in indeg.items() if d == 0)
    order: list[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
                ready.sort()

    # 窗口（日期字段不允许任务引用，先解析以便越界校验）
    window_start = _eval_datetime_field(scenario.get("window_start"), values, {},
                                        "维护窗口开始时间")
    window_end = _eval_datetime_field(scenario.get("window_end"), values, {},
                                      "维护窗口结束时间")
    if window_start >= window_end:
        raise TemplateError(
            f"维护窗口开始时间必须早于结束时间"
            f"（解析得到 {_iso(window_start)} ≥ {_iso(window_end)}）",
            "window_reversed")

    # --- 沿拓扑序计算每个任务的计划结束时间与固定开始时间 ---
    ends: dict[str, datetime] = {}
    anchored: list[str] = []
    for nid in order:
        node = nodes[nid]
        preds = [s for (s, t) in all_edges if t == nid and s in nodes]
        raw_fs = node["fixed_raw"]
        if raw_fs is not None:
            if _looks_like_datetime_literal(raw_fs):
                fs = _parse_dt(raw_fs, f"任务 {nid} 固定开始时间")
            else:
                fs = _eval_datetime(parse_expr(raw_fs), values, ends,
                                    f"任务 {nid} 固定开始时间")
            node["fixed_dt"] = fs
            end = fs + timedelta(minutes=node["duration_minutes"])
            anchored.append(nid)
            # 时间越界：固定任务越窗
            if fs < window_start or end > window_end:
                raise TemplateError(
                    f"任务「{node['name']}」固定开始时间 {_iso(fs)} 解析后"
                    f"（结束 {_iso(end)}）超出维护窗口"
                    f"（{_iso(window_start)} ~ {_iso(window_end)}）",
                    "fixed_out_of_window",
                    f"scenario.nodes.{nid}.fixed_start")
        else:
            # 弹性任务：计划时间 = max(窗口起点, 前置结束)
            earliest = window_start
            for p in preds:
                if p in ends and ends[p] > earliest:
                    earliest = ends[p]
            end = earliest + timedelta(minutes=node["duration_minutes"])
        ends[nid] = end

    # --- 禁排期解析与越界校验 ---
    blackouts: list[dict[str, str]] = []
    for i, b in enumerate(scenario.get("blackouts") or []):
        bs = _eval_datetime_field(b.get("start"), values, {},
                                  f"禁排期「{b.get('name', '')}」开始时间")
        be = _eval_datetime_field(b.get("end"), values, {},
                                  f"禁排期「{b.get('name', '')}」结束时间")
        if bs >= be:
            raise TemplateError(
                f"禁排期「{b.get('name', '')}」开始必须早于结束"
                f"（解析得到 {_iso(bs)} ~ {_iso(be)}）",
                "blackout_reversed", f"scenario.blackouts[{i}]")
        blackouts.append({"name": str(b.get("name") or "禁排期"),
                          "start": _iso(bs), "end": _iso(be)})

    # --- 组装具体场景（结构与 scheduler.normalize 输入一致）---
    concrete = {
        "window_start": _iso(window_start),
        "window_end": _iso(window_end),
        "resources": capacities,
        "nodes": [
            {
                "id": n["id"], "name": n["name"],
                "duration_minutes": n["duration_minutes"],
                "resources": dict(n["resources"]),
                "fixed_start": _iso(n["fixed_dt"]) if n["fixed_dt"] else None,
            }
            for n in (nodes[nid] for nid in order)
        ],
        "edges": [{"source": s, "target": t} for s, t in sorted(all_edges)],
        "blackouts": blackouts,
    }

    timeline = [
        {"node_id": nid,
         "name": nodes[nid]["name"],
         "planned_start": _iso((nodes[nid]["fixed_dt"]
                                if nodes[nid]["fixed_dt"]
                                else ends[nid] - timedelta(
                                    minutes=nodes[nid]["duration_minutes"]))),
         "planned_end": _iso(ends[nid]),
         "anchored": nodes[nid]["fixed_dt"] is not None}
        for nid in order
    ]

    resolved = {
        "variables": resolved_vars,
        "timeline": timeline,
        "anchored_nodes": anchored,
        "implicit_edges": implicit_edges,
        "expressions": {
            "window_start": str(scenario.get("window_start") or ""),
            "window_end": str(scenario.get("window_end") or ""),
            "nodes": {
                nid: {
                    "duration_minutes": str(raw_node.get("duration_minutes") or ""),
                    "fixed_start": nodes[nid]["fixed_raw"] or "",
                    "resources": {k: str(v) for k, v in
                                  (raw_node.get("resources") or {}).items()},
                }
                for nid, raw_node in ((str(n["id"]).strip(), n) for n in raw_nodes)
            },
        },
        "warnings": warnings,
        "window": {"start": concrete["window_start"], "end": concrete["window_end"]},
    }

    return {
        "scenario": concrete,
        "resolved": resolved,
        "template": {
            "name": tpl["name"],
            "description": tpl["description"],
            "variables": tpl["variables"],
            "scenario": tpl["scenario"],
        },
        "instance_name": instance_name or tpl["name"],
    }
