/* 灵犀配置面板 · 壳（UI-4a）。
 * 纯静态、原生 JS，无任何 CDN / 第三方库 / 外部字体。
 * 对外接口见 UI-0 §4（window.LX）；表单与覆盖表标签由 form.js / tab_overrides.js 通过 LX.registerTab 挂进来，
 * 那两个文件缺失时标签栏只是少标签，不报错。 */
(function () {
  "use strict";

  var bridge = window.AstrBotPluginPage;

  function deepEqual(a, b) {
    if (a === b) return true;
    if (typeof a !== typeof b) return false;
    if (a === null || b === null) return a === b;
    if (typeof a !== "object") return a === b;
    if (Array.isArray(a) !== Array.isArray(b)) return false;
    if (Array.isArray(a)) {
      if (a.length !== b.length) return false;
      for (var i = 0; i < a.length; i++) {
        if (!deepEqual(a[i], b[i])) return false;
      }
      return true;
    }
    var ka = Object.keys(a);
    var kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    for (var j = 0; j < ka.length; j++) {
      if (!Object.prototype.hasOwnProperty.call(b, ka[j])) return false;
      if (!deepEqual(a[ka[j]], b[ka[j]])) return false;
    }
    return true;
  }

  function clone(v) {
    return v === undefined ? undefined : JSON.parse(JSON.stringify(v));
  }

  // textarea 的 .value 会把换行规整成 \n；库里存的覆盖表可能是 \r\n。比较前双方都规整，免得"没改也算改"。
  function normNl(s) {
    return String(s === undefined || s === null ? "" : s).replace(/\r\n?/g, "\n");
  }

  // ---- el：建元素（attrs 里 html 键会被忽略，不要用它拼用户数据） ----
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === undefined || v === null || v === false) return;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = String(v);
      else if (k === "html") return; // 故意不支持：用户数据一律走 textContent
      else if (k === "on" && typeof v === "object") {
        Object.keys(v).forEach(function (ev) {
          if (typeof v[ev] === "function") node.addEventListener(ev, v[ev]);
        });
      } else if (k === "dataset" && typeof v === "object") {
        Object.keys(v).forEach(function (dk) { node.dataset[dk] = String(v[dk]); });
      } else if (k === "value" && ("value" in node)) node.value = v;
      else if ((k === "checked" || k === "disabled") && (k in node)) node[k] = !!v;
      else node.setAttribute(k, v === true ? "" : String(v));
    });
    var list = children === undefined ? [] : (Array.isArray(children) ? children : [children]);
    list.forEach(function (c) {
      if (c === undefined || c === null || c === false) return;
      if (Array.isArray(c)) {
        c.forEach(function (x) {
          if (x === undefined || x === null || x === false) return;
          node.appendChild(x instanceof Node ? x : document.createTextNode(String(x)));
        });
      } else {
        node.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
      }
    });
    return node;
  }

  function $(sel) { return document.querySelector(sel); }

  // 图标由 icons.js 提供（加载顺序在 app.js 之后）；缺了就不画图标，别的照常。
  function ic(name, size) {
    return window.LX && typeof window.LX.icon === "function" ? window.LX.icon(name, size) : null;
  }

  var TOAST_ICONS = { ok: "check-circle", warn: "warning", error: "alert-circle" };
  var toastTimer = null;
  function toast(message, kind) {
    var t = $("#toast");
    if (!t) return;
    kind = TOAST_ICONS[kind] ? kind : "ok";
    while (t.firstChild) t.removeChild(t.firstChild);
    var icon = ic(TOAST_ICONS[kind], 18);
    if (icon) t.appendChild(icon);
    t.appendChild(el("span", { class: "toast-text", text: String(message) }));
    t.className = "toast show " + kind;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.className = "toast hidden"; }, kind === "error" ? 7000 : 3500);
  }

  async function apiCall(method, route, payload) {
    var fn = method === "GET" ? bridge.apiGet : bridge.apiPost;
    var resp = await fn.call(bridge, "api/" + route, payload || {});
    // 真宿主的桥接（PluginPagePage 的 api:get / api:post）会把响应解包：
    //   status === "error" → 直接让请求失败（Error(message)）；
    //   成功 → 只回传 data 部分（resp.data ?? 整个响应）。
    // 本地夹具 / 旧版桥接则可能回传整个 {status, data} 信封。两种都认：
    // 带 status 字段的按信封处理，否则把返回值本身当 data。
    if (resp && typeof resp === "object" && (resp.status === "ok" || resp.status === "error")) {
      if (resp.status !== "ok") {
        throw new Error(resp.message || "接口返回异常");
      }
      return resp.data === undefined || resp.data === null ? {} : resp.data;
    }
    if (resp === undefined || resp === null) {
      throw new Error("接口返回异常");
    }
    return resp;
  }

  var LX = {
    api: {
      get: function (route, params) { return apiCall("GET", route, params); },
      post: function (route, body) { return apiCall("POST", route, body); }
    },
    el: el,
    toast: toast,
    state: { meta: null, config: null, choices: {}, dirty: new Map(), overridesText: null, overridesDirty: false,
             rules: null, rulesDirty: false },
    _tabs: [],
    _changeFns: [],
    _reloadFns: [],
    _errors: new Map(), // key scope:path -> message
    _bannerErrors: [],   // 没归到具体项的错误条（顶部提示条）
    _overrideErrors: [], // 覆盖表被服务端拒绝的错误条；覆盖表一改文本就清
    _rulesErrors: [],    // 逐项表单被服务端拒绝的错误条；表单一动就清
    _flushers: []        // 保存 / 放弃前要先冲刷的待决防抖（form.js 注册）
  };

  // ---- 回调注册（调用时 try/catch，一个坏了不影响别的） ----
  LX.onChange = function (fn) { LX._changeFns.push(fn); };
  LX.onReload = function (fn) { LX._reloadFns.push(fn); };
  function fireChange(scope, path) {
    LX._changeFns.forEach(function (fn) {
      try { fn(scope, path); } catch (e) { console.error(e); }
    });
  }
  function fireReload() {
    LX._reloadFns.forEach(function (fn) {
      try { fn(); } catch (e) { console.error(e); }
    });
  }

  // ---- 取值 / 暂存 ----
  function loadedValue(scope, path) {
    var cfg = LX.state.config;
    if (!cfg || !cfg[scope]) return undefined;
    var dot = path.indexOf(".");
    var block = dot < 0 ? path : path.slice(0, dot);
    var name = dot < 0 ? "" : path.slice(dot + 1);
    var b = cfg[scope][block];
    return b ? clone(b[name]) : undefined;
  }

  LX.getValue = function (scope, path) {
    var key = scope + ":" + path;
    if (LX.state.dirty.has(key)) return clone(LX.state.dirty.get(key).value);
    return loadedValue(scope, path);
  };

  LX.setValue = function (scope, path, value) {
    var key = scope + ":" + path;
    if (deepEqual(value, loadedValue(scope, path))) {
      LX.state.dirty.delete(key);
    } else {
      LX.state.dirty.set(key, { scope: scope, path: path, value: clone(value) });
    }
    // 用户动了这一项：上次保存时服务端对它的指责作废，随后的 fireChange 会让表单刷新红字
    LX._errors.delete(key);
    refreshSavebar();
    renderTabDots();
    fireChange(scope, path);
  };

  // 撤销某一项的暂存（回到已加载的值）。控件里的输入无效时用：无效输入不能算改动，更不能留着上一次的有效暂存
  LX.revertValue = function (scope, path) {
    var key = scope + ":" + path;
    LX._errors.delete(key);
    LX.state.dirty.delete(key);
    refreshSavebar();
    renderTabDots();
    fireChange(scope, path);
  };

  // 待决防抖的冲刷：文本 / 列表控件有 150ms 防抖，点保存 / 放弃之前先把它们立刻落到暂存里
  LX.addFlusher = function (fn) {
    if (typeof fn === "function") LX._flushers.push(fn);
  };
  function flushPending() {
    LX._flushers.forEach(function (fn) {
      try { fn(); } catch (e) { console.error(e); }
    });
  }

  LX.isDirty = function (scope, path) { return LX.state.dirty.has(scope + ":" + path); };

  LX.fieldMeta = function (scope, path) {
    var meta = LX.state.meta;
    if (!meta || !meta.blocks || !meta.blocks[scope]) return null;
    var blocks = meta.blocks[scope];
    for (var i = 0; i < blocks.length; i++) {
      var fields = blocks[i].fields || [];
      for (var j = 0; j < fields.length; j++) {
        if (fields[j].path === path) return fields[j];
      }
    }
    return null;
  };

  LX.getError = function (scope, path) {
    var v = LX._errors.get(scope + ":" + path);
    return v === undefined ? null : v;
  };

  LX.discardAll = function () {
    LX.state.dirty.clear();
    LX.state.overridesText = null;
    LX.state.overridesDirty = false;
    LX.state.rules = null;
    LX.state.rulesDirty = false;
    LX._errors.clear();
    LX._bannerErrors = [];
    LX._overrideErrors = [];
    LX._rulesErrors = [];
    renderBanner();
    hideConflict();
    refreshSavebar();
    renderTabDots();
    fireChange(null, null);
    fireReload();
  };

  LX.markOverridesDirty = function (text) {
    var base = LX.state.config ? LX.state.config.session_overrides : "";
    var nt = normNl(text);
    if (nt === normNl(base)) {
      LX.state.overridesText = null;
      LX.state.overridesDirty = false;
    } else {
      LX.state.overridesText = nt;
      LX.state.overridesDirty = true;
    }
    // 覆盖表文本一改，上次保存时服务端对覆盖表的指责就作废了
    if (LX._overrideErrors.length) {
      LX._overrideErrors = [];
      renderBanner();
    }
    refreshSavebar();
    fireChange(null, "session_overrides");
  };

  /* SR-UI：逐项表单（条目列表）的暂存。条目是 {scope, session, block, items} 的对象数组，
   * 顺序无关：先按 (scope, session, block) 排好再比，免得只是排了个序也被算成改动。 */
  function normEntries(list) {
    if (!Array.isArray(list)) return [];
    return list.map(function (e) {
      var copy = {
        scope: e && e.scope, session: String((e && e.session) === undefined ? "" : e.session).trim(),
        block: e && e.block, items: {}
      };
      var items = (e && e.items) || {};
      Object.keys(items).sort().forEach(function (k) { copy.items[k] = clone(items[k]); });
      return copy;
    }).sort(function (a, b) {
      var ka = a.scope + "|" + a.session + "|" + a.block;
      var kb = b.scope + "|" + b.session + "|" + b.block;
      return ka < kb ? -1 : (ka > kb ? 1 : 0);
    });
  }

  LX.markRulesDirty = function (entries) {
    var base = LX.state.config && LX.state.config.session_rules;
    if (deepEqual(normEntries(entries), normEntries(base))) {
      LX.state.rules = null;
      LX.state.rulesDirty = false;
    } else {
      LX.state.rules = entries;
      LX.state.rulesDirty = true;
    }
    // 表单一动，上次保存时服务端对它的指责就作废了
    if (LX._rulesErrors.length) {
      LX._rulesErrors = [];
      renderBanner();
    }
    refreshSavebar();
    renderTabDots();
    fireChange(null, "session_rules");
  };

  // ---- 标签 ----
  // 标签图标：registerTab 可以传 icon（LX.icon 的名字）；没传就按标签 id 找默认图标，再没有用 list。
  var TAB_ICONS = { "private": "chat", "group": "group", overrides: "sliders", about: "info", diag: "pulse" };

  LX.registerTab = function (tab) {
    if (!tab || !tab.id) return;
    var found = null;
    LX._tabs.forEach(function (t) { if (t.id === tab.id) found = t; });
    if (found) {
      found.title = tab.title || found.title;
      found.order = tab.order === undefined ? found.order : tab.order;
      if (tab.icon) found.icon = tab.icon;
      if (tab.mount) found.mount = tab.mount;
      if (tab.onShow) found.onShow = tab.onShow;
      if (tab.isDirty) found.isDirty = tab.isDirty;
    } else {
      LX._tabs.push({
        id: tab.id, title: tab.title || tab.id,
        order: tab.order === undefined ? 99 : tab.order,
        icon: tab.icon || TAB_ICONS[tab.id] || "list",
        mount: tab.mount || null, onShow: tab.onShow || null,
        isDirty: tab.isDirty || null, _mounted: false
      });
    }
    LX._tabs.sort(function (a, b) { return a.order - b.order; });
    renderTabs();
  };

  var activeTabId = null;
  function tabById(id) {
    for (var i = 0; i < LX._tabs.length; i++) {
      if (LX._tabs[i].id === id) return LX._tabs[i];
    }
    return null;
  }

  function renderTabs() {
    var bar = $("#tabs");
    var host = $("#tab-host");
    if (!bar || !host) return;
    while (bar.firstChild) bar.removeChild(bar.firstChild);
    bar.setAttribute("role", "tablist");
    LX._tabs.forEach(function (t) {
      var kids = [];
      var icon = ic(t.icon, 16);
      if (icon) kids.push(icon);
      kids.push(el("span", { class: "tab-label", text: t.title }));
      var b = el("button", {
        class: t.id === activeTabId ? "active" : "",
        dataset: { tab: t.id },
        on: { click: function () { showTab(t.id); } }
      }, kids);
      b.setAttribute("data-tab", t.id);
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", t.id === activeTabId ? "true" : "false");
      b.setAttribute("aria-controls", "tab-" + t.id);
      bar.appendChild(b);
      var sec = document.getElementById("tab-" + t.id);
      if (!sec) {
        sec = el("section", { id: "", class: "tab" });
        sec.id = "tab-" + t.id;
        sec.setAttribute("role", "tabpanel");
        host.appendChild(sec);
      }
      sec.classList.toggle("active", t.id === activeTabId);
    });
    renderTabDots();
  }

  // 标签上的"有未保存改动"小圆点：一个空 span（样式画圆），文字说明放 aria-label / title
  function renderTabDots() {
    LX._tabs.forEach(function (t) {
      var dirty = false;
      try { dirty = t.isDirty ? !!t.isDirty() : false; } catch (e) { console.error(e); }
      var btns = document.querySelectorAll('#tabs button[data-tab="' + t.id + '"]');
      Array.prototype.forEach.call(btns, function (b) {
        var dot = b.querySelector(".dot");
        if (dirty && !dot) {
          var s = document.createElement("span");
          s.className = "dot";
          s.setAttribute("role", "img");
          s.setAttribute("aria-label", "有未保存的改动");
          s.setAttribute("title", "有未保存的改动");
          b.appendChild(s);
        } else if (!dirty && dot) {
          dot.remove();
        }
      });
    });
  }

  function showTab(id) {
    var t = tabById(id);
    if (!t) return;
    activeTabId = id;
    try {
      if (window.location.hash !== "#tab=" + id) {
        window.location.hash = "#tab=" + id;
      }
    } catch (e) { /* 忽略 */ }
    var bar = $("#tabs");
    if (bar) {
      Array.prototype.forEach.call(bar.querySelectorAll("button"), function (b) {
        var on = b.getAttribute("data-tab") === id;
        b.classList.toggle("active", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
      });
    }
    var host = $("#tab-host");
    if (host) {
      Array.prototype.forEach.call(host.querySelectorAll(".tab"), function (s) {
        s.classList.toggle("active", s.id === "tab-" + id);
      });
    }
    var sec = document.getElementById("tab-" + id);
    if (sec && !t._mounted && t.mount) {
      t._mounted = true;
      try { t.mount(sec); } catch (e) { console.error(e); }
    }
    if (t.onShow) {
      try { t.onShow(); } catch (e) { console.error(e); }
    }
  }
  LX._showTab = showTab;

  // ---- 顶栏 / 提示条 ----
  function dirtyCount() {
    return LX.state.dirty.size + (LX.state.overridesDirty ? 1 : 0) + (LX.state.rulesDirty ? 1 : 0);
  }
  LX._dirtyCount = dirtyCount;

  function refreshSavebar() {
    var n = dirtyCount();
    var dc = $("#dirty-count");
    if (dc) dc.textContent = n > 0 ? n + " 项未保存" : "";
    var save = $("#btn-save");
    if (save) save.disabled = n === 0;
  }

  var warnOpen = false; // 提示条“还有 N 条”是否展开

  function renderBanner() {
    var banner = $("#banner");
    if (!banner) return;
    while (banner.firstChild) banner.removeChild(banner.firstChild);
    var warns = (LX.state.config && LX.state.config.warnings) || [];
    var errs = (LX._bannerErrors || []).concat(LX._overrideErrors || []).concat(LX._rulesErrors || []);
    if (!warns.length && !errs.length) {
      banner.className = "banner hidden";
      return;
    }
    banner.className = "banner alert show " + (errs.length ? "error alert-danger" : "alert-warn");
    var icon = ic(errs.length ? "alert-circle" : "warning", 18);
    if (icon) banner.appendChild(el("span", { class: "alert-icon" }, [icon]));
    var body = el("div", { class: "alert-body" });
    banner.appendChild(body);
    errs.forEach(function (m) {
      body.appendChild(el("div", { class: "banner-err", text: String(m) }));
    });
    if (warns.length) {
      var box = el("div", { class: "warn-box" });
      box.appendChild(el("div", { class: "alert-title", text: "加载配置时有 " + warns.length + " 条提示" }));
      // 展开状态存在模块变量里：每次 renderBanner 都会重建 box，存在节点上等于没存
      var showAll = warns.length <= 3 || warnOpen;
      var list = showAll ? warns : warns.slice(0, 3);
      list.forEach(function (w) { box.appendChild(el("div", { class: "warn", text: String(w) })); });
      if (warns.length > 3) {
        var btn = el("button", { class: "small secondary", text: showAll ? "收起" : "还有 " + (warns.length - 3) + " 条，点我展开" });
        btn.addEventListener("click", function () {
          warnOpen = !warnOpen;
          renderBanner();
        });
        box.appendChild(btn);
      }
      body.appendChild(box);
    }
  }

  function hideConflict() {
    var c = $("#conflict-banner");
    if (c) c.className = "banner alert alert-danger conflict hidden";
  }
  function showConflict() {
    var c = $("#conflict-banner");
    if (c) c.className = "banner alert alert-danger conflict show";
  }

  function showFatal(msg) {
    var f = $("#fatal");
    if (!f) return;
    while (f.firstChild) f.removeChild(f.firstChild);
    f.className = "fatal show";
    var icon = ic("alert-circle", 20);
    if (icon) f.appendChild(el("span", { class: "alert-icon" }, [icon]));
    f.appendChild(el("div", { class: "alert-body", text: String(msg) }));
    var btn = el("button", { class: "small primary", text: "重试" });
    btn.addEventListener("click", function () {
      while (f.firstChild) f.removeChild(f.firstChild);
      f.className = "fatal hidden";
      boot();
    });
    f.appendChild(el("div", { class: "alert-actions" }, [btn]));
  }

  // ---- 加载 ----
  LX.reload = async function () {
    var r = await apiCall("GET", "meta", {});
    var cfg = await apiCall("GET", "config", {});
    var choices = {};
    try { choices = await apiCall("GET", "choices", {}); } catch (e) { choices = {}; }
    LX.state.meta = r;
    LX.state.config = cfg;
    LX.state.choices = choices || {};
    // 保留暂存：与新加载值相等的暂存项自动撤销
    var drop = [];
    LX.state.dirty.forEach(function (item, key) {
      if (deepEqual(item.value, loadedValue(item.scope, item.path))) drop.push(key);
    });
    drop.forEach(function (k) { LX.state.dirty.delete(k); });
    if (LX.state.overridesDirty && normNl(LX.state.overridesText) === normNl(cfg.session_overrides)) {
      LX.state.overridesText = null;
      LX.state.overridesDirty = false;
    }
    if (LX.state.rulesDirty && deepEqual(normEntries(LX.state.rules), normEntries(cfg.session_rules))) {
      LX.state.rules = null;
      LX.state.rulesDirty = false;
    }
    renderBanner();
    hideConflict();
    refreshSavebar();
    renderTabs();
    fireReload();
  };

  // ---- 改动清单与保存 ----
  function scopeTitle(scope) {
    var meta = LX.state.meta;
    if (meta && meta.scopes) {
      for (var i = 0; i < meta.scopes.length; i++) {
        if (meta.scopes[i].scope === scope) return meta.scopes[i].title || scope;
      }
    }
    return scope;
  }

  function fmtVal(field, value) {
    if (typeof value === "boolean") return value ? "开" : "关";
    if (Array.isArray(value)) return value.length + " 行";
    if (typeof value === "string") {
      if (value === "") return "（空）";
      if (field && (field.type === "text" || field.type === "string") && value.length > 80) {
        return value.slice(0, 80) + "…";
      }
      return value;
    }
    if (value === null || value === undefined) return "（空）";
    if (typeof value === "object") {
      var s = JSON.stringify(value);
      return s.length > 80 ? s.slice(0, 80) + "…" : s;
    }
    return String(value);
  }

  function itemTitle(scope, path) {
    var f = LX.fieldMeta(scope, path);
    return f && f.title ? f.title : path;
  }

  // list 项（白名单等）：只写"N 行 → M 行"看不出动了谁，改成列出新增 / 删除了哪些条目
  var LIST_DIFF_MAX = 10;
  function listItemText(s) {
    s = String(s);
    return s.length > 40 ? s.slice(0, 40) + "…" : s;
  }
  function listChunk(items) {
    var shown = items.slice(0, LIST_DIFF_MAX).map(listItemText).join("、");
    var rest = items.length - LIST_DIFF_MAX;
    return items.length + " 个：" + shown + (rest > 0 ? "，另有 " + rest + " 个" : "");
  }
  function listDiffText(oldV, newV) {
    var oldL = Array.isArray(oldV) ? oldV.map(String) : [];
    var newL = Array.isArray(newV) ? newV.map(String) : [];
    var oldSet = new Set(oldL);
    var newSet = new Set(newL);
    var seen = new Set();
    var added = [];
    newL.forEach(function (s) {
      if (!oldSet.has(s) && !seen.has(s)) { seen.add(s); added.push(s); }
    });
    seen = new Set();
    var removed = [];
    oldL.forEach(function (s) {
      if (!newSet.has(s) && !seen.has(s)) { seen.add(s); removed.push(s); }
    });
    var parts = [];
    if (added.length) parts.push("新增 " + listChunk(added));
    if (removed.length) parts.push("删除 " + listChunk(removed));
    if (!parts.length) {
      return "没有增删条目，只调整了顺序或重复项（" + oldL.length + " 行 → " + newL.length + " 行）";
    }
    return parts.join("；");
  }

  LX._openDiff = function () {
    var list = $("#diff-list");
    if (!list) return;
    while (list.firstChild) list.removeChild(list.firstChild);
    if (dirtyCount() === 0) return;
    LX.state.dirty.forEach(function (item) {
      var f = LX.fieldMeta(item.scope, item.path);
      var old = loadedValue(item.scope, item.path);
      var head = el("div", { class: "diff-head" }, [
        el("span", { class: "pill pill-accent", text: scopeTitle(item.scope) }),
        el("span", { class: "diff-title", text: itemTitle(item.scope, item.path) }),
        el("code", { class: "diff-path", text: item.path })
      ]);
      var change = el("div", { class: "diff-change" });
      if (Array.isArray(item.value) || Array.isArray(old)) {
        change.textContent = listDiffText(old, item.value);
      } else {
        var oldText = fmtVal(f, old);
        var newText = fmtVal(f, item.value);
        if (oldText.length + newText.length > 56) change.classList.add("diff-stack");
        change.appendChild(el("span", { class: "diff-old", text: oldText }));
        change.appendChild(el("span", { class: "diff-arrow", text: "→" }));
        change.appendChild(el("span", { class: "diff-new", text: newText }));
      }
      var div = el("div", { class: "diff-item" }, [head, change]);
      div.setAttribute("data-scope", item.scope);
      div.setAttribute("data-path", item.path);
      list.appendChild(div);
    });
    if (LX.state.overridesDirty) {
      var n = (LX.state.overridesText || "").split("\n").length;
      var odiv = el("div", { class: "diff-item" }, [
        el("div", { class: "diff-head" }, [
          el("span", { class: "pill pill-accent", text: "覆盖表" }),
          el("span", { class: "diff-title", text: "分会话覆盖表已修改（" + n + " 行）" })
        ])
      ]);
      odiv.setAttribute("data-scope", "overrides");
      odiv.setAttribute("data-path", "session_overrides");
      list.appendChild(odiv);
    }
    if (LX.state.rulesDirty) {
      var nRules = (LX.state.rules || []).length;
      var rdiv = el("div", { class: "diff-item" }, [
        el("div", { class: "diff-head" }, [
          el("span", { class: "pill pill-accent", text: "逐项表单" }),
          el("span", { class: "diff-title", text: "分会话逐项表单已修改（" + nRules + " 个会话）" })
        ])
      ]);
      rdiv.setAttribute("data-scope", "rules");
      rdiv.setAttribute("data-path", "session_rules");
      list.appendChild(rdiv);
    }
    var panel = $("#diff-panel");
    if (panel) panel.classList.remove("hidden");
    var go = $("#btn-confirm-save");
    if (go) go.focus();
  };

  LX._closeDiff = function () {
    var panel = $("#diff-panel");
    if (!panel) return;
    var wasOpen = !panel.classList.contains("hidden");
    panel.classList.add("hidden");
    // 键盘用户关掉清单后，焦点回到保存按钮
    var save = $("#btn-save");
    if (wasOpen && save && !save.disabled) save.focus();
  };

  LX._doSave = async function () {
    var changes = [];
    LX.state.dirty.forEach(function (item) {
      changes.push({ scope: item.scope, path: item.path, value: clone(item.value) });
    });
    var body = { revision: LX.state.config ? LX.state.config.revision : "", changes: changes };
    if (LX.state.overridesDirty) body.session_overrides = LX.state.overridesText;
    if (LX.state.rulesDirty) body.session_rules = LX.state.rules;
    var btn = $("#btn-confirm-save");
    if (btn) { btn.disabled = true; btn.classList.add("loading"); btn.setAttribute("aria-busy", "true"); }
    try {
      var data = await LX.api.post("config/save", body);
      if (data.saved) {
        LX.state.config = data.config;
        LX.state.dirty.clear();
        LX.state.overridesText = null;
        LX.state.overridesDirty = false;
        LX.state.rules = null;
        LX.state.rulesDirty = false;
        LX._errors.clear();
        LX._bannerErrors = [];
        LX._overrideErrors = [];
        LX._rulesErrors = [];
        LX._closeDiff();
        renderBanner();
        hideConflict();
        refreshSavebar();
        renderTabDots();
        if (data.reloaded === false && data.reload_note) {
          toast("已保存，但要重载插件才生效：" + data.reload_note, "warn");
        } else {
          toast("已保存", "ok");
        }
        fireChange(null, null);
        fireReload();
      } else if (data.conflict) {
        LX._closeDiff();
        showConflict();
        toast("配置在别处被改过，请先刷新", "warn");
      } else {
        var errs = data.errors || [];
        LX._errors.clear();
        LX._bannerErrors = [];
        LX._overrideErrors = [];
        LX._rulesErrors = [];
        errs.forEach(function (er) {
          if (er && er.scope && er.path) {
            LX._errors.set(er.scope + ":" + er.path, er.message || "没通过检查");
          } else if (er && er.message) {
            // 覆盖表 / 逐项表单的错误单独记：它们一改就该消失，别的未归属错误不受影响
            if (er.path === "session_overrides") LX._overrideErrors.push(er.message);
            else if (er.path === "session_rules") LX._rulesErrors.push(er.message);
            else LX._bannerErrors.push(er.message);
          }
        });
        LX._closeDiff();
        renderBanner();
        refreshSavebar();
        toast("有 " + errs.length + " 项没通过检查", "error");
        fireChange(null, null);
      }
    } catch (e) {
      toast("保存失败：" + (e && e.message ? e.message : String(e)), "error");
    } finally {
      if (btn) { btn.disabled = false; btn.classList.remove("loading"); btn.removeAttribute("aria-busy"); }
    }
  };

  // ---- 按钮接线 ----
  function wireButtons() {
    var save = $("#btn-save");
    if (save) save.addEventListener("click", function () {
      flushPending(); // 刚输入的最后几个字符还在 150ms 防抖里，先落到暂存再看清单
      if (dirtyCount() === 0) return;
      try { LX._openDiff(); } catch (e) { console.error(e); }
    });
    var cancel = $("#btn-cancel-save");
    if (cancel) cancel.addEventListener("click", function () { LX._closeDiff(); });
    var confirmBtn = document.getElementById("btn-confirm-save");
    if (confirmBtn) confirmBtn.addEventListener("click", function () { LX._doSave(); });
    var refresh = $("#btn-refresh");
    if (refresh) refresh.addEventListener("click", function () {
      LX.reload().then(function () {
        toast("已刷新", "ok");
      }).catch(function (e) {
        toast("刷新失败：" + (e && e.message ? e.message : String(e)), "error");
      });
    });
    var discard = $("#btn-discard");
    if (discard) {
      var armed = false;
      var timer = null;
      var label = discard.querySelector(".btn-label") || discard; // 按钮里的文字节点，改它不会动按钮本身的结构
      discard.addEventListener("click", function () {
        // 先冲刷待决防抖：否则放弃之后 150ms 它又把刚输入的文字暂存回来
        flushPending();
        if (!armed) {
          if (dirtyCount() === 0) return;
          armed = true;
          label.textContent = "再点一次确认放弃";
          discard.classList.add("armed");
          timer = setTimeout(function () {
            armed = false;
            label.textContent = "放弃";
            discard.classList.remove("armed");
          }, 3000);
        } else {
          if (timer) clearTimeout(timer);
          armed = false;
          label.textContent = "放弃";
          discard.classList.remove("armed");
          try { LX.discardAll(); } catch (e) { console.error(e); }
          toast("已放弃未保存的改动", "ok");
        }
      });
    }
    // Esc 关掉改动清单（键盘用户不用摸到"再想想"）
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      var panel = $("#diff-panel");
      if (panel && !panel.classList.contains("hidden")) LX._closeDiff();
    });
  }

  // 首次加载时先摆一张骨架屏，免得一片空白；加载完（或出错）就撤掉
  function showBootSkeleton() {
    var host = $("#tab-host");
    if (!host || host.querySelector(".boot-skeleton")) return;
    var card = el("div", { class: "card boot-skeleton", "aria-hidden": "true" }, [
      el("span", { class: "skeleton skel-title" }),
      el("span", { class: "skeleton skel-line" }),
      el("span", { class: "skeleton skel-line skel-short" }),
      el("span", { class: "skeleton skel-block" })
    ]);
    host.appendChild(card);
  }
  function hideBootSkeleton() {
    var host = $("#tab-host");
    var s = host && host.querySelector(".boot-skeleton");
    if (s) s.remove();
  }

  async function boot() {
    showBootSkeleton();
    try {
      if (bridge && bridge.ready) await bridge.ready();
      await LX.reload();
      hideBootSkeleton();
      var first = null;
      try {
        var m = (window.location.hash || "").match(/tab=([A-Za-z0-9_-]+)/);
        if (m && tabById(m[1])) first = m[1];
      } catch (e) { /* 忽略 */ }
      if (!first && LX._tabs.length) first = LX._tabs[0].id;
      if (first) showTab(first);
      else {
        var host = $("#tab-host");
        if (host && !host.firstChild) {
          host.appendChild(el("p", { class: "muted", text: "还没有可显示的标签。" }));
        }
      }
    } catch (e) {
      hideBootSkeleton();
      showFatal("配置没加载出来：" + (e && e.message ? e.message : String(e)));
    }
  }

  // 保存区 / 冲突刷新 / 两次点击放弃
  wireButtons();
  refreshSavebar();
  window.LX = LX;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
