"""Task2 主动行为环测试：闭环决策解析 + 执行桥安全门槛 + 防打扰 + 降级提案。

v2 新增：调查工具（investigator.py）安全测试 + 两阶段决策管道测试。

不依赖真实 LLM：直接构造 CloseLoopDecision 测试执行桥，并验证决策解析的容错。
调查工具用临时目录测试路径安全和只读约束。
"""

import os
import tempfile

# 必须在导入 executor 之前把状态目录指到临时目录，避免污染真实的
# ~/.deskmate/memory/disturb_state.json 与 close_loop_pending.jsonl。
_TMP = tempfile.mkdtemp(prefix="deskmate_close_loop_test_")
os.environ["DESKMATE_DIR"] = _TMP
os.environ.pop("DESKMATE_CLOSE_LOOP_AUTO", None)  # 让默认自动模式生效

import pytest

from deskmate.proactivity import executor
from deskmate.proactivity.close_loop import CloseLoopDecision, _parse_decision
from deskmate.proactivity import investigator


# ---------------------------------------------------------------------------
# 决策解析（容错）
# ---------------------------------------------------------------------------
def test_parse_decision_plain_json():
    resp = (
        '{"should_close_loop": true, "action_summary": "同步网站副本", '
        '"rationale": "公众号已改", "confidence": 0.92, "target_hint": "site", '
        '"reversible": true, "risk_level": "low"}'
    )
    d = _parse_decision(resp, "调查发现内容")
    assert d.should_close_loop
    assert d.confidence == 0.92
    assert d.reversible
    assert d.risk_level == "low"
    assert d.investigation_notes == "调查发现内容"


def test_parse_decision_codeblock():
    resp = '```json\n{"should_close_loop": false, "confidence": 0}\n```'
    d = _parse_decision(resp, "")
    assert not d.should_close_loop


def test_parse_decision_bad_risk_normalized():
    d = _parse_decision('{"should_close_loop": true, "risk_level": "CRITICAL!"}', "")
    assert d.risk_level == "low"  # 非法值归一化，绝不主动执行


def test_parse_decision_no_json():
    d = _parse_decision("这段回复里根本没有 json", "")
    assert not d.should_close_loop


def test_parse_decision_empty():
    d = _parse_decision("", "")
    assert not d.should_close_loop


# ---------------------------------------------------------------------------
# 自动执行门槛
# ---------------------------------------------------------------------------
def test_auto_eligible_pass():
    d = CloseLoopDecision(True, "同步网站", "r", 0.9, "t", True, "low", "")
    assert executor._is_auto_eligible(d)


@pytest.mark.parametrize(
    "d",
    [
        CloseLoopDecision(True, "x", "r", 0.5, "t", True, "low", ""),   # 低置信度
        CloseLoopDecision(True, "x", "r", 0.9, "t", False, "low", ""),  # 不可逆
        CloseLoopDecision(True, "x", "r", 0.9, "t", True, "high", ""),  # 高风险
        CloseLoopDecision(False, "x", "r", 0.9, "t", True, "low", ""),  # 不建议
    ],
)
def test_auto_eligible_blocked(d):
    assert not executor._is_auto_eligible(d)


# ---------------------------------------------------------------------------
# 执行桥（隔离全局状态）
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_executor_state():
    executor._executor_fn = None
    executor._auto_mode = True
    executor._get_disturb().silent_mode = False
    executor._get_disturb().records = []
    yield
    executor._get_disturb().records = []


def _eligible_decision():
    return CloseLoopDecision(
        True, "把公众号的修改同步到网站", "两处内容应一致",
        0.92, "website", True, "low", "调查发现网站副本未更新",
    )


def test_try_execute_falls_back_to_proposal_without_executor():
    """达门槛但未注册执行器 -> 降级为提案（写入 pending），绝不静默自动执行。"""
    res = executor.try_execute_close_loop(_eligible_decision(), "/tmp/ws")
    assert res.executed is False
    assert res.proposed is True
    pending = executor._pending_path().read_text(encoding="utf-8")
    assert "把公众号的修改同步到网站" in pending


def test_try_execute_runs_executor_when_registered():
    """达门槛且已注册执行器 + 自动模式开 -> 真正提交执行。"""
    called = {}
    executor.register_close_loop_executor(
        lambda instr, ws: called.setdefault("hit", (instr, ws))
    )
    res = executor.try_execute_close_loop(_eligible_decision(), "/tmp/ws")
    assert res.executed is True
    assert called["hit"] == ("把公众号的修改同步到网站", "/tmp/ws")


def test_try_execute_blocked_by_silent_mode():
    """防打扰静默模式 -> 拦截，不做也不提案。"""
    executor._get_disturb().set_silent_mode(True)
    res = executor.try_execute_close_loop(_eligible_decision(), "/tmp/ws")
    assert res.skipped is True
    assert res.executed is False
    assert res.proposed is False


def test_try_execute_below_auto_threshold_falls_to_proposal():
    """决策建议闭环但置信度低于自动门槛 -> 不自动执行，降级为提案等待用户确认。"""
    d = CloseLoopDecision(True, "x", "r", 0.5, "t", True, "low", "")  # 低于 AUTO 门槛
    res = executor.try_execute_close_loop(d, "/tmp/ws")
    assert res.executed is False
    assert res.proposed is True
    assert res.skipped is False


def test_auto_mode_off_falls_back_to_proposal():
    """自动模式关 -> 即便达门槛也降级为提案。"""
    executor.set_auto_mode(False)
    res = executor.try_execute_close_loop(_eligible_decision(), "/tmp/ws")
    assert res.executed is False
    assert res.proposed is True


# ---------------------------------------------------------------------------
# v2: 调查工具安全测试
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_workspace():
    """创建临时工作目录 + 测试文件。"""
    with tempfile.TemporaryDirectory() as ws:
        ws_path = ws
        # 创建测试文件
        with open(os.path.join(ws_path, "config.yml"), "w") as f:
            f.write("version: 1.0\nname: test-project\n")
        # 创建子目录 + 文件
        os.makedirs(os.path.join(ws_path, "src"), exist_ok=True)
        with open(os.path.join(ws_path, "src", "app.py"), "w") as f:
            f.write("VERSION = '1.0'\nprint('hello')\n")
        # 创建 .git 目录（模拟 git 仓库）
        os.makedirs(os.path.join(ws_path, ".git"), exist_ok=True)
        yield ws_path


def test_investigator_read_file_within_workspace(temp_workspace):
    """read_file 能读 workspace 内的文件。"""
    content = investigator.tool_read_file("config.yml", temp_workspace)
    assert "version: 1.0" in content
    assert "test-project" in content


def test_investigator_read_file_traversal_blocked(temp_workspace):
    """read_file 拒绝路径穿越（读 workspace 外的文件）。"""
    # 尝试用相对路径穿越
    content = investigator.tool_read_file("../../../etc/passwd", temp_workspace)
    assert "错误" in content or "不存在" in content


def test_investigator_read_file_nonexistent(temp_workspace):
    """read_file 对不存在的文件返回错误信息。"""
    content = investigator.tool_read_file("nonexistent.md", temp_workspace)
    assert "错误" in content or "不存在" in content


def test_investigator_list_dir(temp_workspace):
    """list_dir 列出目录内容。"""
    content = investigator.tool_list_dir(".", temp_workspace)
    assert "config.yml" in content
    assert "src/" in content


def test_investigator_search_files(temp_workspace):
    """search_files 能搜到文件内容中的关键词。"""
    content = investigator.tool_search_files("VERSION", ".", temp_workspace)
    assert "app.py" in content
    assert "1.0" in content


def test_investigator_search_files_no_match(temp_workspace):
    """search_files 无匹配时返回提示。"""
    content = investigator.tool_search_files("NONEXISTENT_PATTERN_XYZ", ".", temp_workspace)
    assert "无匹配" in content


def test_investigator_git_status(temp_workspace):
    """git_status 在有 .git 的目录能执行（即使没有提交）。"""
    content = investigator.tool_git_status(temp_workspace)
    # 可能是 "（工作区干净）" 或具体的 status 输出，不应是错误
    assert "失败" not in content


def test_investigator_git_status_no_repo():
    """git_status 在非 git 目录返回提示。"""
    with tempfile.TemporaryDirectory() as ws:
        content = investigator.tool_git_status(ws)
        assert "不是 git 仓库" in content


def test_investigator_read_memory_with_query():
    """read_memory 带 query 时按 ## 分节关键词过滤。"""
    with tempfile.TemporaryDirectory() as base:
        os.environ["DESKMATE_DIR"] = base
        memory_path = os.path.join(base, "MEMORY.md")
        with open(memory_path, "w") as f:
            f.write(
                "## 用户偏好 #preferences\n"
                "用户喜欢简洁的代码\n"
                "用户反感模板化内容\n\n"
                "## 环境信息 #env\n"
                "服务器在 212.64.18.62\n"
            )
        result = investigator.tool_read_memory("用户偏好", base)
        assert "简洁的代码" in result
        # 不含无关分节
        assert "212.64.18.62" not in result


def test_investigator_read_memory_no_query():
    """read_memory 不带 query 时返回全量记忆（截断）。"""
    with tempfile.TemporaryDirectory() as base:
        os.environ["DESKMATE_DIR"] = base
        memory_path = os.path.join(base, "MEMORY.md")
        with open(memory_path, "w") as f:
            f.write("## 测试\n这是一条测试记忆\n")
        result = investigator.tool_read_memory("", base)
        assert "测试记忆" in result


def test_investigator_read_memory_empty():
    """read_memory 无记忆文件时返回提示。"""
    with tempfile.TemporaryDirectory() as base:
        result = investigator.tool_read_memory("", base)
        assert "无可用记忆" in result


def test_investigator_search_files_skips_heavy_dirs(temp_workspace):
    """search_files 应跳过 _SEARCH_SKIP_DIRS（如 node_modules），不枚举全树。"""
    heavy = os.path.join(temp_workspace, "node_modules")
    os.makedirs(heavy, exist_ok=True)
    with open(os.path.join(heavy, "deep.js"), "w") as f:
        f.write("VERSION = '1.0'\n")  # 命中但所在目录应被跳过
    with open(os.path.join(temp_workspace, "real.txt"), "w") as f:
        f.write("VERSION = '1.0'\n")   # 普通目录命中
    content = investigator.tool_search_files("VERSION", ".", temp_workspace)
    assert "real.txt" in content
    assert "node_modules" not in content
    assert "deep.js" not in content


def test_safe_resolve_no_workspace_blocks_outside():
    """无 workspace 且无 base_dir：相对路径拒绝；绝对路径限制在 home 内。"""
    assert investigator._safe_resolve("foo", None) is None
    assert investigator._safe_resolve("/etc/passwd", None) is None
    home = os.path.expanduser("~")
    assert investigator._safe_resolve(home, None) is not None


def test_safe_resolve_relative_scoped_to_base_dir(tmp_path):
    """无 workspace 但给 base_dir：相对路径基于 base_dir 解析。"""
    target = tmp_path / "note.txt"
    target.write_text("hi", encoding="utf-8")
    res = investigator._safe_resolve("note.txt", None, str(tmp_path))
    assert res == target.resolve()


def test_investigator_tool_registry_complete():
    """工具注册表与实现函数一一对应。"""
    for tool_def in investigator.TOOL_DEFINITIONS:
        name = tool_def["function"]["name"]
        assert name in investigator.TOOL_IMPLEMENTATIONS, f"工具 {name} 缺少实现"


# ---------------------------------------------------------------------------
# v2: after_action 记忆加载测试
# ---------------------------------------------------------------------------

def test_load_close_loop_memory_loads_global_memory():
    """_load_close_loop_memory 加载全局 MEMORY.md。"""
    with tempfile.TemporaryDirectory() as base:
        from pathlib import Path
        base_path = Path(base)
        (base_path / "MEMORY.md").write_text("§ 偏好 #pref\n用户喜欢简洁\n", encoding="utf-8")
        # 重新导入以使用新的 DESKMATE_DIR
        import importlib
        from deskmate.memory import after_action
        importlib.reload(after_action)
        memory = after_action._load_close_loop_memory(base_path, None)
        assert "简洁" in memory
        assert "全局记忆" in memory


def test_load_close_loop_memory_loads_project_memory():
    """_load_close_loop_memory 加载项目 longterm.md。"""
    with tempfile.TemporaryDirectory() as base:
        from pathlib import Path
        base_path = Path(base)
        # 创建项目记忆
        proj_dir = base_path / "projects" / "test-project"
        proj_dir.mkdir(parents=True)
        (proj_dir / "longterm.md").write_text("项目约定：用 Tailwind CSS\n", encoding="utf-8")

        import importlib
        from deskmate.memory import after_action
        importlib.reload(after_action)
        memory = after_action._load_close_loop_memory(base_path, "/some/path/test-project")
        assert "Tailwind CSS" in memory
        assert "项目记忆" in memory


def test_load_close_loop_memory_empty():
    """无记忆文件时返回空字符串。"""
    with tempfile.TemporaryDirectory() as base:
        from pathlib import Path
        import importlib
        from deskmate.memory import after_action
        importlib.reload(after_action)
        memory = after_action._load_close_loop_memory(Path(base), None)
        assert memory == ""
