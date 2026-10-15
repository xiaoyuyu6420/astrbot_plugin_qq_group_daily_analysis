"""网页元信息抓取器：采集简报的链接富化模块。

设计原则（与 sk_verifier.py 同范式）：
- aiohttp + trust_env=True：跟随系统代理，适应国内网络环境
- 严格超时 + 体积上限：单链接最多读 128KB，防止大页面/大文件拖垮任务
- 分级降级：抓不到标题/摘要不是错误，返回"仅 URL"的 LinkInfo，LLM 仍可凭 URL 域名和消息上下文归类
- SSRF 防护：拒绝内网地址与非常规 scheme，群消息里的链接不可信
"""

from __future__ import annotations

import asyncio
import html as html_lib
import ipaddress
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

import aiohttp

from ..utils.logger import logger

_FETCH_TIMEOUT = 10
_MAX_BODY_BYTES = 128 * 1024
_MAX_CONCURRENCY = 6
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]，。）】\u3000]+", re.IGNORECASE)
_MEDIA_EXT_RE = re.compile(
    r"\.(jpe?g|png|gif|webp|bmp|svg|ico|mp4|mp3|mov|avi|mkv|flv|wav|m4a|zip|rar|7z|apk|exe|dmg|pkg|iso|pdf)([?#].*)?$",
    re.IGNORECASE,
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
    re.IGNORECASE | re.DOTALL,
)
_OG_DESC_RE = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
    re.IGNORECASE | re.DOTALL,
)
_META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class LinkInfo:
    """单个链接的富化结果。fetch_failed=True 表示完全没抓到页面。"""

    url: str
    title: str = ""
    description: str = ""
    fetch_failed: bool = False


def _is_safe_host(hostname: str) -> bool:
    """SSRF 防护：拒绝内网/回环地址。解析失败（如非常规域名）视为安全，交给 DNS 之后的连接层。"""
    if not hostname or hostname.lower() in ("localhost",):
        return False
    try:
        addr = ipaddress.ip_address(hostname)
        return not (addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local)
    except ValueError:
        return True


def is_fetchable_url(url: str) -> bool:
    """判断 URL 是否值得抓取：http(s) + 非内网 + 非媒体资源文件。"""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    if not _is_safe_host(parsed.hostname):
        return False
    if _MEDIA_EXT_RE.search(parsed.path or ""):
        return False
    return True


def extract_urls(text: str) -> list[str]:
    """从文本中提取去重后的可抓取 URL（过滤内网与媒体资源）。"""
    if not text:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?，。；：！？")
        if url not in seen and is_fetchable_url(url):
            seen.add(url)
            urls.append(url)
    return urls


def _clean_text(raw: str, max_len: int) -> str:
    """去标签、压空白、解 HTML 实体、截断。"""
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", raw)).strip()
    text = html_lib.unescape(text)
    return text[:max_len]


def _decode_body(body: bytes, content_type: str, html_head: bytes) -> str:
    """尽力正确解码：charset 声明 > utf-8 严格 > gb18030（中文站超集）> utf-8 宽松。"""
    encoding = ""
    m = re.search(rb"charset=[\"']?([\w-]+)", content_type.encode("utf-8", "ignore") + html_head[:4096], re.IGNORECASE)
    if m:
        encoding = m.group(1).decode("ascii", "ignore").lower()
    if encoding:
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            pass
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return body.decode("gb18030")
    except UnicodeDecodeError:
        return body.decode("utf-8", errors="replace")


def parse_link_meta(html_text: str) -> tuple[str, str]:
    """从 HTML 提取 (标题, 摘要)：og:title > <title>；og:description > meta description。"""
    title = ""
    for pattern in (_OG_TITLE_RE, _TITLE_RE):
        m = pattern.search(html_text)
        if m:
            title = _clean_text(m.group(1), 200)
            if title:
                break
    desc = ""
    for pattern in (_OG_DESC_RE, _META_DESC_RE):
        m = pattern.search(html_text)
        if m:
            desc = _clean_text(m.group(1), 300)
            if desc:
                break
    return title, desc


async def fetch_link_info(session: aiohttp.ClientSession, url: str) -> LinkInfo:
    """抓单个链接的标题与摘要。任何失败都降级为 fetch_failed，不抛异常。"""
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if resp.status != 200 or "html" not in content_type.lower():
                return LinkInfo(url=url, fetch_failed=True)
            chunks: list[bytes] = []
            received = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                chunks.append(chunk)
                received += len(chunk)
                if received >= _MAX_BODY_BYTES:
                    break
            body = b"".join(chunks)
            html_text = _decode_body(body, content_type, body[:4096])
            title, desc = parse_link_meta(html_text)
            if not title and not desc:
                # 页面能打开但没有元信息（如 SPA 空壳）：保留 URL，不算失败
                return LinkInfo(url=url, fetch_failed=True)
            return LinkInfo(url=url, title=title, description=desc)
    except asyncio.TimeoutError:
        logger.debug(f"[WebFetcher] 抓取超时: {url}")
    except aiohttp.ClientError as e:
        logger.debug(f"[WebFetcher] 网络错误 {url}: {type(e).__name__}")
    except Exception as e:
        logger.debug(f"[WebFetcher] 异常 {url}: {type(e).__name__}: {e}")
    return LinkInfo(url=url, fetch_failed=True)


async def fetch_links_batch(
    urls: Iterable[str], max_concurrency: int = _MAX_CONCURRENCY
) -> dict[str, LinkInfo]:
    """并发抓取一批链接，返回 url → LinkInfo 映射（失败的也包含，值为 fetch_failed）。"""
    url_list = [u for u in dict.fromkeys(urls) if is_fetchable_url(u)]
    if not url_list:
        return {}

    results: dict[str, LinkInfo] = {}
    semaphore = asyncio.Semaphore(max_concurrency)
    timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT)
    # trust_env=True 走系统代理（与 sk_verifier.py 同范式）
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:

        async def _worker(u: str) -> None:
            async with semaphore:
                results[u] = await fetch_link_info(session, u)

        await asyncio.gather(*[_worker(u) for u in url_list])
    ok = sum(1 for info in results.values() if not info.fetch_failed)
    logger.info(f"[WebFetcher] 链接富化完成: {ok}/{len(results)} 成功")
    return results
