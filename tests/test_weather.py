"""天气感知单测（2026-10-15）。

覆盖：条件归一化（wttr 中文 / WMO 码）、她的口味规则表、缓存新鲜/陈旧兜底、
双源切换、情绪去重（同日同天气只算一次）、引擎规则缺失时的能力探测、
聊天注入（关键词/氛围行/安静）、三个开关、全链路 fail-open。

网络一律不打：HTTP 层 monkeypatch，用例与网络状况无关。
"""
from __future__ import annotations

import json

import pytest

from deskmate.perception import weather as wx_mod
from deskmate.perception.weather import (
    WEATHER_MOOD_DELTAS,
    apply_weather_mood,
    condition_from_wmo,
    condition_from_wttr_text,
    fetch_weather,
    load_cache,
    save_cache,
    weather_context,
    weather_mood_event,
)

# ---------------------------------------------------------------------------
# fixtures 数据（两个源的原始响应形状）
# ---------------------------------------------------------------------------
WTTR_J1 = {
    "current_condition": [{
        "lang_zh": [{"value": "多云"}],
        "temp_C": "21", "FeelsLikeC": "20", "humidity": "62",
    }],
    "weather": [{
        "maxtempC": "24", "mintempC": "18",
        "hourly": [{"chanceofrain": "30"}, {"chanceofrain": "10"}],
    }],
}

# lang_zh 缺位（真机实测过）：只有英文 weatherDesc
WTTR_J1_EN = {
    "current_condition": [{
        "weatherDesc": [{"value": "Overcast"}],
        "temp_C": "20", "FeelsLikeC": "19", "humidity": "68",
    }],
    "weather": [{
        "maxtempC": "26", "mintempC": "15",
        "hourly": [{"chanceofrain": "22"}],
    }],
}

OPEN_METEO = {
    "current": {
        "weather_code": 3, "temperature_2m": 21.0,
        "relative_humidity_2m": 62, "apparent_temperature": 20.0,
    },
    "daily": {
        "temperature_2m_max": [24.0], "temperature_2m_min": [18.0],
        "precipitation_probability_max": [30],
    },
}


@pytest.fixture
def base(tmp_path, monkeypatch):
    """隔离 base_dir + 默认开关全开 + 网络永不通（漏 patch 会立刻炸）。"""
    monkeypatch.setenv("DESKMATE_WEATHER", "1")
    monkeypatch.setenv("DESKMATE_WEATHER_MOOD", "1")
    monkeypatch.setenv("DESKMATE_WEATHER_LOCATION", "宁波")
    monkeypatch.setattr(
        wx_mod, "_http_get_json",
        lambda url: (_ for _ in ()).throw(AssertionError(f"网络未 patch: {url}")),
    )
    return tmp_path


@pytest.fixture
def engine_spy(monkeypatch):
    """间谍引擎：记录 apply_event_direct 调用；全部 weather_* 规则已在表内。"""
    from deskmate.growth.emotion_engine import EmotionEngine

    calls: list[str] = []
    extra = {f"weather_{c}": d for c, d in WEATHER_MOOD_DELTAS.items()}
    monkeypatch.setattr(
        EmotionEngine, "EVENT_DIRECT_DELTAS",
        {**EmotionEngine.EVENT_DIRECT_DELTAS, **extra},
    )
    monkeypatch.setattr(
        EmotionEngine, "apply_event_direct",
        lambda self, event: calls.append(event),
    )
    return calls


# ---------------------------------------------------------------------------
# 条件归一化（纯函数）
# --------------------------------------------------------------------------
def test_condition_from_wttr_text_priority():
    """顺序即优先级：雷雨→thunder（不是 rain），小雪→snow。"""
    assert condition_from_wttr_text("雷雨") == "thunder"
    assert condition_from_wttr_text("小雨") == "drizzle"
    assert condition_from_wttr_text("中雨") == "rain"
    assert condition_from_wttr_text("多云") == "partly_cloudy"
    assert condition_from_wttr_text("阴") == "overcast"
    assert condition_from_wttr_text("晴") == "clear"
    assert condition_from_wttr_text("霾") == "haze"
    assert condition_from_wttr_text("薄雾") == "fog"
    assert condition_from_wttr_text("外星天气") == "unknown"


def test_condition_from_wmo():
    assert condition_from_wmo(0) == "clear"
    assert condition_from_wmo(3) == "overcast"
    assert condition_from_wmo(61) == "rain"
    assert condition_from_wmo(75) == "snow"
    assert condition_from_wmo(95) == "thunder"
    assert condition_from_wmo(48) == "fog"
    assert condition_from_wmo(9999) == "unknown"


def test_condition_from_wttr_english_fallback():
    """lang=zh 不稳：英文描述也必须能归一化（真机冒烟实测 wttr 返回过 Overcast）。"""
    assert condition_from_wttr_text("Overcast") == "overcast"
    assert condition_from_wttr_text("Sunny") == "clear"
    assert condition_from_wttr_text("Light rain") == "drizzle"
    assert condition_from_wttr_text("Patchy rain nearby") == "drizzle"
    assert condition_from_wttr_text("Heavy rain") == "rain"
    assert condition_from_wttr_text("Moderate snow") == "snow"
    assert condition_from_wttr_text("Thundery outbreaks possible") == "thunder"
    assert condition_from_wttr_text("Mist") == "fog"
    assert condition_from_wttr_text("Partly cloudy") == "partly_cloudy"


# ---------------------------------------------------------------------------
# 她的口味（萧哥 2026-10-15 举例：喜欢雨天/晴天，不喜阴天）
# ----------------------------------------------------------------------
def test_taste_matches_xiaoge_examples():
    """口味表兑现拍板举例：雨/晴让她愉悦，阴让她发闷。"""
    assert WEATHER_MOOD_DELTAS["rain"]["serotonin"] > 0
    assert WEATHER_MOOD_DELTAS["clear"]["dopamine"] > 0
    assert WEATHER_MOOD_DELTAS["overcast"]["dopamine"] < 0
    assert WEATHER_MOOD_DELTAS["overcast"]["cortisol"] > 0


def test_taste_ambient_magnitude_capped():
    """天气是氛围不是事件：单项幅度 ≤1.5（她不因连阴三天而崩）。"""
    for cond, deltas in WEATHER_MOOD_DELTAS.items():
        for k, v in deltas.items():
            assert abs(v) <= 1.5, f"{cond}.{k}={v} 超氛围幅度"


def test_weather_mood_event():
    assert weather_mood_event("rain") == "weather_rain"
    assert weather_mood_event("clear") == "weather_clear"
    assert weather_mood_event("unknown") is None
    assert weather_mood_event("不存在的天气") is None


# ---------------------------------------------------------------------------
# 取天气：缓存 / 双源 / 兜底
# ----------------------------------------------------------------------
def test_fresh_cache_no_network(base, monkeypatch):
    """缓存新鲜 → 直接返回，绝不打网络（fixture 里网络已设炸）。"""
    save_cache(base, {
        "data": {"condition": "clear", "temp_c": 25.0, "location": "宁波"},
        "fetched_at_ts": __import__("time").time(),
    })
    wx = fetch_weather(base)
    assert wx and wx["temp_c"] == 25.0


def test_stale_cache_both_sources_fail_returns_stale(base, monkeypatch):
    """刷新失败 → 退回旧缓存（陈旧事实 > 没有事实）。"""
    import time as _t
    save_cache(base, {
        "data": {"condition": "overcast", "temp_c": 18.0, "location": "宁波"},
        "fetched_at_ts": _t.time() - 3600,
    })
    monkeypatch.setattr(wx_mod, "_fetch_wttr", lambda: None)
    monkeypatch.setattr(wx_mod, "_fetch_open_meteo", lambda: None)
    wx = fetch_weather(base)
    assert wx and wx["condition"] == "overcast"


def test_no_cache_all_fail_returns_none(base, monkeypatch):
    monkeypatch.setattr(wx_mod, "_fetch_wttr", lambda: None)
    monkeypatch.setattr(wx_mod, "_fetch_open_meteo", lambda: None)
    assert fetch_weather(base) is None


def test_wttr_success_normalizes(base, monkeypatch):
    """wttr j1 → 归一化：中文描述、当日区间取 hourly 最大降水概率。"""
    monkeypatch.setattr(wx_mod, "_http_get_json", lambda url: WTTR_J1)
    wx = fetch_weather(base)
    assert wx["provider"] == "wttr"
    assert wx["condition"] == "partly_cloudy"
    assert wx["condition_text"] == "多云"
    assert wx["temp_c"] == 21.0 and wx["humidity"] == 62
    assert wx["today"]["max_c"] == 24.0 and wx["today"]["chance_of_rain"] == 30
    # 落缓存
    assert load_cache(base)["data"]["temp_c"] == 21.0


def test_wttr_english_desc_normalized_with_zh_label(base, monkeypatch):
    """lang_zh 缺位 → 英文描述归一化 + 中文标签显示（真机冒烟形态复现）。"""
    monkeypatch.setattr(wx_mod, "_http_get_json", lambda url: WTTR_J1_EN)
    wx = fetch_weather(base)
    assert wx["provider"] == "wttr"
    assert wx["condition"] == "overcast"
    assert wx["condition_text"] == "阴", "英文描述应显示中文标签"
    assert wx["temp_c"] == 20.0
    assert wx["today"]["chance_of_rain"] == 22


def test_wttr_down_falls_back_to_open_meteo(base, monkeypatch):
    """wttr 挂 → open-meteo 兜底（WMO 3 → overcast）。"""
    def fake_get(url):
        if "wttr.in" in url:
            return None
        return OPEN_METEO
    monkeypatch.setattr(wx_mod, "_http_get_json", fake_get)
    wx = fetch_weather(base)
    assert wx["provider"] == "open-meteo"
    assert wx["condition"] == "overcast"
    assert wx["temp_c"] == 21.0
    assert wx["today"]["chance_of_rain"] == 30


def test_both_sources_garbage_fail_open(base, monkeypatch):
    """两个源都返回解析不了的垃圾 → None，不抛。"""
    monkeypatch.setattr(wx_mod, "_http_get_json", lambda url: {"junk": True})
    assert fetch_weather(base) is None


def test_master_switch_off(base, monkeypatch):
    """DESKMATE_WEATHER=0 → 取数/注入/情绪全静默（连缓存都不读）。"""
    monkeypatch.setenv("DESKMATE_WEATHER", "0")
    save_cache(base, {"data": {"condition": "clear", "temp_c": 25.0},
                      "fetched_at_ts": __import__("time").time()})
    assert fetch_weather(base) is None
    assert weather_context("今天天气怎么样", base) is None
    assert apply_weather_mood(base) is None


# ---------------------------------------------------------------------------
# 情绪影响：去重 / 能力探测 / fail-open
# ----------------------------------------------------------------------
def _seed(base, condition="rain", temp=18.0):
    save_cache(base, {
        "data": {"condition": condition, "temp_c": temp, "location": "宁波",
                 "condition_text": "小雨"},
        "fetched_at_ts": __import__("time").time(),
    })


def test_mood_applies_once_per_day_same_condition(base, engine_spy):
    """同一天同一天气只影响一次（天气是慢变量，不按小时刷）。"""
    _seed(base, "rain")
    r1 = apply_weather_mood(base)
    assert r1 and r1["event"] == "weather_rain"
    assert engine_spy == ["weather_rain"]
    r2 = apply_weather_mood(base)
    assert r2 is None
    assert engine_spy == ["weather_rain"], "第二次不应再刷"


def test_mood_reapplies_on_condition_change(base, engine_spy):
    """天气变了（雨→晴）→ 再算一次。"""
    _seed(base, "rain")
    apply_weather_mood(base)
    _seed(base, "clear")
    r = apply_weather_mood(base)
    assert r and r["event"] == "weather_clear"
    assert engine_spy == ["weather_rain", "weather_clear"]


def test_mood_skips_when_engine_rule_missing(base, monkeypatch):
    """引擎还没接 weather_* 规则（二期交付前的窗口期）→ 静默跳过、不记去重。

    规则补上后自动生效——同一天内下次调用仍会尝试（不因跳过而吞掉一次）。
    """
    from deskmate.growth.emotion_engine import EmotionEngine

    monkeypatch.setattr(
        EmotionEngine, "EVENT_DIRECT_DELTAS",
        {k: v for k, v in EmotionEngine.EVENT_DIRECT_DELTAS.items()},
    )
    monkeypatch.setattr(
        EmotionEngine, "apply_event_direct",
        lambda self, event: (_ for _ in ()).throw(AssertionError("不该被调用")),
    )
    _seed(base, "rain")
    r = apply_weather_mood(base)
    assert r == {"skipped": "engine_rule_missing", "event": "weather_rain"}
    assert "last_mood" not in load_cache(base), "跳过不应记去重"


def test_mood_switch_off_but_report_still_works(base, monkeypatch, engine_spy):
    """DESKMATE_WEATHER_MOOD=0 → 只关情绪，播报照常。"""
    monkeypatch.setenv("DESKMATE_WEATHER_MOOD", "0")
    _seed(base, "rain")
    assert apply_weather_mood(base) is None
    assert engine_spy == []
    ctx = weather_context("今天要下雨吗", base)
    assert ctx and "小雨" in ctx


def test_mood_fail_open_on_fetch_error(base, monkeypatch, engine_spy):
    """取数抛异常 → None，绝不崩主流程。"""
    monkeypatch.setattr(
        wx_mod, "fetch_weather",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert apply_weather_mood(base) is None


# ---------------------------------------------------------------------------
# 聊天注入：关键词 / 氛围行 / 安静
# ----------------------------------------------------------------------
def test_context_keyword_full_block(base):
    """他问天气 → 完整块：气温/湿度/今日区间/降水概率 + 「别编」嘱托。"""
    save_cache(base, {
        "data": {"condition": "rain", "condition_text": "小雨", "temp_c": 18.0,
                 "humidity": 88, "location": "宁波",
                 "today": {"max_c": 20.0, "min_c": 16.0, "chance_of_rain": 80}},
        "fetched_at_ts": __import__("time").time(),
    })
    ctx = weather_context("明天会下雨吗？出门要带伞吗", base)
    assert ctx
    assert "宁波现在 18°C，小雨" in ctx
    assert "湿度 88%" in ctx
    assert "今天 16~20°C" in ctx
    assert "降水概率 80%" in ctx
    assert "别编" in ctx


def test_context_ambient_precip_without_keyword(base):
    """没人问但外面正下雨 → 一行氛围（她置身其中，时间朝向的「当下」）。"""
    _seed(base, "rain")
    ctx = weather_context("在干嘛", base)
    assert ctx == "外面正下着雨（宁波）。"


def test_context_quiet_when_clear_and_unasked(base):
    """晴着且没人问 → None（不是什么都值得说）。"""
    _seed(base, "clear")
    assert weather_context("在干嘛", base) is None


def test_context_no_data_returns_none(base, monkeypatch):
    monkeypatch.setattr(wx_mod, "_fetch_wttr", lambda: None)
    monkeypatch.setattr(wx_mod, "_fetch_open_meteo", lambda: None)
    assert weather_context("今天天气怎么样", base) is None


def test_context_fail_open(base, monkeypatch):
    monkeypatch.setattr(
        wx_mod, "fetch_weather",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert weather_context("天气", base) is None
