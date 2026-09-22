# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the bundled `openworker-server` (desktop sidecar).

One-DIR bundle (exe + `_internal/` support folder) shipped via Tauri's `resources` slot.
It used to be a onefile binary in the externalBin slot, but onefile self-extracts its whole
archive to a temp dir on EVERY launch — 6-7s of "Starting coworker…" splash (measured; the
actual Python import is ~0.5s). The wrinkles handled here:
  - aisuite is a regular pip dependency (git-pinned in pyproject.toml); collect coworker +
    aisuite submodules from the venv.
  - uvicorn loads its protocol/lifespan impls dynamically → collect_all.
  - certifi's CA bundle must ship for TLS (OpenAI, web search, Telegram/Slack).
  - messaging extras (slack_bolt, telegram) are optional; collected if importable.

Cross-platform: paths are derived from this spec's own location (SPECPATH), never hardcoded,
so the same spec builds native binaries on macOS, Windows, and Linux. On Windows PyInstaller
appends `.exe` to `name`. The binary is built as a normal console app on every OS — a windowed
(console=False) build leaves sys.stdout/stderr as None, which breaks uvicorn's startup logging
and hangs the server. To avoid a console window flashing in the desktop app, the Tauri shell
spawns this sidecar with the Windows CREATE_NO_WINDOW flag (see src-tauri/src/lib.rs), which
hides the window while keeping stdio intact.
"""

import importlib.util
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# SPECPATH is injected by PyInstaller and points at this file's directory
# (<repo>/packaging). Derive everything else from it — no hardcoded paths.
PACKAGING = SPECPATH
ROOT = os.path.dirname(PACKAGING)

IS_WINDOWS = sys.platform == "win32"

# Experimental (use-at-your-own-risk) connectors are excluded from official builds: the code
# is stripped, not just disabled. Self-builders opt in with COWORKER_EXPERIMENTAL=1; the
# loader in coworker/connectors/descriptors.py treats the missing package as a no-op.
INCLUDE_EXPERIMENTAL = os.environ.get("COWORKER_EXPERIMENTAL") == "1"

hiddenimports = []
datas = []
binaries = []

for pkg in ("coworker", "aisuite", "mcp", "ddgs", "croniter", "docstring_parser", "deskmate"):
    # `deskmate` must be fully collected: the frozen sidecar relaunches itself as
    # `openworker-server --mcp-run <module>` to run the 5 builtin MCP servers
    # (no `python -m` is possible inside a PyInstaller binary), and those modules
    # live under deskmate.* — so they have to be importable from the bundle.
    hiddenimports += collect_submodules(pkg)

# Builtin personas ship as DATA, not code: personas/builtin/<id>/manifest.md plus their
# skills/<name>/SKILL.md. collect_submodules only takes .py files, so without this the
# packaged sidecar starts with NO builtin coworkers — the picker comes up empty and every
# persona-scoped skill silently disappears. (pyproject's package-data covers pip installs;
# PyInstaller needs its own instruction.) Keep this even if the persona set changes — it
# collects whatever non-.py files the package carries.
datas += collect_data_files("coworker")

# skill_store.py 现依赖 PyYAML 稳健解析 agentskills.io 嵌套 frontmatter；
# 显式列入以保证冻结 DMG 包含（缺失会触发兜底降级解析，Hermes 包 tags 会变字符串）。
hiddenimports += ["yaml"]

if not INCLUDE_EXPERIMENTAL:
    hiddenimports = [
        m for m in hiddenimports if not m.startswith("coworker.connectors.experimental")
    ]

# `websockets` powers the managed Slack relay client (relay_client.py). It is
# lazy-imported inside a function, so PyInstaller's static analysis misses it —
# collect it explicitly or the packaged relay adapter fails to open its socket.
# `pypdf`/`pypdfium2` are lazy-imported the same way (pdf_support.py) — and pypdfium2
# carries the libpdfium binary, which collect_all is what actually stages.
for pkg in ("uvicorn", "certifi", "anyio", "websockets", "pypdf", "pypdfium2"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Windows has no system tz database; tzdata ships the zoneinfo files the scheduler needs.
if IS_WINDOWS:
    try:
        d, b, h = collect_all("tzdata")
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# [bedrock] extra — boto3 is lazy-imported (bedrock_provider.py) so static analysis
# misses it, and botocore's service-model JSON data dir only ships via collect_all.
for pkg in ("boto3", "botocore"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

for pkg in ("slack_bolt", "telegram"):  # [messaging] extra — optional
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# [deskmate-embed] extra — fastembed + BGE 本地向量（deskmate/memory/local_embed.py）。
# 这三条都是**实测**踩出来的，缺任何一条，打包版的语义检索都会静默降级成纯关键词：
#   1. onnxruntime / tokenizers / py-rust-stemmers 是**原生扩展**，PyInstaller 的静态分析
#      看不到它们的 .so 与 libonnxruntime.*.dylib → 必须 collect_all，否则 bundle 里只留
#      一个 import 就会炸的空壳。
#   2. `fastembed/__init__.py` 顶层就 `from fastembed.image import ImageEmbedding`，而
#      `fastembed/common/types.py` 顶层 `from PIL import Image` —— 这条链**同时也在文本
#      嵌入的依赖路径上**，所以下面 excludes 里的 `PIL` 必须按需摘掉。
#      实测法（把 PIL 变成不可导入以模拟冻结环境）：
#          sys.meta_path.insert(0, <blocker for "PIL">) → local_embed.embed(...)
#      结果：ImportError → fail-open 返回 None → **语义检索静默失效、只剩关键词、不报错**，
#      正是最难查的那种坏法（本项目专门为它加了 _note_embed_failure 告警，见 semantic_search）。
#   3. 装没装都安全：未安装 → 整块跳过（与 boto3 / slack_bolt 同款 optional 语义）。
# 体积代价（macOS arm64 **实测**，未压缩）：冻结产物 **92MB → 253MB（+161MB）**
# （onnxruntime 75MB / PIL 11MB / tokenizers 9.4MB / hf_xet 7.4MB / numpy 6.8MB …）。
# 模型（~102MB）**不打进 bundle**：缓存在 `<deskmate 数据目录>/models`，首次用时下载，
# 好处是应用升级不重下、且不必把 102MB 塞进 app 包（离线分发可另行预置该目录）。
_EMBED_IMPORTABLE = importlib.util.find_spec("fastembed") is not None
if _EMBED_IMPORTABLE:
    for pkg in ("fastembed", "onnxruntime", "tokenizers", "huggingface_hub",
                "py_rust_stemmers", "hf_xet"):
        try:
            d, b, h = collect_all(pkg)
            datas += d
            binaries += b
            hiddenimports += h
        except Exception:
            pass
else:
    # **不要让跳过变成静默事件**——静默正是本项目反复踩的坑。少收集就是「打包版语义检索
    # 消失、只剩关键词」，而日志里什么都不说，回头只能靠猜。所以这里显式吭一声。
    print(
        "[openworker-server.spec] ⚠ 未安装可选依赖 fastembed → 本次冻结**不含**本地语义检索，"
        "打包版会退回关键词通道。\n"
        "   要带上它：.venv/bin/pip install -e '.[deskmate-embed]'（实测 +161MB），然后重跑本构建。"
    )

a = Analysis(
    [os.path.join(PACKAGING, "server_entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide6"]
    # fastembed 顶层 `from PIL import Image`（见上面 [deskmate-embed] 第 2 条）——
    # 只有装了该 extra 时才把 PIL 放回来，否则维持原样（不多占 ~15MB）。
    + ([] if _EMBED_IMPORTABLE else ["PIL"])
    + ([] if INCLUDE_EXPERIMENTAL else ["coworker.connectors.experimental"]),
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="openworker-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Console on every OS: a windowed build nulls stdout/stderr and hangs uvicorn. The Tauri
    # shell hides the window on Windows via CREATE_NO_WINDOW when spawning the sidecar.
    console=True,
    # target_arch left unset → PyInstaller builds for the host architecture.
)
# Onedir: dist/openworker-server/{openworker-server[.exe], _internal/}. The build scripts stage
# this whole folder into src-tauri/binaries/sidecar/ for Tauri's `resources` bundling.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="openworker-server",
)
