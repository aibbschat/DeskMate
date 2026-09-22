"""8.5 画像的迁移与扩展：全局创作偏好跨项目自动注入。

核心：换新书/开新项目时，把用户在所有项目中一贯的创作偏好（全局风格画像 +
vault 偏好 + 查史料铁律）自动注入改编圣经上下文，让圆桌专家/审核/终审都能看到
——决定「开新书后 AI 还懂不懂你」。

- 数据源（全部只读，不修改源文件）：
  1. ``~/.deskmate/style_profile.json``（P1-5 全局风格画像：summary + features）
  2. ``~/.deskmate/vault/preferences/萧盛写作风格.md``（写作风格关键特征）
  3. ``~/.deskmate/vault/preferences/`` 最新 2 个偏好文件（用户最新偏好）
- 注入点：``CreativePipeline._novel_context("bible")`` 组装圣经上下文时，
  在 bible 与章节目录之间插入【全局创作偏好】段。
- 幂等：文本含 ``<!-- global-profile:v1 -->`` 标记即视为已注入，不重复。
- 零 LLM、fail-open：任一数据源缺失则跳过该部分，绝不阻断创作流程。
"""

from __future__ import annotations

import re
from pathlib import Path
from deskmate.paths import deskmate_base_dir

PROFILE_MARK = "<!-- global-profile:v1 -->"


def _desk_dir(base_dir: str | Path | None) -> Path:
    return Path(base_dir) if base_dir else deskmate_base_dir()


def _strip_frontmatter(text: str) -> str:
    """去掉 markdown 文件开头的 YAML frontmatter（---...---）。"""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            return text[end + 3 :].strip()
    return text.strip()


def _read_style_profile(desk: Path) -> dict:
    """读全局风格画像，返回 {summary, features} 字典（缺失返回空）。

    默认全局画像从统一库 deskmate.db 读；若 desk 下仍有 legacy 文件
    （自定义 base_dir 场景），回退读文件，兼容尚未迁移的环境。
    """
    import json

    if (desk / "style_profile.json").is_file():
        # 自定义 base_dir 下的 legacy 项目级画像 → 读文件
        try:
            data = json.loads((desk / "style_profile.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    else:
        from deskmate.style_profile_store import DEFAULT_PROFILE_PATH, load_profile as _load_style

        data = _load_style(DEFAULT_PROFILE_PATH) or {}
    summary = (
        data.get("summary")
        or (data.get("profile") or {}).get("summary")
        or ""
    )
    features = data.get("features") or {}
    return {"summary": str(summary).strip(), "features": features if isinstance(features, dict) else {}}


def _read_latest_preferences(desk: Path, limit: int = 2) -> list[tuple[str, str]]:
    """读 vault/preferences 下最新的 N 个偏好文件（排除写作风格专档），返回 [(日期, 内容)]。"""
    pref_dir = desk / "vault" / "preferences"
    if not pref_dir.is_dir():
        return []
    files = [
        f
        for f in pref_dir.glob("*.md")
        if f.is_file() and "写作风格" not in f.name
    ]
    # 按文件名日期排序（文件名以 YYYY-MM-DD 开头）
    files.sort(key=lambda f: f.name, reverse=True)
    out: list[tuple[str, str]] = []
    for f in files[:limit]:
        try:
            content = _strip_frontmatter(f.read_text(encoding="utf-8"))
            if content:
                # 取前 120 字，避免偏好原文过长污染圣经
                snippet = content[:120].rstrip() + ("…" if len(content) > 120 else "")
                date = f.stem[:10] if re.match(r"\d{4}-\d{2}-\d{2}", f.stem) else f.stem
                out.append((date, snippet))
        except OSError:
            continue
    return out


def _read_writing_style_note(desk: Path) -> str:
    """读「萧盛写作风格.md」专档，返回标题行（不展开全文，避免过长）。"""
    p = desk / "vault" / "preferences" / "萧盛写作风格.md"
    if not p.is_file():
        return ""
    try:
        text = _strip_frontmatter(p.read_text(encoding="utf-8"))
        # 取第一个 # 标题行 + 第一条特征（简洁引用）
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("# "):
                return line.lstrip("# ").strip()
    except OSError:
        pass
    return ""


def build_global_profile(base_dir: str | Path | None = None) -> str:
    """聚合全局创作偏好，返回一段 markdown 文本（含幂等标记）。

    无任何数据源时返回空串（调用方应跳过注入）。
    """
    desk = _desk_dir(base_dir)
    style = _read_style_profile(desk)
    writing_note = _read_writing_style_note(desk)
    latest_prefs = _read_latest_preferences(desk, limit=2)

    if not style.get("summary") and not writing_note and not latest_prefs:
        return ""

    lines: list[str] = [
        f"【全局创作偏好】{PROFILE_MARK}",
        "> 以下是你在所有项目中一贯的创作偏好（跨项目自动迁移，换新书也适用）：",
        "",
    ]

    if style.get("summary"):
        summary = style["summary"][:120] + ("…" if len(style["summary"]) > 120 else "")
        lines.append(f"- **文风画像**：{summary}")

    # features 取关键维度（句式/语域/节奏/用词偏好/禁忌）
    feat = style.get("features") or {}
    feat_labels = {
        "sentence_style": "句式",
        "register": "语域",
        "rhythm": "节奏",
        "word_preference": "用词偏好",
        "taboos": "禁忌",
    }
    feat_bits: list[str] = []
    for key, label in feat_labels.items():
        val = feat.get(key)
        if val:
            if isinstance(val, list):
                val = "、".join(str(v) for v in val[:3])
            feat_bits.append(f"{label}：{str(val)[:60]}")
    if feat_bits:
        lines.append(f"- **风格细节**：{'；'.join(feat_bits[:4])}")

    if writing_note:
        lines.append(f"- **写作风格专档**：{writing_note}（详见 vault/preferences/萧盛写作风格.md）")

    for date, snippet in latest_prefs:
        lines.append(f"- **最新偏好（{date}）**：{snippet}")

    lines.append("")
    return "\n".join(lines)


def has_profile(text: str) -> bool:
    """检查文本是否已注入全局画像（幂等判断）。"""
    return PROFILE_MARK in text


def inject_profile(context_text: str, profile: str) -> str:
    """把全局创作偏好注入圣经上下文。

    注入位置：在「【章节目录】」之前（bible 之后）。
    如果上下文已含标记或 profile 为空，则原样返回。
    """
    if not profile or has_profile(context_text):
        return context_text
    # 在【章节目录】前插入；如果没有章节目录标记，则追加到末尾
    marker = "【章节目录】"
    if marker in context_text:
        return context_text.replace(marker, f"{profile}\n{marker}", 1)
    return f"{context_text}\n\n{profile}"


def load_and_inject(context_text: str, base_dir: str | Path | None = None) -> str:
    """便捷方法：加载全局画像并注入上下文。"""
    profile = build_global_profile(base_dir)
    return inject_profile(context_text, profile)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "show":
        print(build_global_profile() or "（暂无全局画像数据）")
    else:
        print("用法: python -m deskmate.creation.global_profile show")
