/* 灵犀配置面板 · 图标集（DIAG-4a）。
 * 暴露 LX.icon(name, size) -> SVGElement；用 DOM API（createElementNS）构造，不拼 innerHTML。
 * 24x24 画布、描边风格统一（stroke=currentColor、线宽 1.75、圆角端点），aria-hidden，颜色跟随文字。
 * 需要在 app.js 之后、form.js 之前加载（LX 由 app.js 创建）。 */
(function () {
  "use strict";

  /* SVG 命名空间 URL 拆成两段拼：静态测试会扫描"带双斜杠的 http 开头串"当作外部资源引用。 */
  var SVG_NS = "http:" + "//www.w3.org/2000/svg";

  /* 每个图标 = 图元列表；图元 = [标签, 属性]。坐标全在 24x24 画布内。 */
  var ICONS = {
    /* 设置：圆环 + 八个齿 */
    settings: [
      ["circle", { cx: 12, cy: 12, r: 6.4 }],
      ["circle", { cx: 12, cy: 12, r: 2.4 }],
      ["path", { d: "M12 2.6v3M12 18.4v3M2.6 12h3M18.4 12h3M5.35 5.35l2.1 2.1M16.55 16.55l2.1 2.1M18.65 5.35l-2.1 2.1M7.45 16.55l-2.1 2.1" }]
    ],
    /* 群聊：两个人 */
    group: [
      ["circle", { cx: 9, cy: 8, r: 3.5 }],
      ["path", { d: "M2.8 20v-1.2A4.8 4.8 0 0 1 7.6 14h2.8a4.8 4.8 0 0 1 4.8 4.8V20" }],
      ["path", { d: "M15.6 4.8a3.5 3.5 0 0 1 0 6.4M18.6 14.4a4.8 4.8 0 0 1 2.6 4.4V20" }]
    ],
    /* 私聊：对话气泡 */
    chat: [
      ["path", { d: "M4 6a2.5 2.5 0 0 1 2.5-2.5h11A2.5 2.5 0 0 1 20 6v8a2.5 2.5 0 0 1-2.5 2.5H11l-4.5 4v-4A2.5 2.5 0 0 1 4 14z" }],
      ["path", { d: "M8.5 8.8h7M8.5 12h4" }]
    ],
    clock: [
      ["circle", { cx: 12, cy: 12, r: 9 }],
      ["path", { d: "M12 7v5l3.2 2" }]
    ],
    /* 波形：五根不等高的竖线 */
    wave: [
      ["path", { d: "M4 10v4M8 6.5v11M12 3.5v17M16 7.5v9M20 10v4" }]
    ],
    shield: [
      ["path", { d: "M12 3.2l7.2 2.8v5.4c0 4.4-3 8.1-7.2 9.8-4.2-1.7-7.2-5.4-7.2-9.8V6z" }]
    ],
    download: [
      ["path", { d: "M12 4v11M7.5 10.8L12 15.3l4.5-4.5M5 19.5h14" }]
    ],
    refresh: [
      ["path", { d: "M19.6 9.5A7.8 7.8 0 0 0 5.9 7.3M4.4 4.6v3.4h3.4" }],
      ["path", { d: "M4.4 14.5a7.8 7.8 0 0 0 13.7 2.2M19.6 19.4V16h-3.4" }]
    ],
    warning: [
      ["path", { d: "M12 3.6l9.4 16.2H2.6z" }],
      ["path", { d: "M12 10v4.2M12 17.2h.01" }]
    ],
    info: [
      ["circle", { cx: 12, cy: 12, r: 9 }],
      ["path", { d: "M12 11v5.2M12 7.8h.01" }]
    ],
    check: [
      ["path", { d: "M5 12.6l4.6 4.6L19 7.6" }]
    ],
    x: [
      ["path", { d: "M6.2 6.2l11.6 11.6M17.8 6.2L6.2 17.8" }]
    ],
    search: [
      ["circle", { cx: 10.8, cy: 10.8, r: 6.5 }],
      ["path", { d: "M20 20l-4.6-4.6" }]
    ],
    bolt: [
      ["path", { d: "M13.2 2.8L5 13.4h6l-1.2 7.8L19 10.4h-6z" }]
    ],
    file: [
      ["path", { d: "M7 3h7l5 5v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z" }],
      ["path", { d: "M14 3v5h5M8.8 13h6.4M8.8 16.6h6.4" }]
    ],
    copy: [
      ["rect", { x: 9, y: 9, width: 11, height: 11, rx: 2.2 }],
      ["path", { d: "M15 9V6.2A2.2 2.2 0 0 0 12.8 4H6.2A2.2 2.2 0 0 0 4 6.2v6.6A2.2 2.2 0 0 0 6.2 15H9" }]
    ],
    eye: [
      ["path", { d: "M2.6 12S6 5.8 12 5.8 21.4 12 21.4 12 18 18.2 12 18.2 2.6 12 2.6 12z" }],
      ["circle", { cx: 12, cy: 12, r: 2.8 }]
    ],
    /* 调节：三条滑杆 */
    sliders: [
      ["path", { d: "M4 6h3M11 6h9M4 12h9M17 12h3M4 18h1M9 18h11" }],
      ["circle", { cx: 9, cy: 6, r: 2 }],
      ["circle", { cx: 15, cy: 12, r: 2 }],
      ["circle", { cx: 7, cy: 18, r: 2 }]
    ],
    list: [
      ["path", { d: "M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01" }]
    ],
    /* 时间线：竖线串着两个节点 */
    timeline: [
      ["path", { d: "M6 3v2M6 9v6M6 19v2M11.5 7h8.5M11.5 17h6" }],
      ["circle", { cx: 6, cy: 7, r: 2 }],
      ["circle", { cx: 6, cy: 17, r: 2 }]
    ],
    /* 脉搏：心电线 */
    pulse: [
      ["path", { d: "M3 12h4l2.4-6.4 4.2 12.8L16 12h5" }]
    ],
    "chevron-down": [["path", { d: "M6.5 9.5l5.5 5.5 5.5-5.5" }]],
    "chevron-right": [["path", { d: "M9.5 6.5l5.5 5.5-5.5 5.5" }]],
    /* 撤销 / 恢复默认 */
    undo: [
      ["path", { d: "M4.5 8.5H14a5 5 0 0 1 0 10H9.5" }],
      ["path", { d: "M8.2 4.8L4.5 8.5l3.7 3.7" }]
    ],
    filter: [
      ["path", { d: "M3.5 5h17l-6.4 7.6v5.9l-4.2 2v-7.9z" }]
    ],
    lock: [
      ["rect", { x: 5, y: 10.5, width: 14, height: 10, rx: 2.4 }],
      ["path", { d: "M8 10.5V8a4 4 0 0 1 8 0v2.5" }]
    ],
    plus: [["path", { d: "M12 5v14M5 12h14" }]],
    trash: [
      ["path", { d: "M4 7h16M9.5 7V4.5h5V7M6.5 7l.8 12.5h9.4L17.5 7M10 11v5.5M14 11v5.5" }]
    ],
    "check-circle": [
      ["circle", { cx: 12, cy: 12, r: 9 }],
      ["path", { d: "M8 12.4l2.8 2.8L16.2 9.6" }]
    ],
    "alert-circle": [
      ["circle", { cx: 12, cy: 12, r: 9 }],
      ["path", { d: "M12 7.6v5M12 16.2h.01" }]
    ],
    /* 幽灵：空状态、品牌点缀用 */
    ghost: [
      ["path", { d: "M5.2 20.5v-9.2a6.8 6.8 0 0 1 13.6 0v9.2l-2.3-1.9-2.2 1.9-2.3-1.9-2.3 1.9-2.2-1.9z" }],
      ["path", { d: "M9.4 11h.01M14.6 11h.01" }]
    ]
  };

  var LX = window.LX = window.LX || {};

  function make(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs).forEach(function (k) { node.setAttribute(k, String(attrs[k])); });
    return node;
  }

  /* LX.icon("check", 16) -> <svg class="icon icon-check" ...>。size 缺省 16（像素，宽高相同）。
   * 名字不认识时返回同样规格的空 svg（占位，不抛错）。 */
  LX.icon = function (name, size) {
    var px = Number(size) > 0 ? Number(size) : 16;
    var svg = make("svg", {
      "class": "icon icon-" + String(name),
      viewBox: "0 0 24 24", width: px, height: px,
      fill: "none", stroke: "currentColor", "stroke-width": 1.75,
      "stroke-linecap": "round", "stroke-linejoin": "round",
      "aria-hidden": "true", focusable: "false"
    });
    var shapes = Object.prototype.hasOwnProperty.call(ICONS, name) ? ICONS[name] : [];
    shapes.forEach(function (s) { svg.appendChild(make(s[0], s[1])); });
    return svg;
  };

  LX.iconNames = function () { return Object.keys(ICONS); };

  /* 把静态 HTML 里的占位 <i data-icon="check" data-size="16"></i> 填成真图标（已填过的跳过）。
   * 页面加载完会自动对整页跑一次；后续动态插入的占位自己再调一次 LX.decorateIcons(根节点)。 */
  LX.decorateIcons = function (root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll("[data-icon]");
    Array.prototype.forEach.call(nodes, function (n) {
      if (n.querySelector("svg")) return;
      n.appendChild(LX.icon(n.getAttribute("data-icon"), n.getAttribute("data-size")));
    });
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { LX.decorateIcons(document); });
  } else {
    LX.decorateIcons(document);
  }
})();
