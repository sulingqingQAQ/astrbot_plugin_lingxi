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
    return t.length > 80 ? t.slice(0, 80) + "…" : t;
  }

  function clearNode(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  var SRC_TEXT = { "default": "默认", "global": "全局设置", "override": "分会话覆盖" };

  /* ================= 分会话覆盖标签 ================= */
  var ov = {
    ta: null, meta: null, result: null, exampleBtn: null,
    saveNote: null, helpPre: null,
    exScope: null, exId: null, exIds: null, exRun: null, exHead: null,
    exOnly: null, exTable: null, exDirtyWarn: null,
    checkSeq: 0, debounceTimer: null,
    lastSessions: [], lastExplain: null
  };

  function savedOverridesText() {
    return (LX.state.config && LX.state.config.session_overrides) || "";
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

  function renderCheck(data) {
    clearNode(ov.result);
    if (!data || !data.ok) {
      var errs = (data && data.errors) || ["检查失败"];
      var box = LX.el("div", { class: "ov-errors" });
      errs.forEach(function (m) {
        box.appendChild(LX.el("div", { class: "ov-err", text: String(m) }));
      });
      ov.result.appendChild(box);
      return;
    }
    var sessions = data.sessions || [];
    ov.lastSessions = sessions;
    refreshDatalist();
    if (!sessions.length) {
      ov.result.appendChild(LX.el("div", { class: "muted", text: "现在没有任何分会话覆盖" }));
      return;
    }
    ov.result.appendChild(LX.el("div", { class: "ov-ok", text: "识别到 " + sessions.length + " 个会话" }));
    sessions.forEach(function (s) {
      var keys = (s.keys || []).map(function (p) { return fieldTitle(s.scope, p); });
      var line = scopeTitle(s.scope) + " " + s.id + " · 改了：" + (keys.length ? keys.join("、") : "（没有改任何项）");
      ov.result.appendChild(LX.el("div", { class: "ov-session", text: line }));
    });
  }

  function runCheck() {
    if (!ov.ta) return;
    var text = ov.ta.value;
    if (!text.trim()) {
      ov.checkSeq++;
      clearNode(ov.result);
      ov.result.appendChild(LX.el("div", { class: "muted", text: "现在没有任何分会话覆盖" }));
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
      box.appendChild(LX.el("div", {
        class: "ov-err",
        text: "检查失败：" + (e && e.message ? e.message : String(e))
      }));
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
      var headRow = LX.el("tr", { class: "ex-block" });
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
        badge.className = "src-" + item.source;
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
      var parts = "白名单：" + (wl.enabled ? "开" : "关") + " · " + (wl.listed ? "在名单里" : "不在名单里") +
        " · " + (data.enabled ? "启用" : "停用");
      if (data.scope === "private") parts += " · 私聊接管：" + (data.takes_over ? "是" : "否");
      clearNode(ov.exHead);
      ov.exHead.appendChild(LX.el("span", { text: parts }));
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
      LX.el("h3", { text: "分会话覆盖（YAML）" })
    ]);
    ov.ta = document.createElement("textarea");
    ov.ta.id = "ov-text";
    ov.ta.setAttribute("spellcheck", "false");
    ov.ta.setAttribute("rows", "16");
    ov.ta.value = currentText();
    card.appendChild(ov.ta);
    var row = LX.el("div", { class: "ov-row" });
    ov.meta = LX.el("span", { class: "muted ov-meta", text: "" });
    row.appendChild(ov.meta);
    var btnCheck = LX.el("button", { class: "small primary", text: "检查" });
    btnCheck.id = "ov-check";
    var btnRevert = LX.el("button", { class: "small secondary", text: "还原为已保存的内容" });
    btnRevert.id = "ov-revert";
    row.appendChild(btnCheck);
    row.appendChild(btnRevert);
    card.appendChild(row);
    ov.result = LX.el("div", { id: "ov-result", class: "ov-result" });
    card.appendChild(ov.result);
    ov.saveNote = LX.el("div", {
      class: "warn-line hidden",
      text: "保存时被服务端拒绝，见顶部提示。"
    });
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
    ov.exampleBtn = LX.el("button", { class: "small secondary", text: "把示例插到编辑框末尾" });
    ov.exampleBtn.id = "ov-insert-example";
    help.appendChild(ov.exampleBtn);
    card.appendChild(help);
    root.appendChild(card);

    var card2 = LX.el("div", { class: "card" }, [
      LX.el("h3", { text: "试算：看某个会话最终生效的设置" })
    ]);
    ov.exDirtyWarn = LX.el("div", {
      class: "warn-line hidden",
      text: "试算用的是已保存的配置；你有未保存的改动，先保存再试算。"
    });
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
    ov.exRun = LX.el("button", { class: "small primary", text: "试算" });
    ov.exRun.id = "ex-run";
    exRow.appendChild(ov.exScope);
    exRow.appendChild(ov.exId);
    exRow.appendChild(ov.exIds);
    exRow.appendChild(ov.exRun);
    card2.appendChild(exRow);
    ov.exHead = LX.el("div", { id: "ex-head", class: "ex-head" });
    card2.appendChild(ov.exHead);
    var onlyLabel = document.createElement("label");
    onlyLabel.className = "ex-only";
    ov.exOnly = document.createElement("input");
    ov.exOnly.id = "ex-only-nondefault";
    ov.exOnly.setAttribute("type", "checkbox");
    onlyLabel.appendChild(ov.exOnly);
    onlyLabel.appendChild(document.createTextNode("只看不是默认的"));
    card2.appendChild(onlyLabel);
    ov.exTable = document.createElement("table");
    ov.exTable.id = "ex-table";
    ov.exTable.className = "ex-table";
    card2.appendChild(ov.exTable);
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
    id: "overrides", title: "分会话覆盖", order: 30,
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
      ab.warnings.appendChild(LX.el("span", { class: "muted", text: "没有" }));
    } else {
      warns.forEach(function (w) {
        ab.warnings.appendChild(LX.el("div", { class: "warn", text: String(w) }));
      });
    }
    clearNode(ab.backups);
    var list = (data && data.backups) || [];
    if (!list.length) {
      ab.backups.appendChild(LX.el("span", { class: "muted", text: "还没有备份" }));
    } else {
      var table = document.createElement("table");
      table.className = "ex-table";
      var thr = document.createElement("tr");
      ["名字", "大小", "时间"].forEach(function (t) {
        var th = document.createElement("th");
        th.textContent = t;
        thr.appendChild(th);
      });
      table.appendChild(thr);
      list.forEach(function (b) {
        var tr = document.createElement("tr");
        var c1 = document.createElement("td");
        c1.textContent = b.name;
        var c2 = document.createElement("td");
        c2.textContent = (Number(b.size) / 1024).toFixed(1) + " KB";
        var c3 = document.createElement("td");
        try {
          c3.textContent = new Date(Number(b.mtime) * 1000).toLocaleString();
        } catch (e) {
          c3.textContent = String(b.mtime);
        }
        tr.appendChild(c1);
        tr.appendChild(c2);
        tr.appendChild(c3);
        table.appendChild(tr);
      });
      ab.backups.appendChild(table);
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
    var card = LX.el("div", { class: "card" }, [
      LX.el("h3", { text: "关于" })
    ]);
    function row(label, id) {
      var line = LX.el("div", { class: "about-row" });
      line.appendChild(LX.el("span", { class: "about-label", text: label + "：" }));
      var val = LX.el("span", { id: id });
      line.appendChild(val);
      return { line: line, val: val };
    }
    var r1 = row("插件版本", "about-version");
    var r2 = row("配置版本", "about-schema");
    ab.version = r1.val;
    ab.schema = r2.val;
    card.appendChild(r1.line);
    card.appendChild(r2.line);
    var wline = LX.el("div", { class: "about-row" });
    wline.appendChild(LX.el("span", { class: "about-label", text: "启动提示：" }));
    ab.warnings = LX.el("span", { id: "about-warnings" });
    wline.appendChild(ab.warnings);
    card.appendChild(wline);
    var bline = LX.el("div", { class: "about-row" });
    bline.appendChild(LX.el("span", { class: "about-label", text: "备份文件：" }));
    ab.backups = LX.el("span", { id: "about-backups" });
    bline.appendChild(ab.backups);
    card.appendChild(bline);
    var note = document.createElement("p");
    note.className = "muted";
    note.textContent = "配置在 AstrBot 自带的配置页里也能改，两边是同一份数据；" +
      "这里改完保存立即生效，不用重载插件。";
    card.appendChild(note);
    var btn = LX.el("button", { class: "small secondary", text: "刷新" });
    btn.id = "about-refresh";
    btn.addEventListener("click", loadAbout);
    card.appendChild(btn);
    root.appendChild(card);
    loadAbout();
  }

  LX.registerTab({
    id: "about", title: "关于", order: 90,
    mount: mountAbout,
    onShow: loadAbout
  });
})();
