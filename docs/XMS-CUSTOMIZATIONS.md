# XMS 定制改动说明

> 本分支 `xms` 基于 `v4.10.8` 改造，相对上游的差异清单。
> 用途：(1) 知道改了什么、为什么改；(2) 未来从上游 sync 更新时，作为重新应用定制的对照表。

## 一句话概括

把上游「抓逆天言论、发回群」的群聊分析插件，改造成「抓信息差/商机/干货、定时报告私聊推送给管理员」的情报订阅版。

## 定制点总览

| # | 定制点 | 影响文件 | 改动规模 |
|---|--------|----------|----------|
| 1 | 管理员私聊推送（定时报告不发群） | `dispatcher.py`, `onebot_adapter.py`, `config_manager.py`, `analysis_application_service.py` | +222 / -27 |
| 2 | 手动命令 `/群分析` 也改私聊 | `main.py` | +15 / -0 |
| 3 | 信息差主题（替换原"逆天言论"） | `_conf_schema.json` 的 `prompts` | 见下 |
| 4 | 配置面板重排（admin_notify / prompts 提到最上） | `_conf_schema.json` | 结构调整 |
| 5 | 关闭用户称号 / 聊天质量锐评板块 | `_conf_schema.json` 的 `analysis_features` | 默认值改 false |
| 6 | 元数据 + logo | `metadata.yaml`, `logo.png` | 版本/作者/占位图 |

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

### 定制点 5：关闭无用板块

**改动**（`_conf_schema.json` 的 `analysis_features`）：默认值改 false
- `user_title_analysis_enabled`（用户称号/MBTI 板块）→ false，不在报告里显示
- `chat_quality_analysis_enabled`（聊天质量锐评板块）→ false，不在报告里显示

### 定制点 6：元数据 + logo

**`metadata.yaml`**：
| 字段 | 上游 | 定制 |
|------|------|------|
| `display_name` | 群分析总结插件 | 群分析总结·管理员推送版 |
| `version` | v4.10.8 | v4.10.8-xms |
| `author` | SXP-Simon | SXP-Simon (xms 定制) |
| `desc` | 原描述 | 标注「定制版 + 管理员私聊推送」 |

**`logo.png`**：原图（225×225, 104KB）→ 1×1 透明占位（69B）。目的是在 astrbot 插件列表里不显示原图。

---

## 行为矩阵

`enable_admin_notify` 开关下的所有触发路径：

| 触发方式 | 开关 ON | 开关 OFF |
|----------|---------|----------|
| 定时任务（到点自动跑） | 私聊管理员，不发群 | 发群（上游原行为） |
| 手动 `/群分析` 命令 | 群里回"已私聊发送"，报告私聊管理员 | 发群（上游原行为） |

**关键保证**：只要开关打开，报告内容**绝不会**出现在群聊里。

---

## 部署时的前置条件（让私聊推送真正工作）

1. **开启开关**：插件配置 → `admin_notify.enable_admin_notify` = true
2. **填管理员真实 QQ**（二选一）：
   - AstrBot 通用设置 → `admins_id`（推荐，所有插件共享）
   - 或插件配置 → `admin_notify.extra_admin_qq`
3. **napcat 登录的 QQ 与管理员 QQ 互为好友**：OneBot 的 `send_private_msg` 要求双方好友关系，否则发不出（日志会显示「私聊发送 xxx 返回失败」）。
4. **配置定时任务**：`auto_analysis.scheduled_group_list`（要分析的群号）+ `auto_analysis.auto_analysis_time`（出报告时间）。

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
