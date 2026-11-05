"""本地 SenseVoice 听写的回归测试。

锁的是 2026-11-05 萧哥反馈的「听写完显示繁体字」：桌面 Composer 走 whisper.cpp
ggml-base.bin，Whisper 中文输出天然偏繁体。本模块是换引擎的落地件，必须保证
①简体原样输出（不改写文字）②缺模型/格式错立刻失败而非静默降级 ③能体检繁体回归。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import wave
from array import array
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sensevoice_stt.py"
spec = importlib.util.spec_from_file_location("sensevoice_stt", SCRIPT)
assert spec and spec.loader
stt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stt)


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.lang = "<|zh|>"
        self.emotion = "<|NEUTRAL|>"
        self.event = "<|Speech|>"


class _FakeStream:
    def __init__(self, text: str) -> None:
        self.result = _FakeResult(text)
        self.rate: int | None = None
        self.samples: list[float] = []

    def accept_waveform(self, rate: int, samples: list[float]) -> None:
        self.rate = rate
        self.samples = samples


class _FakeRecognizer:
    def __init__(self, text: str) -> None:
        self.stream = _FakeStream(text)

    def create_stream(self) -> _FakeStream:
        return self.stream

    def decode_stream(self, stream: _FakeStream) -> None:
        pass


def _write_wav(path: Path, *, channels: int = 1, rate: int = 16_000, n: int = 160) -> None:
    frames = array("h", [0] * (n * channels))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames.tobytes())


def _assets(dirpath: Path) -> None:
    (dirpath / stt.MODEL_FILE).write_bytes(b"fake-model")
    (dirpath / stt.TOKENS_FILE).write_bytes(b"<unk> 0\n")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, check=False,
    )


def test_default_model_dir_resolves_state_dir_at_call_time(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "a"))
    assert stt.default_model_dir() == tmp_path / "a" / "models" / "sensevoice"
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "b"))
    assert stt.default_model_dir() == tmp_path / "b" / "models" / "sensevoice"


def test_check_assets_reports_every_missing_file(tmp_path: Path) -> None:
    problems = stt.check_assets(tmp_path)
    assert len(problems) == 2
    assert any(stt.MODEL_FILE in p for p in problems)
    assert any(stt.TOKENS_FILE in p for p in problems)


def test_check_assets_detects_sha_mismatch(tmp_path: Path) -> None:
    _assets(tmp_path)
    assert stt.check_assets(tmp_path) == []
    assert stt.check_assets(tmp_path, verify_sha=True)  # 假文件必然校验不符


def test_read_wave_normalizes_samples(tmp_path: Path) -> None:
    p = tmp_path / "x.wav"
    raw = array("h", [0, 16384, -16384, 32767])
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16_000)
        w.writeframes(raw.tobytes())
    samples, rate = stt.read_wave(p)
    assert rate == 16_000
    assert samples[0] == 0.0
    assert abs(samples[1] - 0.5) < 1e-6
    assert abs(samples[2] + 0.5) < 1e-6


def test_read_wave_rejects_stereo_and_non_pcm16(tmp_path: Path) -> None:
    p = tmp_path / "stereo.wav"
    _write_wav(p, channels=2)
    with pytest.raises(ValueError, match="单声道"):
        stt.read_wave(p)


def test_transcribe_wav_returns_simplified_text_unchanged(tmp_path: Path, monkeypatch) -> None:
    _assets(tmp_path)
    wav = tmp_path / "mic.wav"
    _write_wav(wav)
    fake = _FakeRecognizer("今天天气不错，第十一章烧粮仓。")
    monkeypatch.setattr(stt, "build_recognizer", lambda *a, **k: fake)
    out = stt.transcribe_wav(wav, tmp_path, recognizer=fake)
    assert out["text"] == "今天天气不错，第十一章烧粮仓。"
    assert out["lang"] == "<|zh|>"
    assert fake.stream.rate == 16_000  # 采样率必须原样传给引擎


def test_transcribe_wav_fails_fast_without_model(tmp_path: Path) -> None:
    wav = tmp_path / "mic.wav"
    _write_wav(wav)
    with pytest.raises(RuntimeError, match="缺少"):
        stt.transcribe_wav(wav, tmp_path / "no-model")


def test_traditional_hits_catches_whisper_style_output() -> None:
    assert stt.traditional_hits("這是第十一章：燒糧倉，販夫走卒都來說話") != []
    assert stt.traditional_hits("这是第十一章：烧粮仓，贩夫走卒都来说话") == []


def test_main_strict_simplified_rejects_traditional(tmp_path: Path, monkeypatch, capsys) -> None:
    _assets(tmp_path)
    wav = tmp_path / "mic.wav"
    _write_wav(wav)
    monkeypatch.setattr(
        stt, "build_recognizer", lambda *a, **k: _FakeRecognizer("這是第十一章：燒糧倉")
    )
    rc = stt.main(["--wav", str(wav), "--model-dir", str(tmp_path), "--strict-simplified"])
    assert rc == 3
    assert "检出繁体字" in capsys.readouterr().err


def test_main_json_keeps_simplified_text(tmp_path: Path, monkeypatch, capsys) -> None:
    _assets(tmp_path)
    wav = tmp_path / "mic.wav"
    _write_wav(wav)
    monkeypatch.setattr(
        stt, "build_recognizer", lambda *a, **k: _FakeRecognizer("第十一章烧粮仓")
    )
    rc = stt.main(["--wav", str(wav), "--model-dir", str(tmp_path), "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["text"] == "第十一章烧粮仓"


def test_cli_missing_assets_exits_nonzero(tmp_path: Path) -> None:
    wav = tmp_path / "mic.wav"
    _write_wav(wav)
    r = _run("--wav", str(wav), "--model-dir", str(tmp_path / "empty"))
    assert r.returncode == 2
    assert "资产不可用" in r.stderr


# conftest 会把 COWORKER_STATE_DIR 指向 pytest 临时沙箱；真模型用例必须显式
# 指定真实落地目录，否则「默认目录」在子进程里会被沙箱 env 劫持。
REAL_MODEL_DIR = Path.home() / ".config" / "coworker" / "models" / "sensevoice"


@pytest.mark.skipif(
    not (REAL_MODEL_DIR / stt.MODEL_FILE).is_file(),
    reason="本机未安装 SenseVoice 模型",
)
def test_real_model_output_is_simplified() -> None:
    """真模型端到端：可选，用 SENSEVOICE_TEST_WAV 指向一段中文语音。"""
    wav = os.environ.get("SENSEVOICE_TEST_WAV", "/tmp/xj_stt_test.wav")
    if not Path(wav).is_file():
        pytest.skip("未提供测试音频（SENSEVOICE_TEST_WAV）")
    r = _run("--wav", wav, "--model-dir", str(REAL_MODEL_DIR), "--json", "--strict-simplified")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["text"], "真模型不应返回空文本"
    assert stt.traditional_hits(out["text"]) == [], f"输出含繁体：{out['text']}"
