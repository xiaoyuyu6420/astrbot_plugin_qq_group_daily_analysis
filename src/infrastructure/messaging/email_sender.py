"""SMTP 邮件发送器（零依赖，用 Python 标准库 smtplib）。

设计原则：
- 标准协议，不依赖任何专有 CLI（换邮箱只改配置）
- 发 HTML 邮件（支持 <details> 折叠等交互）+ 附件
- 失败优雅降级（打日志，不阻断主流程）

使用方式：
    sender = EmailSender(
        host="smtp.126.com", port=465,
        username="wbq@126.com", auth_code="xxx",
        from_name="群聊日报",
    )
    await sender.send_html(
        to=["672178818@qq.com"],
        subject="分类日报",
        html_body="<h1>...</h1>",
        attachments=[("AI日报.md", "markdown 内容")],
    )
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import make_msgid, formatdate
from email.header import Header
from typing import Any

from ...utils.logger import logger


class EmailSender:
    """SMTP 邮件发送（SSL 直连，端口 465）。"""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        auth_code: str,
        from_name: str = "群聊日报",
    ):
        self.host = host
        self.port = port
        self.username = username
        self.auth_code = auth_code
        self.from_name = from_name

    def _is_configured(self) -> bool:
        """配置是否完整（缺任一项无法发邮件）。"""
        return all([self.host, self.port, self.username, self.auth_code])

    async def send_html(
        self,
        to: list[str],
        subject: str,
        html_body: str,
        attachments: list[tuple[str, str]] | None = None,
    ) -> bool:
        """发送 HTML 邮件（可选附件）。

        Args:
            to: 收件人邮箱列表
            subject: 邮件主题
            html_body: HTML 正文
            attachments: [(filename, content_str), ...] 文本类附件

        Returns:
            是否发送成功
        """
        if not self._is_configured():
            logger.warning(
                f"[EmailSender] SMTP 配置不完整 "
                f"(host={self.host}, user={self.username})，跳过邮件发送"
            )
            return False
        if not to:
            logger.warning("[EmailSender] 无收件人，跳过邮件发送")
            return False

        # 在线程里跑（smtplib 是同步阻塞的）
        try:
            return await asyncio.to_thread(
                self._send_sync, to, subject, html_body, attachments
            )
        except Exception as e:
            logger.error(f"[EmailSender] 邮件发送异常: {type(e).__name__}: {e}")
            return False

    def _send_sync(
        self,
        to: list[str],
        subject: str,
        html_body: str,
        attachments: list[tuple[str, str]] | None,
    ) -> bool:
        """同步发送（在线程池里执行）。"""
        # 构建 MIME 邮件
        # Message-ID/Date 是 RFC 5322 强制头，QQ/腾讯接收侧缺这两项会在网关层静默丢弃
        # （连垃圾箱都不进）。SMTP sendmail 返回空 dict 不等于送达。
        #
        # From 头必须做 RFC 2047 编码（实测铁证，勿回退）：
        #   2026-08-05 用 wbq20040526@126.com → 672178818@qq.com 投递，
        #   POP3 拉 126 收件箱发现 QQ 系统退信，原文：
        #     550 The "From" header is missing or invalid.
        #     Please follow RFC5322, RFC2047, RFC822 standard protocol.
        #     因信头from字段拒收邮件
        #   根因：中文显示名「群聊日报」未编码时，QQ 的 MX 严格校验 From 头，
        #   判定 invalid 后直接拒收（不进垃圾箱，直接退信）。
        #   修复后（Header().encode()）重发，POP3 查 126 收件箱无新退信 → QQ 接受。
        #   注意：无退信只证明 QQ MX 接受入站，不等于落到收件箱（可能进广告邮件文件夹）。
        domain = self.username.split("@")[-1] if "@" in self.username else "localhost"
        msg = MIMEMultipart("mixed")
        msg["Message-ID"] = make_msgid(domain=domain)
        msg["Date"] = formatdate(localtime=True)
        msg["Subject"] = subject
        # From 名含中文时必须 RFC 2047 编码，否则 QQ 等严格网关会 550 拒收（见上方实测证据）
        msg["From"] = f"{Header(self.from_name, 'utf-8').encode()} <{self.username}>"
        msg["To"] = ", ".join(to)

        # HTML 正文
        html_part = MIMEText(html_body, "html", "utf-8")
        msg.attach(html_part)

        # 附件
        if attachments:
            for filename, content in attachments:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(content.encode("utf-8"))
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    f'attachment; filename="{filename}"',
                )
                msg.attach(part)

        # 发送
        ctx = ssl.create_default_context()
        try:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=30, context=ctx) as s:
                s.login(self.username, self.auth_code)
                s.sendmail(self.username, to, msg.as_string())
            logger.info(
                f"[EmailSender] 邮件发送成功: {self.username} → {to} "
                f"({subject}, {len(attachments or [])} 附件)"
            )
            return True
        except Exception as e:
            logger.error(
                f"[EmailSender] SMTP 发送失败 ({self.host}:{self.port}): "
                f"{type(e).__name__}: {e}"
            )
            return False


def build_email_sender_from_config(config_manager: Any) -> EmailSender | None:
    """从 config_manager 构造 EmailSender（配置不全返回 None）。"""
    if not hasattr(config_manager, "is_digest_email_enabled"):
        return None
    if not config_manager.is_digest_email_enabled():
        return None
    if not hasattr(config_manager, "get_smtp_host"):
        return None
    sender = EmailSender(
        host=config_manager.get_smtp_host(),
        port=config_manager.get_smtp_port(),
        username=config_manager.get_smtp_username(),
        auth_code=config_manager.get_smtp_auth_code(),
        from_name=config_manager.get_smtp_from_name(),
    )
    if not sender._is_configured():
        return None
    return sender
