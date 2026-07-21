# 功能特性与设计说明

> 本文档记录本项目相对基础群聊分析能力的功能演进，供维护者查阅"为什么这么设计"。

## 一句话概括

**每天到点看一眼 QQ 有没有有价值的信息**：群少单群完整日报，群多按用户分类（科技/AI/自定义下挂群）聚合摘要；私聊管理员。  
主动推送（实时监控）是次要模块。保留图片/文本/HTML、模板与 prompt 自定义。

## 产品心智模型

```
定时日报（核心）
├── per_group   → 每群完整日报
└── by_category → 用户分类聚合（科技=[群…], AI=[群…]）
                  ├── split  每分类一条
                  └── merged 一条分块

主动推送 / 实时监控（次要，默认关）
└── 关键词 / 整窗；跨群时按「内容频道」打包（密钥/资源…）
    （内容频道 ≠ 用户分类）
```

## 配置面板顺序（当前）

```
① admin_notify     推送给谁
② auto_analysis    定时日报核心（时间 / delivery_mode / categories）
③ analysis_features / prompts
④ basic / llm / performance / …
⑤ message_monitor  主动推送（最后）
```

## 定制点总览

| # | 定制点 | 影响文件 | 改动规模 |
|---|--------|----------|----------|
| 1 | 管理员私聊推送（定时报告不发群） | `dispatcher.py`, `onebot_adapter.py`, `config_manager.py`, `analysis_application_service.py` | +222 / -27 |
| 2 | 手动命令 `/群分析` 也改私聊 | `main.py` | +15 / -0 |
| 3 | 信息差主题（替换原"逆天言论"） | `_conf_schema.json` 的 `prompts` | 见下 |
| 4 | 配置面板以定时为核心（实时垫底） | `_conf_schema.json` | 结构调整 |
| 5 | **物理移除娱乐功能**（称号/MBTI/锐评） | 全层级（domain/infrastructure/application/templates/config） | 功能收敛 + 死代码清除 |
| 6 | 元数据 + logo | `metadata.yaml`, `logo.png` | 版本/作者/占位图 |
| 7 | 单群情报场景默认值 | `_conf_schema.json`, `config_manager.py` | 默认更贴近「私聊 + 单群」 |
| 8 | 私聊推送可靠性（图片失败回退文本） | `dispatcher.py` | 及时送达 |
| 9 | **实时消息监控（盯人预警，次要）** | `message_monitor_service.py`(新), `main.py`, `config_manager.py`, `_conf_schema.json` | +~300 行 |
| 10 | **关键词即时推送模式** | `message_monitor_service.py`, `config_manager.py`, `_conf_schema.json` | +~150 行 |
| 11 | **智能降噪层** | `noise_reducer.py`(新), `message_monitor_service.py`, `config_manager.py`, `_conf_schema.json` | +~300 行 |
| 12 | **跨群智能聚合（实时内容频道）** | `message_monitor_service.py`, `config_manager.py`, `_conf_schema.json` | +~200 行 |
| 13 | **定时用户分类聚合（by_category）** | `push_category.py`, `scheduled_category_digest_service.py`, `auto_scheduler.py`, `config_manager.py`, `_conf_schema.json` | 新增 |

---

## 详细说明

### 定制点 1：管理员私聊推送

**问题**：上游定时生成的报告默认发回**原群**。需求是发到管理员私聊，避免在群里刷屏，也便于管理员集中查看。

**改动链路**（4 个文件，定时路径全覆盖）：

#### `src/infrastructure/reporting/dispatcher.py`
- `dispatch()` 入口加开关判断：`is_admin_notify_enabled()` 为真时走 `_dispatch_to_admins()` 后直接 `return`，跳过所有发群逻辑（`_dispatch_image` / `_dispatch_html` / `_dispatch_text`）。
- 新增 `_get_admin_qqs()`：合并两个来源的管理员 QQ —— ① AstrBot 全局配置 `admins_id`（超管）② 插件配置 `extra_admin_qq`（额外）。过滤掉非数字项和默认占位 `"astrbot"`，去重。
- 新增 `_dispatch_to_admins()`：复用上游的图片报告生成逻辑（不重写），图片优先、文本兜底，逐个私聊发送。

#### `src/infrastructure/platform/adapters/onebot_adapter.py`
- 新增 `send_private(user_id, text, image_path)`：调 OneBot 的 `send_private_msg`。复用 `_execute_transmission_strategy` 的图片转码逻辑（base64 优先 / 路径 / URL 兜底）。
- 只在 OneBot adapter 实现（QQ 平台）。其他平台 adapter 没有 `send_private`，`dispatcher` 会通过 `hasattr` 检查跳过。

#### `src/infrastructure/config/config_manager.py`
- 新增 `is_admin_notify_enabled()`：读 `admin_notify.enable_admin_notify`。
- 新增 `get_extra_admin_qqs()`：读 `admin_notify.extra_admin_qq`，归一化为字符串列表。

#### `src/application/services/analysis_application_service.py`
- 三处 `is_group_muted` 检查加开关判断：`admin_notify` 模式下**跳过群禁言检查**（因为报告走私聊，群是否禁言不影响发送）。对应定时、增量、增量最终三种触发路径。

### 定制点 2：手动命令也改私聊

**问题**：定制点 1 只覆盖定时路径。群里手动发 `/群分析` 仍会发回群（走 `main.py` 的 `_send_analysis_report`）。

**改动**（`main.py` 第 548-565 行）：手动命令处理函数里，在调用 `_send_analysis_report` 之前加开关判断 —— 开关开时改调 `dispatcher.dispatch()`（自动私聊），群里只回一句 `✅ 分析完成，报告已私聊发送给管理员`。

### 定制点 3：信息差主题

**问题**：上游金句分析抓「逆天神人发言」「发情/性压抑话题」等娱乐向内容。需求是抓**信息差/搞钱商机/资源干货/行业情报/认知差**。

**改动**（`_conf_schema.json` 的 `prompts` 分组）：
- **金句分析 prompt** → 改为「信息差/商机/干货提取」，价值标准按优先级：搞钱商机 > 资源干货 > 行业情报 > 认知差。明确约束「宁缺毋滥，没有有价值信息就返回空数组」。
- **话题分析 prompt** → 改为「有讨论价值的话题」，聚焦有信息量、有深度、有结论的讨论，忽略纯闲聊灌水。
- description 字段同步标注「信息差/商机/干货提取提示词（定制版，原金句分析）」。

### 定制点 4：配置面板以定时为核心

**改动**（`_conf_schema.json` 顶层 object 顺序）：
```
① admin_notify        ← 推送给谁
② auto_analysis       ← 定时日报核心（delivery_mode / categories）
③ analysis_features / prompts
④ basic / llm / …
⑤ message_monitor     ← 主动推送（最后，次要）
```

### 定制点 5：物理移除娱乐功能，只留有效信息挖掘

**目标**：保留「输出方式 + 自定义程度」，彻底移除娱乐向分析的代码，而非仅硬关闭开关。

**保留**：
- 输出：`image` / `text` / `html`
- 模板：`report_template`（默认改为 `simple`）
- 自定义：话题 prompt、信息差 prompt、LLM provider、定时时间、群白名单、管理员推送

**已物理移除**（v4.11.0-xms 起不再存在代码）：
- 用户称号 + MBTI / SBTI / ACGTI 画像：整条链路删除（domain/value_objects、domain/services、infrastructure/analyzers、templates、profile_assets、config getter/setter/prompt）
- 聊天质量锐评：整条链路删除（domain/data_models QualityReview/QualityDimension、infrastructure/analyzers、config getter/setter/prompt）
- `DEFAULT_PROFILE_MAPPING`、`_load_profile_asset_manifest`、`_resolve_profile_info` 等 profile 映射代码
- HTML 模板中 `user_title_item.html` / `chat_quality_item.html`（8 主题 × 2 = 16 个文件已删）
- HTML 模板中 `{% if titles_html %}` / `{% if chat_quality_html %}` 整块已删
- `assets/profile_assets/` 目录（manifest.json + 图片资源）已删

**实现方式**：
1. `_conf_schema.json`：从面板删除娱乐配置项（称号/锐评 prompt、profile_*、对应 provider）
2. `config_manager.py`：删除所有 user_title/chat_quality/profile 的 getter/setter/prompt 方法
3. `generators.py`：删除 `DEFAULT_PROFILE_MAPPING`、profile 解析方法、称号/锐评渲染分支；`titles_html`/`chat_quality_html` 渲染数据保留空字符串占位（模板兼容）
4. `analysis_application_service.py`：`analyze_all_concurrent` 返回 3 元组（topics, golden_quotes, total_usage），`analyze_incremental_concurrent` 同理；`analysis_result` dict 保留 `user_titles: []` / `chat_quality_review: None` 占位 key
5. `llm_analyzer.py`：删除 `analyze_user_titles` / `summarize_quality_reviews` 方法，不再实例化 `UserTitleAnalyzer` / `ChatQualityAnalyzer`
6. domain 层：删除 `UserTitle`、`QualityReview`、`QualityDimension` 类；`IAnalysisProvider` 签名改为 3 元组
7. 若干模板 `quote_item.html` / `topic_item.html` 标题从「群圣经 / 热门话题」改为情报向文案

### 定制点 6：品牌标识

**`metadata.yaml`**：
| 字段 | 当前值 |
|------|--------|
| `name` | `astrbot_plugin_qq_group_daily_analysis` |
| `display_name` | 世健世健你的好友 |
| `version` | v4.12.0-xms |
| `author` | SXP-Simon (xms 定制 by xiaoyuyu6420) |
| `repo` | https://github.com/xiaoyuyu6420/astrbot_plugin_qq_group_daily_analysis |

**说明**：沿用上游插件识别名，保证 AstrBot 本地目录/更新链路不因改名断掉；版本走 `v4.x.x-xms` 定制线。

### 定制点 7：单群情报场景默认值

**目标**：开箱更贴近「监控一个群 + 每天总结有价值信息 + 私聊管理员」，不靠大删代码做轻量化。

| 配置项 | 上游/旧默认 | 定制默认 | 说明 |
|--------|-------------|----------|------|
| `admin_notify.enable_admin_notify` | `false` | **`true`** | 默认走私聊，不发群 |
| `basic.group_list_mode` | `"none"` | **`"whitelist"`** | 默认只允许白名单群，避免误分析所有群 |
| `basic.min_messages_threshold` | `200` | **`50`** | 安静情报群也更容易出报告 |
| 信息差相关文案 | 「金句」 | 「信息差/干货」 | 只改 description/hint，**不改配置 key** |

代码 fallback 同步：`config_manager.is_admin_notify_enabled()` 缺 key 时默认 `True`。

**重要：已有配置不会自动覆盖。**  
AstrBot 对已安装插件会保留现有配置文件；schema 默认值只在**新装**或**缺失该 key** 时生效。若你之前已经装过，需要手动把 `enable_admin_notify` 设为 `true`、`group_list_mode` 设为 `whitelist`。

**仍然必须手动填写（不能写死默认）**：
- `basic.group_list`：要监控的群
- `auto_analysis.scheduled_group_list`：定时分析的群
- 管理员 QQ（`admins_id` 或 `extra_admin_qq`）

### 定制点 8：私聊推送可靠性（及时送达）

`dispatcher._dispatch_to_admins()`：
- 图片优先
- **图片发送失败时回退纯文本**（原先图片失败只打日志，可能丢推送）
- 发送异常时再尝试一次文本，尽量保证情报不丢

### 定制点 9：实时消息监控（盯人预警）

**问题**：定时日报是「每天 23:00 汇总」，但群里高价值信息（如有人丢了个 API key）出现即需知道，隔天看就晚了。需要实时盯住特定 QQ 的发言，命中有用信息立即推送。

**架构**（与定时日报链路完全独立，互不影响）：

```
监控群消息（所有人）→ 攒入该群缓冲区
                          │
          后台 flush 任务（每 flush_interval 分钟醒来）
                          │
          按 max_context_messages 截断（以目标QQ为中心保留上下文）
                          │
          该窗口内有目标 QQ 发言？否 → 跳过（省 LLM 调用）
                          │
          批量 LLM 总结：在完整对话上下文中提取目标 QQ 的价值信息
             有价值 → 推一条汇总
             无价值 → 丢弃
             LLM 不可用 → 降级推目标 QQ 原始发言（保证不漏）
```

**为什么要整窗（攒所有人的消息）**：群聊是多人的对话。目标 QQ 单独说"这个能用""我也想要"，脱离上下文就有语义歧义。把完整对话（标注每个人）给 LLM，它能看到：目标 QQ 在回答谁的问题？别人在讨论什么？这样才能准确判断价值。

**为什么按条数截断**：活跃群 X 分钟可能几百条消息全送 LLM 会 token 爆。截断策略：消息数超过 `max_context_messages`（默认 50）时，以目标 QQ 发言为中心，向前向后扩展窗口，保证上下文连续性。

**新增文件**：`src/application/services/message_monitor_service.py`（`MessageMonitorService`）

**修改文件**：
- `main.py`：新增 `monitor_qq_messages` 拦截器（`@filter.platform_adapter_type(AIOCQHTTP)`），克隆 Telegram 拦截器模式；`__init__` 实例化服务；`terminate` 调用 `stop()` 清理后台任务
- `config_manager.py`：新增 7 个 getter（`is_monitor_enabled` / `get_monitored_qqs` / `get_monitored_groups` / `get_monitor_extra_keywords` / `is_llm_confirm_enabled` / `get_flush_interval` / `get_alert_admin_qqs`）
- `_conf_schema.json`：新增 `message_monitor` 配置组（放在 `admin_notify` 之后）

**关键设计**：
| 决策 | 说明 |
|------|------|
| 整窗模式 | 攒群里所有人的消息（标注发送者），让 LLM 在完整对话上下文里判断目标 QQ 的价值。解决"单看一个人发言有歧义"问题 |
| 按条数截断 | 活跃群消息量大时，以目标 QQ 发言为中心保留 `max_context_messages` 条上下文（默认 50） |
| 窗口无目标发言 → 跳过 | flush 时先检查该窗口有没有目标 QQ 发言，没有就不调 LLM（省钱） |
| 批量汇总 | 攒 X 分钟（默认 10）一起总结，省 token + 推送安静 |
| 后台 flush | 惰性启动，首次有消息才起；插件卸载时 cancel |
| LLM 降级 | LLM 不可用/超时 → 直接推目标 QQ 原始发言（标注「降级模式」） |
| 推送目标 | 默认走管理员 QQ，可在 `alert_admin_qqs` 单独配预警接收人 |
| 不存库 | 推完即弃，不建表不依赖 |
| 群里无痕 | 不回复、不表态，只私聊推给你 |
| 独立链路 | 与定时日报、Telegram 拦截器完全独立 |

### 定制点 10：关键词即时推送模式

**问题**：整窗汇总模式是「攒 X 分钟再总结」，但有些信息要秒级响应——群里有人丢了个 API key，你希望立刻收到，不想等 10 分钟。

**新增模式**：`monitor_mode = keyword`

```
群消息（任何人）→ 群在监控列表？→ 关键词/正则命中？
                                        │
                                   否 → 丢弃
                                   是 → (LLM 确认? 可选) → 立即推送
```

**与整窗模式的区别**：

| | keyword 模式 | window 模式 |
|---|---|---|
| 触发方式 | 命中关键词立即推 | 攒 X 分钟批量总结 |
| 检测对象 | 任何人（不限 QQ） | 特定 QQ（需上下文消歧） |
| 速度 | 秒级 | 分钟级 |
| token 消耗 | 低（可选 LLM） | 中（每批一次 LLM） |
| 适合 | 盯全群关键词（API key/资源） | 盯特定人发言价值 |
| 用到哪些配置 | extra_keywords + 内置正则 | monitored_qqs + flush_interval + max_context_messages |

**内置正则规则**（keyword 模式自动检测）：
- API key：OpenAI (`sk-`)、Google (`AIza`)、GitHub (`ghp_`)、Slack (`xox`)、AWS (`AKIA`)、长 hex/base64 串
- 资源：网址、磁力链、网盘提取码
- 渠道：邀请码
- 外加你的 `extra_keywords`

**LLM 可选**：`use_llm_confirm` 开时，命中后 LLM 二次确认是否真有用（减少误推）；关时纯规则即时推（零成本）。LLM 不可用自动降级为命中即推。

**配置方式**：把 `monitor_mode` 改成 `keyword`，填 `monitored_groups` 和 `extra_keywords` 即可。`monitored_qqs` 在 keyword 模式下可不填（检测所有人）。

### 定制点 11：智能降噪层

**问题**：关键词即时推送太吵——同一条 API key 被转发到 3 个群你收到 3 遍；有人在群里刷链接你被轰炸；低价值命中也在推。

**新增文件**：`src/application/services/noise_reducer.py`（`NoiseReducer`）

**降噪四件套**：

| 降噪能力 | 配置项 | 默认值 | 说明 |
|----------|--------|--------|------|
| 优先级分级 | 内置规则 | — | API Key → critical（立即推）、资源链接/关键词 → normal（批量合并）、LLM 判定无用 → low（丢弃） |
| 推送冷却 | `cooldown_seconds` | 60 | 同一发送者@同一群的最小推送间隔。critical 豁免 |
| 内容去重 | `dedup_minutes` | 30 | 内容相似消息在 N 分钟内只推一次，防跨群重复轰炸 |
| keyword 批量合并 | `keyword_batch_seconds` | 60 | normal 优先级攒 N 秒合并成一条推送。0=立即推 |

**优先级分级规则**（无需用户手动配置）：
- 命中 `API Key` 类正则 → **critical** → 立即推，不受冷却/批量限制
- 命中 `资源链接`/`渠道`/`自定义关键词` → **normal** → 进入批量合并队列（`keyword_batch_seconds=0` 时直接推）
- LLM 确认后判定 `useful=false` 或 `category=其他` → **low** → 丢弃（等 window 简报兜底）
- LLM 不可用 + 正则命中 → 降级为该正则类别的默认优先级

**keyword 模式降噪流程**：
```
命中正则 → classify_priority()
  critical → 去重检查? → 立即推
  normal   → keyword_batch_seconds>0? 入队 : 直接推（走冷却+去重）
  low      → 丢弃
```

**window 模式**：降噪主要在跨群聚合时起作用——去重在缓冲阶段自然消除（同窗口不重复），冷却和批量合并不适用整窗模式。

**修改文件**：
- `message_monitor_service.py`：`_process_keyword` 接入降噪层（5 层过滤），`__init__` 实例化 `NoiseReducer`，`stop()` 清理
- `config_manager.py`：新增 `get_cooldown_seconds` / `get_dedup_minutes` / `get_keyword_batch_seconds`
- `_conf_schema.json`：`message_monitor` 组新增 3 项配置

### 定制点 12：跨群智能聚合（配置面板拆分：单群 / 多群）

**问题**：
1. window 模式按群×QQ 双重切分，3 个群 × 1 个目标 QQ = 3 条独立推送，同一事件被切碎。
2. 配置面板里「实时监控」与「定时日报」概念混在一组文案里，且「单群分析 vs 多群汇总」没有一等配置项，只有埋在降噪区附近的 `enable_cross_group` 布尔开关。

**面板结构（当前）**：
- `auto_analysis`：定时日报核心（靠前）
- `message_monitor`：主动推送 · 实时监控（次要，靠后）；内部「内容频道」≠ 定时「用户分类」
- 实时链路内部顺序：总开关 → **触发方式** `monitor_mode`（keyword/window）→ **分析范围** `window_scope`（per_group/cross_group）→ 内容频道订阅 → 监控群/目标QQ/推送人 → window/keyword 专用参数 → 降噪

**开启多群汇总**：
- 新配置：`message_monitor.window_scope = "cross_group"`（单群独立 = `"per_group"`，默认）
- 旧配置兼容：`enable_cross_group=true` 仍映射为 `cross_group`；若同时写了 `window_scope`，以新键为准

**关闭时（per_group）**：window 模式逐群独立推送（每群一份简报）。

**开启时（cross_group）**：flush 合并所有监控群的消息 → LLM 按话题聚类 → 输出一份统一简报。

**架构**：
```
_flush_all() 检测 window_scope / enable_cross_group
  cross_group → _flush_cross_group(batches)
           合并所有群消息（标注来源群）
           → 过滤：至少一个目标 QQ 发言
           → 截断（max_context_messages）
           → LLM 跨群聚类提取（_CROSS_GROUP_SYSTEM_PROMPT）
           → 输出统一简报
  per_group → 逐群 _flush_group()（原行为）
```

**LLM 聚类 prompt** 返回 JSON：
```json
{
  "has_value": true,
  "topics": [
    {
      "topic": "话题名",
      "groups": ["群号列表"],
      "items": [{"content": "原文", "source_qq": "QQ", "category": "apikey|资源|商机|情报|其他", "reason": "价值"}],
      "summary": "一句话概括"
    }
  ],
  "overall_summary": "跨群整体概述"
}
```

**简报推送格式**：
```
🧠 跨群情报简报（近 10 分钟）
━━━━━━━━━━━━━━━━━━━━━
📊 3 个群 · 127 条消息 · 筛出 4 个话题

📌 [话题 1] OpenAI Key 泄露  🔴
   涉及群：技术交流群(12345)、AI 破解圈(67890)
   💡 张三在技术群发了 sk-xxx，李四在 AI 群确认可用

📌 [话题 2] 某课程资源分享
   涉及群：学习交流群(11111)
   💡 含百度网盘链接+提取码

━━━━━━━━━━━━━━━━━━━━━
💡 跨群概述：3个群中检测到 API Key 泄露和资源分享
⏰ 2026-07-19 14:30:00
```

**LLM 降级**：跨群聚合依赖 LLM 聚类。LLM 不可用时自动降级为逐群独立推送（回退到 `_flush_group`），不会丢信息。

**与降噪层的联动**：
- `_flush_all` 开头调用 `noise_reducer.cleanup()` 清理过期冷却/指纹记录
- 跨群聚合天然解决整窗模式的跨群重复（同一段内容在多个群出现，LLM 聚类时合并为同一话题）
- API Key 类话题自动标记 🔴

**修改文件**：
- `message_monitor_service.py`：新增 `_flush_cross_group` / `_build_cross_group_dialog` / `_llm_cross_group_extract` / `_push_cross_group_brief` 方法，新增 `_CROSS_GROUP_SYSTEM_PROMPT` / `_CROSS_GROUP_USER_TEMPLATE` 常量，`_flush_all` 加分流逻辑
- `config_manager.py`：`get_window_scope()` + `is_cross_group_enabled()`（读 `window_scope`，兼容旧 `enable_cross_group`）
- `_conf_schema.json`：`message_monitor` 用 `window_scope` 一等选项表达单群/多群；组文案与 `auto_analysis` 明确拆成实时 vs 定时

### 定制点 13：定时用户分类聚合（by_category）

**问题**：群多时，到点对每个群出完整日报噪音大、看不过来。真实需求是「科技群一块看、AI 群一块看」，分类是**用户自定义的群桶**，不是消息内容类型。

**语义边界**：
| 概念 | 归属 | 例子 |
|------|------|------|
| 用户分类 | 定时日报 `auto_analysis` | 科技=[群A,群B]，AI=[群C] |
| 内容频道 | 实时监控 `message_monitor` | apikey / resource / deal / … |

**配置**（`auto_analysis`）：
- `delivery_mode`：`per_group`（默认）| `by_category`
- `category_push_mode`：`split`（每分类一条）| `merged`（一条分块）
- `categories`：JSON 数组，如 `[{"name":"科技","groups":["111","222"]},{"name":"AI","groups":["333"]}]`
- `scheduled_group_list*`：仅 `per_group` 使用

**运行时**：
```
_run_scheduled_report()
  by_category → ScheduledCategoryDigestService.run()
                每分类 × 每群：拉消息 → 清洗 → LLM 信息差/话题
                → 指纹去重 → pack(split|merged) → 私聊管理员
  per_group   → 原 _get_scheduled_targets 逐群完整日报
```

**约束**：
- by_category 不注册增量任务
- categories 空 → 不注册定时 / 触发时打 error 日志并跳过
- 分类内群默认放行（无需在 `basic` 白名单重复填写）；仅当 `basic` 设为黑名单且群在黑名单内时才跳过
- categories 与 basic 黑名单冲突时，启动/热更新会打 warning 提示矛盾配置
- 同一群可出现在多个分类
- `scheduled_group_list*` 在 by_category 下完全忽略（仅 per_group 使用）
- 输出首版为文本 digest（非完整图片日报合并）

**新增/修改文件**：
- `src/domain/entities/push_category.py`
- `src/application/services/scheduled_category_digest_service.py`
- `src/infrastructure/scheduler/auto_scheduler.py`
- `src/infrastructure/config/config_manager.py`
- `_conf_schema.json` / docs / metadata / README

---

## 行为矩阵

`enable_admin_notify` 开关下的所有触发路径：

| 触发方式 | 开关 ON（定制默认） | 开关 OFF |
|----------|---------------------|----------|
| 定时任务（到点自动跑） | 私聊管理员，不发群 | 发群（上游原行为） |
| 手动 `/群分析` 命令 | 群里回"已私聊发送"，报告私聊管理员 | 发群（上游原行为） |

**关键保证**：只要开关打开，报告内容**绝不会**出现在群聊里。

---

## 部署时的前置条件（让私聊推送真正工作）

1. **确认开关**：插件配置 → `admin_notify.enable_admin_notify` = true（定制版新装默认已开；旧配置需手动确认）
2. **填管理员真实 QQ**（二选一）：
   - AstrBot 通用设置 → `admins_id`（推荐，所有插件共享）
   - 或插件配置 → `admin_notify.extra_admin_qq`
3. **napcat 登录的 QQ 与管理员 QQ 互为好友**：OneBot 的 `send_private_msg` 要求双方好友关系，否则发不出（日志会显示「私聊发送 xxx 返回失败」）。
4. **配置群权限 + 定时任务**：
   - `basic.group_list_mode` = `whitelist`
   - `basic.group_list` = 你要看的群（UMO 或群号）
   - `auto_analysis.auto_analysis_time`（默认 `["23:00"]`）
   - **群少（单群完整日报）**：
     - `delivery_mode` = `per_group`
     - `scheduled_group_list` = 同上
   - **群多（分类聚合）**：
     - `delivery_mode` = `by_category`
     - `category_push_mode` = `split` 或 `merged`
     - `categories` = `[{"name":"科技","groups":["群A","群B"]},{"name":"AI","groups":["群C"]}]`
     - 群多场景只需配 categories；basic 白名单对分类聚合链路不再构成阻碍（分类群默认放行，仅 basic 黑名单会生效）
5. **注意**：`basic` 白名单空 = 手动 `/群分析` 等命令路径无群可用；`per_group` 名单空或 `by_category` 无分类 = 不注册定时。

### 启用实时消息监控（主动推送，次要）

1. **开启开关**：`message_monitor.enable_monitor` = `true`
2. **填监控群**（必填）：`message_monitor.monitored_groups` = `["群号"]`（插件会攒这些群里所有人的消息作为上下文）
3. **填目标 QQ**：`message_monitor.monitored_qqs` = `["目标QQ号"]`（LLM 重点提取这些人的发言）
4. **自定义关键词**（可选）：`message_monitor.extra_keywords` = `["破解", "激活码", "资源"]`
5. **汇总间隔**（可选）：`message_monitor.flush_interval` = `10`（分钟，默认 10，建议 5~30）
6. **上下文条数**（可选）：`message_monitor.max_context_messages` = `50`（默认 50，建议 30~100）
7. **推送目标**（可选）：`message_monitor.alert_admin_qqs` = `["你的QQ"]`（空=复用管理员 QQ）
8. **好友关系**：同上，bot QQ 与推送目标需互为好友

**默认行为**：开关 `false`（需手动开）；`use_llm_confirm` = `true`；`flush_interval` = `10` 分钟；`max_context_messages` = `50`。

### 启用智能降噪

降噪默认已启用（配置项有默认值），如需调整：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `cooldown_seconds` | 60 | 同一发送者@同一群的最小推送间隔（秒）。0=不冷却。critical 优先级豁免 |
| `dedup_minutes` | 30 | 内容去重窗口（分钟）。内容相似消息在窗口内只推一次。0=不去重 |
| `keyword_batch_seconds` | 60 | keyword 模式 normal 优先级批量合并间隔（秒）。0=不合并（立即推） |

### 启用跨群聚合（多群汇总分析）

1. **触发方式**：`message_monitor.monitor_mode` = `window`（只对整窗模式有效）
2. **分析范围**：`message_monitor.window_scope` = `cross_group`  
   （单群独立：`per_group`；旧配置 `enable_cross_group=true` 仍兼容）
3. **效果**：flush 时合并所有监控群消息，LLM 按话题聚类输出统一简报（而非每群独立一条）
4. **降级**：LLM 不可用时自动回退为逐群独立推送

---

## 维护备忘

如需对照功能演进历史，可查阅 `CHANGELOG.md`。冲突高发文件（按改动频率排序）：
1. `_conf_schema.json` —— 配置面板高频调整
2. `main.py` —— 命令处理逻辑
3. `dispatcher.py` / `analysis_application_service.py` —— 报告分发与并发逻辑
