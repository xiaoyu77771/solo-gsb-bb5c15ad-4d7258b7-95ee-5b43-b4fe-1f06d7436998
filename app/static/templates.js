/* 场景模板：模板库、模板编辑（含变量与表达式骨架）、参数预览与实例化。
 * 依赖 app.js 暴露的 window.SchedulerApp。
 */
"use strict";

const TplUI = (() => {
  const App = () => window.SchedulerApp;
  const $ = (sel) => document.querySelector(sel);

  // -------------------------------------------------------------------------
  // 本地状态
  // -------------------------------------------------------------------------
  const tstate = {
    view: "library",            // library | edit | apply
    editing: null,              // {id, name, description, variables, scenario}
    apply: null,                // {template, params, preview, instanceName}
    list: [],
  };

  // -------------------------------------------------------------------------
  // API 辅助
  // -------------------------------------------------------------------------
  async function api(method, url, body) {
    const resp = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const detail = data.detail;
      const message = typeof detail === "object" ? detail?.message : detail;
      const err = new Error(message || `HTTP ${resp.status}`);
      err.detail = detail;
      throw err;
    }
    return data;
  }

  // -------------------------------------------------------------------------
  // 内置示例模板
  // -------------------------------------------------------------------------
  const SAMPLE_TEMPLATE = {
    name: "夜间标准发布（含变量）",
    description:
      "以基准日期为锚点的夜间维护模板：窗口=基准日期起 8 小时；" +
      "升级时长与切换偏移为变量；灰度发布固定在「数据库版本升级」结束 + 偏移后开始。",
    variables: [
      { name: "base", type: "base_date", label: "基准日期（窗口开始）",
        default: "2026-09-23T22:00",
        help: "窗口与所有相对时间的锚点，如 2026-09-23T22:00" },
      { name: "upgrade_min", type: "duration", label: "数据库升级时长（分钟）",
        default: 60, help: "用于「数据库版本升级」任务时长" },
      { name: "gray_offset", type: "offset", label: "升级后到灰度发布的偏移（分钟）",
        default: 15, help: "灰度发布固定开始 = 数据库版本升级.end + 此偏移" },
      { name: "verify_min", type: "duration", label: "回归验证时长（分钟）",
        default: 30 },
    ],
    scenario: {
      window_start: "base",
      window_end: "base + 8h",
      resources: { DBA: 1, 网络组: 1, 应用组: 2 },
      nodes: [
        { id: "n1", name: "流量切走", duration_minutes: 20, resources: { 网络组: 1 } },
        { id: "n2", name: "停应用集群", duration_minutes: 20, resources: { 应用组: 2 } },
        { id: "n3", name: "数据库主备切换", duration_minutes: 30,
          resources: { DBA: 1, 网络组: 1 } },
        { id: "n4", name: "数据库版本升级", duration_minutes: "upgrade_min",
          resources: { DBA: 1 }, fixed_start: "base + 2h + 30m" },
        { id: "n5", name: "应用灰度发布", duration_minutes: 45,
          resources: { 应用组: 1 }, fixed_start: "n4.end + gray_offset" },
        { id: "n6", name: "全量回归验证", duration_minutes: "verify_min",
          resources: { 应用组: 1 } },
        { id: "n7", name: "流量切回", duration_minutes: 30,
          resources: { 网络组: 1 }, fixed_start: "base + 7h + 30m" },
        { id: "n8", name: "备份一致性校验", duration_minutes: 45,
          resources: { DBA: 1 } },
        { id: "n9", name: "缓存集群重启", duration_minutes: 40,
          resources: { 应用组: 1 } },
      ],
      edges: [
        { source: "n1", target: "n2" },
        { source: "n2", target: "n3" },
        { source: "n3", target: "n4" },
        { source: "n4", target: "n6" },
        { source: "n5", target: "n6" },
        { source: "n6", target: "n7" },
        { source: "n2", target: "n8" },
      ],
      blackouts: [
        { name: "整点计费批处理", start: "base + 2h", end: "base + 2h + 30m" },
      ],
    },
  };

  // -------------------------------------------------------------------------
  // 打开 / 关闭
  // -------------------------------------------------------------------------
  function open(view = "library") {
    $("#tplModal").classList.remove("hidden");
    switchView(view);
  }
  function close() {
    $("#tplModal").classList.add("hidden");
  }
  function switchView(view) {
    tstate.view = view;
    if (view === "library") renderLibrary();
    else if (view === "edit") renderEditor();
    else if (view === "apply") renderApply();
  }

  function setBody(html) { $("#tplModalBody").innerHTML = html; }

  // -------------------------------------------------------------------------
  // 视图一：模板库
  // -------------------------------------------------------------------------
  async function refreshList() {
    tstate.list = await api("GET", "/api/templates");
  }

  async function renderLibrary() {
    $("#tplModalTitle").textContent = "📋 场景模板库";
    try {
      await refreshList();
    } catch (e) {
      setBody(`<div class="tpl-error">加载模板失败：${App().esc(e.message)}</div>`);
      return;
    }
    const rows = tstate.list.map((t) => `
      <tr>
        <td><b>${App().esc(t.name)}</b>
          <div class="hint">${App().esc(t.description || "")}</div></td>
        <td style="width:64px;text-align:center">${t.var_count} 个变量</td>
        <td style="width:230px;white-space:nowrap">
          <button class="btn btn-small btn-primary" data-tpl-apply="${t.id}">▶ 参数化使用</button>
          <button class="btn btn-small" data-tpl-edit="${t.id}">✎ 编辑</button>
          <button class="btn btn-small btn-danger" data-tpl-del="${t.id}">🗑</button>
        </td>
      </tr>`).join("");

    setBody(`
      <div class="tpl-toolbar">
        <button class="btn btn-primary btn-small" id="tplBtnNew">＋ 新建模板</button>
        <button class="btn btn-small" id="tplBtnSample">载入内置示例模板</button>
        <span class="hint">模板把窗口/资源/任务/依赖保存为含变量的骨架；
          变量支持<b>基准日期</b>、<b>任务时长</b>、<b>前置任务结束时间偏移</b>。</span>
      </div>
      <table class="mini-table">
        <thead><tr><th>模板</th><th>变量</th><th>操作</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="3" class="hint" style="padding:14px;text-align:center">
          暂无模板，点击「＋ 新建模板」或「载入内置示例模板」开始</td></tr>`}</tbody>
      </table>
    `);

    $("#tplBtnNew").onclick = () => {
      tstate.editing = blankTemplate();
      switchView("edit");
    };
    $("#tplBtnSample").onclick = () => {
      tstate.editing = { ...JSON.parse(JSON.stringify(SAMPLE_TEMPLATE)), id: null };
      switchView("edit");
    };
    $$body("[data-tpl-apply]").forEach((b) => {
      b.onclick = () => startApply(+b.dataset.tplApply);
    });
    $$body("[data-tpl-edit]").forEach((b) => {
      b.onclick = () => startEdit(+b.dataset.tplEdit);
    });
    $$body("[data-tpl-del]").forEach((b) => {
      b.onclick = () => removeTemplate(+b.dataset.tplDel);
    });
  }

  function $$body(sel) {
    return Array.from($("#tplModalBody").querySelectorAll(sel));
  }

  async function startEdit(id) {
    const t = await api("GET", `/api/templates/${id}`);
    tstate.editing = {
      id, name: t.name, description: t.description || "",
      variables: t.variables || [], scenario: t.scenario || {},
    };
    switchView("edit");
  }

  async function removeTemplate(id) {
    const t = tstate.list.find((x) => x.id === id);
    if (!confirm(`确认删除模板「${t?.name || id}」？（已实例化的独立场景不受影响）`)) return;
    try {
      await api("DELETE", `/api/templates/${id}`);
      App().toast("模板已删除");
      renderLibrary();
    } catch (e) {
      App().toast("删除失败：" + e.message, true);
    }
  }

  function blankTemplate() {
    const now = new Date();
    now.setSeconds(0, 0);
    const base = App().dtLocal(now);
    return {
      id: null,
      name: "新模板",
      description: "",
      variables: [
        { name: "base", type: "base_date", label: "基准日期", default: base },
        { name: "task_min", type: "duration", label: "任务时长(分钟)", default: "30" },
      ],
      scenario: {
        window_start: "base",
        window_end: "base + 4h",
        resources: { DBA: 1 },
        nodes: [
          { id: "a", name: "任务A", duration_minutes: "task_min",
            resources: { DBA: 1 } },
        ],
        edges: [],
        blackouts: [],
      },
    };
  }

  // -------------------------------------------------------------------------
  // 视图二：模板编辑器
  // -------------------------------------------------------------------------
  function renderEditor() {
    $("#tplModalTitle").textContent = tstate.editing?.id ? "✎ 编辑模板" : "＋ 新建模板";
    const e = tstate.editing;

    const varRows = e.variables.map((v, i) => `
      <tr data-var-row="${i}">
        <td><input data-vk="name" value="${App().esc(v.name)}" placeholder="变量名（英文/中文）" style="min-width:110px"></td>
        <td style="width:120px">
          <select data-vk="type">
            <option value="base_date" ${v.type === "base_date" ? "selected" : ""}>基准日期</option>
            <option value="duration" ${v.type === "duration" ? "selected" : ""}>任务时长(分)</option>
            <option value="offset" ${v.type === "offset" ? "selected" : ""}>前置结束偏移(分)</option>
          </select>
        </td>
        <td style="width:120px"><input data-vk="label" value="${App().esc(v.label || "")}" placeholder="显示名"></td>
        <td style="width:170px"><input data-vk="default" value="${App().esc(v.default ?? "")}"
          placeholder="${v.type === "base_date" ? "2026-09-23T22:00" : "分钟数"}"></td>
        <td style="width:34px"><button class="btn btn-small btn-danger" data-vdel="${i}">✕</button></td>
      </tr>`).join("");

    setBody(`
      <div class="tpl-toolbar">
        <button class="btn btn-small" data-tpl-back>← 返回模板库</button>
        <button class="btn btn-small" id="tplBtnFromCurrent">⤓ 从当前编辑器生成骨架（字面量）</button>
        <span class="hint">生成后可把具体时间/时长替换成变量表达式。</span>
      </div>

      <div class="tpl-form-grid">
        <label>模板名称 <input id="tplName" value="${App().esc(e.name)}" maxlength="200"></label>
        <label class="tpl-full">说明 <input id="tplDesc" value="${App().esc(e.description)}"
          placeholder="这个模板适用的维护场景，如：核心系统夜间标准发布"></label>
      </div>

      <h4 class="tpl-h4">① 变量定义</h4>
      <table class="mini-table">
        <thead><tr><th>变量名</th><th>类型</th><th>显示名</th><th>默认值</th><th></th></tr></thead>
        <tbody id="tplVarBody">${varRows}</tbody>
      </table>
      <div class="tpl-toolbar">
        <button class="btn btn-small" id="tplBtnAddVar">＋ 变量</button>
      </div>

      <h4 class="tpl-h4">② 场景骨架（JSON，可用表达式）</h4>
      <details class="tpl-syntax" open>
        <summary class="hint">表达式语法（点击折叠）</summary>
        <ul>
          <li><b>基准日期</b>：窗口/禁排期/固定开始可写 <code>base</code>、<code>base + 1d + 22h</code>、<code>base - 30m</code></li>
          <li><b>任务时长</b>变量（分钟）：<code>duration_minutes</code> 可写 <code>upgrade_min</code>、<code>upgrade_min + 30m</code></li>
          <li><b>前置结束偏移</b>：任务固定开始可写 <code>n4.end + gray_offset</code>、<code>n4.end + 15m</code>
            （引用 <code>&lt;任务id&gt;.end</code> 会自动补依赖边）</li>
          <li>单位：<code>m</code>=分钟、<code>h</code>=小时、<code>d</code>=24小时；支持 +、- 与小数</li>
          <li>不写表达式的字段按普通字面量处理（如绝对时间 <code>2026-09-24T03:00</code>）</li>
        </ul>
      </details>
      <textarea id="tplSkeleton" class="tpl-json" spellcheck="false"></textarea>
      <div id="tplEditMsg"></div>

      <div class="tpl-toolbar tpl-footer">
        <button class="btn btn-small" id="tplBtnCheck">🔍 校验（按默认值试解析）</button>
        <button class="btn btn-primary btn-small" id="tplBtnSave">💾 保存模板</button>
        <button class="btn btn-small btn-primary" id="tplBtnTry">▶ 保存并参数化使用</button>
      </div>
    `);

    const skel = $("#tplSkeleton");
    skel.value = JSON.stringify(e.scenario, null, 2);

    // 绑定
    $$body("[data-tpl-back]").forEach((b) => (b.onclick = () => switchView("library")));
    $("#tplName").oninput = (ev) => (e.name = ev.target.value);
    $("#tplDesc").oninput = (ev) => (e.description = ev.target.value);

    $("#tplVarBody").addEventListener("input", onVarChange);
    $("#tplVarBody").addEventListener("change", onVarChange);
    function onVarChange(ev) {
      const row = ev.target.closest("[data-var-row]");
      if (!row) return;
      const i = +row.dataset.varRow;
      const key = ev.target.dataset.vk;
      if (!key) return;
      // 类型变化会触发整表重绘，先把骨架框内容同步回编辑对象
      if (key === "type") {
        const raw = $("#tplSkeleton")?.value.trim();
        if (raw) {
          try { e.scenario = JSON.parse(raw); } catch (_) { /* 保留旧值 */ }
        }
      }
      e.variables[i][key] = ev.target.value;
      if (key === "type") renderEditor();
    }
    $("#tplVarBody").addEventListener("click", (ev) => {
      const b = ev.target.closest("[data-vdel]");
      if (!b) return;
      e.variables.splice(+b.dataset.vdel, 1);
      renderEditor();
    });

    $("#tplBtnAddVar").onclick = () => {
      syncSkeleton(e);
      e.variables.push({ name: nextVarName(e.variables), type: "duration",
                         label: "新变量", default: "10" });
      renderEditor();
    };

    $("#tplBtnFromCurrent").onclick = () => {
      syncSkeleton(e);
      e.scenario = App().buildPayload();
      skel.value = JSON.stringify(e.scenario, null, 2);
      editMsg("已把当前编辑器内容（含具体时间）写入骨架，可开始替换为变量表达式", false);
    };

    $("#tplBtnCheck").onclick = () => checkCurrent(false);
    $("#tplBtnSave").onclick = () => saveCurrent(false);
    $("#tplBtnTry").onclick = () => saveCurrent(true);
  }

  function syncSkeleton(e) {
    const raw = $("#tplSkeleton").value.trim();
    if (raw) {
      try { e.scenario = JSON.parse(raw); } catch (_) { /* 保留旧值，保存时报错 */ }
    }
  }

  function nextVarName(vars) {
    let i = vars.length + 1;
    while (vars.some((v) => v.name === `var${i}`)) i++;
    return `var${i}`;
  }

  function editMsg(htmlOrText, isErr) {
    const box = $("#tplEditMsg");
    if (!box) return;
    box.className = isErr ? "tpl-feedback tpl-error" : "tpl-feedback tpl-ok";
    box.textContent = htmlOrText;
  }

  function collectEditing() {
    const e = tstate.editing;
    e.name = $("#tplName").value.trim();
    e.description = $("#tplDesc").value.trim();
    let scenario;
    try {
      scenario = JSON.parse($("#tplSkeleton").value);
    } catch (err) {
      throw Object.assign(new Error("场景骨架 JSON 解析失败：" + err.message),
                          { _json: true });
    }
    e.scenario = scenario;
    return {
      name: e.name, description: e.description,
      variables: e.variables, scenario: e.scenario,
    };
  }

  async function checkCurrent(showOkToast) {
    let definition;
    try {
      definition = collectEditing();
    } catch (err) {
      editMsg(err.message, true);
      return null;
    }
    if (!definition.name) {
      editMsg("模板名称不能为空", true);
      return null;
    }
    try {
      const r = await api("POST", "/api/templates/validate", definition);
      if (!r.valid) {
        editMsg(`✗ ${r.error.message}（${r.error.code}${r.error.path ? " @ " + r.error.path : ""}）`, true);
        return null;
      }
      editMsg(`✓ 校验通过：${r.variables} 个变量；默认参数下窗口 ${r.window.start} ~ ${r.window.end}`, false);
      if (showOkToast) App().toast("模板校验通过");
      return definition;
    } catch (err) {
      editMsg("✗ " + err.message, true);
      return null;
    }
  }

  async function saveCurrent(thenApply) {
    const definition = await checkCurrent(false);
    if (!definition) return;
    const e = tstate.editing;
    try {
      let saved;
      if (e.id) {
        saved = await api("PUT", `/api/templates/${e.id}`, definition);
      } else {
        saved = await api("POST", "/api/templates", definition);
        e.id = saved.id;
      }
      App().toast(saved.updated ? `模板「${definition.name}」已更新` : `模板「${definition.name}」已保存`);
      if (thenApply) {
        await startApply(e.id);
      }
    } catch (err) {
      editMsg("✗ 保存失败：" + err.message, true);
    }
  }

  // -------------------------------------------------------------------------
  // 视图三：参数输入 + 预览 + 实例化
  // -------------------------------------------------------------------------
  async function startApply(id) {
    const template = await api("GET", `/api/templates/${id}`);
    const params = {};
    for (const v of template.variables || []) {
      if (v.default !== undefined && v.default !== null && v.default !== "") {
        params[v.name] = v.default;
      }
    }
    tstate.apply = {
      template,
      params,
      preview: null,
      instanceName: template.name + " 实例",
    };
    switchView("apply");
    doPreview();
  }

  function renderApply() {
    $("#tplModalTitle").textContent = "▶ 模板参数化与实例化预览";
    const a = tstate.apply;
    const t = a.template;

    const varInputs = (t.variables || []).map((v, i) => {
      const val = a.params[v.name] ?? "";
      if (v.type === "base_date") {
        return `<tr>
          <td><b>${App().esc(v.label || v.name)}</b>
            <div class="hint">${App().esc(v.help || "基准日期变量")}</div></td>
          <td style="width:230px"><input type="datetime-local" step="300"
            data-pname="${App().esc(v.name)}" value="${App().esc(val)}"></td>
          <td style="width:150px" class="hint" id="pval-${i}"></td>
        </tr>`;
      }
      const typeLabel = v.type === "duration" ? "任务时长" : "前置结束偏移";
      return `<tr>
        <td><b>${App().esc(v.label || v.name)}</b>
          <div class="hint">${App().esc(v.help || typeLabel + "（分钟，可写表达式引用其他变量）")}</div></td>
        <td style="width:230px"><input type="number" min="0" step="1"
          data-pname="${App().esc(v.name)}" value="${App().esc(val)}"></td>
        <td style="width:150px" class="hint" id="pval-${i}">分钟</td>
      </tr>`;
    }).join("");

    setBody(`
      <div class="tpl-toolbar">
        <button class="btn btn-small" data-tpl-back>← 返回模板库</button>
        <b>${App().esc(t.name)}</b>
        <span class="hint">${App().esc(t.description || "")}</span>
      </div>

      <h4 class="tpl-h4">① 输入参数</h4>
      <table class="mini-table">
        <thead><tr><th>变量</th><th>参数值</th><th></th></tr></thead>
        <tbody id="tplParamBody">${varInputs}</tbody>
      </table>
      <div class="tpl-toolbar">
        <label class="chk">实例名称
          <input id="tplInstanceName" value="${App().esc(a.instanceName)}" style="width:220px;margin-left:6px">
        </label>
        <button class="btn btn-small btn-primary" id="tplBtnPreview">🔍 预览解析与排程</button>
      </div>

      <div id="tplPreviewMsg"></div>
      <div id="tplPreviewBody"></div>

      <div class="tpl-toolbar tpl-footer">
        <button class="btn btn-primary btn-small" id="tplBtnInstantiate" disabled>
          ✓ 实例化为独立场景并载入编辑器</button>
        <button class="btn btn-small" id="tplBtnSaveScenario" disabled>💾 另存为已保存场景</button>
      </div>
    `);

    $$body("[data-tpl-back]").forEach((b) => (b.onclick = () => switchView("library")));
    $("#tplParamBody").addEventListener("change", (ev) => {
      const name = ev.target.dataset.pname;
      if (!name) return;
      a.params[name] = ev.target.value;
      doPreview();
    });
    $("#tplInstanceName").oninput = (ev) => (a.instanceName = ev.target.value);
    $("#tplBtnPreview").onclick = () => doPreview();
    $("#tplBtnInstantiate").onclick = () => instantiateIntoEditor(false);
    $("#tplBtnSaveScenario").onclick = () => instantiateIntoEditor(true);
  }

  function previewMsg(text, kind) {
    const box = $("#tplPreviewMsg");
    box.className = "tpl-feedback " + (kind === "err" ? "tpl-error"
                                        : kind === "ok" ? "tpl-ok" : "");
    box.textContent = text || "";
  }

  async function doPreview() {
    const a = tstate.apply;
    if (!a) return;
    $("#tplBtnPreview")?.setAttribute("disabled", "");
    previewMsg("解析变量并交给排程器计算…", "");
    try {
      const body = {
        template: {
          name: a.template.name,
          description: a.template.description || "",
          variables: a.template.variables || [],
          scenario: a.template.scenario,
        },
        params: a.params,
        instance_name: $("#tplInstanceName")?.value || a.instanceName,
      };
      const preview = await api("POST", "/api/templates/preview", body);
      a.preview = preview;
      a.instanceName = preview.instance_name;
      renderPreview(preview);
      previewMsg("", "");
      $("#tplBtnInstantiate")?.removeAttribute("disabled");
      $("#tplBtnSaveScenario")?.removeAttribute("disabled");
    } catch (err) {
      a.preview = null;
      const d = err.detail;
      if (d && typeof d === "object") {
        previewMsg(`✗ ${d.message}（${d.code}${d.path ? " @ " + d.path : ""}）`, "err");
      } else {
        previewMsg("✗ " + err.message, "err");
      }
      $("#tplPreviewBody").innerHTML = "";
      $("#tplBtnInstantiate")?.setAttribute("disabled", "");
      $("#tplBtnSaveScenario")?.setAttribute("disabled", "");
    } finally {
      $("#tplBtnPreview")?.removeAttribute("disabled");
    }
  }

  function renderPreview(p) {
    const rv = p.resolved.variables || [];
    const varChips = rv.map((v) => {
      const src = v.source === "param" ? "用户参数" : "默认值";
      return `<tr><td>${App().esc(v.label)}</td>
        <td><code>${App().esc(v.name)}</code></td>
        <td>${App().esc(v.input)}</td>
        <td><b>${App().esc(v.value)}${v.unit === "minutes" ? " 分" : ""}</b></td>
        <td class="hint">${src}</td></tr>`;
    }).join("");

    const tl = (p.resolved.timeline || []).map((t) => `
      <tr class="${t.anchored ? "row-fixed" : ""}">
        <td>${App().esc(t.node_id)}</td>
        <td>${App().esc(t.name)}</td>
        <td>${App().esc(t.planned_start)}</td>
        <td>${App().esc(t.planned_end)}</td>
        <td>${t.anchored ? "🔒 表达式固定" : "弹性（最早估计）"}</td>
      </tr>`).join("");

    const implicit = (p.resolved.implicit_edges || []);
    const warnings = (p.resolved.warnings || []);
    const r = p.result;
    const conflicts = (r.conflicts || []).map((c) =>
      `<li><span class="conflict-tag">${App().esc(c.type)}</span> ${App().esc(c.message)}</li>`
    ).join("");

    $("#tplPreviewBody").innerHTML = `
      <h4 class="tpl-h4">② 解析后的参数</h4>
      <table class="mini-table">
        <thead><tr><th>变量</th><th>标识</th><th>输入</th><th>解析值</th><th>来源</th></tr></thead>
        <tbody>${varChips}</tbody>
      </table>
      <div class="hint">窗口：${App().esc(p.scenario.window_start)} ~ ${App().esc(p.scenario.window_end)}
        ｜ 自动补全依赖边 ${implicit.length} 条
        ${implicit.length ? "：" + implicit.map((e) => `${App().esc(e.source)}→${App().esc(e.target)}`).join("，") : ""}
        ${warnings.length ? "｜ ⚠ " + warnings.map((w) => App().esc(w.message)).join("；") : ""}
      </div>

      <h4 class="tpl-h4">③ 时间预览（表达式解析结果）</h4>
      <table class="mini-table">
        <thead><tr><th>ID</th><th>任务</th><th>计划开始</th><th>计划结束</th><th>来源</th></tr></thead>
        <tbody>${tl}</tbody>
      </table>

      <h4 class="tpl-h4">④ 排程器计算结果</h4>
      <div class="tpl-result-status ${r.feasible ? "ok" : "err"}">
        ${r.feasible
          ? `✓ 可行：${r.stats.scheduled}/${r.stats.total} 个任务，总跨度 ${r.stats.makespan_minutes} 分钟`
          : `✗ ${r.stats.conflicts} 个冲突，${r.stats.scheduled}/${r.stats.total} 个任务完成排程`}
      </div>
      ${conflicts ? `<ul class="tpl-conflicts">${conflicts}</ul>` : ""}
      <div class="hint">实例化后将作为<b>独立场景</b>载入主编辑器，可继续拖动调整并自行保存；
        原模板不会被修改。导出 JSON 会同时包含模板定义、解析后的参数与排程结果。</div>
    `;
  }

  async function instantiateIntoEditor(saveAsScenario) {
    const a = tstate.apply;
    if (!a?.preview) return;
    const p = a.preview;
    const instanceName = ($("#tplInstanceName")?.value || a.instanceName).trim();

    // 无论是否另存，先把具体场景载入主编辑器（独立场景，后续编辑与模板无关）
    App().loadConcreteScene(p.scenario, {
      template: p.template, instance_name: instanceName, resolved: p.resolved,
    });
    await App().recompute(false);

    let savedId = null;
    if (saveAsScenario) {
      const name = prompt("保存为独立场景，名称：", instanceName);
      if (!name) {
        App().toast(`已实例化「${instanceName}」（未另存场景）`);
        close();
        return;
      }
      try {
        const saved = await api("POST", "/api/scenarios", {
          name,
          // serializeState 已包含完整窗口/资源/任务/依赖/禁排期与 templateContext
          data: App().serializeState(),
        });
        savedId = saved.id;
        App().toast(`已实例化并保存为独立场景「${name}」`);
        App().refreshScenarioList();
      } catch (e) {
        App().toast("实例已载入，但保存场景失败：" + e.message, true);
      }
    } else {
      App().toast(`已实例化「${instanceName}」：模板变量已解析，现作为独立场景编辑`);
    }

    close();
    if (savedId) {
      const sel = document.querySelector("#scenarioSelect");
      if (sel) sel.value = String(savedId);
    }
  }

  // ---------------------------------------------------------------------------
  // 初始化
  // ---------------------------------------------------------------------------
  function init() {
    $("#btnTemplates").addEventListener("click", () => open("library"));
    $("#btnTplClose").addEventListener("click", close);
    $("#tplModal").addEventListener("click", (ev) => {
      if (ev.target.id === "tplModal") close();
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && !$("#tplModal").classList.contains("hidden")) close();
    });
  }

  return { init };
})();

document.addEventListener("DOMContentLoaded", TplUI.init);
