"""可公开性闸门 —— 生成前按「来源 / 类型 + 私密词表」拦，不靠事后擦文本。

设计出处：《AI 圈 · 小桔手记展示通道设计》§5.3 / §8.1。

口径（2026-09-17 萧盛放开）：萧盛是作家、身份与创作基本公开，手记**可以**直呼
「萧盛 / 我的主人」、可以写他正在做的事、包括还没出版的书。所以闸门的硬线收敛到
只剩一条——**家人**。因此：
- 私密词表**默认为空**：家人姓名由主人在线维护 `~/.deskmate/agency/journal_blocklist.txt`
  （一行一词，`#` 注释）或 `visibility_gate.block_keywords`。没填＝不额外拦。
- 仍默认拦「感知原始流裸数据」（剪贴板 / 文件 / 日历 / 窗口 / 屏幕…）：这不是"萧盛的
  隐私化"，而是别把她替萧盛盯着桌面的原始遥测（他刚复制了什么、开了哪个文件）当手记素材
  抖出去。要连这条也放开，设 `visibility_gate.block_perception_sources=false`。

与别处 fail-open 不同——**这里判断不确定时倾向拦**（宁可少放，不误放）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# 感知原始流的来源/类型标——这些是"替萧盛盯桌面"的裸遥测，默认不进公开手记
# （见模块 docstring：可用 block_perception_sources=false 一并放开）。
_PERCEPTION_SOURCES: tuple[str, ...] = (
    "clipboard", "剪贴板",
    "file", "filesystem", "文件",
    "calendar", "日历", "日程",
    "window", "窗口",
    "focus", "专注",
    "screen", "截图", "屏幕",
)

# 内置默认私密词表：萧盛放开后**留空**——唯一的硬线"家人"由主人在文件 / 配置里列。
_DEFAULT_BLOCK_KEYWORDS: tuple[str, ...] = ()


def _blocklist_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "agency" / "journal_blocklist.txt"


def load_blocklist(base_dir: str | Path, cfg: Optional[dict] = None) -> list[str]:
    """合并「内置默认 + settings 配置项 + 主人维护的 txt 文件」成一张词表。

    fail-soft：任何读盘异常都不抛，退回到已有的部分。
    """
    cfg = cfg or {}
    kws: list[str] = []
    # 1) 内置（除非显式置 block_defaults=false 关掉）
    if cfg.get("block_defaults", True):
        kws.extend(_DEFAULT_BLOCK_KEYWORDS)
    # 2) settings.visibility_gate.block_keywords
    for k in (cfg.get("block_keywords") or []):
        s = str(k).strip()
        if s:
            kws.append(s)
    # 3) 主人在线维护的词表文件
    try:
        p = _blocklist_path(base_dir)
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    kws.append(s)
    except Exception as e:  # 读盘失败不影响，用已合并的部分
        logger.debug("visibility_gate: 读 blocklist 文件失败（忽略）: %s", e)
    # 去重保序
    return list(dict.fromkeys(kws))


def _hits(text: str, keywords: Iterable[str]) -> bool:
    """任一关键词作为子串命中即为真。"""
    if not text:
        return False
    for k in keywords:
        if k and k in text:
            return True
    return False


def _is_perception_source(rec: dict) -> bool:
    src = str(rec.get("source") or rec.get("kind") or rec.get("origin") or "")
    low = src.lower()
    return any(ps in low or ps in src for ps in _PERCEPTION_SOURCES)


def is_public_safe(rec: dict, keywords: list, *, block_perception: bool = True) -> bool:
    """判断一条候选素材是否可进公开手记种子。保守：来源感知流 / 命中私密词表 → 拦。

    Args:
        rec: 素材 dict，可能含 body / subject / title / peer / source / kind / origin。
        keywords: 私密词表（load_blocklist 的结果）。
        block_perception: 是否拦感知来源（关了就只按词表拦）。
    """
    try:
        if block_perception and _is_perception_source(rec):
            return False
        blob = " ".join(
            str(rec.get(k) or "") for k in ("body", "subject", "title", "peer", "text")
        )
        if _hits(blob, keywords):
            return False
        return True
    except Exception as e:  # 判断本身出错 → 保守拦
        logger.debug("visibility_gate: is_public_safe 异常，保守拦: %s", e)
        return False


def filter_public_safe(records: Iterable[dict], keywords: list, *, block_perception: bool = True) -> list[dict]:
    """过滤出一批素材里可公开的子集。fail-open：异常返回已过滤部分或空。"""
    out: list[dict] = []
    try:
        for rec in records:
            if isinstance(rec, dict) and is_public_safe(rec, keywords, block_perception=block_perception):
                out.append(rec)
    except Exception as e:
        logger.warning("visibility_gate: filter 异常（保守返回已收集）: %s", e)
    return out


def scrub_worldview(text: str, keywords: list) -> str:
    """世界观是她自己的判断字符串，但个别行可能带出"替他排书稿"这类私事立场。

    逐行拦命中词表的行（其余保留）。不做整段丢弃，尽量保住她的声音。
    """
    if not text:
        return ""
    kept: list[str] = []
    try:
        for line in text.splitlines():
            if _hits(line, keywords):
                continue
            kept.append(line)
    except Exception:
        # 出错时最保守：整段不要（宁可不带世界观，也不误带私密立场）
        return ""
    return "\n".join(kept).strip()
