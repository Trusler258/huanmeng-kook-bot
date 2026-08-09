"""
多功能本地搜索工具 + 页面抓取
- 必应 + 百度百科 两数据源（并行搜索）
- PageScraper 深度页面正文提取（readability-lxml）
- 适配 CALL 调用 + 深度读取流程
"""

from __future__ import annotations

import time
import concurrent.futures as cf
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

import requests
from bs4 import BeautifulSoup

from core.logger import get_logger

logger = get_logger("local_search")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class LocalMultiSearch:
    def __init__(self, default_limit: int = 4, timeout: int = 5, request_delay: float = 0.3):
        self.default_limit = default_limit
        self.timeout = timeout
        self.request_delay = request_delay
        logger.info("本地多源搜索器已初始化: limit=%d timeout=%ds delay=%.1fs",
                    default_limit, timeout, request_delay)

    def _search_one(self, query: str, source: str, limit: int) -> str:
        """单个数据源搜索（线程安全）"""
        time.sleep(self.request_delay)  # 每个源的延时，防止同一目标短时间大量请求
        try:
            if source == "bing":
                return self._search_bing(query, limit)
            elif source == "baidu":
                return self._search_baidu_web(query, limit)
            elif source == "baike":
                return self._search_baike(query, limit)
        except Exception as e:
            logger.warning("%s搜索失败: %s", source, e)
        return ""

    def _format_entry(self, idx: int, title: str, snippet: str, link: str) -> str:
        """精简格式化：标题 + 摘要(≤150字) + URL"""
        short = snippet[:150].replace("\n", " ") if snippet else ""
        return f"{idx}. {title}\n   {short}...\n   {link}"

    def _search_bing(self, query: str, limit: int) -> str:
        params = {"q": query, "mkt": "zh-CN", "setlang": "zh", "count": limit}
        resp = requests.get("https://cn.bing.com/search", headers=_HEADERS, params=params, timeout=self.timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.find_all("li", class_="b_algo")
        if not items:
            return ""
        output = []
        for i, item in enumerate(items):
            if i >= limit:
                break
            h2 = item.find("h2")
            if not h2 or not h2.a:
                continue
            title = h2.get_text(strip=True)
            link = h2.a["href"]
            p = item.find("p")
            snippet = p.get_text(strip=True) if p else ""
            output.append(self._format_entry(i+1, title, snippet, link))
        return "\n".join(output)

    def _search_baidu_web(self, query: str, limit: int) -> str:
        params = {"wd": query, "rn": limit}
        resp = requests.get("https://www.baidu.com/s", headers=_HEADERS, params=params, timeout=self.timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.find_all("div", class_="result-op c-container xpath-log")
        if not items:
            items = soup.find_all("div", class_="c-container")
        if not items:
            return ""
        output = []
        for i, item in enumerate(items):
            if i >= limit:
                break
            h3 = item.find("h3")
            if not h3 or not h3.a:
                continue
            title = h3.get_text(strip=True)
            link = h3.a["href"]
            div_snip = item.find("div", class_="c-abstract")
            snippet = div_snip.get_text(strip=True) if div_snip else ""
            output.append(self._format_entry(i+1, title, snippet, link))
        return "\n".join(output)

    def _search_baike(self, query: str, limit: int) -> str:
        """百度百科 — 直接尝试访问词条页面"""
        # 简化：直接构造词条 URL 访问
        # 把 query 里的空格替换成 %20 (URL 编码)
        from urllib.parse import quote
        candidates = [
            f"https://baike.baidu.com/item/{quote(query)}",
            # 去掉"详细参数"等修饰词,只保留主体
            f"https://baike.baidu.com/item/{quote(query.split(' ')[0])}",
        ]

        for url in candidates:
            try:
                resp = requests.get(url, headers=_HEADERS, timeout=self.timeout, allow_redirects=True)
                if resp.status_code != 200:
                    continue
                # 检查是否真的到了词条页(不是搜索页/404)
                if "/item/" not in resp.url:
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")
                title = soup.title.string.strip() if soup.title and soup.title.string else query
                # 取摘要段 (多种 class 兼容)
                summary = soup.find("div", class_="lemma-summary") or \
                          soup.find("div", class_="para") or \
                          soup.find("div", class_="J-lemma-content")
                snippet = summary.get_text(strip=True)[:200] if summary else ""
                if snippet:  # 有内容才算成功
                    return self._format_entry(1, title, snippet, resp.url)
            except Exception as e:
                logger.debug("baike 尝试失败 [%s]: %s", url[:60], e)
                continue
        return ""

    def search_all(self, query: str, limit: int = None) -> str:
        """单源搜索（bing），5s 超时"""
        limit = limit if limit is not None else self.default_limit
        result = self._search_one(query, "bing", limit)
        if not result:
            return ""
        return f"===== 必应 =====\n{result}"

    def run_search(self, query: str, source: Literal["bing", "baidu", "baike", "all"] = "all", limit: int = None) -> str:
        if source == "all":
            return self.search_all(query, limit)
        return self._search_one(query, source, limit or self.default_limit)


# ════════════════════════════════════════════════════════════
#  PageScraper: 单页正文提取
# ════════════════════════════════════════════════════════════

class PageScraper:
    def __init__(self, timeout: int = 12, request_delay: float = 0.3):
        self.timeout = timeout
        self.request_delay = request_delay

    def scrape(self, url: str, max_chars: int = 6000) -> str | None:
        """抓取网页正文，返回纯文本。失败返回 None"""
        time.sleep(self.request_delay)
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=self.timeout, allow_redirects=True)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            html = resp.text
        except Exception as e:
            logger.warning("页面请求失败 [%s]: %s", url[:60], e)
            return None

        # 优先用 readability 提取正文（自动去除导航/广告/侧边栏）
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
            logger.debug("readability 未安装，回退 bs4 提取")
            soup = BeautifulSoup(html, "html.parser")
            title = soup.title.string.strip() if soup.title and soup.title.string else ""
            for tag in soup(["script", "style", "iframe", "noscript", "nav", "footer", "header", "aside"]):
                tag.decompose()
            # 优先取 article / main 标签（比 body 更精准）
            main = soup.find("article") or soup.find("main") or soup.find("body") or soup
            text = main.get_text(strip=True, separator="\n")
        except Exception as e:
            logger.warning("readability 提取失败 [%s]: %s", url[:60], e)
            soup = BeautifulSoup(html, "html.parser")
            title = soup.title.string.strip() if soup.title and soup.title.string else ""
            main = soup.find("article") or soup.find("main") or soup.find("body") or soup
            text = main.get_text(strip=True, separator="\n")

        # 压缩连续空行
        import re
        text = re.sub(r'\n{3,}', '\n\n', text)

        # 智能截断：保留段落完整性，不要在句中截断
        if len(text) > max_chars:
            # 找到最后一个完整句子的结束位置
            cut = text[:max_chars]
            # 尝试在句号/问号/感叹号后截断
            for end_mark in ['。', '？', '！', '.', '?', '!', '\n']:
                last_end = cut.rfind(end_mark)
                if last_end > max_chars * 0.7:
                    cut = cut[:last_end + 1]
                    break
            text = cut + "\n...(内容已截断)"

        result = f"【{title}】\n{text}"
        logger.info("页面提取完成: %s (%d字)", url[:50], len(result))
        return result


# 全局单例
_searcher: LocalMultiSearch | None = None
_scraper: PageScraper | None = None


def get_searcher() -> LocalMultiSearch:
    global _searcher
    if _searcher is None:
        _searcher = LocalMultiSearch(default_limit=4, timeout=5, request_delay=0.3)
    return _searcher


def get_scraper() -> PageScraper:
    global _scraper
    if _scraper is None:
        _scraper = PageScraper(timeout=12, request_delay=0.3)
    return _scraper
