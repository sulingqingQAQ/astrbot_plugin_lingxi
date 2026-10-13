/* DIAG-3m：诊断标签（最小版）。
 * 概览条 / 7 个指标磁贴 / 触发源状态卡 / 决策时间线 / 日志 / 导出区。
 * 纯静态、原生 JS，无 CDN。所有动态文字走 textContent（LX.el 的 text），不拼 HTML。
 * DOM 钩子名见 DIAG-0 §4.3，不许改名。阶段 2 才做的（条形图 #diag-chart、自动刷新 #diag-auto、
 * 事件 / 日志筛选与搜索、日志复制、条目展开按钮）这里一个都没画。 */
(function () {
  "use strict";
  if (!window.LX) return;

  var EXPORT_REVOKE_MS = 1500;

  var ui = {};          // 建好的节点引用
  var seq = 0;          // 请求序号：慢的旧响应不能盖掉新的
  var loadedOnce = false;

  // ---- 小零件 ----
  function ic(name, size) {
    return typeof LX.icon === "function" ? LX.icon(name, size) : null;
  }

  function clearNode(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function iconButton(attrs, iconName) {
    var b = LX.el("button", attrs);
    var i = ic(iconName, 14);
    if (i) b.insertBefore(i, b.firstChild);
    return b;
  }

  function cardHead(iconName, title, sub) {
    var chip = LX.el("span", { class: "card-icon" });
    var i = ic(iconName, 18);
    if (i) chip.appendChild(i);
    var titles = LX.el("div", { class: "card-titles" }, [LX.el("h3", { text: title })]);
    if (sub) titles.appendChild(LX.el("div", { class: "card-sub", text: sub }));
    return LX.el("div", { class: "card-head" }, [chip, titles]);
  }

  function pad2(n) { return n < 10 ? "0" + n : String(n); }

  function clockText(ts) {
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return "--:--:--";
    var hms = pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
    var now = new Date();
    if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()) {
      return hms;
    }
    return pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) + " " + hms;
  }

  // 长写法："3 小时 12 分"
  function spanText(sec) {
    var s = Math.max(0, Math.round(Number(sec) || 0));
    if (s < 60) return s + " 秒";
    var m = Math.floor(s / 60);
    if (m < 60) return m + " 分钟";
    var h = Math.floor(m / 60);
    if (h < 24) return h + " 小时" + (m % 60 ? " " + (m % 60) + " 分" : "");
    var d = Math.floor(h / 24);
    return d + " 天" + (h % 24 ? " " + (h % 24) + " 小时" : "");
  }

  // 短写法（磁贴大数字用）："3时12分"
  function spanShort(sec) {
    var s = Math.max(0, Math.round(Number(sec) || 0));
    if (s < 60) return s + " 秒";
    var m = Math.floor(s / 60);
    if (m < 60) return m + " 分";
    var h = Math.floor(m / 60);
    if (h < 24) return h + "时" + pad2(m % 60) + "分";
    return Math.floor(h / 24) + "天" + (h % 24) + "时";
  }

  function cnt(counters, key) {
    var v = counters && counters[key];
    return typeof v === "number" ? v : 0;
  }

  function errorEvents(counters) {
    var n = 0;
    Object.keys(counters || {}).forEach(function (k) {
      if (/^error\.[^.]+$/.test(k) && typeof counters[k] === "number") n += counters[k];
    });
    return n;
  }

  function isObj(v) { return v !== null && typeof v === "object"; }

  // ---- 当前开关 → 接口参数 ----
  function currentOptions() {
    return {
      include_text: ui.optText && ui.optText.checked ? 1 : 0,
      mask_ids: ui.optIds && ui.optIds.checked ? 0 : 1
    };
  }

  // ---- 指标磁贴 ----
  var TILES = [
    { key: "uptime", label: "运行时长", icon: "clock", tone: "" },
    { key: "seen", label: "见到的消息", icon: "chat", tone: "" },
    { key: "triggers", label: "触发", icon: "bolt", tone: "info" },
    { key: "delivered", label: "送达", icon: "check", tone: "ok" },
    { key: "silent", label: "沉默", icon: "ghost", tone: "info" },
    { key: "denied", label: "被拦下", icon: "shield", tone: "warn" },
    { key: "problems", label: "错误与警告", icon: "alert-circle", tone: "danger" }
  ];

  function buildTiles() {
    ui.tiles = {};
    var kids = TILES.map(function (t) {
      var label = LX.el("div", { class: "metric-label" }, [ic(t.icon, 14), t.label]);
      var value = LX.el("div", { class: "metric-value diag-tile-value", text: "—" });
      var note = LX.el("div", { class: "metric-note diag-tile-note" });
      var tile = LX.el("div", {
        class: "metric diag-tile" + (t.tone ? " metric-" + t.tone : ""),
        dataset: { key: t.key }
      }, [label, value, note]);
      ui.tiles[t.key] = { box: tile, value: value, note: note };
      return tile;
    });
    return LX.el("div", { class: "metric-grid diag-tiles" }, kids);
  }

  function renderTiles(data) {
    var c = data.counters || {};
    var seenP = cnt(c, "seen.private");
    var seenG = cnt(c, "seen.group");
    var delivered = cnt(c, "outcome.delivered");
    var silent = cnt(c, "outcome.silent");
    var denied = cnt(c, "gate.deny");
    var errs = errorEvents(c);
    var logs = data.logs || [];
    var logProblems = 0;
    logs.forEach(function (r) { if (r && (r.level === "error" || r.level === "warn")) logProblems += 1; });
    var vals = {
      uptime: [spanShort(data.uptime_seconds), "自插件启动起"],
      seen: [String(seenP + seenG), "私聊 " + seenP + " · 群聊 " + seenG],
      triggers: [String(delivered + silent + denied + errs), "送达 + 沉默 + 被拦下 + 出错（自启动起）"],
      delivered: [String(delivered), "真的发出去的回复"],
      silent: [String(silent), "想了想决定不说话"],
      denied: [String(denied), "闸门拦下，没去问模型"],
      problems: [String(errs + logProblems), "出错事件 + 错误 / 警告日志"]
    };
    TILES.forEach(function (t) {
      var slot = ui.tiles[t.key];
      slot.value.textContent = vals[t.key][0];
      slot.note.textContent = vals[t.key][1];
    });
  }

  // ---- 概览条 ----
  var HEALTH = {
    ok: ["正常", "badge-ok"],
    warn: ["有提示", "badge-warn"],
    error: ["有错误", "badge-danger"]
  };

  function repliesLastHour(data) {
    var now = Number(data.generated_at_ts) || Date.now() / 1000;
    var n = 0;
    (data.events || []).forEach(function (e) {
      if (e && e.result === "delivered" && !e.prev_run && Number(e.ts) >= now - 3600) n += 1;
    });
    return n;
  }

  function renderOverview(data) {
    var h = HEALTH[data.health] ? data.health : "ok";
    ui.health.setAttribute("data-health", h);
    ui.health.className = "badge dot " + HEALTH[h][1];
    ui.health.textContent = HEALTH[h][0];
    ui.summary.textContent = "已运行 " + spanText(data.uptime_seconds) + " · 近 1 小时回复 " + repliesLastHour(data) + " 次";
    ui.updated.textContent = "更新于 " + clockText(data.generated_at_ts || Date.now() / 1000);
    clearNode(ui.notices);
    var warns = (data.warnings || []).filter(function (w) { return typeof w === "string" && w; });
    if (warns.length) {
      var shown = warns.slice(0, 5);
      var body = LX.el("div", { class: "alert-body" }, shown.map(function (w) {
        return LX.el("div", { class: "diag-notice", text: w });
      }));
      if (warns.length > shown.length) {
        body.appendChild(LX.el("div", { class: "hint", text: "还有 " + (warns.length - shown.length) + " 条没显示" }));
      }
      ui.notices.appendChild(LX.el("div", { class: "alert alert-warn" }, [
        LX.el("span", { class: "alert-icon" }, [ic("warning", 16)]), body
      ]));
    }
    if (data.file_error) {
      ui.notices.appendChild(LX.el("div", { class: "alert alert-info" }, [
        LX.el("span", { class: "alert-icon" }, [ic("info", 16)]),
        LX.el("div", { class: "alert-body", text: "诊断记录文件写不进去，重启后看不到上次运行的轨迹：" + String(data.file_error) })
      ]));
    }
    ui.notices.classList.toggle("hidden", !ui.notices.firstChild);
  }

  // ---- 触发源状态卡 ----
  var SOURCES = [
    { key: "idle", name: "空闲触发", icon: "clock" },
    { key: "wake", name: "唤醒", icon: "bolt" },
    { key: "followup", name: "追发", icon: "chat" },
    { key: "next_reply", name: "自约回复", icon: "refresh" },
    { key: "ticker", name: "调度器", icon: "pulse" },
    { key: "history", name: "历史缓冲", icon: "list" },
    { key: "handlers", name: "消息处理", icon: "settings" },
    { key: "runner", name: "运行器", icon: "wave" }
  ];

  function numOrDash(v) { return typeof v === "number" ? String(v) : "—"; }

  function inText(v) { return typeof v === "number" ? spanText(v) : null; }

  // 返回 { nums: [[数字, 标签], …], text: 一句大白话 }
  function sourceView(key, st) {
    var n = function (k) { return typeof st[k] === "number" ? st[k] : 0; };
    if (key === "idle") {
      var due = inText(st.next_due_in);
      return {
        nums: [[numOrDash(st.anchors), "会话"], [numOrDash(st.waits), "等待中"], [due || "—", "最近到点"]],
        text: n("anchors") === 0 && n("waits") === 0 ? "还没有会话在等空闲触发。"
          : n("anchors") + " 个会话在等" + (due ? "，最快 " + due + "后到点。" : "，暂时估不出到点时间。")
      };
    }
    if (key === "wake") {
      var fire = inText(st.next_fire_in);
      return {
        nums: [[numOrDash(st.tasks), "任务"], [fire || "—", "最近触发"]],
        text: n("tasks") === 0 ? "没有唤醒任务。"
          : n("tasks") + " 个唤醒任务" + (fire ? "，最快 " + fire + "后触发。" : "，暂时估不出触发时间。")
      };
    }
    if (key === "followup") {
      return {
        nums: [[numOrDash(st.pending), "排队中"]],
        text: n("pending") === 0 ? "没有追发在排队。" : "有 " + n("pending") + " 个追发在排队。"
      };
    }
    if (key === "next_reply") {
      return {
        nums: [[numOrDash(st.pending), "排队中"]],
        text: n("pending") === 0 ? "没有自约回复在排队。" : "有 " + n("pending") + " 个自约回复在排队。"
      };
    }
    if (key === "ticker") {
      return {
        nums: [[numOrDash(st.sources), "触发源"], [numOrDash(st.recent_fires), "近 30 秒触发"]],
        text: "调度器盯着 " + n("sources") + " 个触发源，近 30 秒触发了 " + n("recent_fires") + " 次。"
      };
    }
    if (key === "history") {
      return {
        nums: [[numOrDash(st.sessions), "会话"], [numOrDash(st.messages), "消息"]],
        text: "内存里缓存了 " + n("sessions") + " 个会话、" + n("messages") + " 条消息（只数数量，不看内容）。"
      };
    }
    if (key === "handlers") {
      return {
        nums: [[numOrDash(st.busy), "处理中"], [numOrDash(st.merging), "合并中"]],
        text: n("busy") + " 个会话正在处理，" + n("merging") + " 个在合并消息。"
      };
    }
    // runner
    var on = st.running === true;
    return {
      nums: [[on ? "运行中" : "没在跑", "状态"]],
      text: on ? "定时运行器在跑。" : "定时运行器没在跑。"
    };
  }

  function renderSources(data) {
    clearNode(ui.sources);
    var state = isObj(data.state) ? data.state : {};
    SOURCES.forEach(function (s) {
      var st = state[s.key];
      var head = LX.el("div", { class: "diag-source-head" }, [
        LX.el("span", { class: "card-icon" }, [ic(s.icon, 16)]),
        LX.el("span", { class: "diag-source-name", text: s.name })
      ]);
      var kids = [head];
      if (!isObj(st) || typeof st.error === "string") {
        kids.push(LX.el("div", { class: "diag-source-text diag-source-missing", text: "没装 / 取不到" }));
        if (isObj(st) && st.error) kids.push(LX.el("div", { class: "hint", text: "原因：" + st.error }));
      } else {
        var v = sourceView(s.key, st);
        kids.push(LX.el("div", { class: "diag-nums" }, v.nums.map(function (p) {
          return LX.el("div", { class: "diag-num" }, [
            LX.el("div", { class: "diag-num-value num", text: p[0] }),
            LX.el("div", { class: "diag-num-label", text: p[1] })
          ]);
        })));
        kids.push(LX.el("div", { class: "diag-source-text", text: v.text }));
      }
      ui.sources.appendChild(LX.el("div", {
        class: "card card-flat diag-source", dataset: { key: s.key }
      }, kids));
    });
  }

  // ---- 决策时间线 ----
  // [结果文字, 徽章色调, 时间线圆点色调]
  var RESULT = {
    allow: ["放行", "neutral", ""],
    deny: ["拦下", "warn", "warn"],
    delivered: ["送达", "ok", "ok"],
    silent: ["沉默", "info", "info"],
    empty: ["空回复", "warn", "warn"],
    undelivered: ["没送到", "danger", "danger"],
    error: ["出错", "danger", "danger"]
  };
  var SOURCE_TEXT = {
    direct: "直接触发", wait: "等待", interval: "定时", activity: "活跃",
    idle: "空闲", followup: "追发", next_reply: "自约", wake: "唤醒"
  };
  var SCOPE_TEXT = { "private": "私聊", group: "群聊" };

  function eventItem(e) {
    var res = RESULT[e.result] || [String(e.result || "未知"), "neutral", ""];
    var li = LX.el("li", {
      class: "tl-item diag-event" + (res[2] ? " tl-" + res[2] : "") + (e.prev_run ? " tl-muted" : ""),
      dataset: { result: e.result || "", source: e.source || "none" }
    });
    if (e.prev_run) li.setAttribute("data-prev", "1");
    li.appendChild(LX.el("span", { class: "tl-dot" }));
    var head = LX.el("div", { class: "tl-head" }, [LX.el("span", { class: "tl-time", text: clockText(e.ts) })]);
    if (e.prev_run) head.appendChild(LX.el("span", { class: "pill diag-event-prev", text: "上次运行" }));
    head.appendChild(LX.el("span", { class: "pill diag-event-source", text: SOURCE_TEXT[e.source] || "—" }));
    if (e.scope) head.appendChild(LX.el("span", { class: "pill pill-accent diag-event-scope", text: SCOPE_TEXT[e.scope] || String(e.scope) }));
    if (e.session) head.appendChild(LX.el("span", { class: "pill pill-mono diag-event-session", text: String(e.session) }));
    head.appendChild(LX.el("span", { class: "badge badge-" + res[1] + " diag-event-result", text: res[0] }));
    li.appendChild(head);
    var bits = [];
    if (e.reason) bits.push(String(e.reason));
    if (Number(e.chars) > 0) bits.push(Number(e.chars) + " 字");
    if (bits.length) li.appendChild(LX.el("div", { class: "tl-body diag-event-reason", text: bits.join(" · ") }));
    if (typeof e.text === "string" && e.text !== "") {
      li.appendChild(LX.el("div", { class: "diag-event-text", text: e.text }));
    }
    return li;
  }

  function renderTimeline(data) {
    var events = Array.isArray(data.events) ? data.events : [];
    clearNode(ui.timeline);
    events.forEach(function (e) { if (isObj(e)) ui.timeline.appendChild(eventItem(e)); });
    ui.timelineCount.textContent = String(events.length);
    ui.empty.classList.toggle("hidden", events.length > 0);
    ui.timeline.classList.toggle("hidden", events.length === 0);
    ui.timeline.scrollTop = ui.timeline.scrollHeight;
  }

  // ---- 日志 ----
  var LEVEL_TEXT = { info: "INFO", warn: "WARN", error: "ERROR" };

  function renderLogs(data) {
    var logs = Array.isArray(data.logs) ? data.logs : [];
    clearNode(ui.logs);
    logs.forEach(function (r) {
      if (!isObj(r)) return;
      var level = LEVEL_TEXT[r.level] ? r.level : "info";
      ui.logs.appendChild(LX.el("div", { class: "diag-log", dataset: { level: level } }, [
        LX.el("span", { class: "diag-log-time", text: clockText(r.ts) }),
        LX.el("span", { class: "diag-log-level", text: LEVEL_TEXT[level] }),
        LX.el("span", { class: "diag-log-text", text: String(r.text === undefined || r.text === null ? "" : r.text) })
      ]));
    });
    ui.logsCount.textContent = String(logs.length);
    ui.logsEmpty.classList.toggle("hidden", logs.length > 0);
    ui.logs.classList.toggle("hidden", logs.length === 0);
    ui.logs.scrollTop = ui.logs.scrollHeight;
  }

  // ---- 加载 ----
  function setBusy(on) {
    ui.refresh.disabled = !!on;
    ui.refresh.classList.toggle("loading", !!on);
    if (on) ui.refresh.setAttribute("aria-busy", "true");
    else ui.refresh.removeAttribute("aria-busy");
  }

  function showError(message) {
    clearNode(ui.errorText);
    ui.errorText.appendChild(LX.el("div", { class: "alert-title", text: "没能读到诊断数据" }));
    ui.errorText.appendChild(LX.el("div", { class: "alert-text", text: String(message || "接口没有返回") }));
    ui.error.classList.remove("hidden");
  }

  function render(data) {
    renderOverview(data);
    renderTiles(data);
    renderSources(data);
    renderTimeline(data);
    renderLogs(data);
  }

  function load() {
    var mine = ++seq;
    setBusy(true);
    if (!loadedOnce) {
      ui.loading.classList.remove("hidden");
      ui.content.classList.add("hidden");
    }
    return LX.api.get("diag/status", currentOptions()).then(function (data) {
      if (mine !== seq) return;
      if (!isObj(data)) throw new Error("接口返回异常");
      ui.error.classList.add("hidden");
      render(data);
      loadedOnce = true;
      ui.loading.classList.add("hidden");
      ui.content.classList.remove("hidden");
      // 内容区刚显示出来，这时才量得到高度：时间线和日志都停在最新（最下面）
      ui.timeline.scrollTop = ui.timeline.scrollHeight;
      ui.logs.scrollTop = ui.logs.scrollHeight;
      setBusy(false);
    }).catch(function (e) {
      if (mine !== seq) return;
      showError(e && e.message ? e.message : String(e));
      ui.loading.classList.add("hidden");
      if (loadedOnce) ui.content.classList.remove("hidden");
      setBusy(false);
    });
  }

  // ---- 导出 ----
  function decodeBase64(b64) {
    var bin = atob(String(b64));
    var out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function sizeText(n) {
    var b = Number(n) || 0;
    return b < 1024 ? b + " B" : (b / 1024).toFixed(1) + " KB";
  }

  function triggerDownload(bytes, filename, mime) {
    var blob = new Blob([bytes], { type: mime || "application/zip" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, EXPORT_REVOKE_MS);
  }

  function setExportBusy(on) {
    ui.exportBtn.disabled = !!on;
    ui.exportBtn.classList.toggle("loading", !!on);
    if (on) ui.exportBtn.setAttribute("aria-busy", "true");
    else ui.exportBtn.removeAttribute("aria-busy");
  }

  function doExport() {
    var o = currentOptions();
    setExportBusy(true);
    ui.exportResult.textContent = "正在打包……";
    return LX.api.post("diag/export", { include_text: !!o.include_text, mask_ids: !!o.mask_ids }).then(function (data) {
      if (!isObj(data) || !data.base64 || !data.filename) throw new Error("接口没有返回诊断包");
      triggerDownload(decodeBase64(data.base64), String(data.filename), data.mime);
      ui.exportResult.textContent = "已导出 " + data.filename + " · " + sizeText(data.size) + " · " +
        (data.contains_text ? "含回复正文" : "不含回复正文");
      setExportBusy(false);
    }).catch(function (e) {
      var msg = e && e.message ? e.message : String(e);
      ui.exportResult.textContent = "导出失败：" + msg;
      LX.toast("导出失败：" + msg, "error");
      setExportBusy(false);
    });
  }

  // ---- 挂载 ----
  function toggleRow(id, text, hint) {
    var input = document.createElement("input");
    input.id = id;
    input.setAttribute("type", "checkbox");
    input.setAttribute("role", "switch");
    var label = LX.el("label", { class: "toggle" }, [
      input, LX.el("span", { class: "toggle-track" }), LX.el("span", { class: "toggle-text", text: text })
    ]);
    return { input: input, row: LX.el("div", { class: "diag-opt" }, [label, LX.el("div", { class: "hint", text: hint })]) };
  }

  function mount(root) {
    // 出错条
    ui.errorText = LX.el("div", { class: "alert-body" });
    var retry = LX.el("button", { class: "small", text: "重试", on: { click: function () { load(); } } });
    ui.error = LX.el("div", { id: "diag-error", class: "alert alert-danger hidden", role: "alert" }, [
      LX.el("span", { class: "alert-icon" }, [ic("alert-circle", 18)]),
      ui.errorText,
      LX.el("div", { class: "alert-actions" }, [retry])
    ]);

    // 概览条
    ui.health = LX.el("span", { id: "diag-health", class: "badge badge-neutral dot", text: "加载中" });
    ui.summary = LX.el("div", { id: "diag-summary", class: "diag-summary", text: "正在读取诊断数据……" });
    ui.updated = LX.el("span", { id: "diag-updated", class: "hint diag-updated" });
    ui.refresh = iconButton({ id: "diag-refresh", class: "small", text: "刷新", on: { click: function () { load(); } } }, "refresh");
    ui.notices = LX.el("div", { class: "diag-notices hidden" });
    var overview = LX.el("div", { class: "card diag-overview" }, [
      LX.el("div", { class: "diag-overview-row" }, [
        ui.health, ui.summary,
        LX.el("div", { class: "diag-overview-end" }, [ui.updated, ui.refresh])
      ]),
      ui.notices
    ]);

    // 骨架
    ui.loading = LX.el("div", { id: "diag-loading", class: "diag-loading" }, [
      LX.el("div", { class: "card" }, [
        LX.el("span", { class: "skeleton skel-title" }),
        LX.el("span", { class: "skeleton skel-line" }),
        LX.el("span", { class: "skeleton skel-line skel-short" }),
        LX.el("span", { class: "skeleton skel-block" })
      ])
    ]);

    // 内容区
    ui.sources = LX.el("div", { class: "diag-sources" });
    ui.timelineCount = LX.el("span", { class: "count", text: "0" });
    ui.timeline = LX.el("ol", { id: "diag-timeline", class: "timeline hidden" });
    ui.empty = LX.el("div", { id: "diag-empty", class: "empty hidden" }, [
      LX.el("div", { class: "empty-icon" }, [ic("ghost", 22)]),
      LX.el("div", { class: "empty-title", text: "还没有任何决策记录" }),
      LX.el("div", { class: "empty-text", text: "机器人一旦判断要不要说话，这里就会出现一条。刚启动、还没人说话时是空的。" })
    ]);
    ui.logsCount = LX.el("span", { class: "count", text: "0" });
    ui.logs = LX.el("div", { id: "diag-logs", class: "diag-logs hidden" });
    ui.logsEmpty = LX.el("div", { class: "empty hidden" }, [
      LX.el("div", { class: "empty-icon" }, [ic("file", 22)]),
      LX.el("div", { class: "empty-title", text: "还没有日志" })
    ]);
    ui.content = LX.el("div", { id: "diag-content", class: "hidden" }, [
      buildTiles(),
      LX.el("div", { class: "group-title" }, ["触发源状态"]),
      ui.sources,
      LX.el("div", { class: "group-title" }, ["决策时间线 ", ui.timelineCount]),
      LX.el("div", { class: "card" }, [
        LX.el("div", { class: "hint diag-tl-hint", text: "最近 100 条，新的在下。灰色的是上次运行留下的。" }),
        ui.empty, ui.timeline
      ]),
      LX.el("div", { class: "group-title" }, ["日志 ", ui.logsCount]),
      LX.el("div", { class: "card" }, [
        LX.el("div", { class: "hint diag-tl-hint", text: "最近 150 行，新的在下。" }),
        ui.logsEmpty, ui.logs
      ])
    ]);

    // 导出区
    var optText = toggleRow("diag-opt-text", "包含回复正文", "默认不勾。勾上后，时间线和导出的包会带上机器人的回复正文（每条最多 300 字）。");
    var optIds = toggleRow("diag-opt-ids", "显示完整会话号", "默认不勾，会话号和 QQ 号会打码成 # 加一串字母数字；勾上显示原号。");
    ui.optText = optText.input;
    ui.optIds = optIds.input;
    ui.textWarn = LX.el("div", { id: "diag-text-warn", class: "alert alert-warn hidden" }, [
      LX.el("span", { class: "alert-icon" }, [ic("warning", 16)]),
      LX.el("div", { class: "alert-body", text: "回复正文里可能有你们聊天的隐私内容，发给别人之前先看一眼。别人的发言和昵称永远不会被带出来。" })
    ]);
    optText.input.addEventListener("change", function () {
      ui.textWarn.classList.toggle("hidden", !optText.input.checked);
      load();
    });
    optIds.input.addEventListener("change", function () { load(); });
    ui.exportBtn = iconButton({ id: "diag-export", class: "primary", text: "导出诊断包（.zip）", on: { click: doExport } }, "download");
    ui.exportResult = LX.el("div", { id: "diag-export-result", class: "hint diag-export-result", role: "status" });
    var exportCard = LX.el("div", { class: "card diag-export" }, [
      cardHead("download", "导出诊断包", "里面有 diag.json（给程序看）和 summary.md（给人看），出问题时发给开发者。"),
      optText.row, ui.textWarn, optIds.row,
      LX.el("div", { class: "row diag-export-actions" }, [ui.exportBtn]),
      ui.exportResult
    ]);

    root.appendChild(ui.error);
    root.appendChild(overview);
    root.appendChild(ui.loading);
    root.appendChild(ui.content);
    root.appendChild(LX.el("div", { class: "group-title" }, ["导出"]));
    root.appendChild(exportCard);
  }

  LX.registerTab({
    id: "diag", title: "诊断", order: 80, icon: "pulse",
    mount: mount,
    onShow: load
  });
})();
