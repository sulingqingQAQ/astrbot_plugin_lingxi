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

  var toastTimer = null;
  function toast(message, kind) {
    var t = $("#toast");
    if (!t) return;
    t.textContent = String(message);
    t.className = "toast show " + (kind || "ok");
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
    state: { meta: null, config: null, choices: {}, dirty: new Map(), overridesText: null, overridesDirty: false },
    _tabs: [],
    _changeFns: [],
    _reloadFns: [],
    _errors: new Map(), // key scope:path -> message
    _bannerErrors: []
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
    refreshSavebar();
    renderTabDots();
    fireChange(scope, path);
  };

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
    LX._errors.clear();
    LX._bannerErrors = [];
    renderBanner();
    hideConflict();
    refreshSavebar();
    renderTabDots();
    fireChange(null, null);
    fireReload();
  };

  LX.markOverridesDirty = function (text) {
    var base = LX.state.config ? LX.state.config.session_overrides : "";
    if ((text || "") === (base || "")) {
      LX.state.overridesText = null;
      LX.state.overridesDirty = false;
    } else {
      LX.state.overridesText = text;
      LX.state.overridesDirty = true;
    }
    refreshSavebar();
    fireChange(null, "session_overrides");
  };

  // ---- 标签 ----
  LX.registerTab = function (tab) {
    if (!tab || !tab.id) return;
    var found = null;
    LX._tabs.forEach(function (t) { if (t.id === tab.id) found = t; });
    if (found) {
      found.title = tab.title || found.title;
      found.order = tab.order === undefined ? found.order : tab.order;
      if (tab.mount) found.mount = tab.mount;
      if (tab.onShow) found.onShow = tab.onShow;
      if (tab.isDirty) found.isDirty = tab.isDirty;
    } else {
      LX._tabs.push({
        id: tab.id, title: tab.title || tab.id,
        order: tab.order === undefined ? 99 : tab.order,
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
    LX._tabs.forEach(function (t) {
      var b = el("button", {
        class: t.id === activeTabId ? "active" : "",
        dataset: { tab: t.id },
        on: { click: function () { showTab(t.id); } }
      }, [t.title]);
      b.setAttribute("data-tab", t.id);
      bar.appendChild(b);
      var sec = document.getElementById("tab-" + t.id);
      if (!sec) {
        sec = el("section", { id: "", class: "tab" });
        sec.id = "tab-" + t.id;
        host.appendChild(sec);
      }
      sec.classList.toggle("active", t.id === activeTabId);
    });
    renderTabDots();
  }

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
          s.textContent = "●";
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
        b.classList.toggle("active", b.getAttribute("data-tab") === id);
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
    return LX.state.dirty.size + (LX.state.overridesDirty ? 1 : 0);
  }
  LX._dirtyCount = dirtyCount;

  function refreshSavebar() {
    var n = dirtyCount();
    var dc = $("#dirty-count");
    if (dc) dc.textContent = n > 0 ? n + " 项未保存" : "";
    var save = $("#btn-save");
    if (save) save.disabled = n === 0;
  }

  function renderBanner() {
    var banner = $("#banner");
    if (!banner) return;
    while (banner.firstChild) banner.removeChild(banner.firstChild);
    var warns = (LX.state.config && LX.state.config.warnings) || [];
    var errs = LX._bannerErrors || [];
    if (!warns.length && !errs.length) {
      banner.className = "banner hidden";
      return;
    }
    banner.className = "banner show" + (errs.length ? " error" : "");
    errs.forEach(function (m) {
      banner.appendChild(el("div", { class: "banner-err", text: String(m) }));
    });
    if (warns.length) {
      var box = el("div", { class: "warn-box" });
      var showAll = warns.length <= 3 || box.getAttribute("data-open") === "1";
      var list = showAll ? warns : warns.slice(0, 3);
      list.forEach(function (w) { box.appendChild(el("div", { class: "warn", text: String(w) })); });
      if (warns.length > 3) {
        var btn = el("button", { class: "small secondary", text: showAll ? "收起" : "还有 " + (warns.length - 3) + " 条，点我展开" });
        btn.addEventListener("click", function () {
          box.setAttribute("data-open", showAll ? "0" : "1");
          renderBanner();
        });
        box.appendChild(btn);
      }
      banner.appendChild(box);
    }
  }

  function hideConflict() {
    var c = $("#conflict-banner");
    if (c) c.className = "banner conflict hidden";
  }
  function showConflict() {
    var c = $("#conflict-banner");
    if (c) c.className = "banner conflict show";
  }

  function showFatal(msg) {
    var f = $("#fatal");
    if (!f) return;
    while (f.firstChild) f.removeChild(f.firstChild);
    f.className = "fatal show";
    f.appendChild(el("span", { text: msg + " " }));
    var btn = el("button", { class: "small primary", text: "重试" });
    btn.addEventListener("click", function () {
      f.className = "fatal hidden";
      boot();
    });
    f.appendChild(btn);
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
    if (LX.state.overridesDirty && LX.state.overridesText === (cfg.session_overrides || "")) {
      LX.state.overridesText = null;
      LX.state.overridesDirty = false;
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
    if (Array.isArray(value)) return value.length + " 行";
    if (typeof value === "string") {
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

  LX._openDiff = function () {
    var list = $("#diff-list");
    if (!list) return;
    while (list.firstChild) list.removeChild(list.firstChild);
    if (dirtyCount() === 0) return;
    LX.state.dirty.forEach(function (item) {
      var f = LX.fieldMeta(item.scope, item.path);
      var line = scopeTitle(item.scope) + " · " + itemTitle(item.scope, item.path) +
        "（" + item.path + "）：" + fmtVal(f, loadedValue(item.scope, item.path)) +
        " → " + fmtVal(f, item.value);
      var div = el("div", { class: "diff-item", text: line });
      div.setAttribute("data-scope", item.scope);
      div.setAttribute("data-path", item.path);
      list.appendChild(div);
    });
    if (LX.state.overridesDirty) {
      var n = (LX.state.overridesText || "").split("\n").length;
      var odiv = el("div", { class: "diff-item", text: "分会话覆盖表已修改（" + n + " 行）" });
      odiv.setAttribute("data-scope", "overrides");
      odiv.setAttribute("data-path", "session_overrides");
      list.appendChild(odiv);
    }
    var panel = $("#diff-panel");
    if (panel) panel.classList.remove("hidden");
  };

  LX._closeDiff = function () {
    var panel = $("#diff-panel");
    if (panel) panel.classList.add("hidden");
  };

  LX._doSave = async function () {
    var changes = [];
    LX.state.dirty.forEach(function (item) {
      changes.push({ scope: item.scope, path: item.path, value: clone(item.value) });
    });
    var body = { revision: LX.state.config ? LX.state.config.revision : "", changes: changes };
    if (LX.state.overridesDirty) body.session_overrides = LX.state.overridesText;
    var btn = $("#btn-confirm-save");
    if (btn) btn.disabled = true;
    try {
      var data = await LX.api.post("config/save", body);
      if (data.saved) {
        LX.state.config = data.config;
        LX.state.dirty.clear();
        LX.state.overridesText = null;
        LX.state.overridesDirty = false;
        LX._errors.clear();
        LX._bannerErrors = [];
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
        errs.forEach(function (er) {
          if (er && er.scope && er.path) {
            LX._errors.set(er.scope + ":" + er.path, er.message || "没通过检查");
          } else if (er && er.message) {
            LX._bannerErrors.push(er.message);
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
      if (btn) btn.disabled = false;
    }
  };

  // ---- 按钮接线 ----
  function wireButtons() {
    var save = $("#btn-save");
    if (save) save.addEventListener("click", function () {
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
      discard.addEventListener("click", function () {
        if (!armed) {
          if (dirtyCount() === 0) return;
          armed = true;
          discard.textContent = "再点一次确认放弃";
          discard.classList.add("armed");
          timer = setTimeout(function () {
            armed = false;
            discard.textContent = "放弃";
            discard.classList.remove("armed");
          }, 3000);
        } else {
          if (timer) clearTimeout(timer);
          armed = false;
          discard.textContent = "放弃";
          discard.classList.remove("armed");
          try { LX.discardAll(); } catch (e) { console.error(e); }
          toast("已放弃未保存的改动", "ok");
        }
      });
    }
  }

  async function boot() {
    try {
      if (bridge && bridge.ready) await bridge.ready();
      await LX.reload();
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
