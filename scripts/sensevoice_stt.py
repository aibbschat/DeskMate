#!/usr/bin/env python3
"""开发期入口：本地 SenseVoice 简体中文听写。

真正的实现在 ``deskmate.stt``（console script ``openworker-stt`` 与冻结包都从
那里 import），本文件只是让仓库里也能直接 ``python scripts/sensevoice_stt.py`` 跑。

用法::

    python scripts/sensevoice_stt.py --wav /tmp/dictation.wav
    python scripts/sensevoice_stt.py --wav /tmp/dictation.wav --json

模型资产与约定见 ``deskmate/stt.py`` 的模块文档。
"""

from __future__ import annotations

from deskmate.stt import main

if __name__ == "__main__":
    raise SystemExit(main())
