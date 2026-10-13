/* UI-4c 私聊 / 群聊表单（form.js）。
 * 原生 JS，无 CDN / 第三方库。页面代码里不写死任何具体配置项名；
 * 唯一例外是文件顶部 TALL_TEXT_SUFFIXES（三个让用户自己填内容的高文本框）。
 * 壳（app.js）只读：经 window.LX 注册两个标签，由 meta 动态生成表单。 */
(function () {
  "use strict";

  /* 高文本框：path 以这些后缀结尾的 text 项用高文本框（至少 12 行、可竖拉、有字数）。 */
  var TALL_TEXT_SUFFIXES = [".style_text", ".glossary_text", ".members_text"];

  var DEBOUNCE_MS = 150;

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

  function sanitizeId(s) {
    return String(s).replace(/[^A-Za-z0-9_-]/g, "-");
  }

  /* 每个 scope 一份 UI 状态。 */
  var views = {};
  function viewOf(scope) {
    if (!views[scope]) {
      views[scope] = {
        scope: scope, root: null, blockKey: null, search: "", onlyChanged: false,
        fields: {},  /* path -> rec */
        blockBtns: {},
        blockSelect: null, itemsHost: null, navHost: null,
        searchInput: null, onlyChangedInput: null,
        localErr: {} /* path -> 本地校验文字（如数字非法），优先于服务端错误显示 */
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
    /* “只看和默认值不同的项”：比当前值（暂存优先）与规格默认值，不是只看未保存的改动。 */
    if (onlyChanged && deepEqual(window.LX.getValue(scope, field.path), field.default)) return false;
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

  /* 条件显示：condition=[[name,value],…]，同块内这些项当前值都等于给定值才显示。 */
  function condVisible(LX, scope, block, field) {
    var cond = field.condition;
    if (!cond || !cond.length) return true;
    for (var i = 0; i < cond.length; i++) {
      var name = cond[i][0];
      var want = cond[i][1];
      var full = block.key + "." + name;
      if (!deepEqual(LX.getValue(scope, full), want)) return false;
    }
    return true;
  }

  /* 待决的防抖（已排上时钟、还没落到 LX.setValue 的）。点保存 / 放弃之前壳会统一冲刷它们。 */
  var pendingFlushes = [];

  function flushAllPending() {
    pendingFlushes.slice().forEach(function (f) { f(); });
  }

  function debounce(fn, ms) {
    var timer = null;
    var lastSelf = null;
    var lastArgs = null;
    function drop() {
      var i = pendingFlushes.indexOf(run.flush);
      if (i >= 0) pendingFlushes.splice(i, 1);
    }
    function run() {
      lastSelf = this;
      lastArgs = arguments;
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () {
        timer = null;
        drop();
        fn.apply(lastSelf, lastArgs);
      }, ms);
      if (pendingFlushes.indexOf(run.flush) < 0) pendingFlushes.push(run.flush);
    }
    /* 立刻执行待决的那一次（没有待决就什么都不做）。 */
    run.flush = function () {
      if (!timer) return;
      clearTimeout(timer);
      timer = null;
      drop();
      fn.apply(lastSelf, lastArgs);
    };
    return run;
  }

  /* ---- 控件 ---- */

  function markControl(el) {
    el.setAttribute("data-role", "control");
    return el;
  }

  function buildBool(LX, view, block, field, box) {
    var input = markControl(document.createElement("input"));
    input.type = "checkbox";
    input.checked = !!LX.getValue(view.scope, field.path);
    input.addEventListener("change", function () {
      LX.setValue(view.scope, field.path, input.checked);
    });
    box.appendChild(input);
    return { controls: [input] };
  }

  function buildNumber(LX, view, block, field, box) {
    var type = field.type;
    var input = markControl(document.createElement("input"));
    input.type = "number";
    if (field.lo !== null && field.lo !== undefined) input.min = String(field.lo);
    if (field.hi !== null && field.hi !== undefined) input.max = String(field.hi);
    var step = (field.slider && field.slider[2] !== undefined) ? field.slider[2] : (type === "int" ? 1 : "any");
    input.step = String(step);
    var cur = LX.getValue(view.scope, field.path);
    if (typeof cur === "number") input.value = String(cur);
    var slider = null;
    if (field.slider) {
      slider = markControl(document.createElement("input"));
      slider.type = "range";
      slider.min = String(field.slider[0]);
      slider.max = String(field.slider[1]);
      slider.step = String(field.slider[2]);
      if (typeof cur === "number") slider.value = String(cur);
      slider.setAttribute("aria-label", (field.title || field.path) + "滑块");
    }
    var viewRef = view;
    var pathRef = field.path;
    var scopeRef = view.scope;
    function pushValid(num) {
      delete viewRef.localErr[pathRef];
      LX.setValue(scopeRef, pathRef, num);
    }
    /* 无效输入（清空 / 不是数 / 给 int 项填小数）：不算改动，要把这一项的暂存撤销掉，
     * 否则保存会带上上一次的有效值，而输入框里已经是另一回事。 */
    function pushInvalid(msg) {
      viewRef.localErr[pathRef] = msg + "；这次输入没有生效，保存时不会带上它";
      if (typeof LX.revertValue === "function") LX.revertValue(scopeRef, pathRef);
      var rec = viewRef.fields[pathRef];
      if (rec) {
        rec.errEl.textContent = viewRef.localErr[pathRef];
        rec.resetBtn.disabled = false;
      }
    }
    input.addEventListener("input", function () {
      var raw = input.value;
      if (raw === "" || raw === null) { pushInvalid("应该是一个数字"); return; }
      var num = Number(raw);
      if (!isFinite(num)) { pushInvalid("应该是一个数字"); return; }
      if (type === "int" && Math.round(num) !== num) { pushInvalid("这一项只能填整数"); return; }
      if (slider && document.activeElement !== slider) slider.value = String(num);
      pushValid(num);
    });
    input.addEventListener("change", function () {
      var rec = viewRef.fields[pathRef];
      if (rec) syncControl(LX, viewRef, rec);
    });
    if (slider) {
      slider.addEventListener("input", function () {
        var num = Number(slider.value);
        if (document.activeElement !== input) input.value = String(num);
        pushValid(num);
      });
    }
    box.appendChild(input);
    if (slider) box.appendChild(slider);
    var list = slider ? [input, slider] : [input];
    return { controls: list };
  }

  function buildStringOptions(LX, view, block, field, box) {
    var sel = markControl(document.createElement("select"));
    var opts = field.options || [];
    var labels = field.labels || [];
    for (var i = 0; i < opts.length; i++) {
      var opt = document.createElement("option");
      opt.value = opts[i];
      opt.textContent = (labels[i] !== undefined && labels[i] !== null) ? labels[i] : opts[i];
      sel.appendChild(opt);
    }
    var cur = LX.getValue(view.scope, field.path);
    sel.value = (cur === null || cur === undefined) ? "" : String(cur);
    sel.addEventListener("change", function () {
      LX.setValue(view.scope, field.path, sel.value);
    });
    box.appendChild(sel);
    return { controls: [sel] };
  }

  function buildStringDatalist(LX, view, block, field, box) {
    var input = markControl(document.createElement("input"));
    input.type = "text";
    var choices = (LX.state.choices || {});
    var list = field.special === "select_persona" ? (choices.personas || []) : (choices.providers || []);
    var dlId = "lx-dl-" + sanitizeId(view.scope) + "-" + sanitizeId(field.path);
    var dl = document.createElement("datalist");
    dl.id = dlId;
    for (var i = 0; i < list.length; i++) {
      var opt = document.createElement("option");
      opt.value = String(list[i]);
      dl.appendChild(opt);
    }
    input.setAttribute("list", dlId);
    var cur = LX.getValue(view.scope, field.path);
    input.value = (cur === null || cur === undefined) ? "" : String(cur);
    var push = debounce(function () {
      LX.setValue(view.scope, field.path, input.value);
    }, DEBOUNCE_MS);
    input.addEventListener("input", push);
    input.addEventListener("change", push.flush);
    box.appendChild(input);
    box.appendChild(dl);
    return { controls: [input] };
  }

  function buildStringText(LX, view, block, field, box) {
    var input = markControl(document.createElement("input"));
    input.type = "text";
    var cur = LX.getValue(view.scope, field.path);
    input.value = (cur === null || cur === undefined) ? "" : String(cur);
    var push = debounce(function () {
      LX.setValue(view.scope, field.path, input.value);
    }, DEBOUNCE_MS);
    input.addEventListener("input", push);
    input.addEventListener("change", push.flush);
    box.appendChild(input);
    return { controls: [input] };
  }

  function buildText(LX, view, block, field, box) {
    var tall = isTallPath(field.path);
    var area = markControl(document.createElement("textarea"));
    area.rows = tall ? 12 : 5;
    if (tall) area.className = "form-tall";
    var cur = LX.getValue(view.scope, field.path);
    area.value = (cur === null || cur === undefined) ? "" : String(cur);
    var count = null;
    if (tall) {
      count = LX.el("span", { class: "field-count muted", text: String(area.value.length) + " 字" });
    }
    var push = debounce(function () {
      if (count) count.textContent = String(area.value.length) + " 字";
      LX.setValue(view.scope, field.path, area.value);
    }, DEBOUNCE_MS);
    area.addEventListener("input", function () {
      if (count) count.textContent = String(area.value.length) + " 字";
      push();
    });
    area.addEventListener("change", push.flush);
    box.appendChild(area);
    if (count) box.appendChild(count);
    return { controls: [area], countEl: count, countKind: "chars" };
  }

  function parseListLines(raw) {
    var parts = String(raw === null || raw === undefined ? "" : raw).split("\n");
    var out = [];
    for (var i = 0; i < parts.length; i++) {
      var t = parts[i].replace(/^\s+|\s+$/g, "");
      if (t !== "") out.push(t);
    }
    return out;
  }

  function buildList(LX, view, block, field, box) {
    var area = markControl(document.createElement("textarea"));
    area.rows = 5;
    var cur = LX.getValue(view.scope, field.path);
    var arr = Array.isArray(cur) ? cur : [];
    area.value = arr.join("\n");
    var count = LX.el("span", { class: "field-count muted", text: String(arr.length) + " 项" });
    var push = debounce(function () {
      var parsed = parseListLines(area.value);
      count.textContent = String(parsed.length) + " 项";
      LX.setValue(view.scope, field.path, parsed);
    }, DEBOUNCE_MS);
    area.addEventListener("input", function () {
      count.textContent = String(parseListLines(area.value).length) + " 项";
      push();
    });
    area.addEventListener("change", push.flush);
    box.appendChild(area);
    box.appendChild(count);
    var hint = LX.el("div", { class: "field-hint", text: "每行一项，空行会被忽略" });
    box.appendChild(hint);
    return { controls: [area], countEl: count, countKind: "items" };
  }

  function buildControl(LX, view, block, field, box) {
    var t = field.type;
    if (t === "bool") return buildBool(LX, view, block, field, box);
    if (t === "int" || t === "float") return buildNumber(LX, view, block, field, box);
    if (t === "string") {
      if (field.options) return buildStringOptions(LX, view, block, field, box);
      if (field.special === "select_provider" || field.special === "select_persona") {
        return buildStringDatalist(LX, view, block, field, box);
      }
      return buildStringText(LX, view, block, field, box);
    }
    if (t === "text") return buildText(LX, view, block, field, box);
    if (t === "list") return buildList(LX, view, block, field, box);
    var fallback = markControl(document.createElement("input"));
    fallback.type = "text";
    var cur = LX.getValue(view.scope, field.path);
    fallback.value = (cur === null || cur === undefined) ? "" : String(cur);
    var push = debounce(function () {
      LX.setValue(view.scope, field.path, fallback.value);
    }, DEBOUNCE_MS);
    fallback.addEventListener("input", push);
    fallback.addEventListener("change", push.flush);
    box.appendChild(fallback);
    return { controls: [fallback] };
  }

  /* 把控件值同步为 LX.getValue（聚焦中的控件不动，避免输入法/光标被打断）。 */
  function syncControl(LX, view, rec) {
    var v = LX.getValue(view.scope, rec.field.path);
    var ctrls = rec.built ? rec.built.controls : [];
    for (var i = 0; i < ctrls.length; i++) {
      var c = ctrls[i];
      if (document.activeElement === c) continue;
      /* 数字框里是无效输入（红字还挂着）时别用已加载值盖掉用户敲的内容，等他自己改正。 */
      if (c.type === "number" && view.localErr[rec.field.path]) continue;
      if (c.type === "checkbox") {
        c.checked = !!v;
      } else if (c.tagName === "SELECT") {
        c.value = (v === null || v === undefined) ? "" : String(v);
      } else if (c.type === "number" || c.type === "range") {
        c.value = (typeof v === "number") ? String(v) : "";
      } else if (c.tagName === "TEXTAREA" && rec.built && rec.built.countKind === "items") {
        c.value = Array.isArray(v) ? v.join("\n") : "";
      } else {
        c.value = (v === null || v === undefined) ? "" : String(v);
      }
    }
    if (rec.built && rec.built.countEl) {
      if (rec.built.countKind === "chars") {
        rec.built.countEl.textContent = String(ctrls.length ? ctrls[0].value.length : 0) + " 字";
      } else if (rec.built.countKind === "items") {
        var n = Array.isArray(v) ? v.length : 0;
        rec.built.countEl.textContent = String(n) + " 项";
      }
    }
  }

  function refreshField(LX, view, rec) {
    var scope = view.scope;
    var path = rec.field.path;
    var dirty = LX.isDirty(scope, path);
    rec.badgeDirty.classList.toggle("hidden", !dirty);
    var local = view.localErr[path];
    rec.resetBtn.disabled = !local && deepEqual(LX.getValue(scope, path), rec.field.default);
    var serverErr = LX.getError(scope, path);
    rec.errEl.textContent = local || serverErr || "";
    var vis = condVisible(LX, scope, blockOfKey(scope, rec.blockKey), rec.field);
    rec.wrap.style.display = vis ? "" : "none";
    syncControl(LX, view, rec);
  }

  function blockOfKey(scope, key) {
    var blocks = blocksOf(scope);
    for (var i = 0; i < blocks.length; i++) {
      if (blocks[i].key === key) return blocks[i];
    }
    return { key: key, fields: [] };
  }

  /* ---- 项 ---- */
  function renderField(LX, view, block, field) {
    var wrap = LX.el("div", { class: "field", dataset: { scope: view.scope, path: field.path } });
    wrap.appendChild(LX.el("span", { class: "field-title", text: field.title || field.path }));
    var badgeNew = LX.el("span", { class: "badge badge-new" + (field.new_default ? "" : " hidden"), text: "新默认待定" });
    var badgeDirty = LX.el("span", {
      class: "badge badge-dirty" + (LX.isDirty(view.scope, field.path) ? "" : " hidden"),
      text: "已改动"
    });
    wrap.appendChild(badgeNew);
    wrap.appendChild(badgeDirty);
    if (field.hint) wrap.appendChild(LX.el("div", { class: "field-hint", text: field.hint }));
    var box = LX.el("div", { class: "field-control" });
    wrap.appendChild(box);
    var built = buildControl(LX, view, block, field, box);
    wrap.appendChild(LX.el("div", { class: "field-default muted", text: "默认：" + fmtDefault(field.default) }));
    var cur = LX.getValue(view.scope, field.path);
    var resetBtn = LX.el("button", {
      class: "small secondary field-reset", text: "恢复默认",
      disabled: deepEqual(cur, field.default)
    });
    resetBtn.addEventListener("click", function () {
      delete view.localErr[field.path];
      LX.setValue(view.scope, field.path, field.default);
    });
    wrap.appendChild(resetBtn);
    var errEl = LX.el("div", { class: "field-error" });
    var local = view.localErr[field.path];
    var serverErr = LX.getError(view.scope, field.path);
    if (local || serverErr) errEl.textContent = local || serverErr;
    wrap.appendChild(errEl);
    var rec = {
      wrap: wrap, badgeNew: badgeNew, badgeDirty: badgeDirty,
      resetBtn: resetBtn, errEl: errEl, field: field, blockKey: block.key, built: built
    };
    if (!condVisible(LX, view.scope, block, field)) wrap.style.display = "none";
    view.fields[field.path] = rec;
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
    var cur = currentBlock(view);
    for (var i = 0; i < blocks.length; i++) {
      (function (block) {
        var n = dirtyCountInBlock(view.scope, block);
        var btn = LX.el("button", {
          class: "block-btn" + (cur && cur.key === block.key ? " active" : "")
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
        if (cur && cur.key === blocks[k].key) opt.selected = true;
        view.blockSelect.appendChild(opt);
      }
    }
  }

  function renderItems(LX, view) {
    var host = view.itemsHost;
    while (host.firstChild) host.removeChild(host.firstChild);
    view.fields = {};
    /* 控件整批重建，输入框里不再是用户敲过的无效内容（无效输入当时就没算改动），本地红字一起清掉。 */
    view.localErr = {};
    var block = currentBlock(view);
    var vis = visibleBlocks(view);
    var shown = null;
    for (var i = 0; i < vis.length; i++) {
      if (block && vis[i].key === block.key) shown = block;
    }
    if (!shown) {
      if (!vis.length) {
        host.appendChild(LX.el("div", { class: "form-empty muted", text: "没有匹配的项" }));
        return;
      }
      view.blockKey = vis[0].key;
      shown = vis[0];
    }
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
      host.appendChild(renderField(LX, view, shown, fields[j]));
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
      var nav = LX.el("nav", { class: "block-nav" });
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
      if (key.indexOf(scope + ":") === 0) hit = true;
    });
    return hit;
  }

  function boot() {
    var LX = window.LX;
    if (!LX || typeof LX.registerTab !== "function") return;
    /* 保存 / 放弃之前，壳会调用它把 150ms 防抖里还没落下的输入先暂存。 */
    if (typeof LX.addFlusher === "function") LX.addFlusher(flushAllPending);
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
    /* onReload：整体重绘并保持当前块与搜索词。 */
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
    /* onChange：只更新受影响的徽标 / 重置按钮 / 错误文字 / 条件显隐 / 块导航徽标，不整体重绘。 */
    LX.onChange(function (scope, path) {
      ["private", "group"].forEach(function (sc) {
        var view = views[sc];
        if (!view || !view.root) return;
        if (scope !== null && scope !== undefined && sc !== scope) return;
        Object.keys(view.fields).forEach(function (fp) {
          if (path !== null && path !== undefined && fp !== path) {
            /* 同块内条件依赖项也要跟着更新显隐（条件值变了）。 */
            var rec = view.fields[fp];
            var vis = condVisible(LX, sc, blockOfKey(sc, rec.blockKey), rec.field);
            rec.wrap.style.display = vis ? "" : "none";
            return;
          }
          refreshField(LX, view, view.fields[fp]);
        });
        /* 条件目标项实时显隐：该 scope 当前块下所有已渲染项重算一次。 */
        Object.keys(view.fields).forEach(function (fp) {
          var rec2 = view.fields[fp];
          var vis2 = condVisible(LX, sc, blockOfKey(sc, rec2.blockKey), rec2.field);
          rec2.wrap.style.display = vis2 ? "" : "none";
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
