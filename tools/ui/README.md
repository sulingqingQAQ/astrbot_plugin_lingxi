# tools/ui：lingxi 配置面板本地测试夹具（UI-5）

用假桥接 SDK + 无头 Edge，在真页面（`pages/lingxi/`）上把整条链路跑通。
数据全合成，不碰线上与测试端 AstrBot。

## 文件

| 文件 | 说明 |
|---|---|
| `harness.py` | HTTP 服务：静态页面 + 假桥接 SDK + API 分发 + state/reset |
| `drive.py` | CDP 驱动无头 Edge（只用 aiohttp，不依赖 playwright/selenium） |
| `drive_frames.py` | 在 `drive.py` 上加 iframe 执行上下文辅助（真鼠标点击、按上下文收异常，UI-6b） |
| `stub_service.py` | 罐头服务：UI-0 §2 形状的合成数据，save 真改内存并更新 revision |
| `diag_seed.py` | 带历史的真诊断记录器（全合成）+ 固定的触发源状态 `fake_state()`，给诊断标签用 |
| `selftest.py` | 夹具自测（起真 Edge），成功打印 `SELFTEST OK` |
| `selftest_page/` | 自测用的几十行小页面（不是真页面） |
| `scenarios_shell.py` | 壳（顶栏 / 标签 / 保存区 / 主题 / 手机宽度）的端到端场景，成功打印 `SHELL SCENARIOS OK` |
| `scenarios_form.py` | 表单（各配置项控件、保存、校验）的端到端场景，成功打印 `FORM SCENARIOS OK` |
| `scenarios_overrides.py` | 分会话覆盖 / 试算 / 关于的端到端场景，成功打印 `OVERRIDES SCENARIOS OK` |
| `scenarios_diag.py` | 诊断标签的端到端场景（DIAG-3 新增，用法见该文件头注释） |
| `shots_visual.py` | 视觉截图（不做断言）：每个标签 × 浅 / 深 × 桌面 / 手机，外加组件陈列页；`--out` `--round` `--only` |

## 诊断服务怎么挂在夹具上

`Harness` 在真 `ConfigService` 路径上自动挂一个真 `DiagService`：
记录器是 `diag_seed.seed()` 灌好历史的 `DiagRecorder`（近 80 分钟的送达 / 沉默 / 拦下 / 出错，
几行日志，外加"上次运行"读回的几条），触发源状态用 `fake_state()`。
`harness.diag_rec` / `harness.diag_expect` 给场景脚本直接用（拿 `expect` 里的数量对页面数字，别写死）。
罐头 `StubService` 路径不挂诊断服务，`diag/*` 接口会报"没有这个接口"。单测：`tests/test_ui_diag_seed.py`。

## 本地看真页面

```bat
F:\Gemini\AstrBot\AstrBot\backend\python\python.exe -I -B -X utf8 tools/ui/harness.py --page pages/lingxi --port 8765
```

然后用 Edge 打开 `http://127.0.0.1:8765/`（`?theme=dark` 看深色）。

## 跑场景 / 自测

所有 `scenarios_*.py`、`selftest.py`、`shots_visual.py` 都会起一个无头 Edge，**别同时跑两个**；
`--out` 指定截图目录，失败退出码 1。

```bat
F:\Gemini\AstrBot\AstrBot\backend\python\python.exe -I -B -X utf8 tools/ui/selftest.py
F:\Gemini\AstrBot\AstrBot\backend\python\python.exe -I -B -X utf8 tools/ui/scenarios_shell.py
```

`selftest.py` 断言：`#out` 有块数 → 点保存后 `/__harness/state` 变了 →
故意异常被 `js_errors()` 抓到 → `?theme=dark` 生效 →
窄屏 390×844 截图存输出目录。

单测（不起 Edge）：

```bat
F:\Gemini\AstrBot\AstrBot\backend\python\python.exe -I -m unittest discover -s tests -t . -p test_ui_tools.py -v
```

## 铁律

- 无头截图不用 `scrollIntoView`/`scrollTo`、不用 `--virtual-time-budget`。
- `Browser.close()` 只杀自己启动的 Edge 进程（记 PID），不批量杀进程。
- harness 只监听 127.0.0.1，非回环地址一律 403。
