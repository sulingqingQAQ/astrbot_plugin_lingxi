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

  /* ================= 逐项表单（SR-UI） =================
   * 和 AstrBot 自带配置页的 template_list 读写**同一份** session_rules；
   * 面板与后端之间走「条目列表」：{scope, session, block, items}（口径见 SR-UI 卡 §1）。
   * 面板里的工作副本是**按会话**的（一张卡片一个群 / 一个人），一张卡里的项可以来自不同块，
   * 所以项名用全路径（"context.max_messages"）；发给后端前再按块拆回 (scope, session, block)
   * ——见 savedEntries / backendEntries 这一对转换。
   * 控件复用 form.js 的 LX.formKit（bool / 数字 / 滑块 / 下拉 / 文本），不另写一套。 */
  var xr = {
    card: null, list: null, empty: null, saveNote: null, addErr: null, addNote: null,
    scopeSel: null, sessionInput: null, entries: [], pendingAdd: null, armed: {}
  };

  var ADD_ERR_EMPTY = "先填会话号（群号或 QQ 号）。";
  var YAML_HINT = "注：值和出厂默认一样等于跟随全局；要固定成默认值请用下面的 YAML。";
  var SR_SESSION_HINT = "可以直接粘贴 AstrBot 会话列表里的完整标识（如 napcat:GroupMessage:123456），"
    + "会自动留号码，并按标识里的类型归到群聊 / 私聊。";

  /* 会话号规整（SR-UI-2）：和 lx/config/session_rules.py 的 canonical_session 同一口径。
   * 认三种写法——完整标识 "napcat:GroupMessage:1919810"、前缀 "group:1919810" /
   * "private:1919810"（friend: 也算私聊，与 resolver 同口径）、裸号码——出来的都是纯号码、
   * 平台名丢掉；群里「纯数字_纯数字」还原成真群号（strip_isolation 口径），私聊不动。
   * 面板这头：标识说的类型和下拉框选的不一样时，**按标识那一类走**并回一句 note
   * （"已按标识识别为群聊"），别让人拿私聊卡装群标识——后端那条路会直接丢掉这一行。 */
  var SR_PREFIX_SCOPE = { group: "group", private: "private", friend: "private" };
  var SR_MSG_TYPE_SCOPE = {
    GroupMessage: "group", GuildMessage: "group",
    FriendMessage: "private", PrivateMessage: "private"
  };

  function canonicalSession(scope, raw) {
    var s = String(raw === undefined || raw === null ? "" : raw).replace(/^\s+|\s+$/g, "");
    if (!s) return { session: "", scope: scope, switched: false, error: ADD_ERR_EMPTY };
    var ident = s;
    var found = "";                       // 标识 / 前缀自己带的那个范围
    var parts = s.split(":");
    if (parts.length >= 3 && SR_MSG_TYPE_SCOPE[parts[1]]) {
      // 完整标识：平台名可能带冒号，所以第 1 个冒号之后整段都是号码（后端 split(":", 2) 同口径）
      found = SR_MSG_TYPE_SCOPE[parts[1]];
      ident = parts.slice(2).join(":");
    } else if (parts.length >= 2) {
      found = SR_PREFIX_SCOPE[parts[0].replace(/^\s+|\s+$/g, "").toLowerCase()] || "";
      ident = s.slice(parts[0].length + 1);
      if (!found) {
        return {
          session: "",
          scope: scope,
          switched: false,
          error: "看不懂这个会话号（" + s + "）：填群号 / QQ 号，或者粘贴会话列表里的完整标识。"
        };
      }
    }
    ident = ident.replace(/^\s+|\s+$/g, "");
    if (!ident) {
      return { session: "", scope: scope, switched: false, error: "这个会话号里没有号码（" + s + "）。" };
    }
    var target = found || scope;
    if (target === "group") {
      // 开了 unique_session 的群，号码是「发送者_群号」；运行时按真群号找配置（lx/session/key.py 的
      // strip_isolation：只有「纯数字_纯数字」才切，别的平台带下划线的 ID 原样留着）
      var iso = /^\d+_(\d+)$/.exec(ident);
      if (iso) ident = iso[1];
    }
    return { session: ident, scope: target, switched: !!found && found !== scope, error: "" };
  }

  function deepEqual(a, b) {
    if (a === b) return true;
    if (typeof a !== typeof b) return false;
    if (a === null || b === null) return a === b;
    if (typeof a !== "object") return a === b;
    if (Array.isArray(a) !== Array.isArray(b)) return false;
    if (Array.isArray(a)) {
      if (a.length !== b.length) return false;
      for (var i = 0; i < a.length; i++) if (!deepEqual(a[i], b[i])) return false;
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

  /* 后端的条目列表 → 面板的工作副本：同一个会话的多个块并成一张卡，
   * 项名改成全路径（"块.项"）。排序只为看着稳。 */
  function savedEntries() {
    var raw = LX.state.config && LX.state.config.session_rules;
    var out = [];
    var byKey = {};
    if (Array.isArray(raw)) {
      raw.forEach(function (e) {
        if (!e || typeof e !== "object") return;
        var scope = String(e.scope || "");
        var session = String(e.session === undefined ? "" : e.session).trim();
        var block = String(e.block || "");
        var key = scope + "|" + session;
        var entry = byKey[key];
        if (!entry) {
          entry = { scope: scope, session: session, items: {} };
          byKey[key] = entry;
          out.push(entry);
        }
        var items = e.items || {};
        Object.keys(items).forEach(function (name) {
          entry.items[block + "." + name] = clone(items[name]);
        });
      });
    }
    out.sort(function (a, b) {
      var ka = a.scope + "|" + a.session;
      var kb = b.scope + "|" + b.session;
      return ka < kb ? -1 : (ka > kb ? 1 : 0);
    });
    return out;
  }

  /* 面板的工作副本 → 后端的条目列表：按块拆回 (scope, session, block)；
   * 一项都没有的（只加了会话还没加项）不发，不然会写出一行空覆盖。 */
  function backendEntries() {
    var out = [];
    xr.entries.forEach(function (e) {
      var byBlock = {};
      Object.keys(e.items || {}).forEach(function (path) {
        var block = blockOfPath(path);
        if (!block) return;
        if (!byBlock[block]) byBlock[block] = {};
        byBlock[block][nameOfPath(path)] = clone(e.items[path]);
      });
      Object.keys(byBlock).sort().forEach(function (block) {
        out.push({ scope: e.scope, session: e.session, block: block, items: byBlock[block] });
      });
    });
    return out;
  }

  function blockOfPath(path) {
    var dot = String(path).indexOf(".");
    return dot < 0 ? "" : String(path).slice(0, dot);
  }

  function nameOfPath(path) {
    var dot = String(path).indexOf(".");
    return dot < 0 ? String(path) : String(path).slice(dot + 1);
  }

  function scopeBlocks(scope) {
    var meta = LX.state.meta;
    var list = (meta && meta.blocks && meta.blocks[scope]) || [];
    return list.filter(function (b) { return b && b.overridable !== false; });
  }

  function fieldOf(scope, path) {
    var blocks = scopeBlocks(scope);
    for (var i = 0; i < blocks.length; i++) {
      var fields = blocks[i].fields || [];
      for (var j = 0; j < fields.length; j++) {
        if (fields[j].path === path) return fields[j];
      }
    }
    return null;
  }

  /* 该 scope 当前的全局值（含未保存的全局改动 → 用 LX.getValue）。 */
  function globalValue(scope, path) {
    var v = LX.getValue(scope, path);
    if (v !== undefined) return v;
    var f = LX.fieldMeta(scope, path);
    return f ? f.default : undefined;
  }

  function entryKey(e) { return e.scope + ":" + e.session; }

  function touchRules() {
    LX.markRulesDirty(backendEntries());
    updateRulesSaveNote();
  }

  function removeItem(entry, path) {
    if (entry.items && Object.prototype.hasOwnProperty.call(entry.items, path)) {
      delete entry.items[path];
    }
    touchRules();
    renderRules();
  }

  /* 一行：块 · 项名，右边是控件，再右边是全局值和删除。 */
  function itemRow(entry, path, field) {
    var blockKey = blockOfPath(path);
    var name = nameOfPath(path);
    var row = LX.el("div", { class: "sr-item", dataset: { block: blockKey, item: name } });
    var title = LX.el("div", { class: "sr-item-title" }, [
      LX.el("span", { class: "sr-item-block sr-block", text: blockTitle(entry.scope, blockKey) + " · " }),
      LX.el("span", { class: "sr-item-name", text: fieldTitle(entry.scope, path) })
    ]);
    var box = LX.el("div", { class: "field-control sr-control" });
    var errEl = LX.el("div", { class: "field-error sr-item-err" });
    var st = { view: null };
    var view = LX.formKit
      ? LX.formKit.makeView(entry.scope, itemCtx(entry, path, field, row, st))
      : null;
    st.view = view;
    if (view) {
      /* 给 form.js 的字段记录：它的控件在 change 时会读 rec.field / rec.built（syncControl），
       * 无效输入时会读 rec.errEl / rec.resetBtn（pushInvalid），少了哪个都会在运行时炸。
       * 索引键用 field.path（和 form.js 内部一致），不是短名。
       * 本行没有"恢复默认"按钮，resetBtn 只当个占位（form.js 只写它的 disabled）。 */
      var built = LX.formKit.buildControl(view, field, box);
      view.fields[field.path] = {
        wrap: null, badgeNew: null, badgeDirty: null,
        resetBtn: { disabled: false }, errEl: errEl,
        field: field, blockKey: blockKey, built: built
      };
    }
    var cur = entry.items ? entry.items[path] : undefined;
    var gv = globalValue(entry.scope, path);
    var side = LX.el("div", { class: "sr-item-side" }, [
      box, errEl,
      LX.el("div", { class: "sr-global muted", text: "（全局 " + fmtValue(gv) + "）" })
    ]);
    row.appendChild(title);
    row.appendChild(side);
    var del = iconButton({ class: "small ghost sr-del-item", text: "删掉这一项" }, "trash");
    del.setAttribute("aria-label", "删掉这一项");
    del.addEventListener("click", function () { removeItem(entry, path); });
    row.appendChild(del);
    if (deepEqual(cur, field.default)) {
      row.appendChild(sameDefaultNote());
    }
    return row;
  }

  function sameDefaultNote() {
    return LX.el("div", { class: "sr-same-default", text:
      "这一项和出厂默认一样，等于跟随全局，保存时不会存这一项；想固定成默认值请用下面的 YAML。" });
  }

  /* 逐项表单自己的取值口：直接读写这条目里的项（form.js 的控件经它取值）。 */
  function itemCtx(entry, path, field, row, st) {
    return {
      getValue: function () { return entry.items ? entry.items[path] : undefined; },
      setValue: function (_scope, _path, value) {
        if (!entry.items) entry.items = {};
        entry.items[path] = clone(value);
        touchRules();
        refreshRowNote(row, entry, path, field, st);
      },
      /* 无效输入（清空 / 不是数）：这一项不生效，直接把它从条目里去掉，
       * 免得保存时把上一次的有效值发出去，而输入框里已经是另一回事。 */
      revertValue: function () {
        if (entry.items) delete entry.items[path];
        touchRules();
        refreshRowNote(row, entry, path, field, st);
      },
      isDirty: function () { return true; },
      getError: function () { return null; }
    };
  }

  /* 输入之后只更新这一行的红字和"和默认一样"小字，不重建控件（否则会把光标弄丢）。 */
  function refreshRowNote(row, entry, path, field, st) {
    var cur = entry.items ? entry.items[path] : undefined;
    var old = row.querySelector(".sr-same-default");
    if (deepEqual(cur, field.default)) {
      if (!old) row.appendChild(sameDefaultNote());
    } else if (old) {
      old.remove();
    }
    var errEl = row.querySelector(".sr-item-err");
    if (errEl) {
      var local = st && st.view ? st.view.localErr[field.path] : "";
      errEl.textContent = local || "";
    }
  }

  /* 「加一项」：先选块，再选项（选项的值 = 全路径，排除已经加上的）。 */
  function addItemPanel(entry, idx) {
    var blocks = scopeBlocks(entry.scope);
    var st = xr.pendingAdd;
    if (st.block === undefined) st.block = blocks.length ? blocks[0].key : "";
    var blkSel = document.createElement("select");
    blkSel.id = "sr-item-pick";
    blkSel.setAttribute("aria-label", "配置块");
    blocks.forEach(function (b) {
      var o = document.createElement("option");
      o.value = b.key;
      o.textContent = b.title || b.key;
      if (b.key === st.block) o.selected = true;
      blkSel.appendChild(o);
    });
    var cur = null;
    blocks.forEach(function (b) { if (b.key === st.block) cur = b; });
    var avail = ((cur && cur.fields) || []).filter(function (f) {
      return !entry.items || !Object.prototype.hasOwnProperty.call(entry.items, f.path);
    });
    var fldSel = document.createElement("select");
    fldSel.id = "sr-field-pick";
    fldSel.setAttribute("aria-label", "配置项");
    avail.forEach(function (f) {
      var o = document.createElement("option");
      o.value = f.path;
      o.textContent = f.title || f.path;
      if (f.path === st.field) o.selected = true;
      fldSel.appendChild(o);
    });
    var addBtn = iconButton({ class: "small primary", text: "添加" }, "plus");
    addBtn.id = "sr-item-add";
    var cancel = LX.el("button", { class: "small ghost", text: "取消" });
    cancel.id = "sr-item-cancel";
    blkSel.addEventListener("change", function () {
      st.block = blkSel.value;
      st.field = undefined;
      renderRules();
    });
    fldSel.addEventListener("change", function () { st.field = fldSel.value; });
    addBtn.addEventListener("click", function () {
      if (!avail.length) return;
      var f = avail[0];
      for (var i = 0; i < avail.length; i++) {
        if (avail[i].path === (fldSel.value || st.field)) f = avail[i];
      }
      if (!entry.items) entry.items = {};
      // 初值 = 这个 scope 当前的全局值：加了就能看着改，不动就是跟随全局
      entry.items[f.path] = clone(globalValue(entry.scope, f.path));
      xr.pendingAdd = null;
      touchRules();
      renderRules();
    });
    cancel.addEventListener("click", function () {
      xr.pendingAdd = null;
      renderRules();
    });
    var panel = LX.el("div", { class: "sr-add-item-panel" }, [
      LX.el("span", { class: "muted", text: "块" }), blkSel,
      LX.el("span", { class: "muted", text: "项" }), fldSel, addBtn, cancel
    ]);
    if (!avail.length) {
      panel.appendChild(LX.el("span", { class: "sr-err", text: "这一块能覆盖的项都已经加上了，换个块。" }));
    }
    panel.dataset.idx = String(idx);
    return panel;
  }

  function entryCard(entry, idx) {
    var head = LX.el("div", { class: "sr-entry-head" });
    head.appendChild(LX.el("span", { class: "pill pill-accent", text: scopeTitle(entry.scope) }));
    head.appendChild(LX.el("code", { class: "pill pill-mono sr-session-code", text: entry.session }));
    var actions = LX.el("div", { class: "sr-entry-actions" });
    var addBtn = iconButton({ class: "small", text: "加一项" }, "plus");
    addBtn.id = "sr-add-item";
    addBtn.dataset.idx = String(idx);
    addBtn.addEventListener("click", function () {
      xr.pendingAdd = { idx: idx, block: undefined, field: undefined };
      renderRules();
    });
    var delBtn = iconButton({ class: "small ghost", text: "删除会话" }, "trash");
    delBtn.id = "sr-del-session";
    delBtn.dataset.idx = String(idx);
    delBtn.addEventListener("click", function () {
      var key = entryKey(entry) + "#" + idx;
      if (!xr.armed[key]) {
        // 插件页 iframe 没有 allow-modals：不能用系统弹窗，就地二次确认
        xr.armed[key] = setTimeout(function () {
          delete xr.armed[key];
          renderRules();
        }, 3000);
        delBtn.classList.add("armed");
        delBtn.textContent = "再点一次确认删除";
        return;
      }
      clearTimeout(xr.armed[key]);
      delete xr.armed[key];
      xr.entries.splice(idx, 1);
      xr.pendingAdd = null;
      touchRules();
      renderRules();
    });
    if (xr.armed[entryKey(entry) + "#" + idx]) {
      delBtn.classList.add("armed");
      delBtn.textContent = "再点一次确认删除";
    }
    actions.appendChild(addBtn);
    actions.appendChild(delBtn);
    head.appendChild(actions);
    var card = LX.el("div", { class: "sr-entry", dataset: { scope: entry.scope, session: entry.session } }, [head]);
    var names = Object.keys(entry.items || {}).sort();
    if (xr.pendingAdd && xr.pendingAdd.idx === idx) {
      card.appendChild(addItemPanel(entry, idx));
    }
    if (!names.length) {
      card.appendChild(LX.el("div", { class: "sr-entry-empty muted", text: "这个会话还没改任何项：点“加一项”。" }));
    }
    names.forEach(function (path) {
      var field = fieldOf(entry.scope, path);
      if (!field) {
        // 配置里已经不存在的项（老表单留下的）：只报一句，别让整块渲染断掉
        card.appendChild(LX.el("div", {
          class: "sr-item",
          dataset: { block: blockOfPath(path), item: nameOfPath(path) }
        }, [
          LX.el("div", { class: "sr-item-title", text: path }),
          LX.el("div", { class: "sr-item-err", text: "这一项在现在的配置里找不到了，保存会被拒绝；删掉它或者改回去。" })
        ]));
        return;
      }
      card.appendChild(itemRow(entry, path, field));
    });
    return card;
  }

  function renderRules() {
    if (!xr.list) return;
    clearNode(xr.list);
    if (!xr.entries.length) {
      xr.list.appendChild(LX.el("div", { id: "sr-empty", class: "sr-empty muted", text: "现在没有表单覆盖" }));
      return;
    }
    xr.entries.forEach(function (e, i) { xr.list.appendChild(entryCard(e, i)); });
  }

  function updateRulesSaveNote() {
    if (!xr.saveNote) return;
    var show = (LX._rulesErrors || []).length > 0;
    xr.saveNote.classList.toggle("hidden", !show);
  }

  function addSession() {
    if (!xr.scopeSel || !xr.sessionInput) return;
    var got = canonicalSession(xr.scopeSel.value, xr.sessionInput.value);
    xr.addErr.textContent = "";
    if (xr.addNote) xr.addNote.textContent = "";
    if (got.error) {
      xr.addErr.textContent = got.error;
      return;
    }
    var scope = got.scope;
    var session = got.session;
    if (got.switched) {
      xr.scopeSel.value = scope;              // 下拉框跟着标识走，下一条也按这个类型
      if (xr.addNote) {
        xr.addNote.textContent = "已按标识识别为" + scopeTitle(scope) + "（" + session + "），"
          + "按" + scopeTitle(scope) + "添加。";
      }
    }
    var dup = xr.entries.some(function (e) { return e.scope === scope && e.session === session; });
    if (dup) {
      xr.addErr.textContent = "这个会话已经在上面了（" + scopeTitle(scope) + " " + session + "）。";
      return;
    }
    xr.entries.push({ scope: scope, session: session, items: {} });
    xr.sessionInput.value = "";
    xr.pendingAdd = null;
    touchRules();
    renderRules();
  }

  function mountRulesCard(root) {
    var card = LX.el("div", { class: "card sr-card" }, [
      cardHead("sliders", "逐项表单", "给某个群 / 某个人单独改几项。只列改过的项，没列的跟全局走。")
    ]);
    card.id = "sr-card";
    xr.saveNote = alertBox("warn", "保存时被服务端拒绝，见顶部提示。", "warn-line hidden");
    xr.saveNote.id = "sr-save-note";
    xr.list = LX.el("div", { id: "sr-list", class: "sr-list" });
    card.appendChild(xr.list);
    var bar = LX.el("div", { class: "sr-add-bar" });
    xr.scopeSel = document.createElement("select");
    xr.scopeSel.id = "sr-scope";
    xr.scopeSel.setAttribute("aria-label", "会话类型");
    [["private", "私聊"], ["group", "群聊"]].forEach(function (pair) {
      var o = document.createElement("option");
      o.value = pair[0];
      o.textContent = pair[1];
      if (pair[0] === "group") o.selected = true;
      xr.scopeSel.appendChild(o);
    });
    xr.sessionInput = LX.el("input", {
      id: "sr-session", type: "text",
      placeholder: "会话号，或粘贴完整标识（napcat:GroupMessage:123456）", "aria-label": "会话号"
    });
    var addBtn = iconButton({ class: "small primary", text: "添加会话" }, "plus");
    addBtn.id = "sr-add-session";
    bar.appendChild(xr.scopeSel);
    bar.appendChild(xr.sessionInput);
    bar.appendChild(addBtn);
    card.appendChild(bar);
    card.appendChild(LX.el("div", { id: "sr-session-hint", class: "sr-hint muted", text: SR_SESSION_HINT }));
    xr.addNote = LX.el("div", { id: "sr-add-note", class: "sr-hint muted", text: "" });
    card.appendChild(xr.addNote);
    xr.addErr = LX.el("div", { id: "sr-add-err", class: "sr-err" });
    card.appendChild(xr.addErr);
    card.appendChild(LX.el("div", { id: "sr-hint", class: "sr-hint muted", text: YAML_HINT }));
    card.appendChild(xr.saveNote);
    addBtn.addEventListener("click", addSession);
    xr.sessionInput.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); addSession(); }
    });
    root.appendChild(card);
  }

  function reloadRules() {
    if (!xr.list) return;
    if (!LX.state.rulesDirty) {
      xr.entries = savedEntries();
      xr.pendingAdd = null;
    }
    renderRules();
    updateRulesSaveNote();
  }

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
    mountRulesCard(root);
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
    reloadRules();
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
    reloadRules();
  }

  LX.registerTab({
    id: "overrides", title: "分会话覆盖", order: 30, icon: "sliders",
    mount: mountOverrides,
    isDirty: function () { return LX.state.overridesDirty || LX.state.rulesDirty; }
  });
  LX.onReload(reloadOverrides);
  LX.onChange(function () {
    updateExDirtyWarn();
    updateSaveNote();
    updateRulesSaveNote();
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
