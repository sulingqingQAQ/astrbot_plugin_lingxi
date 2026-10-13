/* UI-4b 骨架：先注册两个标签，功能逐块加。原生 JS，无 CDN。 */
(function () {
  "use strict";
  if (!window.LX) return;
  LX.registerTab({
    id: "overrides", title: "分会话覆盖", order: 30,
    mount: function (root) {
      var p = document.createElement("p");
      p.className = "muted";
      p.textContent = "分会话覆盖（UI-4b 施工中）";
      root.appendChild(p);
    },
    isDirty: function () { return LX.state.overridesDirty; }
  });
  LX.registerTab({
    id: "about", title: "关于", order: 90,
    mount: function (root) {
      var p = document.createElement("p");
      p.className = "muted";
      p.textContent = "关于（UI-4b 施工中）";
      root.appendChild(p);
    }
  });
})();
