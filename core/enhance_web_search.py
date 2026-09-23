"""群聊增强：联网搜索工具（移植自 astrbot_plugin_astrbot_enhance_mode 的 grok_web_search）。

用法：给模型注册一个 `enhance_web_search` LLM 工具，调用配置的提供商
直接发 HTTP（优先 /v1/responses + web_search 工具，回退 /v1/chat/completions），
提供商需自带联网搜索能力（如 grok / 支持联网的聚合站）。
"""

from __future__ import annotations

import json
from typing import Any


def normalize_api_base_url(raw_base_url: str) -> str:
    base_url = str(raw_base_url or "").strip().rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[: -len("/v1")]
    return base_url


def join_base_with_path(base_url: str, path: str) -> str:
    cleaned_path = str(path or "").strip()
    if cleaned_path.startswith("http://") or cleaned_path.startswith("https://"):
        return cleaned_path
    if not cleaned_path.startswith("/"):
        cleaned_path = f"/{cleaned_path}"
    return f"{base_url.rstrip('/')}{cleaned_path}"


def extract_provider_api_key(provider: Any) -> str:
    """从 provider 对象尽力提取一个可用的 API key。"""
    get_current_key = getattr(provider, "get_current_key", None)
    if callable(get_current_key):
        try:
            key = str(get_current_key() or "").strip()
            if key:
                return key
        except Exception:
            pass

    keys: list[Any] = []
    get_keys = getattr(provider, "get_keys", None)
    if callable(get_keys):
        try:
            fetched = get_keys()
            if isinstance(fetched, list):
                keys = fetched
            elif isinstance(fetched, str):
                keys = [fetched]
        except Exception:
            keys = []

    if not keys:
        raw_keys = getattr(provider, "provider_config", {}).get("key", [])
        if isinstance(raw_keys, list):
            keys = raw_keys
        elif isinstance(raw_keys, str):
            keys = [raw_keys]

    for item in keys:
        key = str(item or "").strip()
        if key:
            return key
    return ""


def parse_sse_chat_completion(raw_text: str) -> dict[str, Any] | None:
    """把 SSE 流拼接成标准 chat.completion 结构。"""
    chunks: list[dict[str, Any]] = []
    for line in str(raw_text or "").splitlines():
        line = line.strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(chunk, dict):
            chunks.append(chunk)

    if not chunks:
        return None

    merged_content = ""
    model_name = ""
    usage_info: dict[str, Any] = {}
    for chunk in chunks:
        if not model_name:
            model_name = str(chunk.get("model") or "")
        chunk_usage = chunk.get("usage")
        if isinstance(chunk_usage, dict):
            usage_info = chunk_usage
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        choice0 = choices[0]
        if not isinstance(choice0, dict):
            continue
        delta = choice0.get("delta")
        if isinstance(delta, dict):
            delta_content = delta.get("content")
            if isinstance(delta_content, str):
                merged_content += delta_content

    return {
        "choices": [{"message": {"content": merged_content}}],
        "model": model_name,
        "usage": usage_info,
    }


def extract_chat_completion_text(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice0 = choices[0]
    if not isinstance(choice0, dict):
        return ""
    message = choice0.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def extract_usage_tokens(data: dict[str, Any]) -> dict[str, int]:
    usage_raw = data.get("usage")
    if not isinstance(usage_raw, dict):
        return {}
    prompt_tokens = int(
        usage_raw.get("prompt_tokens")
        or usage_raw.get("input_tokens")
        or usage_raw.get("input")
        or 0
    )
    completion_tokens = int(
        usage_raw.get("completion_tokens")
        or usage_raw.get("output_tokens")
        or usage_raw.get("output")
        or 0
    )
    total_tokens = int(
        usage_raw.get("total_tokens")
        or usage_raw.get("total")
        or (prompt_tokens + completion_tokens)
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def normalize_web_search_sources(raw_sources: object) -> list[dict[str, str]]:
    """把各种形态的 sources 字段归一化为 [{url,title,snippet}]。"""
    results: list[dict[str, str]] = []
    if isinstance(raw_sources, dict):
        raw_sources = [raw_sources]
    if not isinstance(raw_sources, list):
        return results
    for item in raw_sources:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("source_url") or "").strip()
        if not url:
            continue
        results.append(
            {
                "url": url,
                "title": str(item.get("title") or "").strip(),
                "snippet": str(item.get("snippet") or "").strip(),
            }
        )
    return results


def extract_responses_text_and_sources(
    data: dict[str, Any],
) -> tuple[str, list[dict[str, str]]]:
    """解析 /v1/responses 的输出结构，返回 (正文, 来源列表)。"""
    text_parts: list[str] = []
    source_map: dict[str, dict[str, str]] = {}

    def push_source(url: str, title: str = "", snippet: str = "") -> None:
        clean_url = str(url or "").strip()
        if not clean_url:
            return
        if clean_url not in source_map:
            source_map[clean_url] = {
                "url": clean_url,
                "title": str(title or "").strip(),
                "snippet": str(snippet or "").strip(),
            }
            return
        if not source_map[clean_url]["title"] and title:
            source_map[clean_url]["title"] = str(title).strip()
        if not source_map[clean_url]["snippet"] and snippet:
            source_map[clean_url]["snippet"] = str(snippet).strip()

    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type == "message":
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    part_type = str(part.get("type") or "")
                    if part_type not in {"output_text", "text"}:
                        continue
                    part_text = part.get("text") or part.get("content")
                    if isinstance(part_text, str) and part_text.strip():
                        text_parts.append(part_text)
                    annotations = part.get("annotations")
                    if not isinstance(annotations, list):
                        continue
                    for annotation in annotations:
                        if not isinstance(annotation, dict):
                            continue
                        if str(annotation.get("type") or "") not in {
                            "url_citation",
                            "citation",
                        }:
                            continue
                        push_source(
                            url=str(
                                annotation.get("url")
                                or annotation.get("source_url")
                                or ""
                            ),
                            title=str(annotation.get("title") or ""),
                            snippet=str(annotation.get("snippet") or ""),
                        )
                continue
            if item_type == "web_search_call":
                action = item.get("action")
                if not isinstance(action, dict):
                    continue
                for source in normalize_web_search_sources(action.get("sources")):
                    push_source(**source)

    merged_text = "\n".join(part for part in text_parts if part.strip()).strip()
    if not merged_text:
        merged_text = str(data.get("output_text") or "").strip()
    return merged_text, list(source_map.values())


def build_web_search_http_requests(
    provider: Any,
    query: str,
    *,
    system_prompt: str,
    request_mode: str = "auto",
    base_url_override: str = "",
    tools_override: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """根据 provider 配置构造 1-2 个候选 HTTP 请求（responses 优先，chat 兜底）。

    tools_override 不为空时作为 responses 模式的 tools 字段（如 x_search），
    chat_completions 兜底则不带服务端工具（普通生成，仅旧式源使用）。
    """
    provider_cfg = (
        provider.provider_config if isinstance(provider.provider_config, dict) else {}
    )
    # provider.meta 是方法（返回元数据对象）而非属性：原写法
    # getattr(provider.meta, "id", "") 恒为空串，回退标签永远落在 "provider"。
    meta_id = ""
    meta = getattr(provider, "meta", None)
    if callable(meta):
        try:
            meta_id = str(getattr(meta(), "id", "") or "")
        except Exception:
            meta_id = ""
    provider_label = str(
        provider.get_model()
        or provider_cfg.get("model")
        or meta_id
        or "provider"
    )

    api_base = normalize_api_base_url(
        str(base_url_override or "").strip()
        or str(provider_cfg.get("api_base") or "")
    )
    if not api_base:
        raise ValueError(f"提供商 `{provider_label}` 缺少 api_base，无法联网搜索")

    api_key = extract_provider_api_key(provider)
    if not api_key:
        raise ValueError(f"提供商 `{provider_label}` 缺少 API key，无法联网搜索")

    model = str(provider.get_model() or provider_cfg.get("model") or "").strip()
    custom_extra_body = provider_cfg.get("custom_extra_body", {})

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    custom_headers = provider_cfg.get("custom_headers", {})
    if isinstance(custom_headers, dict):
        for key, value in custom_headers.items():
            if str(key).lower() in {"authorization", "content-type"}:
                continue
            headers[str(key)] = str(value)

    request_mode = str(request_mode or "auto").strip().lower()
    modes: list[str] = (
        [request_mode] if request_mode in {"responses", "chat_completions"} else ["responses", "chat_completions"]
    )

    requests: list[dict[str, Any]] = []
    for mode in modes:
        if mode == "responses":
            body: dict[str, Any] = {
                "input": query,
                "instructions": system_prompt,
                "temperature": 0.2,
                "tools": tools_override or [{"type": "web_search"}],
                "tool_choice": "auto",
            }
            if model:
                body["model"] = model
            if isinstance(custom_extra_body, dict):
                for key, value in custom_extra_body.items():
                    if str(key) not in {"model", "input", "instructions"}:
                        body[str(key)] = value
            requests.append(
                {
                    "mode": "responses",
                    "url": join_base_with_path(api_base, "/v1/responses"),
                    "headers": headers,
                    "body": body,
                }
            )
            continue

        body = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        if model:
            body["model"] = model
        if isinstance(custom_extra_body, dict):
            for key, value in custom_extra_body.items():
                if str(key) not in {"model", "messages", "stream"}:
                    body[str(key)] = value
        requests.append(
            {
                "mode": "chat_completions",
                "url": join_base_with_path(api_base, "/v1/chat/completions"),
                "headers": headers,
                "body": body,
            }
        )

    return requests, provider_label


async def run_web_search(
    provider: Any,
    query: str,
    *,
    system_prompt: str,
    request_mode: str = "auto",
    base_url_override: str = "",
    timeout_sec: float = 60.0,
    show_sources: bool = False,
    max_sources: int = 5,
    tools_override: list[dict[str, Any]] | None = None,
) -> str:
    """执行一次联网搜索，返回给模型看的纯文本结果（含错误说明，不抛异常）。"""
    import asyncio

    import aiohttp

    try:
        request_specs, provider_label = build_web_search_http_requests(
            provider,
            query,
            system_prompt=system_prompt,
            request_mode=request_mode,
            base_url_override=base_url_override,
            tools_override=tools_override,
        )
    except Exception as e:
        return f"Web search error: {e}"
    if not request_specs:
        return "Web search error: no request spec could be built."

    # 统一日志器（上架规范要求）：logger 一律从 astrbot.api 导入，
    # 不得使用 Python 内置 logging 模块。
    from astrbot.api import logger as _astrbot_logger

    _astrbot_logger.info(
        f"[联网搜索] 开始查询喵 provider={provider_label} query_len={len(query)}"
    )

    text = ""
    sources_from_endpoint: list[dict[str, str]] = []
    last_error = "Web search failed."

    try:
        timeout = aiohttp.ClientTimeout(total=float(timeout_sec))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for request_spec in request_specs:
                mode = str(request_spec.get("mode") or "chat_completions")
                request_url = str(request_spec.get("url") or "")
                headers = request_spec.get("headers")
                body = request_spec.get("body")
                if not request_url or not isinstance(headers, dict):
                    continue

                async with session.post(
                    request_url, json=body, headers=headers
                ) as resp:
                    raw_text = await resp.text()
                    if resp.status != 200:
                        _astrbot_logger.warning(
                            f"[联网搜索] HTTP {resp.status} ({mode}): {raw_text[:300]}"
                        )
                        last_error = f"Web search HTTP {resp.status} ({mode})"
                        continue

                    parsed_data: dict[str, Any] | None = None
                    content_type = resp.headers.get("Content-Type", "")
                    if mode == "chat_completions" and (
                        "text/event-stream" in content_type
                        or raw_text.strip().startswith("data:")
                    ):
                        parsed_data = parse_sse_chat_completion(raw_text)
                    else:
                        try:
                            decoded = json.loads(raw_text)
                            if isinstance(decoded, dict):
                                parsed_data = decoded
                        except json.JSONDecodeError:
                            parsed_data = None

                    if parsed_data is None:
                        last_error = f"Web search response parsing failed (mode={mode})."
                        continue

                    if mode == "responses":
                        text, sources_from_endpoint = (
                            extract_responses_text_and_sources(parsed_data)
                        )
                    else:
                        text = extract_chat_completion_text(parsed_data).strip()
                        sources_from_endpoint = []

                    if not text:
                        last_error = "Provider returned empty response for web search."
                        continue
                    break
    except asyncio.TimeoutError:
        return f"Web search timeout (>{float(timeout_sec):.1f}s)."
    except Exception as e:
        return f"Web search provider call failed: {e}"

    if not text:
        return last_error

    if show_sources:
        merged: list[dict[str, str]] = []
        seen: set[str] = set()
        for source in sources_from_endpoint:
            url = source.get("url", "")
            if url and url not in seen:
                seen.add(url)
                merged.append(source)
        max_sources = max(0, int(max_sources))
        if max_sources > 0:
            merged = merged[:max_sources]
        if merged:
            lines = [text, "", "Sources:"]
            for idx, source in enumerate(merged, 1):
                title = source.get("title") or "(untitled)"
                lines.append(f"{idx}. {title} - {source.get('url', '')}")
            text = "\n".join(lines)

    return text
