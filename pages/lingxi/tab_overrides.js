/* UI-4b：分会话覆盖 / 试算 / 关于标签。
 * 纯静态、原生 JS，无任何 CDN / 第三方库。只用 window.LX 的公开接口，
 * 不碰 app.js / index.html。用户数据一律走 textContent（LX.el 的 text）。 */
(function () {
  "use strict";
  if (!window.LX) return;

  var CHECK_DEBOUNCE_MS = 600;

  function scopeTitle(scope) {
    var meta = LX.state.meta;
    if (meta && meta.scopes) {
      for (var i = 0; i < meta.scopes.length; i++) {
        if (meta.scopes[i].scope === scope) return meta.scopes[i].title || scope;
      }
    }
    if (scope === "group") return "群聊";
    if (scope === "private") return "私聊";
    return String(scope);
  }

  function fieldTitle(scope, path) {
    try {
      var f = LX.fieldMeta(scope, path);
      if (f && f.title) return f.title;
    } catch (e) { /* 查不到就显示 path */ }
    return path;
  }

  function blockTitle(scope, block) {
    var meta = LX.state.meta;
    var list = meta && meta.blocks && meta.blocks[scope];
    if (list) {
      for (var i = 0; i < list.length; i++) {
        if (list[i].key === block) return list[i].title || block;
      }
    }
    return block;
  }

  function byteLen(s) {
    try {
      return new TextEncoder().encode(s).length;
    } catch (e) {
      return s.length;
    }
  }

  function fmtValue(v) {
    if (v === null || v === undefined) return "（空）";
    if (typeof v === "boolean") return v ? "开" : "关";
    if (Array.isArray(v)) {
      if (!v.length) return "（空）";
      var s = v.join("，");
      return s.length > 80 ? s.slice(0, 80) + "…" : s;
    }
    if (typeof v === "object") {
      var j = JSON.stringify(v);
      return j.length > 80 ? j.slice(0, 80) + "…" : j;
    }
    var t = String(v);
    if (t === "") return "（空）";
    return t.length > 80 ? t.slice(0, 80) + "…" : t;
  }

  function clearNode(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // ---- 小零件（长相见 DESIGN.md；图标由 icons.js 提供，缺了就不画） ----
  function ic(name, size) {
    return typeof LX.icon === "function" ? LX.icon(name, size) : null;
  }

  // 带图标的按钮：LX.el 建好后把图标插到文字前面
  function iconButton(attrs, iconName) {
    var b = LX.el("button", attrs);
    var i = ic(iconName, 14);
    if (i) b.insertBefore(i, b.firstChild);
    return b;
  }

  // 卡片头：图标块 + 标题 + 小字说明（+ 右侧操作区）
  function cardHead(iconName, title, sub, actions) {
    var chip = LX.el("span", { class: "card-icon" });
    var i = ic(iconName, 18);
    if (i) chip.appendChild(i);
    var titles = LX.el("div", { class: "card-titles" }, [LX.el("h3", { text: title })]);
    if (sub) titles.appendChild(LX.el("div", { class: "card-sub", text: sub }));
    var head = LX.el("div", { class: "card-head" }, [chip, titles]);
    if (actions) head.appendChild(LX.el("div", { class: "card-actions" }, actions));
    return head;
  }

  // 提示条：tone 为 info / ok / warn / danger；extra 是要保留的钩子类名
  function alertBox(tone, text, extra) {
    var names = { info: "info", ok: "check-circle", warn: "warning", danger: "alert-circle" };
    var box = LX.el("div", { class: "alert alert-" + tone + (extra ? " " + extra : "") });
    var i = ic(names[tone] || "info", 16);
    if (i) box.appendChild(LX.el("span", { class: "alert-icon" }, [i]));
    box.appendChild(LX.el("div", { class: "alert-body", text: text }));
    return box;
  }

  // 开关：<label class="toggle"><input type=checkbox><span class="toggle-track"/><span class="toggle-text">文字</span></label>
  function toggleLabel(inputId, text, extraClass) {
    var input = document.createElement("input");
    input.id = inputId;
    input.setAttribute("type", "checkbox");
    input.setAttribute("role", "switch");
    var label = LX.el("label", { class: "toggle" + (extraClass ? " " + extraClass : "") }, [
      input, LX.el("span", { class: "toggle-track" }), LX.el("span", { class: "toggle-text", text: text })
    ]);
    return { label: label, input: input };
  }

  var SRC_TEXT = { "default": "默认", "global": "全局设置", "override": "分会话覆盖" };
  var SRC_TONE = { "default": "neutral", "global": "info", "override": "ok" };

  /* ================= 分会话覆盖标签 ================= */
  var ov = {
    ta: null, meta: null, result: null, exampleBtn: null,
    saveNote: null, helpPre: null,
    exScope: null, exId: null, exIds: null, exRun: null, exHead: null,
    exOnly: null, exTable: null, exWrap: null, exEmpty: null, exDirtyWarn: null,
    checkSeq: 0, debounceTimer: null,
    lastSessions: [], lastExplain: null
  };

  /* 库里存的覆盖表可能是 \r\n；textarea 的 .value 总是 \n。统一成 \n 再比较 / 还原，
   * 否则"还原为已保存的内容"之后仍会被算作有改动。 */
  function normNl(s) {
    return String(s === undefined || s === null ? "" : s).replace(/\r\n?/g, "\n");
  }

  function savedOverridesText() {
    return normNl(LX.state.config && LX.state.config.session_overrides);
  }

  function currentText() {
    return LX.state.overridesDirty ? (LX.state.overridesText || "") : savedOverridesText();
  }

  function updateOvMeta() {
    if (!ov.ta || !ov.meta) return;
    var text = ov.ta.value;
    var lines = text === "" ? 0 : text.split("\n").length;
    var bytes = byteLen(text);
    var max = (LX.state.meta && LX.state.meta.overrides && LX.state.meta.overrides.max_bytes) || 0;
    ov.meta.textContent = lines + " 行 · " + bytes + " 字节" + (max ? "（上限 " + max + " 字节）" : "");
    if (max && bytes > max) ov.meta.classList.add("over");
    else ov.meta.classList.remove("over");
  }

  // 一条检查错误：红色提示条，类名 ov-err 是场景脚本数错误条数用的
  function errLine(text) {
    return alertBox("danger", String(text), "ov-err");
  }

  function renderCheck(data) {
    clearNode(ov.result);
    if (!data || !data.ok) {
      var errs = (data && data.errors) || ["检查失败"];
      var box = LX.el("div", { class: "ov-errors" });
      errs.forEach(function (m) { box.appendChild(errLine(m)); });
      ov.result.appendChild(box);
      return;
    }
    var sessions = data.sessions || [];
    ov.lastSessions = sessions;
    refreshDatalist();
    if (!sessions.length) {
      ov.result.appendChild(LX.el("div", { class: "ov-empty muted", text: "现在没有任何分会话覆盖" }));
      return;
    }
    var okIcon = ic("check-circle", 16);
    ov.result.appendChild(LX.el("div", { class: "ov-ok" }, [
      okIcon, LX.el("span", { text: "识别到 " + sessions.length + " 个会话" })
    ]));
    var list = LX.el("div", { class: "ov-sessions" });
    sessions.forEach(function (s) {
      var row = LX.el("div", { class: "ov-session" }, [
        LX.el("span", { class: "pill pill-accent", text: scopeTitle(s.scope) }),
        LX.el("code", { text: String(s.id) }),
        LX.el("span", { class: "muted", text: "改了：" })
      ]);
      var keys = (s.keys || []).map(function (p) { return fieldTitle(s.scope, p); });
      if (keys.length) {
        keys.forEach(function (k) { row.appendChild(LX.el("span", { class: "pill", text: k })); });
      } else {
        row.appendChild(LX.el("span", { class: "muted", text: "（没有改任何项）" }));
      }
      list.appendChild(row);
    });
    ov.result.appendChild(list);
  }

  function runCheck() {
    if (!ov.ta) return;
    var text = ov.ta.value;
    if (!text.trim()) {
      ov.checkSeq++;
      clearNode(ov.result);
      ov.result.appendChild(LX.el("div", { class: "ov-empty muted", text: "现在没有任何分会话覆盖" }));
      ov.lastSessions = [];
      refreshDatalist();
      return;
    }
    var seq = ++ov.checkSeq;
    LX.api.post("overrides/check", { text: text }).then(function (data) {
      if (seq !== ov.checkSeq) return;
      renderCheck(data);
    }).catch(function (e) {
      if (seq !== ov.checkSeq) return;
      clearNode(ov.result);
      var box = LX.el("div", { class: "ov-errors" });
      box.appendChild(errLine("检查失败：" + (e && e.message ? e.message : String(e))));
      ov.result.appendChild(box);
    });
  }

  function scheduleCheck() {
    if (ov.debounceTimer) clearTimeout(ov.debounceTimer);
    ov.debounceTimer = setTimeout(runCheck, CHECK_DEBOUNCE_MS);
  }

  function whitelistIds(scope) {
    try {
      var cfg = LX.state.config;
      var layer = cfg && cfg[scope];
      var wl = layer && layer.whitelist;
      var ids = wl && wl.ids;
      if (Array.isArray(ids)) return ids.map(String);
    } catch (e) { /* 没有就空着 */ }
    return [];
  }

  function refreshDatalist() {
    if (!ov.exScope || !ov.exIds) return;
    var scope = ov.exScope.value;
    var seen = {};
    var ids = [];
    whitelistIds(scope).forEach(function (id) {
      if (!seen[id]) { seen[id] = 1; ids.push(id); }
    });
    ov.lastSessions.forEach(function (s) {
      if (s.scope === scope && !seen[s.id]) { seen[s.id] = 1; ids.push(String(s.id)); }
    });
    clearNode(ov.exIds);
    ids.forEach(function (id) {
      var o = document.createElement("option");
      o.value = id;
      ov.exIds.appendChild(o);
    });
  }

  function updateExDirtyWarn() {
    if (!ov.exDirtyWarn) return;
    var dirty = LX.state.overridesDirty || (LX.state.dirty && LX.state.dirty.size > 0);
    ov.exDirtyWarn.classList.toggle("hidden", !dirty);
  }

  function updateSaveNote() {
    if (!ov.saveNote) return;
    var show = false;
    try {
      var banner = document.querySelector("#banner");
      if (banner && banner.classList.contains("show") && banner.classList.contains("error")) {
        var t = banner.innerText || "";
        // 服务端拒绝覆盖表时错误进 #banner；文案不一定带“覆盖”二字，
        // 有覆盖表暂存没落下且顶部有错就提示，避免文案一改提示就丢。
        if (t.indexOf("覆盖") >= 0) show = true;
        else if (LX.state.overridesDirty) show = true;
      }
    } catch (e) { /* 读不到就不显示 */ }
    ov.saveNote.classList.toggle("hidden", !show);
  }

  function renderExplainTable() {
    clearNode(ov.exTable);
    var head = document.createElement("thead");
    var hr = document.createElement("tr");
    ["项", "当前值", "来源"].forEach(function (t) {
      var th = document.createElement("th");
      th.textContent = t;
      hr.appendChild(th);
    });
    head.appendChild(hr);
    ov.exTable.appendChild(head);
    var data = ov.lastExplain;
    // 还没试算过：不画空表头，换成一句引导
    if (ov.exWrap) ov.exWrap.classList.toggle("hidden", !data);
    if (ov.exEmpty) ov.exEmpty.classList.toggle("hidden", !!data);
    if (!data) return;
    var onlyNonDefault = ov.exOnly && ov.exOnly.checked;
    var values = data.values || {};
    var scope = data.scope;
    var groups = {};
    var order = [];
    Object.keys(values).forEach(function (path) {
      var dot = path.indexOf(".");
      var block = dot < 0 ? path : path.slice(0, dot);
      if (!groups[block]) { groups[block] = []; order.push(block); }
      if (onlyNonDefault && values[path].source === "default") return;
      groups[block].push(path);
    });
    var anyRow = false;
    order.forEach(function (block) {
      var paths = groups[block];
      if (!paths.length) return;
      anyRow = true;
      var headRow = LX.el("tr", { class: "ex-block row-group" });
      var th = document.createElement("th");
      th.setAttribute("colspan", "3");
      th.textContent = blockTitle(scope, block) + "（" + block + "）";
      headRow.appendChild(th);
      ov.exTable.appendChild(headRow);
      paths.forEach(function (path) {
        var item = values[path];
        var tr = LX.el("tr", { class: "ex-row" });
        tr.setAttribute("data-path", path);
        tr.setAttribute("data-source", item.source);
        var c1 = document.createElement("td");
        c1.textContent = fieldTitle(scope, path);
        var c2 = document.createElement("td");
        c2.textContent = fmtValue(item.value);
        var c3 = document.createElement("td");
        var badge = document.createElement("span");
        badge.className = "badge badge-" + (SRC_TONE[item.source] || "neutral") + " src-" + item.source;
        badge.textContent = SRC_TEXT[item.source] || item.source;
        c3.appendChild(badge);
        tr.appendChild(c1);
        tr.appendChild(c2);
        tr.appendChild(c3);
        ov.exTable.appendChild(tr);
      });
    });
    if (!anyRow) {
      var tr = LX.el("tr", {});
      var td = document.createElement("td");
      td.setAttribute("colspan", "3");
      td.className = "muted";
      td.textContent = "全是默认值，没有被改过的项。";
      tr.appendChild(td);
      ov.exTable.appendChild(tr);
    }
  }

  function runExplain() {
    var scope = ov.exScope.value;
    var id = ov.exId.value.trim();
    clearNode(ov.exHead);
    ov.lastExplain = null;
    renderExplainTable();
    if (!id) {
      ov.exHead.appendChild(LX.el("span", { class: "ov-err", text: "请填写会话 id（私聊填 QQ 号，群聊填群号）。" }));
      return;
    }
    LX.api.get("explain", { scope: scope, id: id }).then(function (data) {
      ov.lastExplain = data;
      var wl = data.whitelist || {};
      // 一行小徽章：白名单开没开 / 在不在名单里 / 这个会话是否启用 /（私聊）是否接管
      var chips = [
        ["白名单：" + (wl.enabled ? "开" : "关"), "neutral"],
        [wl.listed ? "在名单里" : "不在名单里", wl.listed ? "ok" : (wl.enabled ? "warn" : "neutral")],
        [data.enabled ? "启用" : "停用", data.enabled ? "ok" : "danger"]
      ];
      if (data.scope === "private") chips.push(["私聊接管：" + (data.takes_over ? "是" : "否"), data.takes_over ? "info" : "neutral"]);
      clearNode(ov.exHead);
      chips.forEach(function (c) {
        ov.exHead.appendChild(LX.el("span", { class: "badge badge-" + c[1], text: c[0] }));
      });
      renderExplainTable();
    }).catch(function (e) {
      clearNode(ov.exHead);
      ov.exHead.appendChild(LX.el("span", {
        class: "ov-err",
        text: "试算失败：" + (e && e.message ? e.message : String(e))
      }));
    });
  }

  function mountOverrides(root) {
    var card = LX.el("div", { class: "card" }, [
      cardHead("sliders", "分会话覆盖（YAML）", "给某个群 / 某个人单独改几项设置，没写的项沿用全局配置。")
    ]);
    ov.ta = document.createElement("textarea");
    ov.ta.id = "ov-text";
    ov.ta.setAttribute("spellcheck", "false");
    ov.ta.setAttribute("rows", "16");
    ov.ta.setAttribute("aria-label", "分会话覆盖表（YAML）");
    ov.ta.value = currentText();
    card.appendChild(ov.ta);
    var row = LX.el("div", { class: "ov-row" });
    ov.meta = LX.el("span", { class: "muted ov-meta", text: "" });
    row.appendChild(ov.meta);
    var btnCheck = iconButton({ class: "small primary", text: "检查" }, "check");
    btnCheck.id = "ov-check";
    var btnRevert = iconButton({ class: "small ghost", text: "还原为已保存的内容" }, "undo");
    btnRevert.id = "ov-revert";
    row.appendChild(btnRevert);
    row.appendChild(btnCheck);
    card.appendChild(row);
    ov.result = LX.el("div", { id: "ov-result", class: "ov-result" });
    card.appendChild(ov.result);
    ov.saveNote = alertBox("warn", "保存时被服务端拒绝，见顶部提示。", "warn-line hidden");
    card.appendChild(ov.saveNote);

    var help = document.createElement("details");
    help.id = "ov-help";
    var summary = document.createElement("summary");
    summary.textContent = "怎么写";
    help.appendChild(summary);
    var intro = document.createElement("p");
    intro.className = "muted";
    intro.textContent = "顶层写“谁：改什么”。谁写成 group:群号 或 private:QQ号，只能写英文冒号；" +
      "下面按“块名: {键: 值}”写只想改的那几项，没写的不变，沿用全局配置。" +
      "键名必须是配置里有的、可被覆盖的项，写错了检查会告诉你。";
    help.appendChild(intro);
    ov.helpPre = document.createElement("pre");
    ov.helpPre.className = "ov-example";
    var example = (LX.state.meta && LX.state.meta.overrides && LX.state.meta.overrides.example) || "";
    ov.helpPre.textContent = example;
    help.appendChild(ov.helpPre);
    ov.exampleBtn = iconButton({ class: "small secondary", text: "把示例插到编辑框末尾" }, "plus");
    ov.exampleBtn.id = "ov-insert-example";
    help.appendChild(ov.exampleBtn);
    card.appendChild(help);
    root.appendChild(card);

    var card2 = LX.el("div", { class: "card" }, [
      cardHead("eye", "试算：看某个会话最终生效的设置", "选一个会话，看四层配置合并后每一项最终是多少、来自哪一层。")
    ]);
    ov.exDirtyWarn = alertBox("warn", "试算用的是已保存的配置；你有未保存的改动，先保存再试算。", "warn-line hidden");
    card2.appendChild(ov.exDirtyWarn);
    var exRow = LX.el("div", { class: "ov-row" });
    ov.exScope = document.createElement("select");
    ov.exScope.id = "ex-scope";
    [["private", "私聊"], ["group", "群聊"]].forEach(function (pair) {
      var o = document.createElement("option");
      o.value = pair[0];
      o.textContent = pair[1];
      if (pair[0] === "group") o.selected = true;
      ov.exScope.appendChild(o);
    });
    ov.exId = document.createElement("input");
    ov.exId.id = "ex-id";
    ov.exId.setAttribute("type", "text");
    ov.exId.setAttribute("placeholder", "会话 id（群号 / QQ 号）");
    ov.exId.setAttribute("list", "ex-ids");
    ov.exIds = document.createElement("datalist");
    ov.exIds.id = "ex-ids";
    ov.exRun = iconButton({ class: "small primary", text: "试算" }, "bolt");
    ov.exRun.id = "ex-run";
    exRow.appendChild(ov.exScope);
    exRow.appendChild(ov.exId);
    exRow.appendChild(ov.exIds);
    exRow.appendChild(ov.exRun);
    card2.appendChild(exRow);
    var exMeta = LX.el("div", { class: "ex-bar" });
    ov.exHead = LX.el("div", { id: "ex-head", class: "ex-head" });
    exMeta.appendChild(ov.exHead);
    var only = toggleLabel("ex-only-nondefault", "只看不是默认的", "ex-only");
    ov.exOnly = only.input;
    exMeta.appendChild(only.label);
    card2.appendChild(exMeta);
    ov.exEmpty = LX.el("div", { class: "empty ex-empty" }, [
      LX.el("div", { class: "empty-icon" }, [ic("eye", 22)]),
      LX.el("div", { class: "empty-title", text: "还没有试算结果" }),
      LX.el("div", { class: "empty-text", text: "选好会话类型，填上群号 / QQ 号，点“试算”。" })
    ]);
    card2.appendChild(ov.exEmpty);
    ov.exTable = document.createElement("table");
    ov.exTable.id = "ex-table";
    ov.exTable.className = "table ex-table";
    ov.exWrap = LX.el("div", { class: "table-wrap hidden" }, [ov.exTable]);
    card2.appendChild(ov.exWrap);
    root.appendChild(card2);

    ov.ta.addEventListener("input", function () {
      LX.markOverridesDirty(ov.ta.value);
      updateOvMeta();
      updateExDirtyWarn();
      scheduleCheck();
    });
    btnCheck.addEventListener("click", function () {
      if (ov.debounceTimer) clearTimeout(ov.debounceTimer);
      runCheck();
    });
    btnRevert.addEventListener("click", function () {
      ov.ta.value = savedOverridesText();
      LX.markOverridesDirty(ov.ta.value);
      updateOvMeta();
      updateExDirtyWarn();
      runCheck();
    });
    ov.exampleBtn.addEventListener("click", function () {
      var ex = ov.helpPre.textContent || "";
      if (!ov.ta.value) ov.ta.value = ex;
      else ov.ta.value = ov.ta.value.replace(/\s+$/, "") + "\n\n" + ex;
      LX.markOverridesDirty(ov.ta.value);
      updateOvMeta();
      updateExDirtyWarn();
      runCheck();
    });
    ov.exScope.addEventListener("change", refreshDatalist);
    ov.exRun.addEventListener("click", runExplain);
    ov.exOnly.addEventListener("change", renderExplainTable);

    renderExplainTable();
    updateOvMeta();
    updateExDirtyWarn();
    refreshDatalist();
    runCheck();
  }

  function reloadOverrides() {
    if (!ov.ta) return;
    if (!LX.state.overridesDirty) ov.ta.value = savedOverridesText();
    var example = (LX.state.meta && LX.state.meta.overrides && LX.state.meta.overrides.example) || "";
    if (ov.helpPre) ov.helpPre.textContent = example;
    updateOvMeta();
    updateExDirtyWarn();
    updateSaveNote();
    refreshDatalist();
  }

  LX.registerTab({
    id: "overrides", title: "分会话覆盖", order: 30, icon: "sliders",
    mount: mountOverrides,
    isDirty: function () { return LX.state.overridesDirty; }
  });
  LX.onReload(reloadOverrides);
  LX.onChange(function () {
    updateExDirtyWarn();
    updateSaveNote();
  });

  /* ================= 关于标签 ================= */
  var ab = { version: null, schema: null, warnings: null, backups: null };

  function renderAbout(data) {
    clearNode(ab.version);
    ab.version.appendChild(document.createTextNode((data && data.plugin_version) || "（未知）"));
    clearNode(ab.schema);
    var sv = data && data.schema_version;
    ab.schema.appendChild(document.createTextNode(sv === undefined || sv === null ? "（未知）" : String(sv)));
    clearNode(ab.warnings);
    var warns = (data && data.startup_warnings) || [];
    if (!warns.length) {
      var okIcon = ic("check", 12);
      ab.warnings.appendChild(LX.el("span", { class: "badge badge-ok" }, [okIcon, "没有"]));
    } else {
      var stack = LX.el("div", { class: "stack" });
      warns.forEach(function (w) {
        var line = alertBox("warn", String(w), "warn");
        stack.appendChild(line);
      });
      ab.warnings.appendChild(stack);
    }
    clearNode(ab.backups);
    var list = (data && data.backups) || [];
    if (!list.length) {
      ab.backups.appendChild(LX.el("div", { class: "empty about-empty" }, [
        LX.el("div", { class: "empty-icon" }, [ic("file", 22)]),
        LX.el("div", { class: "empty-title", text: "还没有备份" }),
        LX.el("div", { class: "empty-text", text: "第一次从旧配置迁移时，会把旧配置先备份到这里。" })
      ]));
    } else {
      var table = document.createElement("table");
      table.className = "table";
      var thead = document.createElement("thead");
      var thr = document.createElement("tr");
      ["名字", "大小", "时间"].forEach(function (t) {
        var th = document.createElement("th");
        th.textContent = t;
        thr.appendChild(th);
      });
      thead.appendChild(thr);
      table.appendChild(thead);
      var tbody = document.createElement("tbody");
      list.forEach(function (b) {
        var tr = document.createElement("tr");
        var c1 = document.createElement("td");
        c1.className = "mono";
        c1.textContent = b.name;
        var c2 = document.createElement("td");
        c2.className = "num";
        c2.textContent = (Number(b.size) / 1024).toFixed(1) + " KB";
        var c3 = document.createElement("td");
        c3.className = "num";
        try {
          c3.textContent = new Date(Number(b.mtime) * 1000).toLocaleString();
        } catch (e) {
          c3.textContent = String(b.mtime);
        }
        tr.appendChild(c1);
        tr.appendChild(c2);
        tr.appendChild(c3);
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      ab.backups.appendChild(LX.el("div", { class: "table-wrap" }, [table]));
    }
  }

  function loadAbout() {
    if (!ab.version) return;
    LX.api.get("about", {}).then(renderAbout).catch(function (e) {
      clearNode(ab.version);
      ab.version.appendChild(LX.el("span", {
        class: "ov-err",
        text: "没读出来：" + (e && e.message ? e.message : String(e))
      }));
    });
  }

  function mountAbout(root) {
    var btn = iconButton({ class: "small secondary", text: "刷新" }, "refresh");
    btn.id = "about-refresh";
    btn.addEventListener("click", loadAbout);
    var card = LX.el("div", { class: "card about-card" }, [
      cardHead("ghost", "关于", "版本、迁移留下的提示和备份。", [btn])
    ]);
    // 版本信息：两张小键值卡
    ab.version = LX.el("span", { id: "about-version", class: "kv-value mono" });
    ab.schema = LX.el("span", { id: "about-schema", class: "kv-value mono" });
    function kv(label, valueEl) {
      return LX.el("div", { class: "kv" }, [LX.el("div", { class: "kv-label", text: label }), valueEl]);
    }
    card.appendChild(LX.el("div", { class: "kv-grid" }, [kv("插件版本", ab.version), kv("配置版本", ab.schema)]));
    // 启动提示 / 备份文件：分组标题 + 内容
    ab.warnings = LX.el("div", { id: "about-warnings", class: "about-body" });
    ab.backups = LX.el("div", { id: "about-backups", class: "about-body" });
    card.appendChild(LX.el("div", { class: "group-title", text: "启动提示" }));
    card.appendChild(ab.warnings);
    card.appendChild(LX.el("div", { class: "group-title", text: "备份文件" }));
    card.appendChild(ab.backups);
    card.appendChild(alertBox("info", "配置在 AstrBot 自带的配置页里也能改，两边是同一份数据。" +
      "两边同时开着时，后保存的会覆盖先保存的整块设置（私聊 / 群聊 / 覆盖表按整块覆盖），改之前先刷新一下。" +
      "这里改完保存立即生效，不用重载插件。", "about-note"));
    root.appendChild(card);
    loadAbout();
  }

  LX.registerTab({
    id: "about", title: "关于", order: 90, icon: "info",
    mount: mountAbout,
    onShow: loadAbout
  });
})();
