"""
Agent 级 Web 搜索模块
====================
特性:
1. 百度为主源 + Bing 为备 + 百度百科直访（三源并行）
2. 本地关键词预处理（保留年份/地名/限定词，不依赖 LLM）
3. 结果相关性打分排序
4. Top-N 结果深度正文抓取
5. 自适应重试 + 全程超时控制
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from core.logger import get_logger

logger = get_logger("web_search")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── 关键词预处理 ──────────────────────────────────────────

_ORAL_PATTERNS = [
    r'帮[我我们]?\s*(?:查|搜|找|看一下|找一下|搜索)',
    r'请[问帮]\s*',
    r'(?:能|可以|能不能|可不可以)\s*(?:帮|给)?\s*[我我们]?\s*(?:查|搜|找)',
    r'是什[么么的]|是什么意思',
    r'怎么[办样回事]|为什么',
    r'(?:给|跟|和)\s*[我我]?\s*(?:说|讲|介绍)(?:一下|下)?',
    r'(?:有谁知道|谁知道|求(?:助|解答))',
    r'搜[索一]下|查[一]下|找[一]下',
    r'(?:详细|具体)(?:介绍|说明|参数|信息)',
    r'(?:请问|麻烦|大神|大佬)',
]

_DOC_KEYWORDS = [
    '一年级', '二年级', '三年级', '四年级', '五年级', '六年级',
    '七年级', '八年级', '九年级', '高一', '高二', '高三',
    '小学', '初中', '高中', '大学',
    '期末', '期中', '月考', '模拟考', '联考', '中考', '高考',
    '试卷', '试题', '答案', '解析', '真题',
    '上册', '下册', '上学期', '下学期', '秋季', '春季',
    '数学', '语文', '英语', '物理', '化学', '生物', '历史', '地理', '政治',
    '必修', '选修',
    '人教版', '北师大版', '苏教版', '华师大版',
]


def preprocess_query(raw: str) -> tuple[str, str, list[str], str | None]:
    """本地关键词预处理（不依赖 LLM）"""
    s = raw.strip()

    year_match = re.search(r'((?:19|20)\d{2}(?:[-年](?:19|20)\d{2})?)', s)
    year = year_match.group(1) if year_match else None

    s_clean = s
    for pat in _ORAL_PATTERNS:
        s_clean = re.sub(pat, ' ', s_clean, flags=re.IGNORECASE)
    s_clean = re.sub(r'[，。？！,.?!；;：:、（）()\[\]【】「」"\'""''《》]', ' ', s_clean)
    s_clean = re.sub(r'\s+', ' ', s_clean).strip()

    place_tokens = re.findall(r'([\u4e00-\u9fa5]+(?:市|区|县|镇))', s)
    seen = set()
    place_tokens = [p for p in place_tokens if not (p in seen or seen.add(p))]

    doc_tokens = [kw for kw in _DOC_KEYWORDS if kw in s]

    parts = []
    if place_tokens:
        parts.append(place_tokens[0])
    if year:
        parts.append(year)
    if doc_tokens:
        seen2 = set()
        unique_docs = []
        for d in doc_tokens:
            if d not in seen2:
                seen2.add(d)
                unique_docs.append(d)
        parts.extend(unique_docs[:3])

    if len(parts) < 3:
        remainder = s_clean
        for p_ in place_tokens:
            remainder = remainder.replace(p_, " ")
        if year:
            remainder = remainder.replace(year, " ")
        for kw in doc_tokens:
            remainder = remainder.replace(kw, " ")
        remainder = re.sub(r'\s+', ' ', remainder).strip()
        if remainder:
            parts.append(remainder[:30])

    optimized = " ".join(parts)

    main = s_clean
    for p in place_tokens:
        main = main.replace(p, " ")
    if year:
        main = main.replace(year, " ")
    for kw in doc_tokens:
        main = main.replace(kw, " ")
    main = re.sub(r'\s+', ' ', main).strip()
    if not main:
        main = s_clean

    keywords = []
    if year:
        keywords.append(year)
    keywords.extend(place_tokens)
    keywords.extend(doc_tokens)
    # optimized query 按空格分词作为关键词（去掉太短的）
    if optimized:
        for w in optimized.split():
            if len(w) >= 2 and w not in keywords:
                keywords.append(w)

    return optimized, main, keywords, year


# ── 相关性打分 ──────────────────────────────────────────

def _score_result(title: str, snippet: str, keywords: list[str], year: str | None) -> int:
    score = 0
    title_l = (title or "").lower()
    snip_l = (snippet or "").lower()
    for kw in keywords:
        if not kw:
            continue
        kw_l = kw.lower()
        if kw_l in title_l:
            score += 5
        if kw_l in snip_l:
            score += 1
    if year:
        if year in title_l:
            score += 10
        if year in snip_l:
            score += 3
    return score


# ── 搜索源 ──────────────────────────────────────────────

def _format_entry(idx: int, title: str, snippet: str, link: str, source: str = "") -> dict:
    return {"idx": idx, "title": title or "", "snippet": snippet or "",
            "link": link or "", "source": source, "score": 0}


def search_baidu(query: str, limit: int = 5, timeout: float = 8.0) -> list[dict]:
    """百度网页搜索（主源）"""
    params = {"wd": query, "rn": limit, "ie": "utf-8"}
    try:
        resp = requests.get("https://www.baidu.com/s", headers=_HEADERS,
                            params=params, timeout=timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        # c-container 是搜索结果容器
        containers = soup.find_all("div", class_="c-container")
        idx = 0
        for c in containers:
            if idx >= limit:
                break
            h3 = c.find("h3")
            if not h3 or not h3.a:
                # AI 摘要等无标题容器，跳过
                continue
            title = h3.get_text(strip=True)
            link = h3.a["href"]  # 百度跳转链接
            # 摘要：尝试多种选择器
            snippet = ""
            for sel in ["div.c-abstract", "span.content-right_8Zs40",
                        "div.c-span-last", "div.c-abstract-text"]:
                snip_node = c.select_one(sel)
                if snip_node:
                    snippet = snip_node.get_text(strip=True)
                    break
            if not snippet:
                # fallback: 取容器内非标题文本
                all_text = c.get_text(strip=True)
                if title in all_text:
                    snippet = all_text[len(title):][:150].strip()
                else:
                    snippet = all_text[:150]
            # 跳过"上一条搜索"等非结果
            if "上一条" in title or "你觉得满意" in title:
                continue
            results.append(_format_entry(idx + 1, title, snippet, link, "baidu"))
            idx += 1
        return results
    except Exception as e:
        logger.debug("百度搜索失败: %s", e)
        return []


def search_bing_cn(query: str, limit: int = 5, timeout: float = 5.0) -> list[dict]:
    """Bing 中文搜索（备源）"""
    params = {"q": query, "mkt": "zh-CN", "setlang": "zh", "count": limit}
    try:
        resp = requests.get("https://cn.bing.com/search", headers=_HEADERS,
                            params=params, timeout=timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.find_all("li", class_="b_algo")
        results = []
        for i, item in enumerate(items[:limit]):
            h2 = item.find("h2")
            if not h2 or not h2.a:
                continue
            title = h2.get_text(strip=True)
            link = h2.a["href"]
            p = item.find("p")
            snippet = p.get_text(strip=True) if p else ""
            results.append(_format_entry(i + 1, title, snippet, link, "bing_cn"))
        return results
    except Exception as e:
        logger.debug("Bing 中文搜索失败: %s", e)
        return []


def search_baike(query: str, main_query: str = "", timeout: float = 5.0) -> list[dict]:
    """百度百科 — 直接尝试访问词条页面"""
    candidates = []
    if main_query:
        candidates.append(f"https://baike.baidu.com/item/{quote(main_query)}")
    candidates.append(f"https://baike.baidu.com/item/{quote(query.split(' ')[0])}")
    for url in candidates:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
            if resp.status_code != 200 or "/item/" not in resp.url:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            title = soup.title.string.strip() if soup.title and soup.title.string else query
            summary = soup.find("div", class_="lemma-summary") or \
                      soup.find("div", class_="para") or \
                      soup.find("div", class_="J-lemma-content")
            snippet = summary.get_text(strip=True)[:300] if summary else ""
            if snippet:
                return [_format_entry(1, title, snippet, resp.url, "baike")]
        except Exception as e:
            logger.debug("Baike 尝试失败: %s", e)
    return []


# ── 正文抓取 ──────────────────────────────────────────────

def fetch_content(url: str, timeout: float = 6.0, max_chars: int = 2500) -> str | None:
    """抓取单页正文。百度跳转链接会自动重定向到真实 URL。"""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        html = resp.text
    except Exception as e:
        logger.debug("抓取失败 [%s]: %s", url[:50], e)
        return None

    title = ""
    text = ""
    try:
        from readability import Document
        doc = Document(html)
        title = doc.title() or ""
        raw = doc.summary()
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "iframe", "noscript", "nav", "footer", "aside"]):
            tag.decompose()
        text = soup.get_text(strip=True, separator="\n")
    except ImportError:
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        for tag in soup(["script", "style", "iframe", "noscript", "nav", "footer", "header", "aside"]):
            tag.decompose()
        # 过滤导航/菜单区域（链接密集的 div）
        for div in soup.find_all("div"):
            links = div.find_all("a")
            text_len = len(div.get_text(strip=True))
            if text_len > 0 and len(links) > 10 and len(links) * 5 > text_len:
                div.decompose()
        main = soup.find("article") or soup.find("main") or soup.find("body") or soup
        text = main.get_text(strip=True, separator="\n")
    except Exception:
        return None

    text = re.sub(r'\n{3,}', '\n\n', text)
    if len(text) > max_chars:
        cut = text[:max_chars]
        for mark in ['。', '？', '！', '.', '?', '!', '\n']:
            last = cut.rfind(mark)
            if last > max_chars * 0.7:
                cut = cut[:last + 1]
                break
        text = cut + "..."
    if not text:
        return None
    return f"【{title}】\n{text}" if title else text


# ── 主搜索 API ──────────────────────────────────────────────

class AgentSearch:
    def __init__(self, per_source_timeout=8.0, fetch_timeout=5.0,
                 fetch_top_n=2, max_total_results=5, fetch_max_chars=2000):
        self.per_source_timeout = per_source_timeout
        self.fetch_timeout = fetch_timeout
        self.fetch_top_n = fetch_top_n
        self.max_total_results = max_total_results
        self.fetch_max_chars = fetch_max_chars

    def _search_all_sources(self, optimized: str, main: str, limit: int) -> list[dict]:
        """百度优先串行（避免并行反爬），Bing+百科并行兜底"""
        all_results = []

        # Step 1: 百度优先（主源，给 10s）
        baidu_timeout = 8.0
        try:
            baidu_results = search_baidu(optimized, limit, baidu_timeout)
            if baidu_results:
                all_results.extend(baidu_results)
                logger.debug("百度返回 %d 条", len(baidu_results))
        except Exception as e:
            logger.debug("百度搜索失败: %s", e)

        # Step 2: 百科直访（快，2s）
        try:
            baike_results = search_baike(optimized, main, 3.0)
            if baike_results:
                all_results.extend(baike_results)
                logger.debug("百科返回 %d 条", len(baike_results))
        except Exception as e:
            logger.debug("百科搜索失败: %s", e)

        # Step 3: 百度无结果时，Bing 兜底
        if not all_results:
            logger.info("百度+百科无结果，Bing 兜底")
            try:
                bing_results = search_bing_cn(optimized, limit, self.per_source_timeout)
                if bing_results:
                    all_results.extend(bing_results)
                    logger.debug("Bing 返回 %d 条", len(bing_results))
            except Exception as e:
                logger.debug("Bing 搜索失败: %s", e)

        return all_results

    def _deduplicate(self, results: list[dict]) -> list[dict]:
        seen = set()
        out = []
        for r in results:
            url = r.get("link", "")
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            out.append(r)
        return out

    def search(self, raw_query: str, limit: int = 5, deep_fetch: bool = False) -> str:
        t0 = time.time()
        optimized, main, keywords, year = preprocess_query(raw_query)
        logger.info("[Agent搜索] 原始='%s' → 优化='%s' 年份=%s",
                    raw_query[:40], optimized[:60], year)

        results = self._search_all_sources(optimized, main, limit)

        if not results:
            logger.info("[Agent搜索] 优化词无结果，重试用原文")
            results = self._search_all_sources(raw_query, raw_query, limit)

        if not results:
            return ""

        results = self._deduplicate(results)
        for r in results:
            r["score"] = _score_result(r["title"], r["snippet"], keywords, year)
        results.sort(key=lambda x: x["score"], reverse=True)

        # 过滤零分结果：如果最高分 > 0，只保留有分的结果
        max_score = results[0]["score"] if results else 0
        if max_score > 0:
            scored = [r for r in results if r["score"] > 0]
            if scored:
                results = scored
        # 如果最高分是 0（没有相关结果），返回空
        if not results or results[0]["score"] <= 0:
            logger.info("[Agent搜索] 无相关结果（最高分=0）")
            return ""
        results = results[:self.max_total_results]

        # 深度抓取 Top-N（只抓高分结果，完全隔离，失败不影响主结果）
        deep_contents = []
        deep_candidates = [r for r in results if r["score"] >= max_score / 2][:self.fetch_top_n]
        if deep_fetch and deep_candidates:
            top_n = len(deep_candidates)
            ex = ThreadPoolExecutor(max_workers=top_n)
            futures = {
                ex.submit(fetch_content, r["link"], self.fetch_timeout, self.fetch_max_chars): r
                for r in deep_candidates
            }
            try:
                for fut in as_completed(futures, timeout=self.fetch_timeout + 1):
                    try:
                        content = fut.result()
                        if content:
                            deep_contents.append(content)
                    except Exception:
                        pass
            except Exception:
                # 超时：只收集已完成的结果，不阻塞
                logger.debug("深度抓取超时，已收集 %d 条", len(deep_contents))
            finally:
                # 立即关闭，不等待未完成任务
                ex.shutdown(wait=False, cancel_futures=True)

        # 格式化输出
        lines = []
        if deep_contents:
            lines.append("===== 深度内容 =====")
            for c in deep_contents:
                lines.append(c)
                lines.append("")
            lines.append("===== 摘要结果 =====")

        for i, r in enumerate(results, 1):
            short_snip = r["snippet"][:150].replace("\n", " ") if r["snippet"] else ""
            src_tag = f"[{r['source']}]" if r.get("source") else ""
            lines.append(f"{i}. {src_tag} {r['title']}")
            if short_snip:
                lines.append(f"   {short_snip}")
            lines.append(f"   {r['link']}")

        elapsed = time.time() - t0
        logger.info("[Agent搜索] 完成: %d 摘要 + %d 深度, 耗时 %.1fs",
                    len(results), len(deep_contents), elapsed)
        return "\n".join(lines)


# ── 全局单例 + 入口 ──────────────────────────────────────

_searcher: AgentSearch | None = None


def get_agent_searcher() -> AgentSearch:
    global _searcher
    if _searcher is None:
        _searcher = AgentSearch()
    return _searcher


def agent_search(query: str, limit: int = 5, deep_fetch: bool = False) -> str:
    s = get_agent_searcher()
    return s.search(query, limit=limit, deep_fetch=deep_fetch)


async def ds_native_search(query: str) -> str | None:
    """DeepSeek Responses API 原生搜索 — 服务端搜 + 合成，零本地开销"""
    import os, json, asyncio
    import urllib.request

    api_key = os.getenv("DEEPSEEK_KEY", "")
    if not api_key:
        raise RuntimeError("DEEPSEEK_KEY 未配置")

    payload = json.dumps({
        "model": "deepseek-v4-flash",
        "instructions": "你是一个搜索助手，请根据搜索结果简洁回答用户问题，列出关键信息。",
        "input": query,
        "tools": [{"type": "web_search"}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.deepseek.com/responses",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    loop = asyncio.get_running_loop()

    def _call():
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data.get("output_text", "")
        if not text:
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for part in item.get("content", []):
                        if part.get("type") == "output_text":
                            text = part.get("text", "")
            if text:
                return text.strip() or None
        return text.strip() or None

    return await loop.run_in_executor(None, _call)
