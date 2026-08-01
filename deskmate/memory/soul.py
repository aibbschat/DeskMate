"""SOUL.md 人格管理 — AI 人格定义与演化。"""

from __future__ import annotations

from pathlib import Path

_DEFAULT_SOUL = """\
# 人格运行时状态（Soul 层）

> 你的人格核心与「性格双模式」由当前激活的 OpenWorker persona **DeskMate（`deskmate.md`）** 统一定义——它是本层之上的唯一权威来源。
> 本文件**不重复定义人格**，只承载可随对话演化的「运行期状态」。任何写入都不得改写或抵触 persona 中的人格核心、双模式与写作铁律。

## 本层职责
- **表达微调**：随你偏好演化的语气、称呼、主动程度等（只调表达方式，不动物格核心）。
- **纠正与共识**：你纠正过我的点、我们达成的约定，记在这里避免再犯。
- **当前专注 / 模式结论**：我按上下文判断出的当前状态，本层只保存结论。

## 演化护栏（必须遵守）
- 演化只调整表达方式与记录，不重写「身份 / 双模式 / 写作铁律」。
- 不得与 persona（`deskmate.md`）中定义的「性格双模式」「写作铁律」相矛盾。
- 需要调整人格核心时，应在 persona 层面进行，而非本文件。

## 当前状态
（初始为空，随对话积累）
"""


def get_soul_path(base_dir: Path) -> Path:
    return base_dir / "SOUL.md"


def load_soul(base_dir: Path) -> str:
    """加载 SOUL.md，不存在则返回默认人格。"""
    p = get_soul_path(base_dir)
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return _DEFAULT_SOUL


def save_soul(base_dir: Path, content: str) -> None:
    """保存 SOUL.md。"""
    p = get_soul_path(base_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def reset_soul(base_dir: Path) -> str:
    """重置为默认运行期状态层（不含人格定义；人格由 persona 统一定义）。"""
    save_soul(base_dir, _DEFAULT_SOUL)
    return _DEFAULT_SOUL


def evolve_soul(base_dir: Path, instruction: str) -> str:
    """保存演化后的 Soul 层内容（由 LLM 生成新 SOUL.md 全文后调用）。

    **契约**：传入的 `instruction` 必须是「运行期状态层」，不得重新定义人格核心 /
    双模式 / 写作铁律——那些只存在于 persona（`deskmate.md`）。如需护栏校验，
    见 deskmate-memory 的演化保护逻辑。

    Args:
        instruction: LLM 生成的新 SOUL.md 全文（运行期状态，非人格定义）

    Returns:
        保存后的 SOUL.md 内容
    """
    save_soul(base_dir, instruction)
    return instruction
