"""DeepSeek 官方 Search / Fetch 工具接口。

文档：https://api-docs.deepseek.com/zh-cn/guides/search
  · POST /v1/tools/search  {query, count, ori_question, localization, need_content}
      → results[]: rerank_score / title / url / snippet / summary / content /
                   site_name / date / authority
  · POST /v1/tools/fetch   {url} → {title, content(Markdown 正文)}
计费：各 5 元 / 1000 次**成功**请求（失败/无结果不计费）；QPS 各 5。

设计要点：
- 这是**真搜索引擎**（不同于旧的 Responses API web_search 工具，那个常常不联网）。
- 只读接口；408/429/5xx 重试一次，其它 4xx 不重试。
"""
from __future__ import annotations

import asyncio

import httpx

from core.logger import get_logger

logger = get_logger("ds_tools")

_DEFAULT_BASE = "https://api.deepseek.com"


def _normalize_base(url: str) -> str:
    """把模型槽位的 base_url 归一成根地址。

    ⚠️ 项目里 DEEPSEEK_URL 配的是 `https://api.deepseek.com/v1/`（供 OpenAI SDK 用），
    而工具接口的完整路径本身带 `/v1`（`/v1/tools/search`）。直接拼会变成
    `/v1/v1/tools/search` → HTTP 404（已踩）。这里先剥掉尾部 /v1、/beta。
    """
    u = (url or _DEFAULT_BASE).strip().rstrip("/")
    for suffix in ("/v1", "/beta"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
            break
    return (u or _DEFAULT_BASE).rstrip("/")


def _credentials() -> tuple[str, str]:
    """取 DeepSeek 的 (api_key, base_url)：优先模型槽位（provider=DEEPSEEK），回退环境变量。"""
    try:
        from core.config import get_config
        cfg = get_config()
        for attr in ("reply_model", "judge_model", "cheap_model", "image_model"):
            mc = getattr(cfg, attr, None)
            if mc is None:
                continue
            if str(getattr(mc, "provider", "")).upper() != "DEEPSEEK":
                continue
            key = (getattr(mc, "key", "") or "").strip()
            if key:
                return key, _normalize_base(getattr(mc, "url", "") or _DEFAULT_BASE)
    except Exception as e:
        logger.debug("从模型槽位取 DeepSeek 凭证失败: %s", e)
    import os
    key = (os.getenv("DEEPSEEK_KEY", "") or "").strip()
    base = _normalize_base(os.getenv("DEEPSEEK_URL", "") or _DEFAULT_BASE)
    return key, base


async def _post(path: str, payload: dict, timeout: float = 30.0) -> dict:
    """POST 到 DeepSeek 工具接口，408/429/5xx 重试一次。失败抛异常。"""
    key, base = _credentials()
    if not key:
        raise RuntimeError("DEEPSEEK_KEY 未配置")
    url = f"{base}{path}"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
                r = await c.post(url, json=payload, headers=headers)
            if r.status_code == 200:
                return r.json()
            # 只有 408/429/5xx 值得重试
            if r.status_code in (408, 429) or 500 <= r.status_code < 600:
                last_exc = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                if attempt == 0:
                    await asyncio.sleep(1.2)
                    continue
                raise last_exc
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            if attempt == 0:
                await asyncio.sleep(1.2)
                continue
            raise
    raise last_exc or RuntimeError("未知错误")


async def ds_search(
    query: str,
    count: int = 5,
    need_content: bool = False,
    ori_question: str = "",
    timeout: float = 30.0,
) -> list[dict]:
    """网页搜索，返回 results 列表（空列表表示无结果）。失败抛异常。

    query 支持站点/语言/时间语法（site: / -site: / lang: / after: / before: / "精确短语"）。
    """
    q = (query or "").strip()
    if not q:
        return []
    payload = {
        "query": q[:128],
        "count": max(1, min(int(count or 10), 50)),
        "need_content": bool(need_content),
    }
    if ori_question:
        payload["ori_question"] = ori_question[:128]
    data = await _post("/v1/tools/search", payload, timeout=timeout)
    results = data.get("results") or []
    logger.info("DS Search: '%s' → %d 条", q[:40], len(results))
    return results


def format_search_text(results: list[dict], max_chars: int = 4000) -> str:
    """把 results 格式化成给 LLM 读的文本（标题/来源/日期/权威度/摘要）。"""
    if not results:
        return ""
    out: list[str] = []
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip() or "(无标题)"
        url = (r.get("url") or "").strip()
        summary = (r.get("summary") or r.get("snippet") or "").strip()
        content = (r.get("content") or "").strip()
        site = (r.get("site_name") or "").strip()
        date = (r.get("date") or "")[:10]
        authority = r.get("authority", -1)
        try:
            score = f"{float(r.get('rerank_score') or 0):.1f}"
        except Exception:
            score = "?"

        meta = []
        if site:
            meta.append(site)
        if date:
            meta.append(date)
        if isinstance(authority, int) and authority >= 0:
            meta.append(f"权威度 {authority}")
        meta.append(f"相关度 {score}")

        block = [f"【{i}】{title}"]
        if meta:
            block.append("　" + " · ".join(meta))
        if url:
            block.append(f"　链接：{url}")
        body = summary or content
        if body:
            block.append(f"　{body[:600]}")
        out.append("\n".join(block))

    text = "\n\n".join(out)
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


async def ds_fetch(url: str, timeout: float = 45.0) -> dict | None:
    """抓取单个网页，返回 {title, content(Markdown)}；失败返回 None。

    404/403 表示目标站点不可抓（不重试）。
    """
    u = (url or "").strip()
    if not u or not u.lower().startswith(("http://", "https://")):
        return None
    try:
        data = await _post("/v1/tools/fetch", {"url": u[:8192]}, timeout=timeout)
    except Exception as e:
        logger.warning("DS Fetch 失败 %s: %s", u[:60], e)
        return None
    content = (data.get("content") or "").strip()
    if not content:
        logger.info("DS Fetch 无正文: %s", u[:60])
        return None
    logger.info("DS Fetch: %s → %d 字符", u[:60], len(content))
    return {"title": (data.get("title") or "").strip(), "content": content}
