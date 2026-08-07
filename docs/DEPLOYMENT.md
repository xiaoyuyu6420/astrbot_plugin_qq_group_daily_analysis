# 部署手册：astrbot_plugin_qq_group_daily_analysis

> 生产环境部署纪律：**先读本文档，再动手。每步留日志，可回滚。**
> 最后更新：2026-08-02

---

## 1. 环境信息（探查确认，非记忆）

| 项目 | 值 |
|------|-----|
| 服务器 | `192.168.5.100`（局域网，Ubuntu，hostname `ubuntu-server-1`） |
| SSH 用户 | `munich` |
| 认证 | 密码登录（本机公钥未被服务器接受） |
| 部署方式 | **rsync 拷文件**（服务器插件目录**不是** git 仓库） |
| 宿主机插件目录 | `/home/munich/napcat-xms/data/plugins/astrbot_plugin_qq_group_daily_analysis` |
| 容器挂载 | 宿主机 `/home/munich/napcat-xms/data` → 容器 `/AstrBot/data` |
| 容器名 | `astrbot-xms` |
| 容器镜像 | `astrbot:latest` |
| 端口 | 6186（见 docs/E2E-REVIEW-AND-REFLECTION.md） |

## 2. 本地仓库信息

| 项目 | 值 |
|------|-----|
| 本地路径 | `/Users/munich/Desktop/独立项目/astrbot_plugin_qq_group_daily_analysis` |
| 分支 | `xms` |
| origin | `github.com/xiaoyuyu6420/astrbot_plugin_qq_group_daily_analysis` |
| upstream | `github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis` |

## 3. SSH 复用通道（避免反复输密码）

部署前先建立控制连接复用，后续命令走复用通道，不重复传密码：

```bash
export SSHPASS='<密码>'   # 部署时由操作者提供，不写入本文档
mkdir -p /tmp/ssh-ctrl-astrbot
sshpass -e ssh -o ControlMaster=yes \
  -o ControlPath=/tmp/ssh-ctrl-astrbot/%r@%h:%p \
  -o ControlPersist=10m \
  -o ConnectTimeout=8 \
  -o StrictHostKeyChecking=accept-new \
  -o PreferredAuthentications=password \
  -o PubkeyAuthentication=no \
  munich@192.168.5.100 "echo 主控连接已建立"
```

后续命令统一用：
```bash
ssh -o ControlPath=/tmp/ssh-ctrl-astrbot/%r@%h:%p \
    -o PreferredAuthentications=password -o PubkeyAuthentication=no \
    munich@192.168.5.100 "<命令>"
```

## 4. 部署步骤（标准流程）

### 步骤 0：本地预检
```bash
cd "/Users/munich/Desktop/独立项目/astrbot_plugin_qq_group_daily_analysis"
# 确认测试通过（防幻觉：不跑测试就部署 = 赌）
python -m pytest tests/ -q --ignore=tests/real_e2e
# 确认改动范围
git status -s
```

### 步骤 1：备份服务器当前状态（可回滚的前提）
```bash
TS=$(date +%Y%m%d_%H%M%S)
ssh ... munich@192.168.5.100 \
  "cd /home/munich/napcat-xms/data/plugins && \
   tar czf ~/astrbot_plugin_backup_${TS}.tar.gz astrbot_plugin_qq_group_daily_analysis && \
   ls -lh ~/astrbot_plugin_backup_${TS}.tar.gz && \
   echo 备份完成"
```
- 备份路径：服务器 `~/astrbot_plugin_backup_<时间戳>.tar.gz`
- **回滚命令**：`tar xzf ~/astrbot_plugin_backup_<TS>.tar.gz -C /home/munich/napcat-xms/data/plugins/` 然后 `docker restart astrbot-xms`

### 步骤 2：rsync dry-run（先看会推什么，防幻觉）
```bash
cd "/Users/munich/Desktop/独立项目/astrbot_plugin_qq_group_daily_analysis"
rsync -avzn --delete \
  --exclude='data/' \
  --exclude='__pycache__/' \
  --exclude='.ruff_cache/' \
  --exclude='.pytest_cache/' \
  --exclude='.zcode/' \
  --exclude='.git/' \
  --exclude='tests/' \
  -e "sshpass -e ssh -o ControlPath=/tmp/ssh-ctrl-astrbot/%r@%h:%p \
      -o PreferredAuthentications=password -o PubkeyAuthentication=no" \
  ./ munich@192.168.5.100:/home/munich/napcat-xms/data/plugins/astrbot_plugin_qq_group_daily_analysis/ \
  2>&1 | tee /tmp/rsync_dryrun_$(date +%Y%m%d_%H%M%S).log
```
- **必须人工核对 dry-run 日志**：确认没有误删 `data/`、没有推 `tests/`
- 特别检查 `deleting` 行 —— `--delete` 会删服务器上本地没有的文件，但要排除项保护了 data/

### 步骤 3：rsync 实际同步（去掉 -n）
```bash
# 同上命令，去掉 -n，加 --stats
rsync -avz --delete --stats \
  --exclude='data/' \
  --exclude='__pycache__/' \
  --exclude='.ruff_cache/' \
  --exclude='.pytest_cache/' \
  --exclude='.zcode/' \
  --exclude='.git/' \
  --exclude='tests/' \
  -e "sshpass -e ssh -o ControlPath=/tmp/ssh-ctrl-astrbot/%r@%h:%p ..." \
  ./ munich@192.168.5.100:.../ \
  2>&1 | tee /tmp/rsync_actual_<TS>.log
```

### 步骤 4：重启容器
```bash
ssh ... munich@192.168.5.100 "docker restart astrbot-xms && docker ps --filter name=astrbot-xms --format '{{.Status}}'"
```

### 步骤 5：验证（导出容器日志，确认插件加载成功）
```bash
# 等 ~15s 让容器启动，导出最近日志
sleep 15
ssh ... munich@192.168.5.100 \
  "docker logs --since 1m astrbot-xms 2>&1 | grep -iE 'astrbot_plugin_qq_group_daily|error|traceback|import' | tail -30"
```
- 成功标志：看到插件加载、无 Traceback
- 失败处理：用步骤 1 的备份回滚

## 5. 排除项说明（为什么排除这些）

| 排除项 | 原因 |
|--------|------|
| `data/` | 插件运行数据（digest 落盘等），**绝不能覆盖** |
| `__pycache__/` | Python 字节码缓存，各机器自己生成 |
| `.ruff_cache/` `.pytest_cache/` | 本地工具缓存 |
| `.zcode/` | 编辑器/IDE 配置 |
| `.git/` | 服务器不是 git 仓库，不需要 git 元数据 |
| `tests/` | 服务器运行环境不需要测试代码 |

## 6. 防幻觉策略（生产环境强制）

1. **每步留日志**：dry-run、actual rsync、容器日志全部 tee 到 `/tmp/*.log`
2. **先备份再改**：tar 快照是回滚的底气
3. **dry-run 必须人工核对**：不核对就实际推 = 赌
4. **验证必须看真实日志**：不看日志说"部署成功" = 幻觉
5. **密码不进文档/不进 git**：`SSHPASS` 环境变量临时提供

## 7. 已知风险

- `--delete` 会删除服务器上存在但本地没有的文件。当前排除项已保护 `data/`，
  但若服务器有其他本地没有的重要文件（如自定义配置），需先在 dry-run 核对。
- 重启容器会短暂中断服务（~10-30s）。
