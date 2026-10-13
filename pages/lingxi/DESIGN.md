# 灵犀面板设计系统（DIAG-4a）

写新标签只用这里的令牌和类名，不写色值、不另起一套。文件：`app.css`（令牌 + 通用组件）、`icons.js`（图标）；`form.css` / `tab_overrides.css` 是已有标签自己的版式。
气质：安静、干净、冷静的"幽灵房间"——大留白、柔和层次、克制的靛蓝强调色。下面每个 html 代码块都会被 `tools/ui/shots_visual.py` 原样渲染成"组件陈列"截图。

## 令牌
`app.css` 的 `:root`（浅色），深色在 `:root[data-theme="dark"]` 与 `@media (prefers-color-scheme: dark)` 里同名同步（静态测试锁着三处一致）。
- 底与面：`--bg` 页面 / `--surface` 卡片·顶栏 / `--surface-2` 抬高·嵌套 / `--sunken` 凹陷（轨道、代码块）/ `--border` / `--border-strong`
- 文字：`--text` / `--text-2` 次要 / `--text-3` 说明·时间（三者压在 bg·surface 上都 ≥ 4.5:1）
- 强调：`--accent` 实心底 / `--accent-hover` / `--accent-soft` 软底 / `--accent-text` 强调色文字 / `--on-accent` 压在 accent 上的字 / `--accent-2`（只做渐变）/ `--focus`
- 状态：`--ok --warn --danger --info --neutral`（文字·图标色）各配 `-soft`（软底色，前者压后者 ≥ 4.5:1）；`--warn-line` 只做装饰线
- 其余：`--overlay` `--surface-glass` `--shadow-1` `--shadow-2` `--knob`（开关旋钮）
- 间距 `--sp-1…6` = 4 8 12 16 24 32；字阶 `--fs-12 13 14 16 20 28`；圆角 `--r-sm md lg pill` = 6 10 14 999；过渡 `--t-fast base slow` = 120 160 180ms + `--ease`；字体 `--font` `--font-mono`；控件高 `--h-control` `--h-btn` `--h-btn-sm`（手机自动变 42/42/40）
- 小工具类：`.num` 等宽数字 · `.hint` 说明小字 · `.muted` 次要色 · `.faint` 更淡 · `.mono` 等宽 · `.row` 横排换行 · `.stack` 竖排 · `.grow` 撑满 · `.hidden` 隐藏 · `.sr-only` 只给读屏

## 图标
`LX.icon(name, size)` 返回 SVG 节点（`createElementNS` 构造，描边 `currentColor`，`aria-hidden`，size 缺省 16）：`settings group chat clock wave shield download refresh warning info check x search bolt file copy eye sliders list timeline pulse chevron-down chevron-right undo filter lock plus trash check-circle alert-circle ghost`。认不得的名字返回空 svg，不抛错。
HTML 里写占位 `<i data-icon="check" data-size="16"></i>`，页面加载后自动填成图标；动态插入的占位调 `LX.decorateIcons(根节点)`。JS 里直接 `node.appendChild(LX.icon("check", 16))`。图标颜色跟随文字色，要上色就给外层加 `color`（令牌）。

## 新标签怎么挂
```js
LX.registerTab({ id: "diag", title: "诊断", order: 40, icon: "pulse", mount: function (root) { /* 往 root 里建 DOM，只调一次 */ }, onShow: function () {} });
```
`icon` 缺省按标签 id 找默认图标，再没有用 `list`。`mount` 里一律用 `LX.el` / `textContent` 建节点，别拼 innerHTML。

### 卡片 · 分组标题
```html
<div class="card">
  <div class="card-head">
    <span class="card-icon"><i data-icon="pulse" data-size="18"></i></span>
    <div class="card-titles"><h3>卡片标题</h3><div class="card-sub">一句大白话说明</div></div>
    <div class="card-actions"><button class="small">操作</button></div>
  </div>
  内容……
</div>
<div class="group-title">分组标题 <span class="count">3</span></div>
```
`.card-flat` 去阴影；`.divider` 是卡片内的分隔线；`card-head` 可省，卡片里直接放 `<h3>` 也行。

### 状态徽章 · 药丸标签
```html
<span class="badge badge-ok dot">正常</span> <span class="badge badge-warn">有提示</span>
<span class="badge badge-danger"><i data-icon="alert-circle" data-size="12"></i>出错</span>
<span class="badge badge-info">信息</span> <span class="badge badge-neutral">中性</span>
<span class="pill">空闲</span> <span class="pill pill-accent">私聊</span> <span class="pill pill-mono">#a1b2c3</span>
```
徽章表状态（软底色，`dot` 前置小圆点，无修饰类就是中性）；药丸是中性标签（细边，`pill-accent` 强调、`pill-mono` 等宽，适合会话号·来源）。

### 指标磁贴
```html
<div class="metric-grid">
  <div class="metric metric-ok">
    <div class="metric-label"><i data-icon="check" data-size="14"></i>送达</div>
    <div class="metric-value">128</div>
    <div class="metric-note">近 1 小时 12 次</div>
  </div>
  <div class="metric"><div class="metric-label">运行时长</div><div class="metric-value">3 小时</div></div>
</div>
```
`metric-ok|warn|danger|info` 只给标签图标上色；`.metric-grid` 自适应列宽，手机两列。

### 分段控件
```html
<div class="segmented" role="group" aria-label="筛选">
  <button type="button" class="active" aria-pressed="true">全部</button>
  <button type="button" aria-pressed="false">送达</button>
  <button type="button" aria-pressed="false">出错</button>
</div>
```
选中态 `.active` 与 `aria-pressed="true"` 一起改。顶栏的标签条是同一个长相。

### 开关
```html
<label class="toggle"><input type="checkbox" role="switch"><span class="toggle-track"></span><span class="toggle-text">包含回复正文</span></label>
```
真 checkbox 在里面，读 `.checked`、监听 `change`；不要用裸 checkbox。小字说明写在旁边的 `.hint`。

### 按钮
```html
<button class="primary">主按钮</button> <button>次按钮</button> <button class="ghost">幽灵</button>
<button class="danger">危险</button> <button class="armed">实心危险</button> <button class="primary loading" disabled>加载中</button>
<button class="small"><i data-icon="refresh" data-size="14"></i>小按钮</button> <button class="icon-only" aria-label="复制"><i data-icon="copy"></i></button> <button disabled>禁用</button>
```
裸 `<button>` 就是次按钮；`loading` 要和 `disabled` 一起加（旋转圈，文字不变）；`armed` 用于"再点一次确认"。手机宽度按钮高度自动 ≥ 40px。

### 输入控件
```html
<div class="row"><div class="search-box"><i data-icon="search" data-size="16"></i><input type="text" placeholder="搜索"></div>
<select><option>选项一</option><option>选项二</option></select> <input type="number" value="30" style="width:100px"></div>
<textarea rows="3" placeholder="多行文字"></textarea>
```
裸 `input / select / textarea` 就有样式（高度 `--h-control`、焦点环）；`input[type=range]` 的进度色靠 JS 设 `--fill: 40%`（见 `form.js` 的 `paintSlider`）。

### 表格
```html
<div class="table-wrap"><table class="table">
  <thead><tr><th>时间</th><th>来源</th><th class="num">字数</th></tr></thead>
  <tbody>
    <tr class="row-group"><th colspan="3">分组行</th></tr>
    <tr><td>23:15:02</td><td><span class="pill">空闲</span></td><td class="num">42</td></tr>
  </tbody>
</table></div>
```
必须套 `.table-wrap`（手机横向滚动，不撑破页面）；数字列加 `.num`；长串自带换行。

### 时间线条目
```html
<ol class="timeline">
  <li class="tl-item tl-ok"><span class="tl-dot"></span>
    <div class="tl-head"><span class="tl-time">23:15:02</span><span class="pill">空闲</span><span class="badge badge-ok">送达</span></div>
    <div class="tl-body">原因 · 字数 · 会话号……</div></li>
  <li class="tl-item tl-muted"><span class="tl-dot"></span><div class="tl-head"><span class="tl-time">22:40:11</span><span class="pill">上次运行</span></div></li>
</ol>
```
`tl-ok|warn|danger|info` 给圆点上色；`tl-muted` 灰显（上次运行的条目）。

### 空状态 · 骨架屏
```html
<div class="empty"><div class="empty-icon"><i data-icon="ghost" data-size="22"></i></div>
  <div class="empty-title">还没有数据</div><div class="empty-text">一句大白话说明怎样才会有。</div>
  <div class="actions"><button class="small primary">刷新</button></div></div>
<div class="card"><span class="skeleton skel-title"></span><span class="skeleton skel-line"></span><span class="skeleton skel-line skel-short"></span><span class="skeleton skel-block"></span></div>
```
加载中摆骨架屏，加载完替换；出错用 `.alert-danger`（别用空状态）。

### 提示条
```html
<div class="alert alert-warn"><span class="alert-icon"><i data-icon="warning" data-size="18"></i></span>
  <div class="alert-body"><div class="alert-title">标题（可省）</div><div class="alert-text">说明文字</div></div>
  <div class="alert-actions"><button class="small">操作</button></div></div>
<div class="alert alert-info"><span class="alert-icon"><i data-icon="info" data-size="18"></i></span><div class="alert-body">默认就是信息色；还有 alert-ok / alert-danger。</div></div>
```
全局提示条 `#banner`、`#conflict-banner`、`#fatal`、`#toast`、改动清单 `#diff-panel` 由壳（`app.js`）管，别动；`LX.toast(文字, "ok"|"warn"|"error")` 弹 toast。

## 深浅色与手机
- 颜色全走令牌，不写 `#hex` / `rgb()`（静态测试会扫 `form.css`、`tab_overrides.css`，新标签的 css 请同样遵守）；深浅色靠令牌自动切换，不要写 `[data-theme]` 分支。
- 状态色做文字时压在自己的软底（如 `--ok` 压 `--ok-soft`）或 `--surface` 上（已有场景断言 ≥ 4.5:1）；`--text-3` 只做说明小字；`--warn-line` 只做装饰线。
- 手机（≤ 700px）：保存栏由壳固定在底部，页面里别再放 fixed 底栏（`main` 已留 104px）；别给 `.topbar` 加 backdrop-filter / transform（会让里面的 fixed 保存栏失效）；表格套 `.table-wrap`；会话号、路径这类长串加 `overflow-wrap: anywhere`；触控目标 ≥ 40px 靠 `--h-*` 令牌，自己画可点元素请用 `min-height: var(--h-btn)`。
- 动效只用 `--t-fast/base/slow` + `--ease`（120–180ms）；`prefers-reduced-motion` 已在 `app.css` 末尾统一关掉动画，别写 `!important` 的时长。
- 焦点环：自绘可点元素用 `:focus-visible` + `outline: 2px solid var(--focus)`；按下反馈 `transform: translateY(1px)`。
- 钩子：壳的 id / 类（`#btn-save` `#tabs button[data-tab]` `.diff-item` 等，见 UI-0 §4）不许改名；新标签自己的测试钩子用 `id="diag-…"`。
