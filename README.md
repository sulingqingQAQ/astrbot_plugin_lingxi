# astrbot_plugin_lingxi（灵犀）

让 Bot 像活人一样社交的 AstrBot 插件。一句话概括：**它让 Bot 在没人理它的时候也会自己开口、
在被搭话时像真人一样分几条把话说完、在群聊里像群友一样见缝插针、还能自己"看懂"图片和网页。**
**有问题别找我，让ai去修，我也是用ai拉出来的，在这里感谢workbuddy**

> 本插件基于 [Pancakes-Labs/astrbot_plugin_proactive_chat](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat)（AGPL-3.0）深度改造而来，
> 并吸收了 [astrbot_plugin_astrbot_enhance_mode](https://github.com/Axi404/astrbot_plugin_astrbot_enhance_mode)
> 与 [chatluna-llm-web-search](https://github.com/CookSleep/chatluna-llm-web-search) 的部分设计思路。
> 遵循同样的 AGPL-3.0 协议开源。

---

## 功能总览

### 💬 私聊主动消息

Bot 主动给好友发消息，模拟"想起来找你聊聊"的行为：

- **随机间隔触发**：在 `min_interval_minutes` ~ `max_interval_minutes` 区间随机取间隔，
  到点后由 `core\task_scheduler.py` 调度、`core\chat_flow.py` 执行完整的主动消息流程
- **免打扰时段**（`quiet_hours`）与**未回复上限**（`max_unanswered_times`，连续不理 Bot 就暂时不打扰）
- **会话级差异化配置**：`core\session_override_manager.py` 支持对单个 `unified_msg_origin`
  覆盖全局配置（不同的间隔、提示词、开关），由 `core\session_config.py` 与 `core\session_parser.py`
  负责解析与校验
- **记忆召回**：生成主动消息前，可调用 `astrbot_plugin_livingmemory` 等记忆插件检索长期记忆
  （`memory_recall_settings`，支持"最近历史做检索词"或"固定文本做检索词"两种模式），
  让 Bot 主动提起上次聊过的话题
- **上下文注入**：可把平台聊天流水注入提示词（`context_settings`），并控制是否包含 Bot 自己说过的话
- **语音回复**：接 TTS 提供商把主动消息转成语音发出，可选择附带文字原文

### 🔁 私聊对话增强（追发）

AI 正常回复用户之后，按概率（`followup_settings.probability`）在随机延迟后**再追发一条消息**，
像真人一样"发完一条想起来再补一句"：

- 支持连发衰减（`decay_rate`）：越连续追发概率越低，避免刷屏
- 追发内容由 `prompt_templates` 里的模板随机选取驱动生成
- 延迟在 `min_delay_seconds` ~ `max_delay_seconds` 间随机，核心实现在 `core\followup.py`

### 🗣️ 群聊智能接话

让 Bot 像一个真实的群友一样"插话"，而不是永远被动等 @：

- **消息缓冲**：`core\group_chime.py` 把群消息按 `[时间] 昵称(id): 内容` 格式记入环形缓冲
  （`chime_append_transcript()`），白名单群才启用
- **五道闸门**（`chime_check_all_gates()`）：总开关 → 每小时配额 → 每日配额 →
  静音时段 → 积压量 + 判定冷却，全部通过才进入判定，成本控制到最低
- **便宜判定模型决定插不插话**（`chime_run_judge()`）：把聊天记录和机器人身份简介
  （`persona_brief`）交给一个便宜的小模型，输出 JSON（接不接话、置信度、插话角度）；
  判定失败可走 `fallback_probability` 随机兜底
- **喊话必应**：消息中出现唤醒词（`wake_keywords`，默认「小苏」）时无视闸门直接接话，
  前缀、句中、句尾都触发
- **群会话自动创建**（`_chime_get_group_conversation()`）：没会话的群自动 `/new`，零人工干预
- **`<refuse/>` 主动放弃**：生成模型如果觉得自己此刻不该说话，输出 `<refuse/>` 即被拦截不发送
- **回复拆分连发**：接话回复按空行拆成多条消息、带拟人延迟逐条发出；空行拆不开时
  兜底按单个换行拆（`chime_decorating_result()` + `_chime_send_followup_segment()`），
  弥补智能分段插件 `min_length` 门槛导致短回复不分段的缺口
- **输出风格约束**：生成接话时附带 `_CHIME_STYLE_HINT` 系统提示，要求口语化短句、
  空行分段、禁 Markdown，让输出天然可拆分

### 🚀 群聊增强

移植自 `astrbot_plugin_astrbot_enhance_mode` 并扩展，核心在 `core\group_enhance.py`：

- **群聊历史增强**（`group_history`）：群消息以 `[昵称/ID/时间](角色) #msgID:` 格式
  持续记录并注入 system_prompt，Bot 说话时"看得见"群里最近发生了什么
- **图片转述**（`image_caption`）：群消息中的图片后台调用视觉模型转述，
  历史记录里的 `[Image]` 变成 `[Image: 描述]`，Bot 能"看懂"别人发的图
- **语音转文字**（`voice_stt`）：群语音经 AstrBot 的 speech_to_text 提供商转写，
  历史记录里的 `[Voice]` 变成 `[Voice: 内容]`
- **Mention/Quote 标签**（`enhance_tag_utils.py`）：模型输出 `<mention id/>` /
  `<quote id/>` 时自动转成 At / Reply 消息组件，实现精准回复与 @ 某人
- **封禁控制**（`enhance_ban.py`）：给 LLM 提供封禁/解封/查询名单的工具，
  被封禁用户的消息直接拦截，管理员豁免可配、最长封禁时长可配

### 🌐 grok 搜索工具（明示才调用）

一套通过 LLM 工具暴露给主模型的搜索能力，全部**只在用户明确提到搜索/读帖/甩链接时才调用**，
不会污染日常对话。核心在 `core\enhance_web_search.py` 与 `core\web_read.py`：

| 工具 | 功能 | 计费 |
|---|---|---|
| `enhance_web_search` | xAI grok 的 `web_search` 全网搜索，可附来源链接 | 按次 |
| `enhance_x_search` | 搜 X/Twitter 帖子：账号过滤（`allowed_x_handles`）、日期范围（`days_back`）、**图片/视频理解**（grok 服务端分析媒体内容，插件侧再把"描述帖内媒体"追加进系统提示词） | 按次，图片/视频理解另计 |
| `enhance_x_read` | 读取指定 x.com 帖子/线程全文（含帖内媒体理解） | 按次 |
| `enhance_web_read` | 读任意网页正文，走 Jina Reader，免费 | 免费 |

另有独立的**图片描述服务**（`image_describe`）：搜索/读帖结果里出现的图片 URL，
交给指定视觉模型逐张转述，单次最多处理 `max_images` 张。

### 🔨 LLM 工具完整名单（main.py 里注册的全部 7 个）

搜索工具之外，封禁控制也以 LLM 工具形式暴露给模型：

| 工具名 | 功能 |
|---|---|
| `enhance_web_search` | grok 全网搜索 |
| `enhance_x_search` | X/Twitter 帖子搜索 |
| `enhance_x_read` | 读取 x.com 帖子/线程 |
| `enhance_web_read` | 读取任意网页正文 |
| `enhance_ban_user` | 封禁用户（LLM 可调用，管理员豁免与最长时长可配） |
| `enhance_unban_user` | 解封用户 |
| `enhance_get_ban_list_status` | 查询当前封禁名单 |

### 🔔 通知系统

`core\notification_center.py`：轮询式通知中心，可挂定时任务与提醒，
配合 `core\data_storage.py` 做会话持久化与数据清理。

### 📊 遥测与生命周期

- `core\telemetry_manager.py`：主动消息行为的统计遥测（触发次数、回复率等），仅本地记录
- `core\plugin_lifecycle.py`：插件装载/卸载、任务清理与端口防泄漏
- `core\message_events.py` + `core\message_sender.py`：消息事件监听与发送装饰钩子的统一入口
  （追发、`<refuse/>` 拦截、空行拆分、分段补发都从这里分发）

---

## 代码结构

```
astrbot_plugin_lingxi/
├── main.py                     # 钩子入口：只做转发，逻辑全在 core/
├── core/
│   ├── chat_flow.py            # 主动消息核心执行流（生成 → 分段 → TTS → 发送）
│   ├── task_scheduler.py       # 随机间隔调度器与计时器
│   ├── llm_adapter.py          # 上下文获取与 LLM 调用封装
│   ├── message_sender.py       # 发送与装饰钩子（on_decorating_result 分发）
│   ├── message_events.py       # 消息事件监听
│   ├── group_chime.py          # 群聊接话全部逻辑（缓冲/闸门/判定/拆分）
│   ├── group_enhance.py        # 群聊增强（历史注入/图片转述/语音转写/标签）
│   ├── enhance_web_search.py   # grok web_search / x_search / x_read 统一封装
│   ├── web_read.py             # Jina Reader 请求构造
│   ├── enhance_ban.py          # LLM 封禁工具与拦截
│   ├── enhance_tag_utils.py    # Mention/Quote 标签解析
│   ├── followup.py             # 私聊追发
│   ├── notification_center.py  # 通知中心
│   ├── session_override_manager.py / session_config.py / session_parser.py
│   │                           # 会话级差异配置三件套
│   ├── data_storage.py         # 会话持久化
│   ├── telemetry_manager.py    # 本地遥测
│   └── plugin_lifecycle.py     # 生命周期
├── utils/                      # 时间工具与版本号
├── _conf_schema.json           # 全部配置项的面板 schema
└── metadata.yaml / requirements.txt / LICENSE / README.md
```

---

## 用到了什么东西（依赖与配套服务）

### 模型（全部走 AstrBot 的提供商体系，OpenAI 兼容接口均可）

| 用途 | 说明 | 默认建议 |
|---|---|---|
| 主对话模型 | 群聊接话、私聊主动消息的正常生成 | 任意文本模型 |
| 接话判定模型 | `group_chime_settings.judge_provider_id`，判断该不该插话的便宜小模型 | deepseek 系小模型 |
| 分段模型 | `segmented_reply_settings.smart_split`，决定断点；失败自动回退规则分段 | 云端小模型（本地 ollama 亦可，需把 `timeout_seconds` 调大到 15 以上） |
| 图片转述/描述模型 | `image_caption_provider_id` 与 `image_describe.provider_id` | 任意视觉模型 |
| X 搜索/读帖/图片视频理解 | `web_search.provider_id` 等 | xAI 的 grok 系提供商（**必须 xAI Key**，xAI 服务端能力） |

### 外部服务

- **xAI API**：`enhance_web_search` / `enhance_x_search` / `enhance_x_read` 及其图片、视频理解，均按次计费
- **Jina Reader**：`enhance_web_read` 抓网页正文，免费（`jina_api_key` 可选）
- **语音转文字**：需在 AstrBot 配置 speech_to_text 提供商（如 sensevoice / whisper）
- **TTS**：主动消息语音回复，需配置 text_to_speech 提供商

### 与其他插件的协同（可选，装了自动生效）

- `astrbot_plugin_livingmemory`：主动消息生成前的长期记忆召回（`memory_recall_settings.enable`）
- `astrbot_plugin_memory_companion`：结构化记忆注入
- `astrbot_plugin_smart_segmentation_enhanced`：被动回复的智能分段；
  本插件的接话拆分与其互补——它管长回复，接话拆分管短回复与多段连发

---

## 安装

```bash
cd AstrBot/data/plugins/
git clone <本仓库> astrbot_plugin_lingxi
```

或直接下载 zip 解压到 `data/plugins/`，然后在 AstrBot 面板重载插件。

## 配置

所有配置都可在 AstrBot 面板 → 插件 → 灵犀 → 配置里改，全部配置项定义在 `_conf_schema.json`：

1. **私聊/群聊全局配置**：把目标会话的 `unified_msg_origin` 填进 `session_list`，
   按需开自动触发、免打扰、TTS、分段、记忆召回
2. **群聊接话**：把群号加进 `group_whitelist`，配置判定模型与配额，
   唤醒词默认「小苏」（消息里喊一声即触发）
3. **群聊增强**：开 `group_history.enable` 后 Bot 会自动维护群历史；开 `image_caption` 需指定视觉模型
4. **搜索工具**：`web_search` / `x_search` / `x_read` 需要先在 AstrBot 里新建一个 xAI 提供商并填 `provider_id`

## 快速上手

1. 面板里按需打开各功能开关（互不依赖，可单开）
2. 群里喊唤醒词试试「喊话必应」
3. 对话里明确说"搜一下 XX"或甩一个链接，触发对应的搜索工具
4. 私聊冷场等几分钟，看 Bot 是否主动找你

---

## 设计思路

这套插件的核心问题只有一个：**怎么让一个回合制问答机器人在群聊里表现得像个活人**。
拆开来是四个设计决定：

### 1. 成本漏斗：便宜模型管决策，贵模型管表达

群里每条消息如果都丢给主模型，既贵又会频繁插话招人烦。所以接话链路做成
**五道零成本闸门 → 便宜判定模型 → 贵的生成模型** 三级漏斗：前两级把 99% 的消息挡在门外
（白名单、配额、静音、积压不足、判定说"不"），只有真正值得说话的时刻才动用主对话模型。
判定模型输出结构化 JSON（接话与否 + 置信度 + 插话角度），失败时 fail-safe 不接，
宁可错过也不胡说。

### 2. 走完整 pipeline，而不是自己偷偷调 LLM

判定命中后，接话不是插件里直接 `chat()` 一下了事，而是 `yield event.request_llm()`
**把请求交还给 AstrBot 主 pipeline**。这意味着人格、记忆（`astrbot_plugin_livingmemory` 的
召回与反思）、其他插件的处理钩子全部自动生效——接话和正常被 @ 的回复走的是同一条路，
人格一致、记忆一致，插件自己不需要重造任何轮子。

### 3. 拟人化靠"结构"而不是靠"prompt 祈祷"

真人打字是**多条短消息**、有间隔、偶尔补一句，而不是一大段排版工整的散文。本插件把这件事
做成了机制而不是祈祷：生成提示词要求空行分段 → 发送前按空行拆开 → 每段带按字数缩放的
随机延迟逐条发出；智能分段插件只认句末标点、短回复直接不分段，所以接话路径自带
"单换行兜底拆分"补上这个缝隙。私聊侧同理：回复后的概率追发 + 连发衰减，
把"想起什么再补一句"做成了概率机制。

### 4. 能力全部"明示才调用"，边界感优先

搜索工具（grok 全网搜索、X 搜索/读帖）都是按次计费的真金白银，而且一旦模型"顺手"调用，
一次闲聊可能产生几十次搜索计费。所以这套工具的设计原则是**只在用户明确表达意图时使用**——
说"搜一下"才搜、甩链接才读帖；普通聊天里模型根本看不到要用它们的理由。
媒体理解（图片/视频）同理做成独立开关：因为它们是 xAI 服务端按次加收的能力，
开不开由用户权衡费用，插件不替你做决定。

一句话收束：**判定用便宜的，表达走完整的，拆分用机制的，计费用开关的。**

---

## 与原插件（astrbot_plugin_proactive_chat）的主要差异

- 移除独立 WebUI 管理端（4100 端口）与遥测上报
- 新增：私聊追发、群聊智能接话、群聊历史增强、图片转述、语音转文字、
  Mention/Quote 标签、封禁控制、grok 搜索 / X 搜索 / X 读帖 / 网页读取
- 修复：终止流程端口泄漏、分段超时等若干问题

## License

AGPL-3.0（继承自上游项目 astrbot_plugin_proactive_chat）
