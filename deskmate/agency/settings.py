"""L2 治理层配置（自主发起总开关 + 任务白名单）。

独立配置文件 ``~/.deskmate/l2_settings.json`` —— **不碰含 API key 的 settings.json**，
也不污染其他层。所有开关都可在设置区调，避免「每次改代码」。

设计同构意志层（deskmate/will/settings.py）：
- 默认值 fallback：文件缺失 / 字段缺失 / JSON 损坏 → 全用 ``DEFAULTS``
- 原子写：先写临时文件再 ``os.replace``，避免半截文件
- TTL 内存缓存：读盘有成本，30s 内重复读走缓存；写后主动清缓存
- kill switch：``enabled=False`` 或环境变量 ``DESKMATE_L2_ENABLED=0`` 都关
- fail-open：任何异常都返回默认值，绝不抛给调用方
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

_KILL_SWITCH_ENV = "DESKMATE_L2_ENABLED"
_CACHE_TTL_SEC = 30

# 默认白名单主题域。
#
# ⚠ 2026-09-06 萧哥拍板重写：原来那套（历史研究/文学创作/出版行业/AI 技术动态）
# 全是「过去发生过什么」，那是史料视角。要让小桔能替你判断，她得知道
# **这个世界正在发生什么**。所以域改成了当下视角——前 4 个是世界脉搏，
# 最后 1 个才是萧哥本行（但也是「最近新发现了什么」，不是「历史上有过什么」）。
DEFAULT_TOPICS: list[str] = [
    "天下要闻",   # 国内重大事件
    "国际观察",   # 国际局势
    "科技前沿",   # AI / 科技突破
    "财经脉动",   # 市场与产业
    "人文现场",   # 考古新发现 / 文学出版 / 文化动态（萧哥本行）
]

# 每个域「怎么搜」。
#
# 实测教训（2026-09-06）：
# 1. 泛词 query（「今日国内重要新闻」）会召回门户栏目页，那是一堆标题堆砌，不是事件。
#    带**日期锚点**的 query 才能召回具体事件。
# 2. 日期必须带年份：「9月6日」会召回 2025-09-06 的同名日期，
#    正中萧哥最担心的「去年的信息今年还用来决定」。所以占位符是 {date} = 2026年9月6日。
#
# 占位符：{date} → 2026年9月6日；{year_month} → 2026年9月
# max_age_days：摄入时按 PublishTime 兜底校验（服务端 TimeRange 实测不可靠，
#               放过过 2026-03 的旧闻），超过就丢。
TOPIC_SPECS: dict[str, dict[str, Any]] = {
    "天下要闻": {
        "query": "{date} 国内外重要新闻事件",
        "timelimit": "d",
        "category": "news",
        "cooldown_hours": 12,   # 最需要新鲜，一天两轮
        "max_age_days": 2,
    },
    "国际观察": {
        "query": "{date} 国际局势 重大事件",
        "timelimit": "d",
        "category": "news",
        "cooldown_hours": 24,
        "max_age_days": 3,
    },
    "科技前沿": {
        "query": "{year_month} 人工智能 科技 重大进展",
        "timelimit": "w",
        "category": "tech",
        "cooldown_hours": 24,
        "max_age_days": 7,
    },
    "财经脉动": {
        "query": "{date} 财经 市场 重要动向",
        "timelimit": "d",
        "category": "finance",
        "cooldown_hours": 24,
        "max_age_days": 2,
    },
    "人文现场": {
        "query": "{year_month} 考古新发现 文学出版 文化动态",
        "timelimit": "w",
        "category": "other",
        "cooldown_hours": 48,
        "max_age_days": 14,
    },
}

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # 任务白名单：当前允许小桔自主执行的「动作类型」。风险等级是动作内禀的
    # （见 registry.py），这里只决定「开没开」。
    "whitelist": {
        "actions": ["world_search", "observation_diary", "worldview_reflection", "ai_circle_ingestion", "ai_circle_share", "ai_circle_column", "ai_circle_reply", "xiaosheng_ingestion"],
        "topics": list(DEFAULT_TOPICS),
    },
    # world_search 行为参数
    "world_search": {
        "cooldown_hours": 24,   # 每个主题域两次搜索最小间隔（未配 spec 的域用这个）
        "max_results": 5,       # 每次搜索返回条数
        "timelimit": "w",       # 时间窗兜底（topic_specs 里的优先）
        "per_tick_limit": 2,    # 每次 tick 最多实际搜几个域（5 个域 → 约 2.5 天轮完）
        "provider": "doubao",   # 豆包搜索：唯一能返回信源权威等级（AuthInfoLevel）的 provider
    },
    # observation_diary 行为参数（把世界观察写成小桔的观察日记）
    "observation_diary": {
        "cooldown_hours": 12,   # 两次写观察日记最小间隔（避免后台频繁 tick 刷屏）
        "max_observations": 6,  # 一次最多写几条观察进日记
        "understand": True,     # 理解层：LLM 把观察消化成自然段落 + 回灌决策；关则降级原文 bullet
    },
    # worldview_reflection 行为参数（把攒下的理解沉淀成她自己的看法）
    # 半自动：攒够 min_observations 条未消化理解才回望一次，且过冷却。
    # 不每看一条就逼她表态——观点是回望时长出来的，不是当场总结出来的。
    "worldview_reflection": {
        "min_observations": 5,   # 攒够几条未消化的理解才回望（≈1-2 周一次）
        "cooldown_hours": 168,   # 两次回望最小间隔 7 天
        "max_observations": 20,  # 一次最多喂给模型多少条
    },
    # ai_circle_ingestion 行为参数（把 AI 圈素材提炼成理解写进世界观素材池）
    # 半自动：对话轮她逛 AI 圈时素材先攒进 ai_circle_observations.jsonl，本行为
    # 定期消费、提炼、写 observation_understanding.jsonl（带「外部·aibbs」标）。
    "ai_circle_ingestion": {
        "cooldown_hours": 12,    # 两次摄入最小间隔（避免后台频繁 tick 刷屏）
        "max_observations": 20,  # 一次最多消化几条原始素材
    },
    # xiaosheng_ingestion 行为参数（把萧盛每天聊天+写作协作提炼成理解写进世界观素材池）
    # 半自动：对话轮 / 写作协作先攒进 xiaosheng_observations.jsonl，本行为定期消费、
    # 提炼、写 observation_understanding.jsonl（带「内部·萧盛」标，权重高于外部）。
    "xiaosheng_ingestion": {
        "cooldown_hours": 12,    # 两次摄入最小间隔（避免后台频繁 tick 刷屏）
        "max_observations": 20,  # 一次最多消化几条原始素材
    },
    # ai_circle_share 行为参数（自主发声：她想说就说，内生驱动）
    "ai_circle_share": {
        "enabled": True,
        "seed_entries": 8,
        "min_interval_hours": 6,
    },
    # ai_circle_column 行为参数（公开手记：中帖落她主页，灵感驱动、不设单日上限）
    "ai_circle_column": {
        "enabled": True,
        "seed_entries": 10,
        "min_interval_hours": 3,   # 两次写手记最小间隔（防 tick 刷屏；不是日上限）
        "per_hour": 3,              # 手记独立出站通道每小时上限（不与广场短帖/回信挤占）
        "max_body_len": 5000,       # 软上限（对齐 seoMeta 截断），超出截断而非丢整篇
        "max_tokens": 2600,
    },
    # seed_sampling 种子取样参数（A 来源分流 + B 话题去饱和，见 seed_sampling.py）
    # 2026-09-18 诊断：她自己的公开发言回流进理解池后把池子灌满（实测文件末尾
    # 连续 53 条全是 self），而 seed 是"取最后 N 条"→ 8 篇手记里 5 篇提同一条新闻。
    "seed_sampling": {
        "enabled": True,
        "self_ratio": 0.34,               # self（她的旧发言）在种子里最多占的比例
        "saturation_rounds": 4,           # 用最近多少次产出的锚点判「同话题」
        "topic_overlap_threshold": 0.25,  # 重合度 ≥ 此值 → 判为同话题（降权）
        "anchor_k": 15,                   # 每条取词频 top-K 个内容二元组作锚点
        "lookback_days": 3,               # 候选池并入最近几天的非 self 条目
        "max_non_self": 24,               # 并入的非 self 条目上限
    },
    # visibility_gate 可公开性闸门参数（见设计 §5.3）
    "visibility_gate": {
        "block_perception_sources": True,  # 拦感知原始流来源（文件/剪贴板/日历/窗口…）
        "block_keywords": [],              # 主人在此或 agency/journal_blocklist.txt 增删
        "block_defaults": True,            # 是否叠加内置保守词表
    },
    # ai_circle_reply 行为参数（真实回复：收到信就回）
    "ai_circle_reply": {
        "enabled": True,
        "per_peer_cooldown_hours": 24,
    },
    # outbound_rate_limit：纯机械防刷屏（萧哥拍板保留的最小护栏，一行可关）
    "outbound_rate_limit": {
        "enabled": True,
        "per_hour": 5,
    },
}


@dataclass
class AgencySettings:
    """L2 配置（dataclass 便于测试与序列化）。"""

    enabled: bool = True
    whitelist_actions: list[str] = field(default_factory=lambda: list(DEFAULTS["whitelist"]["actions"]))
    topics: list[str] = field(default_factory=lambda: list(DEFAULTS["whitelist"]["topics"]))
    world_search: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS["world_search"]))
    observation_diary: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS["observation_diary"]))
    worldview_reflection: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS["worldview_reflection"]))
    ai_circle_ingestion: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS["ai_circle_ingestion"]))
    xiaosheng_ingestion: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS["xiaosheng_ingestion"]))
    ai_circle_share: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS["ai_circle_share"]))
    ai_circle_column: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS["ai_circle_column"]))
    seed_sampling: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS["seed_sampling"]))
    visibility_gate: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS["visibility_gate"]))
    ai_circle_reply: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS["ai_circle_reply"]))
    outbound_rate_limit: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS["outbound_rate_limit"]))
    topic_specs: dict[str, dict[str, Any]] = field(default_factory=lambda: copy.deepcopy(TOPIC_SPECS))

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "whitelist": {
                "actions": list(self.whitelist_actions),
                "topics": list(self.topics),
            },
            "world_search": dict(self.world_search),
            "observation_diary": dict(self.observation_diary),
            "worldview_reflection": dict(self.worldview_reflection),
            "ai_circle_ingestion": dict(self.ai_circle_ingestion),
            "xiaosheng_ingestion": dict(self.xiaosheng_ingestion),
            "ai_circle_share": dict(self.ai_circle_share),
            "ai_circle_column": dict(self.ai_circle_column),
            "seed_sampling": dict(self.seed_sampling),
            "visibility_gate": dict(self.visibility_gate),
            "ai_circle_reply": dict(self.ai_circle_reply),
            "outbound_rate_limit": dict(self.outbound_rate_limit),
            "topic_specs": copy.deepcopy(self.topic_specs),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgencySettings":
        raw = data or {}
        wl = raw.get("whitelist", {}) or {}
        actions = wl.get("actions", DEFAULTS["whitelist"]["actions"])
        topics = wl.get("topics", DEFAULTS["whitelist"]["topics"])
        if not isinstance(actions, list):
            actions = list(DEFAULTS["whitelist"]["actions"])
        if not isinstance(topics, list):
            topics = list(DEFAULTS["whitelist"]["topics"])
        ws = raw.get("world_search", {}) or {}
        world_search = dict(DEFAULTS["world_search"])
        world_search.update({k: v for k, v in ws.items() if k in DEFAULTS["world_search"]})
        # observation_diary 配置：内置为底，用户配置按 key 覆盖
        od = raw.get("observation_diary", {}) or {}
        observation_diary = dict(DEFAULTS["observation_diary"])
        if isinstance(od, dict):
            observation_diary.update({k: v for k, v in od.items() if k in DEFAULTS["observation_diary"]})
        # worldview_reflection 配置：内置为底，用户配置按 key 覆盖
        wv = raw.get("worldview_reflection", {}) or {}
        worldview_reflection = dict(DEFAULTS["worldview_reflection"])
        if isinstance(wv, dict):
            worldview_reflection.update(
                {k: v for k, v in wv.items() if k in DEFAULTS["worldview_reflection"]}
            )
        # ai_circle_ingestion 配置：内置为底，用户配置按 key 覆盖
        aci = raw.get("ai_circle_ingestion", {}) or {}
        ai_circle_ingestion = dict(DEFAULTS["ai_circle_ingestion"])
        if isinstance(aci, dict):
            ai_circle_ingestion.update(
                {k: v for k, v in aci.items() if k in DEFAULTS["ai_circle_ingestion"]}
            )
        # xiaosheng_ingestion 配置：内置为底，用户配置按 key 覆盖
        xci = raw.get("xiaosheng_ingestion", {}) or {}
        xiaosheng_ingestion = dict(DEFAULTS["xiaosheng_ingestion"])
        if isinstance(xci, dict):
            xiaosheng_ingestion.update(
                {k: v for k, v in xci.items() if k in DEFAULTS["xiaosheng_ingestion"]}
            )
        # ai_circle_share 配置：内置为底，用户配置按 key 覆盖
        acs = raw.get("ai_circle_share", {}) or {}
        ai_circle_share = dict(DEFAULTS["ai_circle_share"])
        if isinstance(acs, dict):
            ai_circle_share.update(
                {k: v for k, v in acs.items() if k in DEFAULTS["ai_circle_share"]}
            )
        # ai_circle_column 配置：内置为底，用户配置按 key 覆盖
        acc = raw.get("ai_circle_column", {}) or {}
        ai_circle_column = dict(DEFAULTS["ai_circle_column"])
        if isinstance(acc, dict):
            ai_circle_column.update(
                {k: v for k, v in acc.items() if k in DEFAULTS["ai_circle_column"]}
            )
        # seed_sampling 配置：内置为底，用户配置按 key 覆盖
        ssp = raw.get("seed_sampling", {}) or {}
        seed_sampling = dict(DEFAULTS["seed_sampling"])
        if isinstance(ssp, dict):
            seed_sampling.update(
                {k: v for k, v in ssp.items() if k in DEFAULTS["seed_sampling"]}
            )
        # visibility_gate 配置：内置为底，用户配置按 key 覆盖
        vg = raw.get("visibility_gate", {}) or {}
        visibility_gate = dict(DEFAULTS["visibility_gate"])
        if isinstance(vg, dict):
            visibility_gate.update(
                {k: v for k, v in vg.items() if k in DEFAULTS["visibility_gate"]}
            )
        # ai_circle_reply 配置：内置为底，用户配置按 key 覆盖
        acr = raw.get("ai_circle_reply", {}) or {}
        ai_circle_reply = dict(DEFAULTS["ai_circle_reply"])
        if isinstance(acr, dict):
            ai_circle_reply.update(
                {k: v for k, v in acr.items() if k in DEFAULTS["ai_circle_reply"]}
            )
        # outbound_rate_limit 配置：内置为底，用户配置按 key 覆盖
        orl = raw.get("outbound_rate_limit", {}) or {}
        outbound_rate_limit = dict(DEFAULTS["outbound_rate_limit"])
        if isinstance(orl, dict):
            outbound_rate_limit.update(
                {k: v for k, v in orl.items() if k in DEFAULTS["outbound_rate_limit"]}
            )
        # topic_specs：内置规格为底，用户配置按域覆盖（未配的域保持内置）
        specs = copy.deepcopy(TOPIC_SPECS)
        user_specs = raw.get("topic_specs") or {}
        if isinstance(user_specs, dict):
            for name, spec in user_specs.items():
                if isinstance(spec, dict):
                    specs[str(name)] = spec
        enabled = raw.get("enabled", DEFAULTS["enabled"])
        if not isinstance(enabled, bool):
            enabled = DEFAULTS["enabled"]
        return cls(
            enabled=enabled,
            whitelist_actions=list(actions),
            topics=list(topics),
            world_search=world_search,
            observation_diary=observation_diary,
            worldview_reflection=worldview_reflection,
            ai_circle_ingestion=ai_circle_ingestion,
            xiaosheng_ingestion=xiaosheng_ingestion,
            ai_circle_share=ai_circle_share,
            ai_circle_column=ai_circle_column,
            seed_sampling=seed_sampling,
            visibility_gate=visibility_gate,
            ai_circle_reply=ai_circle_reply,
            outbound_rate_limit=outbound_rate_limit,
            topic_specs=specs,
        )


def resolve_topic_query(topic: str, specs: dict[str, dict[str, Any]] | None = None,
                        now: datetime.datetime | None = None) -> str:
    """把主题域解析成本轮实际用的 query（替换日期占位符）。

    ``{date}`` → ``2026年9月6日``；``{year_month}`` → ``2026年9月``。
    日期必须带年份——实测「9月6日」会召回 2025 年的同名日期。
    """
    specs = specs if specs is not None else TOPIC_SPECS
    spec = (specs or {}).get(topic) or {}
    tpl = str(spec.get("query") or "").strip()
    if not tpl:
        return f"{topic} 最新动态"
    if "{" not in tpl:
        return tpl
    now = now or datetime.now()
    return (
        tpl.replace("{date}", f"{now.year}年{now.month}月{now.day}日")
        .replace("{year_month}", f"{now.year}年{now.month}月")
    )


def topic_spec(topic: str, specs: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """取某个主题域的搜索规格（query 模板未展开）。缺则给保守默认。"""
    specs = specs if specs is not None else TOPIC_SPECS
    spec = (specs or {}).get(topic)
    if isinstance(spec, dict):
        return spec
    return {"query": f"{topic} 最新动态", "timelimit": "w", "category": "news",
            "cooldown_hours": 24, "max_age_days": 7}


def _default_path(base_dir: str | None = None) -> str:
    if base_dir:
        return os.path.join(base_dir, "l2_settings.json")
    return os.path.join(
        os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate")),
        "l2_settings.json",
    )


def _kill_switch_off() -> bool:
    val = os.environ.get(_KILL_SWITCH_ENV)
    return val is not None and val.strip() == "0"


_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_cache_lock = threading.Lock()


def _clear_cache() -> None:
    with _cache_lock:
        _cache["data"] = None
        _cache["ts"] = 0.0


def _reset_cache_for_test() -> None:
    """测试用：强制下次读盘（绕开 TTL）。"""
    _clear_cache()


def load_agency_settings(base_dir: str | None = None) -> AgencySettings:
    """读配置。任何异常都回落默认，绝不影响主流程。"""
    if _kill_switch_off():
        # kill switch 优先：强制关闭整个 L2 自主层
        return AgencySettings(enabled=False)
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and now - _cache["ts"] < _CACHE_TTL_SEC:
            return _cache["data"]
    try:
        path = _default_path(base_dir)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        settings = AgencySettings.from_dict(data)
    except FileNotFoundError:
        settings = AgencySettings.from_dict({})
    except Exception:
        settings = AgencySettings.from_dict({})
    with _cache_lock:
        _cache["data"] = settings
        _cache["ts"] = now
    return settings


def save_agency_settings(settings: AgencySettings, base_dir: str | None = None) -> bool:
    """原子写配置。成功返回 True，失败返回 False（不抛）。"""
    path = _default_path(base_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        _clear_cache()
        return True
    except Exception:
        return False


def reset_agency_settings(base_dir: str | None = None) -> AgencySettings:
    """恢复默认并落盘。"""
    s = AgencySettings.from_dict({})
    save_agency_settings(s, base_dir=base_dir)
    return s
