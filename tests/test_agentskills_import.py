"""测试：技能库对齐 agentskills.io 开放标准 + 导入社区技能包。

验证：
1. _parse_frontmatter 能解析 agentskills.io 的 metadata 嵌套块。
2. import_skill_dir 复制第三方包到 imported/<name>/，目录式 SKILL.md 被发现。
3. 导入包出现在 list/search/match/build_skill_index，且 name/description/author/
   version/license 被正确解析。
4. 导入包为只读：increment_uses 不改写 SKILL.md；patch_skill/archive_skill 拒绝。
5. 现有自研扁平 .md 技能向后兼容（仍能被发现/搜索）。
"""

from __future__ import annotations

from pathlib import Path

from deskmate.skills.skill_store import SkillStore, _parse_frontmatter


def _write_pkg(root: Path, name: str, body: str, extra_front: str = "") -> Path:
    """写一个合规 agentskills.io 包目录（含 SKILL.md + scripts/ 子文件）。"""
    pkg = root / name
    (pkg / "scripts").mkdir(parents=True, exist_ok=True)
    (pkg / "scripts" / "run.py").write_text("# placeholder\n", encoding="utf-8")
    front = (
        "---\n"
        f"name: {name}\n"
        "description: Extract text and tables from PDF files, fill forms, merge documents. "
        "Use when working with PDF documents or when the user mentions PDFs, forms, or document extraction.\n"
        "license: Apache-2.0\n"
        "compatibility: Requires pdfplumber and network access\n"
        "allowed-tools: Bash(python:*) Read\n"
        "metadata:\n"
        "  author: example-org\n"
        '  version: "1.0"\n'
        f"{extra_front}"
        "---\n"
    )
    (pkg / "SKILL.md").write_text(front + body + "\n", encoding="utf-8")
    return pkg


def test_parse_frontmatter_metadata_block():
    text = (
        "---\n"
        "name: pdf-processing\n"
        "description: Extract PDF text.\n"
        "metadata:\n"
        "  author: example-org\n"
        '  version: "1.0"\n'
        "---\n"
        "# body\n"
    )
    meta, body = _parse_frontmatter(text)
    assert meta["name"] == "pdf-processing"
    assert meta["description"] == "Extract PDF text."
    assert meta["metadata"]["author"] == "example-org"
    assert meta["metadata"]["version"] == '"1.0"'
    assert body.strip() == "# body"


def test_import_skill_dir_and_discover(tmp_path: Path):
    store = SkillStore(skills_dir=tmp_path)
    pkg = _write_pkg(tmp_path / "src", "pdf-processing",
                     "# PDF 处理技能\n步骤一：读文件。\n步骤二：抽取文本。")
    res = store.import_skill_dir(pkg)
    assert res["status"] == "已导入", res

    # 幂等：重复导入不覆盖（不报错）
    res2 = store.import_skill_dir(pkg)
    assert res2["status"] == "已存在，跳过导入", res2

    # 发现
    skills = store.list_skills()
    imported = [s for s in skills if s.get("imported")]
    assert len(imported) == 1
    s = imported[0]
    assert s["name"] == "pdf-processing"
    assert "Extract text and tables from PDF files" in s["description"]
    assert s["author"] == "example-org"
    assert s["version"] == '"1.0"'
    assert s["license"] == "Apache-2.0"
    assert s["compatibility"] == "Requires pdfplumber and network access"
    assert s["allowed_tools"] == "Bash(python:*) Read"
    assert s["category"] == "general"
    assert "步骤一" in s["body"]


def test_imported_search_and_match(tmp_path: Path):
    store = SkillStore(skills_dir=tmp_path)
    pkg = _write_pkg(tmp_path / "src", "pdf-processing", "body")
    store.import_skill_dir(pkg)

    # 关键词搜索命中
    found = store.search("extract pdf text", limit=5)
    assert any(s["name"] == "pdf-processing" for s in found)

    # SkillManager 匹配
    from deskmate.skills.manager import SkillManager
    mgr = SkillManager(skills_dir=tmp_path)
    matched = mgr.match_skills("帮我处理一下 PDF 文档")
    assert any(s["name"] == "pdf-processing" for s in matched)

    # 索引包含导入包（带作者标注）
    idx = mgr.build_skill_index()
    assert "pdf-processing" in idx
    assert "example-org" in idx


def test_imported_read_and_readonly_guard(tmp_path: Path):
    store = SkillStore(skills_dir=tmp_path)
    pkg = _write_pkg(tmp_path / "src", "pdf-processing", "原始正文")
    store.import_skill_dir(pkg)

    # read_skill 能按名解析
    full = store.read_skill("pdf-processing")
    assert full is not None
    assert "原始正文" in full["body"]

    # increment_uses 不改写导入包（只读）
    skill_md = Path(full["path"])
    before = skill_md.read_text(encoding="utf-8")
    store.increment_uses("pdf-processing")
    after = skill_md.read_text(encoding="utf-8")
    assert before == after, "导入包不应被 increment_uses 改写"

    # patch_skill 拒绝导入包
    assert store.patch_skill("pdf-processing", "原始正文", "新正文")["status"] == "error"

    # archive_skill 拒绝导入包
    assert store.archive_skill("pdf-processing")["status"] == "error"


def test_flat_skill_backward_compat(tmp_path: Path):
    """现有自研扁平 .md 技能仍被发现、搜索，不被导入逻辑破坏。"""
    store = SkillStore(skills_dir=tmp_path)
    created = store.create_skill(
        name="小说审校",
        description="审校历史小说章节",
        body="# 审校步骤",
        tags=["写作", "审校"],
        trigger="用户让你审校小说时",
        category="writing",
    )
    assert created["status"] == "已创建"

    skills = store.list_skills()
    flat = [s for s in skills if not s.get("imported")]
    assert any(s["name"] == "小说审校" for s in flat)

    found = store.search("审校小说", limit=5)
    assert any(s["name"] == "小说审校" for s in found)
