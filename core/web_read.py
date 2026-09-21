"""Web Read：通过 Jina Reader 抓取网页正文，返回 Markdown 纯文本。

移植自 chatluna-llm-web-search 的思路：
GET https://r.jina.ai/<原始 URL> → text/markdown 正文。
无 API Key 可用（有速率限制），配置 Key 可缓解。
"""

from __future__ import annotations

import re
from typing import Any


def build_jina_read_request(url: str, api_key: str = "") -> dict[str, Any]:
    """构造 Jina Reader 请求（URL/headers）。url 必须带 scheme。"""
    clean = str(url or "").strip()
    if not re.match(r"^https?://", clean):
        raise ValueError(f"URL 必须以 http(s):// 开头，收到: {clean[:80]}")
    headers: dict[str, str] = {
        "Accept": "text/plain",
        "X-Return-Format": "markdown",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return {
        "url": f"https://r.jina.ai/{clean}",
        "headers": headers,
    }


def clamp_text(text: str, max_chars: int) -> str:
    """超长截断并附说明。"""
    limit = max(500, int(max_chars or 8000))
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n\n[内容过长已截断：原文约 {len(text)} 字符，仅保留前 {limit} 字符]"
    )


async def read_web_page(
    url: str,
    *,
    timeout_sec: float = 45.0,
    jina_api_key: str = "",
    max_chars: int = 8000,
) -> str:
    """读取网页正文，返回给模型看的纯文本（含错误说明，不抛异常）。"""
    import asyncio

    import aiohttp

    try:
        spec = build_jina_read_request(url, jina_api_key)
    except Exception as e:
        return f"Web read error: {e}"

    import logging

    logger = logging.getLogger("proactive_chat.enhance")
    logger.info(f"[Web Read] 开始读取喵 url={url[:120]}")

    timeout = aiohttp.ClientTimeout(total=max(5.0, float(timeout_sec)))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                spec["url"], headers=spec["headers"]
            ) as resp:
                raw = await resp.text()
                if resp.status != 200:
                    logger.warning(f"[Web Read] HTTP {resp.status}: {raw[:200]}")
                    return f"Web read error: HTTP {resp.status} from Jina Reader."
    except asyncio.TimeoutError:
        return "Web read error: timeout."
    except Exception as e:
        return f"Web read error: {e}"

    text = (raw or "").strip()
    if not text:
        return "Web read error: empty response."
    return clamp_text(text, max_chars)
