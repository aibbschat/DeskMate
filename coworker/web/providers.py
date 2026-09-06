"""Web search providers — a keyless default + pluggable third-party services.

`duckduckgo` works with no API key (our "starting version of our own"). `tavily` and `brave`
give better results but need a key (configured via the SecretStore / env). All providers
return a uniform `list[SearchResult]`; the heavy client libs are lazy-imported.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

_TIMEOUT = 20.0

# API Key 的持久化落点（后台 tick 没有交互式 env，光靠环境变量不够）。
# 形如 {"doubao": "ark-xxx"}；文件权限 600。env 优先，文件兜底。
_KEY_FILE = "~/.deskmate/search_keys.json"


def _key_from_file(name: str) -> Optional[str]:
    """从 ~/.deskmate/search_keys.json 读 key。读不到就 None（不抛）。"""
    try:
        with open(os.path.expanduser(_KEY_FILE), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        v = data.get(name) or data.get(f"{name}_api_key")
        return str(v) if v else None
    except Exception:
        return None


def _is_aggregation_page(url: str, publish_time: str) -> bool:
    """是否为门户栏目页/首页（而非具体文章）。

    这类页面返回的是一堆标题堆砌（动辄 1400+ 字），喂给记忆等于灌水。
    判据任一成立即为聚合页：
      1. PublishTime 缺失或早于 2000 年 —— 1970-01-01 是 epoch 0，门户首页铁证
      2. URL 是目录 —— 末段既无文件扩展名也不含数字 id
    """
    if not publish_time or publish_time[:4] < "2000":
        return True
    try:
        from urllib.parse import urlparse

        path = (urlparse(url).path or "").rstrip("/")
    except Exception:
        return False
    if not path:
        return True
    last = path.split("/")[-1]
    if not last:
        return True
    # 具体文章页：末段有扩展名（.shtml/.html）或含数字 id
    return "." not in last and not any(ch.isdigit() for ch in last)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    # 以下为「权威度」扩展字段，目前只有 doubao 提供。
    # 其它 provider 留空；`to_dict` 不输出空字段，保证向后兼容。
    auth_level: Optional[int] = None  # 豆包搜索 AuthInfoLevel：1 非常权威 → 4 一般
    site_name: str = ""
    publish_time: str = ""

    def to_dict(self) -> dict:
        d = {"title": self.title, "url": self.url, "snippet": self.snippet}
        if self.auth_level is not None:
            d["auth_level"] = self.auth_level
            d["site_name"] = self.site_name
            d["publish_time"] = self.publish_time
        return d


class WebSearchProvider(ABC):
    name: str = "base"
    requires_key: bool = False

    @abstractmethod
    def search(
        self, query: str, max_results: int = 5, timelimit: Optional[str] = None
    ) -> list[SearchResult]: ...


class DuckDuckGoProvider(WebSearchProvider):
    """Keyless default via the `ddgs` library."""

    name = "duckduckgo"
    requires_key = False

    def search(
        self, query: str, max_results: int = 5, timelimit: Optional[str] = None
    ) -> list[SearchResult]:
        from ddgs import DDGS

        rows = DDGS().text(query, max_results=max_results, timelimit=timelimit) or []
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("href", "") or r.get("url", ""),
                snippet=r.get("body", "") or r.get("snippet", ""),
            )
            for r in rows
        ]


class TavilyProvider(WebSearchProvider):
    name = "tavily"
    requires_key = True

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def search(
        self, query: str, max_results: int = 5, timelimit: Optional[str] = None
    ) -> list[SearchResult]:
        import httpx

        resp = httpx.post(
            "https://api.tavily.com/search",
            json={"api_key": self.api_key, "query": query, "max_results": max_results},
            timeout=_TIMEOUT,
        )
        data = resp.json()
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content", ""),
            )
            for r in data.get("results", [])
        ]


class BraveProvider(WebSearchProvider):
    name = "brave"
    requires_key = True

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def search(
        self, query: str, max_results: int = 5, timelimit: Optional[str] = None
    ) -> list[SearchResult]:
        import httpx

        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "X-Subscription-Token": self.api_key,
                "Accept": "application/json",
            },
            params={"q": query, "count": max_results},
            timeout=_TIMEOUT,
        )
        data = resp.json()
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("description", ""),
            )
            for r in (data.get("web", {}) or {}).get("results", [])
        ]


class DoubaoSearchProvider(WebSearchProvider):
    """豆包搜索（火山方舟 Agent Plan / 独立 API Key）。

    和 Tavily/Brave 最大的不同：它**返回信源权威等级** ``AuthInfoLevel``（1 非常权威 →
    4 一般），所以外部信息的可信度不再是瞎猜的常量，而是有据可依。这是 deskmate
    外部信息「可信/不可信边界」的关键输入。

    ⚠ 用 `Summary` 而不是 `Snippet`：官方明确 Snippet「仅建议列表展示，强烈不建议
    用于大模型场景」。
    """

    name = "doubao"
    requires_key = True

    _ENDPOINT = "https://open.feedcoopapi.com/search_api/web_search"
    # ddgs 风格的 timelimit → 豆包 Filter.TimeRange
    _TIMELIMIT_MAP = {"d": "OneDay", "w": "OneWeek", "m": "OneMonth", "y": "OneYear"}
    # 屏蔽 UGC 噪音：这些内容没有编辑把关，进不可信区只是浪费裁决时间
    _BLOCK_HOSTS = "weibo.com|zhihu.com|tieba.baidu.com|xiaohongshu.com|douyin.com"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def search(
        self, query: str, max_results: int = 5, timelimit: Optional[str] = None
    ) -> list[SearchResult]:
        import httpx

        body: dict = {
            "Query": (query or "")[:100],  # 官方限制 1~100 字符
            "SearchType": "web",
            "Count": max(1, min(int(max_results), 50)),
            "NeedSummary": True,
        }
        filt: dict = {"BlockHosts": self._BLOCK_HOSTS}
        if timelimit:
            tr = self._TIMELIMIT_MAP.get(str(timelimit).lower())
            if tr:
                filt["TimeRange"] = tr
        body["Filter"] = filt

        resp = httpx.post(
            self._ENDPOINT,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            json=body,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        rows = ((data.get("Result") or {}).get("WebResults")) or []
        out: list[SearchResult] = []
        for r in rows:
            url = r.get("Url", "") or ""
            pub = r.get("PublishTime", "") or ""
            # 挡掉门户栏目页/首页：它们返回的是一堆标题堆砌，不是具体事件。
            # 铁证是 PublishTime 缺失或为 1970-01-01（epoch 0）。
            if _is_aggregation_page(url, pub):
                continue
            out.append(self._to_result(r))
        if not out and rows:
            # 整批都被判成聚合页 → 降级保留 2 条，宁可少也别空手而归
            logging.getLogger(__name__).warning(
                "doubao search: %d 条结果全被判为聚合页（query=%r），降级保留 2 条",
                len(rows), query,
            )
            out = [self._to_result(r) for r in rows[:2]]
        return out

    @staticmethod
    def _to_result(r: dict) -> "SearchResult":
        level = r.get("AuthInfoLevel")
        return SearchResult(
            title=r.get("Title", "") or "",
            url=r.get("Url", "") or "",
            # Summary 优先（适合大模型），退回 Snippet，再退回 Content
            snippet=r.get("Summary") or r.get("Snippet") or r.get("Content") or "",
            auth_level=int(level) if isinstance(level, (int, float)) else None,
            site_name=r.get("SiteName", "") or "",
            publish_time=r.get("PublishTime", "") or "",
        )


_PROVIDERS = {
    "duckduckgo": DuckDuckGoProvider,
    "tavily": TavilyProvider,
    "brave": BraveProvider,
    "doubao": DoubaoSearchProvider,
}


def build_provider(name: str, api_key: Optional[str] = None) -> WebSearchProvider:
    cls = _PROVIDERS.get(name, DuckDuckGoProvider)
    if cls.requires_key:
        # 解析顺序：显式传参 → 环境变量 <NAME>_API_KEY → 本地 key 文件
        key = (
            api_key
            or os.environ.get(f"{name.upper()}_API_KEY")
            or _key_from_file(name)
        )
        if not key:
            raise ValueError(f"web search provider '{name}' needs an API key")
        return cls(key)  # type: ignore[call-arg]
    return cls()  # type: ignore[call-arg]


def provider_names() -> list[str]:
    return list(_PROVIDERS)
