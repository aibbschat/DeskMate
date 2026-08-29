"""萧哥（萧盛）画像 fixture —— 评测集用的确定性样本，不依赖真机数据。

所有内容基于公开可查的萧哥画像（历史小说作家、宁波、70 后、考据严谨等），
写入临时 base_dir，评测跑完即弃，绝不触碰真机 ~/.deskmate 与神圣稿件。

fixture 覆盖五类记忆源，与 benchmark_dataset.json 的 retrieval 字段一一对应：
  - USER.md                         -> load_user
  - MEMORY.md (§ 分节)             -> search_global / prefetch
  - projects/<name>/*.md           -> search_project
  - vault/<category>/*.md          -> search_vault
  - bible/<name>/.deskmate/*.md    -> novel_recall
"""

from __future__ import annotations

from pathlib import Path

from deskmate.memory.user_profile import save_user

# ---------------------------------------------------------------------------
# USER.md —— 用户画像（章节化 markdown）
# ---------------------------------------------------------------------------
USER_MD = """\
# 用户画像

## 基本信息
- 姓名：萧盛（笔名），本名不公开
- 籍贯：浙江宁波
- 出生年代：70 后
- 职业：历史小说作家、自由撰稿人
- 创办：中国写手之家网站
- 个人网站：xiaosheng.net
- 荣誉：2025 年第十一届当当年度影响力作家（获奖作品《刺明：逃生游戏》）

## 写作风格
- 考据严谨：所有史料必须标明出处（正史、实录、方志、档案、学术论文）
- 叙事视角：多用第三人称限知视角
- 语言：文白交融，文言比例适中，拒绝戏说与网文爽文节奏
- 描写偏好：擅用器物、礼仪、服饰细节还原历史现场
- 结构：重史实逻辑链，人物命运紧扣时代矛盾

## 偏好
- 史料来源只接受正史、实录、方志、档案、学术论文；拒绝自媒体文章与网络传言
- 回复要简洁直接，先给结论再给证据，善用 Markdown 表格
- 写作与史料核查都要有具体出处，不接受无来源断言

## 关注领域
- 中国古代史（明、宋、唐为重）、世界史、妖怪文化

## 需要 AI 避免的
- 不要编造史料或人物
- 不要套用现代价值观评判古人
- 不要过度抒情、文艺腔堆砌
- 不要用网文爽文节奏写历史
- 不要无出处地断言史实
"""

# ---------------------------------------------------------------------------
# MEMORY.md —— 全局记忆（§ 分节 + #标签 + 日期前缀）
# ---------------------------------------------------------------------------
MEMORY_MD = """\
§ 喜好 #preference
2026-08-01: 史料只引正史/实录/方志/档案，排除自媒体来源
2026-08-01: 回复要简洁直接、带证据与表格

§ 纠正 #correction
2026-08-05: 不要用爽文节奏写历史小说
2026-08-10: 史料断言必须给具体出处，不能泛泛而谈

§ 工作 #work
2026-08-15: 新书转向宋朝题材调研
2026-08-20: 《国子监秘闻录》进入终审阶段
"""

# ---------------------------------------------------------------------------
# 项目记忆：projects/国子监秘闻录/
# ---------------------------------------------------------------------------
PROJECT_LONGTERM = """\
# 国子监秘闻录 · 长期记忆（已蒸馏，常驻）
- 题材：明代国子监背景的历史悬疑
- 核心设定：以成化年间国子监祭酒被毒杀案为引
- 主角：寒门举子沈砚（虚构）
- 已定稿：第一至二十章
- 史料铁律：所有明代官制、礼仪须以《明会典》《大明律》为据
"""

PROJECT_JOURNAL = """\
# 国子监秘闻录 · 写作日志

---

2026-08-20: 终审反馈——第三章服饰描写被指与成化年间不符，已修订

---

2026-08-22: 新增角色：国子监司业王恕（原型参考历史人物）
"""

# ---------------------------------------------------------------------------
# Vault 长期知识（带 YAML frontmatter，分类目录）
# ---------------------------------------------------------------------------
VAULT_PREF_REPLY = """\
---
type: preference
tags: [回复风格]
created: 2026-08-01
updated: 2026-08-01
source: session
hash: bench-reply
---
萧哥偏好简洁直接的回复，先结论后证据，并用 Markdown 表格呈现对比。
"""

VAULT_KNOWLEDGE_GUUOZIJIAN = """\
---
type: note
tags: [明代, 官制]
created: 2026-08-10
updated: 2026-08-10
source: session
hash: bench-guozijian
---
明代国子监初设于南京（洪武十五年），永乐年间北京另设国子监，形成南监北监并立；正统年间以北监为太学正统。国子监属明代官制体系下的中央官学，祭酒为最高学官。
"""

VAULT_CORRECTION_FABRICATE = """\
---
type: correction
tags: [史料]
created: 2026-08-05
updated: 2026-08-05
source: session
hash: bench-fabricate
---
萧哥明确纠正：任何史料与人物不得编造，必须可考。
"""

# ---------------------------------------------------------------------------
# 圣经（legacy markdown，放在 folder/.deskmate/ 下，read_bible 回退读取）
# ---------------------------------------------------------------------------
BIBLE_CHARACTERS = """\
# 人物圣经
- 沈砚：寒门举子，成化年间入国子监，本书主角
- 王恕：国子监司业，原型参考明中期名臣
- 周洪谟：国子监祭酒，被毒杀案受害者
"""

BIBLE_OUTLINE = """\
# 大纲
- 引：成化年间国子监祭酒暴亡
- 第一至二十章：沈砚查案，牵出官制黑幕
"""

BIBLE_TIMELINE = """\
# 时间线
- 洪武十五年：国子监初设南京
- 成化元年：沈砚入监
- 成化三年：祭酒周洪谟暴亡
"""

BIBLE_NOTES = """\
# 史料笔记
- 国子监生员常服为襕衫，成化前无补子官服之制
- 明代南北监并立，北监后为正统太学
"""


def build_xiaosheng_fixture(base_dir: Path) -> Path:
    """在 base_dir 下构造完整的萧哥画像样本。

    Returns:
        构造好的 base_dir（可直接传给 DeskMateMemoryManager）。
    """
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    # USER.md + MEMORY.md
    save_user(base_dir, USER_MD)
    (base_dir / "MEMORY.md").write_text(MEMORY_MD, encoding="utf-8")

    # 项目记忆
    proj_dir = base_dir / "projects" / "国子监秘闻录"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "longterm.md").write_text(PROJECT_LONGTERM, encoding="utf-8")
    (proj_dir / "journal.md").write_text(PROJECT_JOURNAL, encoding="utf-8")

    # Vault（分类目录，VaultStore.__init__ 也会建，这里直接写内容）
    (base_dir / "vault" / "preferences").mkdir(parents=True, exist_ok=True)
    (base_dir / "vault" / "knowledge").mkdir(parents=True, exist_ok=True)
    (base_dir / "vault" / "corrections").mkdir(parents=True, exist_ok=True)
    (base_dir / "vault" / "preferences" / "简洁直接回复.md").write_text(
        VAULT_PREF_REPLY, encoding="utf-8"
    )
    (base_dir / "vault" / "knowledge" / "明代国子监.md").write_text(
        VAULT_KNOWLEDGE_GUUOZIJIAN, encoding="utf-8"
    )
    (base_dir / "vault" / "corrections" / "不要编造史料.md").write_text(
        VAULT_CORRECTION_FABRICATE, encoding="utf-8"
    )

    # 圣经（legacy markdown）
    bible_dir = base_dir / "bible" / "国子监秘闻录" / ".deskmate"
    bible_dir.mkdir(parents=True, exist_ok=True)
    (bible_dir / "CHARACTERS.md").write_text(BIBLE_CHARACTERS, encoding="utf-8")
    (bible_dir / "OUTLINE.md").write_text(BIBLE_OUTLINE, encoding="utf-8")
    (bible_dir / "TIMELINE.md").write_text(BIBLE_TIMELINE, encoding="utf-8")
    (bible_dir / "NOTES.md").write_text(BIBLE_NOTES, encoding="utf-8")

    # 最小 growth.json（中性默认值，避免任何成长引擎初始化异常）
    (base_dir / "growth.json").write_text(
        '{"intimacy":60,"empathy":60,"trust":0.5,"level":3,"mood":0.6,"energy":85,'
        '"badges":[]}',
        encoding="utf-8",
    )

    return base_dir
