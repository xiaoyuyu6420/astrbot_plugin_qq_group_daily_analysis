# 邮件投递诊断记录

> 状态：根因已定位（From 头），代码已修复，待 QQ 收件箱最终确认
> 日期：2026-08-05
> 关联：`src/infrastructure/messaging/email_sender.py` `_send_sync`

## 背景

126 邮箱（wbq20040526@126.com）通过 SMTP 发到 QQ 邮箱（672178818@qq.com），
SMTP 层返回成功（`sendmail` 的 `refused={}`），但 QQ 邮箱收件箱/垃圾箱均无邮件。

## 根因（实测铁证，勿推翻）

**不是 SPF / DKIM / IP 信誉**——是 **From 头未做 RFC 2047 编码**。

### 证据链

通过 POP3（pop.126.com:995）登录 126 发件邮箱，在收件箱发现 **6 封 QQ 系统退信**，
最新一封正文明确写出 QQ 拒收原因：

```
退信原因：因信头from字段拒收邮件
英文说明: SMTP error, DOT: Host qq.com(157.255.221.253) DOT said
          550 The "From" header is missing or invalid.
          Please follow RFC5322, RFC2047, RFC822 standard protocol.
建议: 检查邮件信头是否包含from字段，from字段中的邮箱地址是否正确
```

### 修复前 vs 修复后

| | From 头 | 结果 |
|---|---------|------|
| 修复前 | `群聊日报 <wbq@126.com>`（中文名裸写） | QQ 每次 550 拒收 + 退信 |
| 修复后 | `=?utf-8?b?576k6IGK5pel5oql?= <wbq@126.com>`（RFC 2047） | 发件后 POP3 查 126 **无新退信** |

### 闭环验证

1. **126→126 自收**：POP3 拉到 #11 邮件「决定性实验-A自收」→ 126 投递链路正常
2. **126→QQ 修复前**：每次触发 QQ 退信（126 收件箱堆 6 封）
3. **126→QQ 修复后**：发件等 10s 后 POP3 查 126，无新退信 → QQ MX 接受

## 代码修复

`src/infrastructure/messaging/email_sender.py` `_send_sync`：

```python
from email.utils import make_msgid, formatdate
from email.header import Header

msg["Message-ID"] = make_msgid(domain=domain)
msg["Date"] = formatdate(localtime=True)
msg["From"] = f"{Header(self.from_name, 'utf-8').encode()} <{self.username}>"
```

## 待确认（验证标准①）

「QQ MX 接受」≠「邮件落到收件箱」。修复后的邮件可能被 QQ 分到**广告邮件文件夹**。
最终确认需二选一：

1. **用户查 QQ 邮箱所有文件夹**（收件箱/垃圾箱/广告邮件/已删除），
   找主题含 `[From头修复验证]` 的邮件
2. **用户提供 QQ 邮箱 POP3/IMAP 授权码**（设置→帐号→开启服务），
   助手用 `poplib.POP3_SSL("pop.qq.com", 995)` 自主查收件箱

## 如果修复后邮件仍未到收件箱（落到广告邮件）

可选优化（按成本排序）：
1. **加纯文本 alternative 部分**：当前只有 HTML，加 `multipart/alternative` 附带 plain text，
   降低被 QQ 反垃圾判广告的概率
2. **换 QQ 邮箱自身发件**（QQ→QQ 同域投递最宽松）：用户提供 QQ 邮箱 + 授权码，
   SMTP 改为 `smtp.qq.com:465`
3. **加 SPF/DKIM**：若用自有域名发件，配 DNS 记录（126 共享 IP 无法配）

## 诊断脚本位置（临时）

- `/tmp/read_bounce.py` — POP3 读 126 退信正文（在容器内执行）
- `/tmp/from_header_verified_test.py` — 发 From 头修复后测试 + 查退信
- `/tmp/pop3_check.py` — POP3 列 126 收件箱验证自收邮件

> 注：`/tmp` 重启清空。如需重跑，参考本文档证据链重建脚本。
