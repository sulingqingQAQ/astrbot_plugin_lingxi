/* ACT-1「群动作」标签（tab_actions.js）。
 * 纯静态、原生 JS，无 CDN / 第三方库 / 外部字体；所有动态文字走 textContent（LX.el 的 text），不拼 HTML。
 * 本文件只提供版面与读写，注册在壳（app.js 的 registerActionsTab）里做。
 *
 * 分工（ACT-1 §7 冻结契约）：工具的中文名 / 风险小字写死在这里（允许）；
 * 配置项的类型 / 范围 / 默认值一律读后端元数据（LX.state.meta → LX.fieldMeta），这里一个字都不写规则。
 * 读写走壳的 LX.getValue / LX.setValue / LX.isDirty，因此保存、放弃、改动数、标签红点全都自动接上。
 * 后端 be1 之前 meta 里没有 actions / actions_global 两个块：那时只显示版面和开关位置，点不了。 */
(function () {
  "use strict";

  var LX = window.LX;
  if (!LX || typeof LX.el !== "function") return; // 壳没起来就什么都不做

  var TAB_TITLE = "群动作";
  var TAB_ICON = "bolt";
  var SCOPE_TEXT = { private: "私聊", group: "群聊" };
  var ALL_SCOPES = ["private", "group"];

  /* 冻结契约（ACT-1 §7）：工具 → 开关键。
   * scopes 是"这条配置项应该存在于哪些范围"的兜底（后端元数据拿到后以元数据为准）；
   * danger = true 的卡片上挂警示色小标（踢人 / 退群 / 换头像 / 处理申请）。 */
  var TOOLS = [
    { name: "poke_user", path: "actions.poke_enabled", title: "戳一戳", icon: "wave", scopes: ["private", "group"], danger: false, risk: "私聊里是拍一拍，群里会 @ 对方一下，对方的 QQ 会抖一下。别拿它烦人。" },
    { name: "set_msg_emoji", path: "actions.emoji_enabled", title: "给消息加表情", icon: "check", scopes: ["private", "group"], danger: false, risk: "要先有一条消息的消息号（聊天记录里每条后面带 #号），取不到号就会失败；再对那条消息贴一个表情回应。" },
    { name: "delete_msg", path: "actions.recall_enabled", title: "撤回消息", icon: "undo", scopes: ["private", "group"], danger: false, risk: "撤了没法恢复，群里会留一条『某人撤回了一条消息』。机器人拿不到自己发出去的消息号，所以只能撤别人的：得是群管理员、消息还没超时；私聊里只能撤自己发的。" },
    { name: "set_group_ban", path: "actions.ban_enabled", title: "禁言", icon: "clock", scopes: ["group"], danger: false, risk: "让对方一段时间不能在群里说话，时长超过上限会被截到上限。需要机器人是管理员，对方不是群主。" },
    { name: "set_group_card", path: "actions.card_enabled", title: "改群名片", icon: "file", scopes: ["group"], danger: false, risk: "改的是对方在这个群里的名片，全群都看得到。需要机器人是管理员。" },
    { name: "set_group_special_title", path: "actions.title_enabled", title: "改专属头衔", icon: "shield", scopes: ["group"], danger: false, risk: "头衔很显眼，写错了全群都看得见。需要机器人是群主。" },
    { name: "set_group_kick", path: "actions.kick_enabled", title: "踢出群", icon: "x", scopes: ["group"], danger: true, risk: "直接把对方踢出群，对话当场断掉；对方能自己再加回来。需要机器人是管理员。" },
    { name: "set_group_leave", path: "actions.leave_enabled", title: "退出群", icon: "trash", scopes: ["group"], danger: true, risk: "让机器人退出当前这个群，退出后要人手动再拉回来；退群期间它在这个群的功能全没了。" },
    { name: "search_group_member", path: "actions.member_search_enabled", title: "查群成员", icon: "search", scopes: ["group"], danger: false, risk: "只在当前群查，最多回 10 个；模型看到的是昵称和号码，不查别的群。" },
    { name: "handle_request", path: "actions_global.request_enabled", title: "处理加好友 / 加群申请", icon: "check-circle", scopes: ["group"], danger: true, risk: "替机器人同意或拒绝加好友 / 入群申请；得先收到过那条申请（插件会记下 flag），没记下来就做不了。同意等于把陌生人放进来，收不回来（只能事后再踢 / 再删）。全局设置，对所有会话生效。" },
    { name: "set_self_profile", path: "actions_global.self_profile_enabled", title: "改机器人资料", icon: "settings", scopes: ["group"], danger: false, risk: "改的是机器人自己号的昵称 / 签名等资料，所有平台都看得到。全局设置，对所有会话生效。" },
    { name: "set_qq_avatar", path: "actions_global.avatar_enabled", title: "换机器人头像", icon: "eye", scopes: ["group"], danger: true, risk: "换掉机器人自己号的头像，所有平台都看得到，旧头像不保留。全局设置，对所有会话生效。" }
  ];

  /* 非工具项：限制（禁言上限 / 是否允许对管理员动手，属 actions，跟着会话走）与
   * 协议（属 actions_global，只有群聊全局一份、对整台机器生效）。
   * 标题只是元数据缺失时的兜底；有元数据时用后端的 title / hint。
   * actions_global 的四项按 §7.4 只在群聊全局存在：读写都只碰 group，私聊那边不放第二份。 */
  /* 非工具项分两段：限制跟着会话走（actions），全局只有一份、对整台机器生效（actions_global）。 */
  var LIMITS = [
    { name: "ban_max_seconds", path: "actions.ban_max_seconds", title: "单次禁言上限", scopes: ["group"] },
    { name: "allow_target_admin", path: "actions.allow_target_admin", title: "允许对管理员动手", scopes: ["group"] }
  ];
  var GLOBAL_SETTINGS = [
    { name: "protocol", path: "actions_global.protocol", title: "协议", scopes: ["group"] }
  ];
  var GLOBALS = LIMITS.concat(GLOBAL_SETTINGS);

  /* actions_global 里的项对整个机器人生效，且全局只有一份（§7.4）：只读只写群聊全局。 */
  function isGlobalPath(path) {
    return path.indexOf("actions_global.") === 0;
  }

  /* 本标签管的暂存键（范围:路径）：标签红点不依赖是否已经挂载过，直接看壳的 dirty。 */
  var PATH_SET = {};
  TOOLS.concat(GLOBALS).forEach(function (def) {
    ALL_SCOPES.forEach(function (scope) { PATH_SET[scope + ":" + def.path] = true; });
  });

  var records = [];
  var rootEl = null;

  function icon(name, size) {
    return typeof LX.icon === "function" ? LX.icon(name, size) : null;
  }

  function ic(name, size) {
    return LX.el("i", { "data-icon": name, "data-size": size === undefined ? 16 : size });
  }

  function hasKit() {
    return !!(LX.formKit && typeof LX.formKit.buildControl === "function" &&
      typeof LX.formKit.makeView === "function");
  }

  function fieldOf(scope, path) {
    return typeof LX.fieldMeta === "function" ? LX.fieldMeta(scope, path) : null;
  }

  /* 这条配置项实际存在于哪些范围：有元数据就按元数据（后端 _g / _both 决定），
   * 没有元数据（be1 之前）用清单里的 scopes 兜底，好把开关位置摆出来。 */
  function scopesFor(def) {
    // §7.4：actions_global 只有「群聊全局」一份、对整台机器生效 —— 元数据怎么给都只读写 group
    if (isGlobalPath(def.path)) return ["group"];
    var found = [];
    ALL_SCOPES.forEach(function (scope) { if (fieldOf(scope, def.path)) found.push(scope); });
    return found.length ? found : def.scopes.slice();
  }

  function hasMeta(def) {
    var hit = false;
    ALL_SCOPES.forEach(function (scope) { if (!hit && fieldOf(scope, def.path)) hit = true; });
    return hit;
  }

  function primaryOf(scopes) {
    return scopes.indexOf("group") >= 0 ? "group" : scopes[0];
  }

  function fmtValue(value) {
    if (LX.formKit && typeof LX.formKit.fmtDefault === "function") return LX.formKit.fmtDefault(value);
    if (value === null || value === undefined) return "（空）";
    if (value === true) return "开";
    if (value === false) return "关";
    return String(value);
  }

  /* 一个控件视图：读写按这条配置项存在的范围分发（一个开关同时写私聊 + 群聊两套设置，
   * 这样模型在两种会话里都看得到；只在群聊生效的动作只写群聊）。 */
  function ctxFor(scopes) {
    var primary = primaryOf(scopes);
    return {
      getValue: function (scope, path) { return LX.getValue(primary, path); },
      setValue: function (scope, path, value) {
        scopes.forEach(function (sc) { LX.setValue(sc, path, value); });
      },
      revertValue: function (scope, path) {
        if (typeof LX.revertValue !== "function") return;
        scopes.forEach(function (sc) { LX.revertValue(sc, path); });
      },
      isDirty: function (scope, path) {
        return scopes.some(function (sc) { return LX.isDirty(sc, path); });
      },
      getError: function (scope, path) {
        for (var i = 0; i < scopes.length; i++) {
          var err = LX.getError(scopes[i], path);
          if (err) return err;
        }
        return null;
      }
    };
  }

  /* 亮的开关：控件由 form.js 的 LX.formKit 生成（类型 / 范围 / 选项 / 默认值全来自元数据）。 */
  function buildControlInto(def, scopes, box) {
    var primary = primaryOf(scopes);
    var field = fieldOf(primary, def.path) || fieldOf(scopes[0], def.path);
    if (!field || !hasKit()) return null;
    var view = LX.formKit.makeView(primary, ctxFor(scopes));
    var built = LX.formKit.buildControl(view, field, box);
    return { field: field, view: view, built: built };
  }

  /* 元数据还没到：只摆一个灰的开关位置（不是真控件，点不了，也不会写任何配置）。 */
  function pendingInto(def, box) {
    box.appendChild(LX.el("span", { id: "act-pending-" + def.name, class: "act-pending" }, [
      LX.el("span", { class: "act-pending-track", "aria-hidden": "true" }),
      LX.el("span", { class: "act-pending-text", text: hasKit() ? "等后端" : "没有 form.js" })
    ]));
  }

  function defaultText(def) {
    var scopes = scopesFor(def);
    var field = fieldOf(primaryOf(scopes), def.path) || fieldOf(scopes[0], def.path);
    return field ? "默认：" + fmtValue(field.default) : "";
  }

  /* 两边值不一样时给一句话：这不影响保存，只是让人知道"改一下会同时改两边"。 */
  function mixedText(def, scopes) {
    if (scopes.length < 2) return "";
    var vals = scopes.map(function (scope) { return fmtValue(LX.getValue(scope, def.path)); });
    var same = vals.every(function (v) { return v === vals[0]; });
    if (same) return "";
    var parts = scopes.map(function (scope, i) { return SCOPE_TEXT[scope] + "：" + vals[i]; });
    return parts.join(" · ") + "（这里的开关会把两边一起改）";
  }

  function badge(id, cls, text) {
    return LX.el("span", { id: id, class: "badge " + cls, text: text });
  }

  function buildRecordBits(def, scopes, has) {
    var bits = {
      dirty: badge("act-dirty-" + def.name, "badge-warn act-dirty hidden", "已改动"),
      mixed: LX.el("div", { id: "act-mixed-" + def.name, class: "hint act-mixed hidden" }),
      fallback: LX.el("span", { id: "act-default-" + def.name, class: "act-default", text: has ? defaultText(def) : "" })
    };
    var rec = { def: def, scopes: scopes, hasMeta: has, bits: bits, built: null };
    return rec;
  }

  function refreshRecord(rec) {
    var dirty = rec.scopes.some(function (scope) { return LX.isDirty(scope, rec.def.path); });
    rec.bits.dirty.classList.toggle("hidden", !dirty);
    var note = mixedText(rec.def, rec.scopes);
    rec.bits.mixed.textContent = note;
    rec.bits.mixed.classList.toggle("hidden", !note);
    syncControls(rec);
  }

  /* 同一个配置项在别的标签（私聊 / 群聊表单）里改了、或者放弃了改动，这里的控件要跟着走。 */
  function syncControls(rec) {
    if (!rec.built) return;
    var controls = (rec.built && rec.built.controls) || [];
    var value = LX.getValue(primaryOf(rec.scopes), rec.def.path);
    controls.forEach(function (control) {
      if (document.activeElement === control) return;
      if (control.type === "checkbox") {
        control.checked = !!value;
        if (rec.built.toggleText) rec.built.toggleText.textContent = control.checked ? "开" : "关";
      } else if (control.tagName === "SELECT") {
        control.value = (value === null || value === undefined) ? "" : String(value);
      } else if (control.type === "number" || control.type === "range") {
        if (typeof value === "number") control.value = String(value);
      }
    });
  }

  function refreshAll() {
    records.forEach(refreshRecord);
    refreshMissingNote();
  }

  function refreshMissingNote() {
    var note = document.getElementById("act-missing");
    if (!note) return;
    var missing = 0;
    records.forEach(function (rec) { if (!rec.hasMeta) missing += 1; });
    if (!missing) {
      note.classList.add("hidden");
      note.textContent = "";
      return;
    }
    note.classList.remove("hidden");
    note.textContent = "后端还没提供 " + missing + " 项配置（actions / actions_global 两个块）：版面和开关位置已经就位，" +
      "等后端把这两个块落到规格里，这里会自动接上；在那之前这些开关不能点，也写不进任何配置。";
  }

  /* ---- 工具卡：一张卡一个工具 ---- */
  function buildToolCard(def) {
    var scopes = scopesFor(def);
    var has = hasMeta(def);
    var rec = buildRecordBits(def, scopes, has);
    var card = LX.el("div", { id: "act-card-" + def.name, class: "card act-card", dataset: { tool: def.name } });
    var head = LX.el("div", { class: "act-card-head" });
    var chip = LX.el("span", { class: "card-icon act-card-icon" });
    var glyph = icon(def.icon || "bolt", 18);
    if (glyph) chip.appendChild(glyph);
    else chip.appendChild(ic(def.icon || "bolt", 18));
    var meta = LX.el("div", { class: "act-card-meta" });
    if (def.danger) {
      var danger = LX.el("span", { id: "act-danger-" + def.name, class: "badge badge-danger act-danger" });
      var warnIcon = icon("warning", 12);
      if (warnIcon) danger.appendChild(warnIcon);
      danger.appendChild(document.createTextNode("危险动作"));
      meta.appendChild(danger);
    }
    if (isGlobalPath(def.path)) {
      meta.appendChild(LX.el("span", {
        id: "act-globalmark-" + def.name, class: "badge badge-info act-globalmark",
        text: "全局生效"
      }));
    }
    meta.appendChild(rec.bits.dirty);
    if (has) meta.appendChild(rec.bits.fallback);
    var titles = LX.el("div", { class: "act-card-titles" }, [
      LX.el("h3", { class: "act-card-title", text: def.title }),
      meta
    ]);
    head.appendChild(chip);
    head.appendChild(titles);
    var switchBox = LX.el("div", { id: "act-toggle-" + def.name, class: "act-card-switch" });
    head.appendChild(switchBox);
    card.appendChild(head);
    card.appendChild(LX.el("div", { id: "act-risk-" + def.name, class: "hint act-risk", text: def.risk }));
    card.appendChild(LX.el("code", { class: "act-path", text: def.path }));
    card.appendChild(rec.bits.mixed);
    var packed = has ? buildControlInto(def, scopes, switchBox) : null;
    if (packed) rec.built = packed.built;
    else {
      pendingInto(def, switchBox);
      card.appendChild(LX.el("div", {
        id: "act-pending-note-" + def.name, class: "hint act-pending-note",
        text: "开关暂时不能点：配置项还没就位。"
      }));
    }
    records.push(rec);
    refreshRecord(rec);
    return card;
  }

  /* ---- 全局项：一行一项 ---- */
  function buildGlobalRow(def) {
    var scopes = scopesFor(def);
    var has = hasMeta(def);
    var rec = buildRecordBits(def, scopes, has);
    var primary = primaryOf(scopes);
    var field = fieldOf(primary, def.path) || fieldOf(scopes[0], def.path);
    var title = (field && field.title) || def.title;
    var row = LX.el("div", { id: "act-global-" + def.name, class: "act-row", dataset: { path: def.path } });
    var main = LX.el("div", { class: "act-row-main" }, [
      LX.el("div", { class: "act-row-title" }, [
        LX.el("span", { class: "act-row-name", text: title }),
        rec.bits.dirty
      ])
    ]);
    if (field && field.hint) main.appendChild(LX.el("div", { class: "hint", text: field.hint }));
    main.appendChild(LX.el("code", { class: "act-path", text: def.path }));
    main.appendChild(rec.bits.mixed);
    row.appendChild(main);
    var side = LX.el("div", { id: "act-global-control-" + def.name, class: "act-row-side" });
    var packed = has ? buildControlInto(def, scopes, side) : null;
    if (packed) {
      rec.built = packed.built;
      side.appendChild(rec.bits.fallback);
    } else {
      pendingInto(def, side);
      side.appendChild(LX.el("div", {
        id: "act-pending-note-" + def.name, class: "hint act-pending-note", text: "等后端提供这一项。"
      }));
    }
    row.appendChild(side);
    records.push(rec);
    refreshRecord(rec);
    return row;
  }

  function buildIntro() {
    var card = LX.el("div", { id: "act-intro", class: "card act-intro" });
    var head = LX.el("div", { class: "card-head" });
    var chip = LX.el("span", { class: "card-icon" });
    var glyph = icon("bolt", 18);
    if (glyph) chip.appendChild(glyph);
    else chip.appendChild(ic("bolt", 18));
    head.appendChild(chip);
    head.appendChild(LX.el("div", { class: "card-titles" }, [
      LX.el("h3", { text: TAB_TITLE }),
      LX.el("div", { class: "card-sub", text: "这些是让模型能做的群里的动作；每项默认关；开了之后模型才看得到这个工具。" })
    ]));
    card.appendChild(head);
    var infoIcon = icon("info", 16);
    var infoIconBox = LX.el("span", { class: "alert-icon" });
    if (infoIcon) infoIconBox.appendChild(infoIcon);
    card.appendChild(LX.el("div", { class: "alert alert-info act-note" }, [
      infoIconBox,
      LX.el("div", { class: "alert-body" }, [
        LX.el("div", { text: "开关会同时写进私聊 / 群聊两套设置（只在群聊生效的动作只写群聊），所以一个开关在改动清单里可能算两项。" }),
        LX.el("div", { text: "标了「全局生效」的动作和「全局（对整个机器人生效）」那一区，只有一份设置，对整台机器上的所有会话生效。" }),
        LX.el("div", { text: "这些动作由机器人的号去执行：机器人得是群管理员（有的还得是群主），OneBot 那边也要给机器人对应的权限。" })
      ])
    ]));
    card.appendChild(LX.el("div", { id: "act-missing", class: "hint act-missing hidden" }));
    return card;
  }

  function mount(root) {
    rootEl = root;
    records = [];
    while (root.firstChild) root.removeChild(root.firstChild);
    root.appendChild(buildIntro());

    root.appendChild(LX.el("div", { class: "group-title" }, [
      "限制 ",
      LX.el("span", { class: "count", text: String(LIMITS.length) })
    ]));
    var limitCard = LX.el("div", { id: "act-limits", class: "card act-global" });
    LIMITS.forEach(function (def) { limitCard.appendChild(buildGlobalRow(def)); });
    root.appendChild(limitCard);

    root.appendChild(LX.el("div", { class: "group-title" }, [
      "全局（对整个机器人生效） ",
      LX.el("span", { class: "count", text: String(GLOBAL_SETTINGS.length) })
    ]));
    var globalCard = LX.el("div", { id: "act-global", class: "card act-global" });
    GLOBAL_SETTINGS.forEach(function (def) { globalCard.appendChild(buildGlobalRow(def)); });
    root.appendChild(globalCard);

    root.appendChild(LX.el("div", { class: "group-title" }, [
      "工具 ",
      LX.el("span", { class: "count", text: String(TOOLS.length) })
    ]));
    var grid = LX.el("div", { id: "act-grid", class: "act-grid" });
    TOOLS.forEach(function (def) { grid.appendChild(buildToolCard(def)); });
    root.appendChild(grid);

    refreshAll();
    if (typeof LX.decorateIcons === "function") LX.decorateIcons(root);
  }

  /* 标签红点：只看壳的暂存，不依赖是否挂载过（没进过这一页也能亮）。 */
  function isDirtyTab() {
    var dirty = LX.state && LX.state.dirty;
    if (!dirty) return false;
    var hit = false;
    dirty.forEach(function (item, key) { if (PATH_SET[key]) hit = true; });
    return hit;
  }

  /* 别的标签改了同一项、放弃改动、重新加载：跟着刷新（标签没挂载过就什么都不做）。 */
  LX.onChange(function () { refreshAll(); });
  LX.onReload(function () { if (rootEl) mount(rootEl); });

  LX.actionsTab = {
    title: TAB_TITLE,
    icon: TAB_ICON,
    mount: mount,
    onShow: refreshAll,
    isDirty: isDirtyTab
  };
})();
