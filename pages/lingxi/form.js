/* UI-4c 私聊 / 群聊表单（form.js）。
 * 原生 JS，无 CDN / 第三方库。页面代码里不写死任何具体配置项名；
 * 唯一例外是文件顶部 TALL_TEXT_SUFFIXES（三个让用户自己填内容的高文本框）。
 * 壳（app.js）只读：经 window.LX 注册两个标签，由 meta 动态生成表单。 */
(function () {
  "use strict";

  /* 高文本框：path 以这些后缀结尾的 text 项用高文本框（至少 12 行、可竖拉、有字数）。 */
  var TALL_TEXT_SUFFIXES = [".style_text", ".glossary_text", ".members_text"];

  var DEBOUNCE_MS = 150;
  var NARROW_HINT = 700;

  function isTallPath(path) {
    for (var i = 0; i < TALL_TEXT_SUFFIXES.length; i++) {
      var s = TALL_TEXT_SUFFIXES[i];
      if (path.slice(path.length - s.length) === s) return true;
    }
    return false;
  }

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

  function fmtDefault(value) {
    if (Array.isArray(value)) return value.length + " 项";
    if (typeof value === "string") {
      if (value.length > 80) return value.slice(0, 80) + "…";
      return value;
    }
    if (value === null || value === undefined) return "（空）";
    if (typeof value === "object") {
      var s = JSON.stringify(value);
      return s.length > 80 ? s.slice(0, 80) + "…" : s;
    }
    return String(value);
  }

  /* 每个 scope 一份 UI 状态。 */
  var views = {};
  function viewOf(scope) {
    if (!views[scope]) {
      views[scope] = {
        scope: scope, root: null, blockKey: null, search: "", onlyChanged: false,
        fields: {},  /* path -> {wrap, badgeNew, badgeDirty, resetBtn, errEl} */
        blockBtns: {}, /* key -> {btn, count} */
        blockSelect: null, itemsHost: null, navHost: null, emptyEl: null,
        searchInput: null, onlyChangedInput: null
      };
    }
    return views[scope];
  }

  function blocksOf(scope) {
    var meta = window.LX.state.meta;
    if (!meta || !meta.blocks || !meta.blocks[scope]) return [];
    return meta.blocks[scope];
  }

  function matchField(field, query, onlyChanged, scope) {
    if (onlyChanged && !window.LX.isDirty(scope, field.path)) return false;
    if (!query) return true;
    var q = query.toLowerCase();
    var hay = [field.title || "", field.hint || "", field.path || ""];
    for (var i = 0; i < hay.length; i++) {
      if (String(hay[i]).toLowerCase().indexOf(q) >= 0) return true;
    }
    return false;
  }

  function dirtyCountInBlock(scope, block) {
    var n = 0;
    var fields = block.fields || [];
    for (var i = 0; i < fields.length; i++) {
      if (window.LX.isDirty(scope, fields[i].path)) n++;
    }
    return n;
  }

  /* ---- 项标题行（骨架）：之后控件会在此基础上逐类型补 ---- */
  function renderFieldHead(LX, view, block, field) {
    var wrap = LX.el("div", { class: "field", dataset: { scope: view.scope, path: field.path } });
    var title = LX.el("span", { class: "field-title", text: field.title || field.path });
    wrap.appendChild(title);
    var badgeNew = LX.el("span", { class: "badge badge-new" + (field.new_default ? "" : " hidden"), text: "新默认待定" });
    var badgeDirty = LX.el("span", {
      class: "badge badge-dirty" + (LX.isDirty(view.scope, field.path) ? "" : " hidden"),
      text: "已改动"
    });
    wrap.appendChild(badgeNew);
    wrap.appendChild(badgeDirty);
    if (field.hint) wrap.appendChild(LX.el("div", { class: "field-hint", text: field.hint }));
    var defLine = LX.el("div", { class: "field-default muted", text: "默认：" + fmtDefault(field.default) });
    wrap.appendChild(defLine);
    var cur = LX.getValue(view.scope, field.path);
    var resetBtn = LX.el("button", {
      class: "small secondary field-reset", text: "恢复默认",
      disabled: deepEqual(cur, field.default)
    });
    resetBtn.addEventListener("click", function () {
      LX.setValue(view.scope, field.path, field.default);
    });
    wrap.appendChild(resetBtn);
    var errEl = LX.el("div", { class: "field-error" });
    var serverErr = LX.getError(view.scope, field.path);
    if (serverErr) errEl.textContent = serverErr;
    wrap.appendChild(errEl);
    view.fields[field.path] = { wrap: wrap, badgeNew: badgeNew, badgeDirty: badgeDirty, resetBtn: resetBtn, errEl: errEl, field: field, blockKey: block.key };
    return wrap;
  }

  function currentBlock(view) {
    var blocks = blocksOf(view.scope);
    if (!blocks.length) return null;
    for (var i = 0; i < blocks.length; i++) {
      if (blocks[i].key === view.blockKey) return blocks[i];
    }
    return blocks[0];
  }

  function visibleBlocks(view) {
    var out = [];
    var blocks = blocksOf(view.scope);
    for (var i = 0; i < blocks.length; i++) {
      var fields = blocks[i].fields || [];
      var any = false;
      for (var j = 0; j < fields.length; j++) {
        if (matchField(fields[j], view.search, view.onlyChanged, view.scope)) { any = true; break; }
      }
      if (any) out.push(blocks[i]);
    }
    return out;
  }

  function renderNav(LX, view) {
    var nav = view.navHost;
    while (nav.firstChild) nav.removeChild(nav.firstChild);
    view.blockBtns = {};
    var blocks = visibleBlocks(view);
    for (var i = 0; i < blocks.length; i++) {
      (function (block) {
        var n = dirtyCountInBlock(view.scope, block);
        var btn = LX.el("button", {
          class: "block-btn" + (currentBlock(view) && currentBlock(view).key === block.key ? " active" : "")
        });
        btn.appendChild(LX.el("span", { class: "block-btn-title", text: block.title || block.key }));
        var count = LX.el("span", { class: "block-count" + (n > 0 ? "" : " hidden"), text: String(n) });
        btn.appendChild(count);
        btn.addEventListener("click", function () {
          view.blockKey = block.key;
          renderAll(LX, view);
        });
        nav.appendChild(btn);
        view.blockBtns[block.key] = { btn: btn, count: count };
      })(blocks[i]);
    }
    if (view.blockSelect) {
      while (view.blockSelect.firstChild) view.blockSelect.removeChild(view.blockSelect.firstChild);
      for (var k = 0; k < blocks.length; k++) {
        var opt = document.createElement("option");
        opt.value = blocks[k].key;
        opt.textContent = blocks[k].title || blocks[k].key;
        if (currentBlock(view) && currentBlock(view).key === blocks[k].key) opt.selected = true;
        view.blockSelect.appendChild(opt);
      }
    }
  }

  function renderItems(LX, view) {
    var host = view.itemsHost;
    while (host.firstChild) host.removeChild(host.firstChild);
    view.fields = {};
    var block = currentBlock(view);
    var vis = visibleBlocks(view);
    var shown = null;
    for (var i = 0; i < vis.length; i++) {
      if (block && vis[i].key === block.key) shown = block;
    }
    if (!shown) {
      if (!vis.length) {
        var empty = LX.el("div", { class: "form-empty muted", text: "没有匹配的项" });
        host.appendChild(empty);
        view.emptyEl = empty;
        return;
      }
      view.blockKey = vis[0].key;
      shown = vis[0];
    }
    view.emptyEl = null;
    var head = LX.el("div", { class: "block-head" });
    head.appendChild(LX.el("h3", { text: shown.title || shown.key }));
    if (shown.hint) head.appendChild(LX.el("div", { class: "block-hint muted", text: shown.hint }));
    if (shown.overridable === false) {
      head.appendChild(LX.el("div", { class: "block-locked muted", text: "这一块只能在全局设置，不能被分会话覆盖" }));
    }
    host.appendChild(head);
    var fields = shown.fields || [];
    var any = false;
    for (var j = 0; j < fields.length; j++) {
      if (!matchField(fields[j], view.search, view.onlyChanged, view.scope)) continue;
      any = true;
      host.appendChild(renderFieldHead(LX, view, shown, fields[j]));
    }
    if (!any) {
      host.appendChild(LX.el("div", { class: "form-empty muted", text: "没有匹配的项" }));
    }
  }

  function renderAll(LX, view) {
    renderNav(LX, view);
    renderItems(LX, view);
  }

  function mountScope(scope) {
    return function (rootEl) {
      var LX = window.LX;
      var view = viewOf(scope);
      view.root = rootEl;
      while (rootEl.firstChild) rootEl.removeChild(rootEl.firstChild);
      var bar = LX.el("div", { class: "form-toolbar" });
      var search = document.createElement("input");
      search.type = "text";
      search.className = "form-search";
      search.placeholder = "搜索（标题 / 说明 / 路径）";
      search.value = view.search;
      search.addEventListener("input", function () {
        view.search = search.value;
        renderAll(LX, view);
      });
      bar.appendChild(search);
      view.searchInput = search;
      var onlyLabel = LX.el("label", { class: "form-only-changed-label" });
      var onlyBox = document.createElement("input");
      onlyBox.type = "checkbox";
      onlyBox.className = "form-only-changed";
      onlyBox.checked = view.onlyChanged;
      onlyLabel.appendChild(onlyBox);
      onlyLabel.appendChild(document.createTextNode("只看和默认值不同的项"));
      onlyBox.addEventListener("change", function () {
        view.onlyChanged = onlyBox.checked;
        renderAll(LX, view);
      });
      bar.appendChild(onlyLabel);
      view.onlyChangedInput = onlyBox;
      rootEl.appendChild(bar);
      var sel = document.createElement("select");
      sel.className = "block-select";
      sel.setAttribute("aria-label", "配置块");
      sel.addEventListener("change", function () {
        view.blockKey = sel.value;
        renderAll(LX, view);
      });
      rootEl.appendChild(sel);
      view.blockSelect = sel;
      var layout = LX.el("div", { class: "form-layout" });
      var nav = LX.el("nav", { class: "block-nav", dataset: {} });
      nav.setAttribute("aria-label", "配置块导航");
      layout.appendChild(nav);
      view.navHost = nav;
      var items = LX.el("div", { class: "form-items" });
      layout.appendChild(items);
      view.itemsHost = items;
      rootEl.appendChild(layout);
      renderAll(LX, view);
    };
  }

  function isDirtyScope(scope) {
    var LX = window.LX;
    var dirty = LX.state && LX.state.dirty;
    if (!dirty) return false;
    var hit = false;
    dirty.forEach(function (item, key) {
      if (key === scope + ":" + item.path || key.indexOf(scope + ":") === 0) hit = true;
    });
    return hit;
  }

  function boot() {
    var LX = window.LX;
    if (!LX || typeof LX.registerTab !== "function") return;
    LX.registerTab({
      id: "private", title: "私聊", order: 10,
      mount: mountScope("private"),
      onShow: function () {},
      isDirty: function () { return isDirtyScope("private"); }
    });
    LX.registerTab({
      id: "group", title: "群聊", order: 20,
      mount: mountScope("group"),
      onShow: function () {},
      isDirty: function () { return isDirtyScope("group"); }
    });
    LX.onReload(function () {
      ["private", "group"].forEach(function (scope) {
        var view = views[scope];
        if (!view || !view.root) return;
        if (view.searchInput && document.activeElement !== view.searchInput) {
          view.searchInput.value = view.search;
        }
        if (view.onlyChangedInput) view.onlyChangedInput.checked = view.onlyChanged;
        renderAll(LX, view);
      });
    });
    LX.onChange(function (scope, path) {
      [scope].forEach(function () {});
      ["private", "group"].forEach(function (sc) {
        var view = views[sc];
        if (!view || !view.root) return;
        if (scope !== null && scope !== undefined && sc !== scope) return;
        Object.keys(view.fields).forEach(function (fp) {
          if (path !== null && path !== undefined && fp !== path) return;
          var rec = view.fields[fp];
          var dirty = LX.isDirty(sc, fp);
          rec.badgeDirty.classList.toggle("hidden", !dirty);
          rec.resetBtn.disabled = deepEqual(LX.getValue(sc, fp), rec.field.default);
          var serverErr = LX.getError(sc, fp);
          rec.errEl.textContent = serverErr || "";
        });
        renderNav(LX, view);
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
