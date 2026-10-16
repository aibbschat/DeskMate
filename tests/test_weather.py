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

# IP 定位源链样本（2026-10-16 真机实测；ipapi.co 为被 CF 拦之前的正常形态）
IP_API_COM_J1 = {  # 备选 JSON 源：status=success + 中文城市 + lat/lon（免费库数据旧）
    "status": "success", "country": "中国", "regionName": "湖南",
    "city": "青园", "lat": 28.1142, "lon": 112.983,
}
IPAPI_CO_J1 = {  # 垫底 JSON 源：city 英文 + latitude/longitude
    "ip": "118.254.98.222", "city": "Shaoxing",
    "latitude": 30.0, "longitude": 120.5,
}
# 国内文本源（2026-10-16 实测：同一湖南出口，国内库准确定位张家界，ip-api.com 错标青园）
IPIP_NET_TEXT = "当前 IP：118.254.98.222  来自于：中国 湖南 张家界  电信\n"
CIP_CC_TEXT = "IP\t: 118.254.98.222\n地址\t: 中国 湖南 张家界\n运营商\t: 电信\n"
GEOCODE_J1 = {"results": [  # open-meteo geocoding 中文「张家界」（补坐标用）
    {"name": "张家界", "latitude": 29.12944, "longitude": 110.47833,
     "country": "中国", "admin1": "湖南"},
]}

# base fixture 会把 _fetch_ip_geo 一刀切关；源链用例（test_geo_source_*）要放回真身
_REAL_FETCH_IP_GEO = wx_mod._fetch_ip_geo


@pytest.fixture
def base(tmp_path, monkeypatch):
    """隔离 base_dir + 默认开关全开 + 网络永不通（漏 patch 会立刻炸）+ 无手动城市。

    注意：不再默认设 DESKMATE_WEATHER_LOCATION——「跟 IP 走」的用例需要无覆盖环境。
    需要固定城市的旧用例用 ``ningbo`` fixture。
    """
    monkeypatch.setenv("DESKMATE_WEATHER", "1")
    monkeypatch.setenv("DESKMATE_WEATHER_MOOD", "1")
    monkeypatch.delenv("DESKMATE_WEATHER_LOCATION", raising=False)
    monkeypatch.delenv("DESKMATE_WEATHER_LAT", raising=False)
    monkeypatch.delenv("DESKMATE_WEATHER_LON", raising=False)
    monkeypatch.setattr(
        wx_mod, "_http_get_json",
        lambda url: (_ for _ in ()).throw(AssertionError(f"网络未 patch: {url}")),
    )
    # 默认「无网 → IP 定位失败」：各用例按需覆盖成具体城市（见 test_geo_*）
    monkeypatch.setattr(wx_mod, "_fetch_ip_geo", lambda: None)
    monkeypatch.setattr(
        wx_mod, "_http_get_text",
        lambda url: (_ for _ in ()).throw(AssertionError(f"网络(text)未 patch: {url}")),
    )
    return tmp_path


@pytest.fixture
def ningbo(base, monkeypatch):
    """旧用例兼容：手动覆盖城市=宁波（跟 IP 走引入前的默认形态）。"""
    monkeypatch.setenv("DESKMATE_WEATHER_LOCATION", "宁波")
    return base


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
    monkeypatch.setattr(wx_mod, "_fetch_wttr", lambda *a, **k: None)
    monkeypatch.setattr(wx_mod, "_fetch_open_meteo", lambda *a, **k: None)
    wx = fetch_weather(base)
    assert wx and wx["condition"] == "overcast"


def test_no_cache_all_fail_returns_none(base, monkeypatch):
    monkeypatch.setattr(wx_mod, "_fetch_wttr", lambda *a, **k: None)
    monkeypatch.setattr(wx_mod, "_fetch_open_meteo", lambda *a, **k: None)
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


# ---------------------------------------------------------------------------
# 跟 IP 走（2026-10-16 萧哥拍板：人不在宁波时她嘴里的天气得跟他走）
# ---------------------------------------------------------------------------
def _fake_wx(location):
    return {
        "location": location, "condition": "clear", "condition_text": "晴",
        "temp_c": 25.0, "feels_like_c": 26.0, "humidity": 40, "today": {},
        "provider": "wttr", "fetched_at": "2026-10-16T10:00:00",
    }


def test_geo_ip_success_feeds_both_sources(base, monkeypatch):
    """IP 定位成功 → wttr/open-meteo 都吃到定位的城市与坐标；缓存落盘。"""
    seen: dict = {}
    monkeypatch.setattr(
        wx_mod, "_fetch_ip_geo",
        lambda: {"city": "Shaoxing", "lat": 30.0, "lon": 120.5},
    )

    def fake_wttr(location=None):
        seen["wttr_loc"] = location
        return None  # 让它走 open-meteo 兜底，两个源都验

    def fake_meteo(lat=None, lon=None, location=None):
        seen.update({"lat": lat, "lon": lon, "meteo_loc": location})
        return {
            "location": location, "condition": "clear", "condition_text": "晴",
            "temp_c": 25.0, "feels_like_c": 26.0, "humidity": 40,
            "today": {}, "provider": "open-meteo",
            "fetched_at": "2026-10-16T10:00:00",
        }

    monkeypatch.setattr(wx_mod, "_fetch_wttr", fake_wttr)
    monkeypatch.setattr(wx_mod, "_fetch_open_meteo", fake_meteo)
    wx = fetch_weather(base)
    assert wx and wx["location"] == "Shaoxing"
    assert seen == {"wttr_loc": "Shaoxing", "lat": 30.0, "lon": 120.5, "meteo_loc": "Shaoxing"}
    # 定位缓存落盘
    geo = load_cache(base)["geo"]
    assert geo["city"] == "Shaoxing" and geo["lat"] == 30.0


def test_geo_fresh_cache_no_network(base, monkeypatch):
    """定位缓存 12h 新鲜 → 不再打 IP 定位（打就炸）。"""
    import time as _t
    save_cache(base, {"geo": {"city": "Shaoxing", "lat": 30.0, "lon": 120.5, "ts": _t.time()}})
    monkeypatch.setattr(
        wx_mod, "_fetch_ip_geo",
        lambda: (_ for _ in ()).throw(AssertionError("新鲜缓存不该打 IP 定位")),
    )
    seen: dict = {}

    def fake_wttr(location=None):
        seen["loc"] = location
        return _fake_wx(location)

    monkeypatch.setattr(wx_mod, "_fetch_wttr", fake_wttr)
    wx = fetch_weather(base)
    assert wx and seen["loc"] == "Shaoxing"


def test_geo_stale_cache_used_when_refresh_fails(base, monkeypatch):
    """定位缓存过期 + 刷新失败 → 用陈旧定位（陈旧 > 没有，跨城当天仍准）。"""
    import time as _t
    save_cache(base, {"geo": {
        "city": "Shaoxing", "lat": 30.0, "lon": 120.5,
        "ts": _t.time() - 24 * 3600,  # 过期
    }})
    monkeypatch.setattr(wx_mod, "_fetch_ip_geo", lambda: None)  # 刷新失败
    seen: dict = {}

    def fake_wttr(location=None):
        seen["loc"] = location
        return _fake_wx(location)

    monkeypatch.setattr(wx_mod, "_fetch_wttr", fake_wttr)
    wx = fetch_weather(base)
    assert wx and seen["loc"] == "Shaoxing"


def test_geo_manual_override_wins(base, monkeypatch):
    """手动覆盖最优先：设了城市就不打 IP 定位，也不看缓存。"""
    import time as _t
    monkeypatch.setenv("DESKMATE_WEATHER_LOCATION", "北京")
    save_cache(base, {"geo": {"city": "Shaoxing", "lat": 30.0, "lon": 120.5, "ts": _t.time()}})
    monkeypatch.setattr(
        wx_mod, "_fetch_ip_geo",
        lambda: (_ for _ in ()).throw(AssertionError("手动覆盖时不该打 IP 定位")),
    )
    seen: dict = {}

    def fake_wttr(location=None):
        seen["loc"] = location
        return _fake_wx(location)

    monkeypatch.setattr(wx_mod, "_fetch_wttr", fake_wttr)
    wx = fetch_weather(base)
    assert wx and seen["loc"] == "北京"


def test_geo_all_fail_falls_back_to_ningbo(base, monkeypatch):
    """无覆盖+无缓存+定位失败 → 宁波兜底（她的家，行为同引入前）。"""
    monkeypatch.setattr(wx_mod, "_fetch_ip_geo", lambda: None)
    seen: dict = {}

    def fake_wttr(location=None):
        seen["loc"] = location
        return _fake_wx(location)

    monkeypatch.setattr(wx_mod, "_fetch_wttr", fake_wttr)
    wx = fetch_weather(base)
    assert wx and seen["loc"] == "宁波"
    # open-meteo 兜底坐标也应是宁波
    seen2: dict = {}

    def fake_meteo(lat=None, lon=None, location=None):
        seen2.update({"lat": lat, "lon": lon})
        return {**_fake_wx(location), "provider": "open-meteo"}

    monkeypatch.setattr(wx_mod, "_fetch_wttr", lambda *a, **k: None)
    monkeypatch.setattr(wx_mod, "_fetch_open_meteo", fake_meteo)
    wx2 = fetch_weather(base, force=True)
    assert wx2 and seen2 == {"lat": 29.87, "lon": 121.55}


def test_geo_ip_response_garbage_fail_open(base, monkeypatch):
    """IP 定位返回垃圾 → 落兜底，不抛。"""
    monkeypatch.setattr(wx_mod, "_http_get_json", lambda url: {"junk": True})
    city, lat, lon, _ = wx_mod._resolve_location(base, {})
    assert (city, lat, lon) == ("宁波", 29.87, 121.55)


# ---------------------------------------------------------------------------
# IP 定位源链（2026-10-16 两轮根治：① ipapi.co 被 CF managed challenge 拦 →
# 单源静默失效；② ip-api.com 免费库数据旧，湖南出口错标「长沙青园」——
# 萧哥实际在张家界。终态：国内文本源 ipip.net/cip.cc 首选，geocoding 补坐标）
# ---------------------------------------------------------------------------
def test_parse_ipip_net_text():
    """ipip.net 文本样本 → 市级城市名（真机实测格式）。"""
    assert wx_mod._parse_ipip_net(IPIP_NET_TEXT) == {"city": "张家界"}
    assert wx_mod._parse_ipip_net("garbage 无来自于") is None


def test_parse_cip_cc_text():
    """cip.cc 多行文本样本 → 市级城市名。"""
    assert wx_mod._parse_cip_cc(CIP_CC_TEXT) == {"city": "张家界"}
    assert wx_mod._parse_cip_cc("no address line") is None


def test_city_from_addr_variants():
    """地址文本切城市名：直辖市省/市同名、自治区多段、运营商后缀都扛住。"""
    parse = wx_mod._city_from_addr
    assert parse("中国 上海 上海 电信") == {"city": "上海"}
    assert parse("中国 内蒙古 呼和浩特 联通") == {"city": "呼和浩特"}
    assert parse("中国 湖南 张家界 电信") == {"city": "张家界"}
    assert parse("   ") is None
    assert parse("中国 电信") is None


def test_geo_source_ipip_net_hits_with_geocode(base, monkeypatch):
    """首选源 ipip.net 文本命中（张家界）→ geocoding 补坐标 → 缓存落盘带坐标。"""
    monkeypatch.setattr(wx_mod, "_fetch_ip_geo", _REAL_FETCH_IP_GEO)
    monkeypatch.setattr(wx_mod, "_http_get_text", lambda url, timeout=None: IPIP_NET_TEXT)
    monkeypatch.setattr(
        wx_mod, "_http_get_json",
        lambda url, timeout=None: GEOCODE_J1 if "geocoding-api" in url
        else (_ for _ in ()).throw(AssertionError(f"意外 JSON 请求: {url}")),
    )
    city, lat, lon, _ = wx_mod._resolve_location(base, {})
    assert (city, lat, lon) == ("张家界", 29.12944, 110.47833)
    geo = load_cache(base)["geo"]
    assert geo["city"] == "张家界" and geo["lat"] == 29.12944


def test_geo_source_cip_cc_backup_when_ipip_down(base, monkeypatch):
    """ipip.net 挂 → 落 cip.cc（国内源互备）→ geocode 补坐标。"""
    monkeypatch.setattr(wx_mod, "_fetch_ip_geo", _REAL_FETCH_IP_GEO)
    hits: list[str] = []

    def fake_text(url, timeout=None):
        hits.append(url)
        return None if "ipip.net" in url else CIP_CC_TEXT

    monkeypatch.setattr(wx_mod, "_http_get_text", fake_text)
    monkeypatch.setattr(wx_mod, "_http_get_json", lambda url, timeout=None: GEOCODE_J1)
    city, lat, lon, _ = wx_mod._resolve_location(base, {})
    assert (city, lat, lon) == ("张家界", 29.12944, 110.47833)
    assert len(hits) == 2  # ipip 挂了才打 cip.cc


def test_geo_source_city_only_when_geocode_fails(base, monkeypatch):
    """国内源只给城市名 + geocode 挂 → city-only（lat/lon=None）：
    wttr 直查城市名可用；open-meteo 无坐标跳过（不错配坐标）。"""
    monkeypatch.setattr(wx_mod, "_fetch_ip_geo", _REAL_FETCH_IP_GEO)
    monkeypatch.setattr(wx_mod, "_http_get_text", lambda url, timeout=None: IPIP_NET_TEXT)
    monkeypatch.setattr(wx_mod, "_http_get_json", lambda url, timeout=None: None)  # geocode 挂
    city, lat, lon, _ = wx_mod._resolve_location(base, {})
    assert (city, lat, lon) == ("张家界", None, None)

    seen: dict = {}

    def fake_wttr(location=None):
        seen["wttr"] = location
        return None  # wttr 也挂，逼后级

    monkeypatch.setattr(wx_mod, "_fetch_wttr", fake_wttr)
    # 不 patch open-meteo：真身收 (None, None, "张家界") 应内部拒绝且不打网
    # （直调用例 test_fetch_open_meteo_no_coords_returns_none 单独锁该行为）
    wx = fetch_weather(base, force=True)
    assert seen["wttr"] == "张家界"
    assert wx is None  # 两源都挂 → None；open-meteo 没有拿错坐标硬撑


def test_fetch_open_meteo_no_coords_returns_none(base, monkeypatch):
    """无坐标直调 open-meteo → None（不拿缓存/宁波坐标硬撑）。"""
    assert wx_mod._fetch_open_meteo(None, None, "张家界") is None
    assert wx_mod._fetch_open_meteo(location="张家界") is None


def test_geo_source_json_fallback_when_text_sources_fail(base, monkeypatch):
    """两个国内文本源全挂 → 落 ip-api.com（JSON 自带坐标，虽然数据旧）。"""
    monkeypatch.setattr(wx_mod, "_fetch_ip_geo", _REAL_FETCH_IP_GEO)
    monkeypatch.setattr(wx_mod, "_http_get_text", lambda url, timeout=None: None)
    hits: list[str] = []

    def fake_json(url, timeout=None):
        hits.append(url)
        return IP_API_COM_J1 if "ip-api.com" in url else None

    monkeypatch.setattr(wx_mod, "_http_get_json", fake_json)
    city, lat, lon, _ = wx_mod._resolve_location(base, {})
    assert (city, lat, lon) == ("青园", 28.1142, 112.983)
    assert len(hits) == 1  # ip-api.com 命中即停，不打 ipapi.co


def test_geo_source_all_blocked_falls_back_to_ningbo(base, monkeypatch):
    """源链全挂（两文本 + 两 JSON 都失败）→ 宁波兜底（fail-open 不变）。"""
    monkeypatch.setattr(wx_mod, "_fetch_ip_geo", _REAL_FETCH_IP_GEO)
    hits: list[str] = []

    def fake_text(url, timeout=None):
        hits.append(url)
        return None

    def fake_json(url, timeout=None):
        hits.append(url)
        return None

    monkeypatch.setattr(wx_mod, "_http_get_text", fake_text)
    monkeypatch.setattr(wx_mod, "_http_get_json", fake_json)
    city, lat, lon, _ = wx_mod._resolve_location(base, {})
    assert (city, lat, lon) == ("宁波", 29.87, 121.55)
    assert len(hits) == 4  # 四个源都试过才兜底


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
    """能力探测：规则表里没有对应 weather_* 时 → 静默跳过、不记去重。

    （2026-10-15 二期已把 weather_* 注册进引擎表，此测改为 monkeypatch
    一张删掉 weather_rain 的表，专门守护「未知事件安全跳过」机制本身。）
    """
    from deskmate.growth.emotion_engine import EmotionEngine

    monkeypatch.setattr(
        EmotionEngine, "EVENT_DIRECT_DELTAS",
        {k: v for k, v in EmotionEngine.EVENT_DIRECT_DELTAS.items()
         if k != "weather_rain"},
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
