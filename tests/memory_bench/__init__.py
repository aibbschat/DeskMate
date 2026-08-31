"""记忆质量评测集（8.1 方向盘）。

纯本地、无 LLM 依赖的回归基准：给定自然语言提问，检查小桔记忆系统能否
从 USER.md / MEMORY.md / 项目记忆 / vault / 圣经 中正确召回相关信息。

运行方式:
  - pytest:  `python -m pytest tests/memory_bench/ -q`
  - 独立:    `python tests/memory_bench/runner.py`
"""
