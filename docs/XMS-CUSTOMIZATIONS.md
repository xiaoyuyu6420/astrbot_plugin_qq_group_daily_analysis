# XMS 定制改动说明

> 本分支 `xms` 基于 `v4.10.8` 改造，相对上游的差异清单。
> 用途：(1) 知道改了什么、为什么改；(2) 未来从上游 sync 更新时，作为重新应用定制的对照表。

## 一句话概括

把上游「抓逆天言论、发回群」的群聊分析插件，改造成「挖掘有效信息 + 及时私聊推送管理员」的情报订阅版：  
**保留输出方式与自定义程度**（图片/文本/HTML、模板、prompt），**去掉娱乐功能**（用户称号/MBTI、聊天质量锐评等）。

## 定制点总览

| # | 定制点 | 影响文件 | 改动规模 |
|---|--------|----------|----------|
| 1 | 管理员私聊推送（定时报告不发群） | `dispatcher.py`, `onebot_adapter.py`, `config_manager.py`, `analysis_application_service.py` | +222 / -27 |
| 2 | 手动命令 `/群分析` 也改私聊 | `main.py` | +15 / -0 |
| 3 | 信息差主题（替换原"逆天言论"） | `_conf_schema.json` 的 `prompts` | 见下 |
| 4 | 配置面板重排（admin_notify / prompts / message_monitor 提到最上） | `_conf_schema.json` | 结构调整 |
| 5 | **硬关闭并移除娱乐功能**（称号/MBTI/锐评） | `_conf_schema.json`, `config_manager.py`, `generators.py`, 模板文案 | 功能收敛 |
| 6 | 元数据 + logo | `metadata.yaml`, `logo.png` | 版本/作者/占位图 |
| 7 | 单群情报场景默认值 | `_conf_schema.json`, `config_manager.py` | 默认更贴近「私聊 + 单群」 |
| 8 | 私聊推送可靠性（图片失败回退文本） | `dispatcher.py` | 及时送达 |
| 9 | **实时消息监控（盯人预警）** | `message_monitor_service.py`(新), `main.py`, `config_manager.py`, `_conf_schema.json` | +~300 行 |
| 10 | **关键词即时推送模式** | `message_monitor_service.py`, `config_manager.py`, `_conf_schema.json` | +~150 行 |
| 11 | **智能降噪层** | `noise_reducer.py`(新), `message_monitor_service.py`, `config_manager.py`, `_conf_schema.json` | +~300 行 |
| 12 | **跨群智能聚合** | `message_monitor_service.py`, `config_manager.py`, `_conf_schema.json` | +~200 行 |

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

### 定制点 4：配置面板重排

**改动**（`_conf_schema.json` 顶层 object 顺序）：把改动过的配置组提到最前面，方便后续微调：
```
① admin_notify        ← 新增的私聊开关（最上）
② prompts             ← 主题核心，方便调 prompt
③ analysis_features   ← 功能开关
④ auto_analysis
⑤ basic
...（其余按原序）
```

### 定制点 5：去掉娱乐功能，只留有效信息挖掘

**目标**：保留「输出方式 + 自定义程度」，去掉娱乐向分析。

**保留**：
- 输出：`image` / `text` / `html`
- 模板：`report_template`（默认改为 `simple`）
- 自定义：话题 prompt、信息差 prompt、LLM provider、定时时间、群白名单、管理员推送

**去掉 / 硬关闭**：
- 用户称号 + MBTI / SBTI / ACGTI 画像
- 聊天质量锐评
- 配置面板中的相关开关、prompt、provider、profile 映射大 JSON

**实现方式**（不是只改默认值）：
1. `_conf_schema.json`：从面板删除娱乐配置项（称号/锐评 prompt、profile_*、对应 provider）
2. `config_manager.get_user_title_analysis_enabled()` / `get_chat_quality_analysis_enabled()` **始终返回 False**  
   （即使旧配置文件里还是 true，也不会再跑娱乐 LLM）
3. `set_*` 强制写 False，防止命令/旧逻辑重新打开
4. `generators.py`：文本/HTML 渲染跳过称号与锐评板块；信息差文案改为「信息差/商机/干货」
5. 若干模板 `quote_item.html` / `topic_item.html` 标题从「群圣经 / 热门话题」改为情报向文案

### 定制点 6：元数据 + logo

**`metadata.yaml`**：
| 字段 | 上游 | 定制 |
|------|------|------|
| `display_name` | 群分析总结插件 | 群分析总结·管理员推送版 |
| `version` | v4.10.8 | v4.10.8-xms |
| `author` | SXP-Simon | SXP-Simon (xms 定制) |
| `desc` | 原描述 | 标注「定制版 + 管理员私聊推送」 |

**`logo.png`**：原图（225×225, 104KB）→ 1×1 透明占位（69B）。目的是在 astrbot 插件列表里不显示原图。

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

### 定制点 12：跨群智能聚合

**问题**：window 模式按群×QQ 双重切分，3 个群 × 1 个目标 QQ = 3 条独立推送。同一事件被切碎、跨群重复信息无法合并。需要「综合多群信息做总结」。

**开启方式**：`message_monitor.enable_cross_group = true`

**关闭时**：window 模式逐群独立推送（原行为，每群每 QQ 一条汇总）。

**开启时**：flush 合并所有监控群的消息 → LLM 按话题聚类 → 输出一份统一简报。

**架构**：
```
_flush_all() 检测 enable_cross_group?
  true  → _flush_cross_group(batches)
           合并所有群消息（标注来源群）
           → 过滤：至少一个目标 QQ 发言
           → 截断（max_context_messages）
           → LLM 跨群聚类提取（_CROSS_GROUP_SYSTEM_PROMPT）
           → 输出统一简报
  false → 逐群 _flush_group()（原行为）
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
- `config_manager.py`：新增 `is_cross_group_enabled`
- `_conf_schema.json`：`message_monitor` 组新增 `enable_cross_group` 配置项

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
4. **配置单群白名单 + 定时任务**：
   - `basic.group_list_mode` = `whitelist`
   - `basic.group_list` = `["onebot:GroupMessage:你的群号"]`
   - `auto_analysis.scheduled_group_list` = 同上
   - `auto_analysis.auto_analysis_time`（默认 `["23:00"]`）
5. **注意**：`whitelist` + 空列表 = 没有任何群可用 / 不会开定时任务，必须先填群号。

### 启用实时消息监控（盯人预警）

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

### 启用跨群聚合

1. **开启开关**：`message_monitor.enable_cross_group` = `true`
2. **前提**：`monitor_mode` = `window`（跨群聚合只对整窗模式有效）
3. **效果**：flush 时合并所有监控群消息，LLM 按话题聚类输出统一简报（而非每群独立一条）
4. **降级**：LLM 不可用时自动回退为逐群独立推送

---

## 未来从上游 sync 更新的方法

```bash
# 拉取上游最新
git fetch upstream

# 基于 xms 分支 merge 上游 main（或最新 tag）
git checkout xms
git merge upstream/main
# 如有冲突，主要冲突点会落在上述 6 个定制文件上
# 解决冲突时：保留定制逻辑，合并上游的功能更新

# 验证：对照本文档的「定制点总览」表，确认每个定制点都还在
git diff v4.10.8..HEAD -- src/infrastructure/reporting/dispatcher.py
```

**冲突高发文件**（按概率排序）：
1. `_conf_schema.json` —— 上游几乎每次发版都改 prompt 或加配置项
2. `main.py` —— 上游改命令处理逻辑时会冲突
3. `dispatcher.py` / `analysis_application_service.py` —— 上游重构报告分发时会冲突

sync 前务必先看上游 CHANGELOG.md，评估这次更新是否触及定制点。
