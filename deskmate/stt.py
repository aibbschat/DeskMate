#!/usr/bin/env python3
"""本地 SenseVoice 语音听写（简体中文，离线）。

从 DSH 的 ``speech-to-text/sensevoice`` 包移植：模型是通用 sherpa-onnx
SenseVoiceSmall int8 格式，与 DSH 的调用代码无关，因此可直接复用。

为什么需要它：桌面 Composer 的听写走 ``stt`` crate 的 whisper.cpp
``ggml-base.bin``，``transcribe()`` 里写死 ``set_language("zh")``；Whisper Base 的
中文训练语料以繁体字幕为主，输出繁体字，萧哥用起来很难受。SenseVoice 中文
输出为简体且自带标点（ITN），8 秒音频实测约 0.18s，模型加载约 0.9s。

入口：console script ``openworker-stt``（pyproject [project.scripts]），桌面 App 的
``stt`` crate 通过它转写；开发时也可用 ``python scripts/sensevoice_stt.py``。

用法::

    openworker-stt --wav /tmp/dictation.wav
    openworker-stt --wav /tmp/dictation.wav --json

约定：
- 录音格式：16 kHz / 单声道 / 16-bit PCM WAV（与 ``stt`` crate 的 resample 输出一致）。
- 模型目录默认 ``<state_dir>/models/sensevoice``，可用 ``--model-dir`` 覆盖；
  ``COWORKER_STATE_DIR`` 在调用时解析，绝不做模块级常量。
- 成功：stdout 一行纯文本（``--json`` 时一行 JSON）；失败：stderr + 非零退出。
- 只读模型，不写任何数据文件，音频不落盘（由调用方负责临时 wav）。

这是 Mac 桌面本地工具，云端 openworker-server 不 import 本模块，故不入
``deploy_cloud_sync.sh`` 清单。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import wave
from pathlib import Path
from typing import Any

MODEL_FILE = "model.int8.onnx"
TOKENS_FILE = "tokens.txt"
VAD_FILE = "silero_vad.onnx"

# DSH runtime/assets.json 公布的校验值（模型未被改动过的凭据）。
KNOWN_SHA256 = {
    MODEL_FILE: "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
    TOKENS_FILE: "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
    VAD_FILE: "a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28",
}

# 诊断用：Whisper 中文输出里高频出现的繁体字。只用于 --strict-simplified 体检，
# 不用来改字（改字会引入新的错误；简体由模型本身保证）。
TRADITIONAL_SENTINELS = "體倉糧燒這說話們來個國與為時將對後裡麼還聲點見聞無動車馬鳥書畫買賣錢銀閒間門問間盡"

SAMPLE_RATE = 16_000


def default_model_dir() -> Path:
    """调用时解析 state_dir；与 Tauri 侧 state_dir() 的缺省规则保持一致。"""
    raw = os.environ.get("COWORKER_STATE_DIR")
    base = Path(raw).expanduser() if raw else Path.home() / ".config" / "coworker"
    return base / "models" / "sensevoice"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_assets(model_dir: Path, verify_sha: bool = False) -> list[str]:
    """返回缺失/损坏的资产名列表（空列表=可用）。"""
    problems: list[str] = []
    for name in (MODEL_FILE, TOKENS_FILE):
        p = model_dir / name
        if not p.is_file():
            problems.append(f"缺少 {p}")
    if verify_sha:
        for name, expected in KNOWN_SHA256.items():
            p = model_dir / name
            if p.is_file() and sha256_of(p) != expected:
                problems.append(f"校验不符 {p}")
    return problems


def read_wave(path: Path) -> tuple[list[float], int]:
    """读 16k/单声道/16-bit PCM WAV，归一化成 float32 采样序列。"""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1:
            raise ValueError(f"需要单声道 WAV，实际 {w.getnchannels()} 声道")
        if w.getsampwidth() != 2:
            raise ValueError(f"需要 16-bit PCM WAV，实际 {w.getsampwidth() * 8} bit")
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    # 不引入 numpy 依赖：array 转 list 足够，短句听写通常 < 30s。
    from array import array

    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return [s / 32768.0 for s in samples], rate


def build_recognizer(model_dir: Path, language: str = "zh", use_itn: bool = True) -> Any:
    """懒加载 sherpa-onnx；导入失败给出可行动的错误，而不是静默降级。"""
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise RuntimeError(
            "缺少 sherpa-onnx 运行时，请先安装：pip install sherpa-onnx==1.13.8"
        ) from exc
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model_dir / MODEL_FILE),
        tokens=str(model_dir / TOKENS_FILE),
        num_threads=4,
        language=language,
        use_itn=use_itn,
        debug=False,
    )


def transcribe_wav(
    wav_path: Path,
    model_dir: Path | None = None,
    language: str = "zh",
    recognizer: Any = None,
) -> dict[str, str]:
    """转写一个 wav 文件，返回 text / lang / emotion / event。"""
    directory = model_dir if model_dir is not None else default_model_dir()
    problems = check_assets(directory)
    if problems:
        raise RuntimeError("；".join(problems))
    samples, rate = read_wave(Path(wav_path))
    engine = recognizer if recognizer is not None else build_recognizer(directory, language)
    stream = engine.create_stream()
    stream.accept_waveform(rate, samples)
    engine.decode_stream(stream)
    result = stream.result
    return {
        "text": (result.text or "").strip(),
        "lang": getattr(result, "lang", "") or "",
        "emotion": getattr(result, "emotion", "") or "",
        "event": getattr(result, "event", "") or "",
    }


def traditional_hits(text: str) -> list[str]:
    """体检：返回文本里出现的繁体哨兵字（不用于改写）。"""
    return sorted({ch for ch in text if ch in TRADITIONAL_SENTINELS})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="本地 SenseVoice 简体中文听写")
    ap.add_argument("--wav", required=True, type=Path, help="16k/单声道/16-bit PCM WAV")
    ap.add_argument("--model-dir", type=Path, default=None, help="模型目录（默认 state_dir/models/sensevoice）")
    ap.add_argument("--language", default="zh", choices=["zh", "auto", "en", "ja", "ko", "yue"])
    ap.add_argument("--json", action="store_true", help="输出一行 JSON（含 lang/emotion/event）")
    ap.add_argument("--verify-sha", action="store_true", help="落盘前核对官方 SHA256")
    ap.add_argument("--strict-simplified", action="store_true", help="检出繁体哨兵字则以退出码 3 失败")
    args = ap.parse_args(argv)

    directory = args.model_dir if args.model_dir is not None else default_model_dir()
    problems = check_assets(directory, verify_sha=args.verify_sha)
    if problems:
        print("SenseVoice 资产不可用：" + "；".join(problems), file=sys.stderr)
        return 2
    try:
        out = transcribe_wav(args.wav, directory, args.language)
    except Exception as exc:
        print(f"听写失败：{exc}", file=sys.stderr)
        return 2

    if args.strict_simplified:
        hits = traditional_hits(out["text"])
        if hits:
            print("检出繁体字：" + "".join(hits), file=sys.stderr)
            return 3
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(out["text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
