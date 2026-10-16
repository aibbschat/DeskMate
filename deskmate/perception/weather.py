"""天气感知 —— 让她知道窗外，也让窗外走进她。

2026-10-15 萧哥定调的两个用途：
1. **影响情绪**（独立通道·规则直驱的延伸）：天气是环境事件，不是 LLM 判的——
   她的口味写在 :data:`WEATHER_MOOD_DELTAS`（喜欢雨天/晴天，不喜阴天），
   数值只走规则表，经 ``EmotionEngine.apply_event_direct("weather_*")`` 落参，
   与 reflection_done / feeling_understood 同一范式：**LLM 不碰参数**。
2. **实用功能**：他问「今天天气怎么样/要下雨吗/出门带伞吗」时，
   :func:`weather_context` 把真实天气注入聊天上下文——她用真实数据回答，不编。

设计红线（延续独立通道）：
- **fail-open**：任何一步出错（没网/超时/解析失败/开关关）→ 当没这回事，
  绝不阻断主聊天与 proactivity tick。
- **缓存优先**：磁盘缓存 + TTL（默认 30min），不每轮打网络；缓存过期时
  先尝试刷新，刷新失败退回旧缓存（陈旧事实 > 没有事实）。
- **不刷屏**：情绪影响按「同一天同一天气只算一次」去重（天气是慢变量，
  每小时扣一次 dopamine 会把她磨成长期低落——那是 bug 不是 feature）。
- **数据源免密钥**：默认 wttr.in（中文城市名+中文描述），兜底 Open-Meteo
  （WMO 码，需坐标）。都不需要账号，本地优先，开箱即用。

配置（环境变量，调用时取值——paths.py 的教训，不缓存模块级常量）：
- ``DESKMATE_WEATHER=0``      总开关（默认开）
- ``DESKMATE_WEATHER_MOOD=0`` 只关情绪影响，保留播报（默认开）
- ``DESKMATE_WEATHER_LOCATION`` 城市名（**手动覆盖**；不设则走 IP 定位——
  萧哥不在宁波时，她嘴里的天气应跟着他走，不能人不在、嘴还拴在宁波）
- ``DESKMATE_WEATHER_LAT/LON`` 手动覆盖坐标（配合 LOCATION 用）
- ``DESKMATE_WEATHER_TTL_MIN`` 天气缓存新鲜期分钟数（默认 30）

位置解析链（2026-10-16 萧哥拍板「跟着 IP 走」）：手动覆盖 → IP 定位缓存（12h）→
ipapi.co 刷新 → 退回宁波（家）。定位缓存与天气缓存同文件，陈旧定位照用（陈旧 > 没有）。
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 她的口味：天气 → 参数变化（情绪规则表，数值唯一来源）
# ---------------------------------------------------------------------------
# 原则：天气是氛围不是事件——单项 ≤1.5，整体温和（她不该因为连阴三天就崩，
# 也不该因为一出太阳就嗨到失真）。喜欢雨天/晴天、不喜阴天（萧哥 2026-10-15 举例）。
WEATHER_MOOD_DELTAS: dict[str, dict[str, float]] = {
    "clear":         {"dopamine": +1.5, "serotonin": +0.5},   # 晴——她喜欢，亮堂
    "partly_cloudy": {"serotonin": +0.5},                     # 少云——舒服
    "overcast":      {"dopamine": -1.0, "cortisol": +0.5},    # 阴——她不喜，发闷
    "rain":          {"serotonin": +1.0, "dopamine": +0.5},   # 雨——她喜欢，有人听雨写字
    "drizzle":       {"serotonin": +0.5},                     # 毛毛雨——湿润的安静
    "thunder":       {"cortisol": +1.0},                      # 雷雨——不安
    "snow":          {"dopamine": +1.0, "serotonin": +0.5},   # 雪——新奇
    "fog":           {"cortisol": +0.5},                      # 雾——轻微不安
    "haze":          {"cortisol": +0.5, "dopamine": -0.5},    # 霾——闷
}
# 其余条件（unknown 等）不进表——没有把握就不动她的参数。

# 人类可读描述（写 emotion_log / 注入用），与 EVENT_DIRECT_DESC 同格式
WEATHER_MOOD_DESC: dict[str, str] = {
    "clear": "外面天晴，亮堂堂的",
    "partly_cloudy": "外面少云，舒坦",
    "overcast": "外面阴着，有点发闷",
    "rain": "外面在下雨，她喜欢听雨",
    "drizzle": "外面飘着毛毛雨",
    "thunder": "外面打雷，她有点不安",
    "snow": "外面下雪了，新奇",
    "fog": "外面有雾",
    "haze": "外面有霾，闷",
}

# 天气条件的标准 key（归一化层输出）
CONDITIONS = (
    "clear", "partly_cloudy", "overcast", "rain", "drizzle",
    "thunder", "snow", "fog", "haze", "unknown",
)

# 标准 key → 中文标签（注入块/氛围行人话；wttr 的 lang=zh 不稳时用它兜底显示）
CONDITION_LABELS: dict[str, str] = {
    "clear": "晴", "partly_cloudy": "多云", "overcast": "阴",
    "rain": "雨", "drizzle": "小雨", "thunder": "雷雨",
    "snow": "雪", "fog": "雾", "haze": "霾", "unknown": "未知",
}

# 降水类（注入「此刻窗外」氛围行的条件——她置身其中）
_PRECIPITATION = {"rain", "drizzle", "thunder", "snow"}

# 氛围行措辞（不说「正下着小雨」这种叠字，按条件给一句人话）
_AMBIENT_PHRASE: dict[str, str] = {
    "rain": "外面正下着雨",
    "drizzle": "外面飘着毛毛雨",
    "thunder": "外面在打雷",
    "snow": "外面下雪了",
}

# 聊天注入关键词：他问起窗外/出门相关时触发（命中即注入真实天气）
_WEATHER_QUERY_WORDS = (
    "天气", "下雨", "雨后", "阵雨", "雷雨", "雷电", "打雷",
    "出门", "伞", "气温", "温度", "多少度", "几度", "冷", "热", "晒",
    "阴天", "晴天", "下雪", "雪花", "雾霾", "霾", "大雾", "湿度",
)

_DEFAULT_LOCATION = "宁波"
_DEFAULT_LAT, _DEFAULT_LON = 29.87, 121.55   # 宁波（她和萧哥的家，全挂时的兜底）
_HTTP_TIMEOUT = 8.0
_IP_GEO_TIMEOUT = 5.0          # IP 定位超时（比天气更短：位置是手段不是目的）
_IP_GEO_TTL = 12 * 3600.0      # 定位缓存新鲜期（位置是慢变量，同城移动无感）
# IP 定位源链（顺序=优先级）。2026-10-16 实测：ipapi.co 对无 JS 客户端一律返回
# Cloudflare managed challenge（HTTP 403 HTML）⇒ 永远解析不出 JSON ⇒ 定位静默
# 失效走宁波兜底——萧哥实测「她识别不出城市、天气还是宁波」的根因。
# ip-api.com 首选：免费免 key、lang=zh-CN 中文城市名、含经纬度。备案两条：
# ① 免费层仅 http（不传任何敏感信息，仅按出口 IP 定个位；45 req/min，
#   我们 12h 才打一次无感）；② 它的 city 可能是区/县级（如长沙「青园」而非
#   「长沙」）——天气双源里 wttr 按城市名查，查不到 open-meteo 按坐标兜底。
# ipapi.co 保留为次选（HTTPS；万一哪天 CF 放过无 JS 客户端）。


# ---------------------------------------------------------------------------
# 配置（调用时取值）
# ---------------------------------------------------------------------------
def _enabled() -> bool:
    return os.environ.get("DESKMATE_WEATHER", "1") != "0"


def _mood_enabled() -> bool:
    return _enabled() and os.environ.get("DESKMATE_WEATHER_MOOD", "1") != "0"


def _location() -> str:
    """城市名纯读取：手动覆盖 → 定位缓存 → 宁波兜底。**不触发网络**。

    注：本函数故意不联网——它被 _fetch_wttr/_fetch_open_meteo/_format_weather_block
    多处调用，联网解析只允许发生在 fetch_weather 单点（见 _resolve_location）。
    """
    env = (os.environ.get("DESKMATE_WEATHER_LOCATION") or "").strip()
    if env:
        return env
    cached = (load_cache(None).get("geo") or {})
    city = str(cached.get("city") or "").strip()
    return city or _DEFAULT_LOCATION


def _location_of(cache: dict) -> str:
    """同 _location，但从给定 cache dict 取（避免 fetch 链路重复读盘）。"""
    env = (os.environ.get("DESKMATE_WEATHER_LOCATION") or "").strip()
    if env:
        return env
    city = str((cache.get("geo") or {}).get("city") or "").strip()
    return city or _DEFAULT_LOCATION


def _coords() -> tuple[float, float]:
    """坐标纯读取：手动 LAT/LON → 定位缓存 → 宁波兜底。不触发网络。"""
    lat_env, lon_env = os.environ.get("DESKMATE_WEATHER_LAT"), os.environ.get("DESKMATE_WEATHER_LON")
    if lat_env and lon_env:
        try:
            return float(lat_env), float(lon_env)
        except (TypeError, ValueError):
            pass
    geo = load_cache(None).get("geo") or {}
    try:
        lat, lon = float(geo.get("lat")), float(geo.get("lon"))
        return lat, lon
    except (TypeError, ValueError):
        return _DEFAULT_LAT, _DEFAULT_LON


def _coords_of(cache: dict) -> tuple[float, float]:
    """同 _coords，但从给定 cache dict 取。"""
    lat_env, lon_env = os.environ.get("DESKMATE_WEATHER_LAT"), os.environ.get("DESKMATE_WEATHER_LON")
    if lat_env and lon_env:
        try:
            return float(lat_env), float(lon_env)
        except (TypeError, ValueError):
            pass
    geo = cache.get("geo") or {}
    try:
        lat, lon = float(geo.get("lat")), float(geo.get("lon"))
        return lat, lon
    except (TypeError, ValueError):
        return _DEFAULT_LAT, _DEFAULT_LON


def _ttl_sec() -> float:
    try:
        return max(60.0, float(os.environ.get("DESKMATE_WEATHER_TTL_MIN", "30")) * 60.0)
    except (TypeError, ValueError):
        return 30.0 * 60.0


# ---------------------------------------------------------------------------
# 缓存（原子写；陈旧事实 > 没有事实）
# ---------------------------------------------------------------------------
def _cache_path(base_dir: Path | str | None) -> Path:
    from deskmate.paths import deskmate_base_dir

    base = Path(base_dir) if base_dir else deskmate_base_dir()
    return base / "perception" / "weather_cache.json"


def load_cache(base_dir: Path | str | None = None) -> dict:
    """读缓存；任何异常 → 空 dict（绝不抛）。"""
    try:
        p = _cache_path(base_dir)
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_cache(base_dir: Path | str | None, cache: dict) -> None:
    """原子写缓存；失败静默（缓存是优化不是事实源）。"""
    try:
        p = _cache_path(base_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        logger.debug("weather cache save failed", exc_info=True)


def _fresh(cache: dict, now_ts: float | None = None) -> bool:
    try:
        fetched = float(cache.get("fetched_at_ts") or 0.0)
        return fetched > 0 and (now_ts or time.time()) - fetched < _ttl_sec()
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 条件归一化：两个源的表述 → 统一 key
# ---------------------------------------------------------------------------
# wttr.in lang=zh 的天气描述 → key
# 顺序即优先级：具体先于通用（「小雨」必须排在「雨」前面，否则被雨水类吞掉）
_WTTR_TEXT_MAP: list[tuple[tuple[str, ...], str]] = [
    (("雷",), "thunder"),
    (("雪", "冰雹"), "snow"),
    (("小雨", "毛毛雨", "细雨", "霰"), "drizzle"),
    (("暴雨", "大雨", "中雨", "阵雨", "雨"), "rain"),
    (("霾",), "haze"),
    (("雾",), "fog"),
    (("阴",), "overcast"),
    (("多云", "少云", "疏云"), "partly_cloudy"),
    (("晴", "晴朗", "艳阳"), "clear"),
]

# Open-Meteo WMO 天气码 → key
_WMO_MAP: dict[int, str] = {
    0: "clear", 1: "clear", 2: "partly_cloudy", 3: "overcast",
    45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle", 56: "drizzle", 57: "drizzle",
    61: "rain", 63: "rain", 65: "rain", 66: "rain", 67: "rain",
    80: "rain", 81: "rain", 82: "rain",
    71: "snow", 73: "snow", 75: "snow", 77: "snow", 85: "snow", 86: "snow",
    95: "thunder", 96: "thunder", 99: "thunder",
}


# wttr.in 的英文天气描述 → key（lang=zh 参数并不总是生效，英文必须也能归一化）
# 顺序即优先级：具体先于通用（"Light rain" 要先于 "rain" 相关generic）
_WTTR_EN_MAP: list[tuple[tuple[str, ...], str]] = [
    (("thunder",), "thunder"),
    (("snow", "sleet", "ice pellet"), "snow"),
    (("drizzle", "light rain", "patchy rain"), "drizzle"),
    (("heavy rain", "torrential", "moderate rain", "rain"), "rain"),
    (("haze", "smoke"), "haze"),
    (("mist", "fog"), "fog"),
    (("overcast",), "overcast"),
    (("partly cloudy", "cloudy", "cloud"), "partly_cloudy"),
    (("sunny", "clear"), "clear"),
]


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


def condition_from_wttr_text(text: str) -> str:
    """wttr 天气描述（中文或英文）→ 标准 key。

    顺序即优先级：雷/雪先于雨；具体（小雨）先于通用（雨）。
    """
    t = (text or "").strip()
    if not t:
        return "unknown"
    for keys, cond in _WTTR_TEXT_MAP:
        if any(k in t for k in keys):
            return cond
    low = t.lower()
    for keys, cond in _WTTR_EN_MAP:
        if any(k in low for k in keys):
            return cond
    return "unknown"


def condition_from_wmo(code: int) -> str:
    """WMO 天气码 → 标准 key。"""
    return _WMO_MAP.get(int(code), "unknown")


# ---------------------------------------------------------------------------
# HTTP（httpx 核心依赖；超时短，失败即换源/退回缓存）
# ---------------------------------------------------------------------------
def _http_get_json(url: str, timeout: float | None = None) -> dict | None:
    try:
        import httpx

        with httpx.Client(timeout=timeout or _HTTP_TIMEOUT, follow_redirects=True) as cli:
            resp = cli.get(url, headers={"User-Agent": "curl/8.0 (deskmate-weather)"})
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception:
        logger.debug("weather http failed: %s", url, exc_info=True)
        return None


def _fetch_wttr(location: str | None = None) -> dict | None:
    """wttr.in（免密钥，中文城市名）。

    请求带 lang=zh，但该参数并不总是生效——lang_zh 缺位时退回 weatherDesc
    英文描述归一化，显示文本取中文标签（CONDITION_LABELS）。
    location 由 fetch_weather 单点解析后传入；None 时退回 _location()（直调兼容）。
    """
    loc = urllib.parse.quote(location or _location())
    data = _http_get_json(f"https://wttr.in/{loc}?format=j1&lang=zh")
    if not isinstance(data, dict):
        return None
    try:
        cur = (data.get("current_condition") or [{}])[0]
        text_zh = ((cur.get("lang_zh") or [{}])[0].get("value") or "").strip()
        text_en = ((cur.get("weatherDesc") or [{}])[0].get("value") or "").strip()
        # lang=zh 参数并不总是生效：中文没有就用英文描述归一化，显示取中文标签
        raw_text = text_zh if _has_cjk(text_zh) else text_en
        if not raw_text:
            return None
        cond = condition_from_wttr_text(raw_text)
        temp_c = float(cur.get("temp_C") or 0.0)
        feels = float(cur.get("FeelsLikeC") or temp_c)
        humidity = int(float(cur.get("humidity") or 0))
        if temp_c == 0.0:
            return None
        cond_text = text_zh if _has_cjk(text_zh) else CONDITION_LABELS.get(cond, raw_text)
        today: dict = {}
        days = data.get("weather") or []
        if days:
            d0 = days[0]
            hourly = d0.get("hourly") or []
            chance = 0
            for h in hourly:
                try:
                    chance = max(chance, int(float(h.get("chanceofrain") or 0)))
                except (TypeError, ValueError):
                    continue
            today = {
                "max_c": float(d0.get("maxtempC") or 0.0),
                "min_c": float(d0.get("mintempC") or 0.0),
                "chance_of_rain": chance,
            }
        return {
            "location": location or _location(),
            "condition": cond,
            "condition_text": cond_text,
            "temp_c": round(temp_c, 1),
            "feels_like_c": round(feels, 1),
            "humidity": humidity,
            "today": today,
            "provider": "wttr",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception:
        logger.debug("wttr parse failed", exc_info=True)
        return None


def _fetch_open_meteo(
    lat: float | None = None, lon: float | None = None, location: str | None = None
) -> dict | None:
    """Open-Meteo（免密钥，WMO 码，需坐标）。坐标/城市由 fetch_weather 单点解析传入。"""
    if lat is None or lon is None:
        lat, lon = _coords()
    if location is None:
        location = _location()
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=Asia%2FShanghai"
    )
    data = _http_get_json(url)
    if not isinstance(data, dict):
        return None
    try:
        cur = data.get("current") or {}
        code = int(cur.get("weather_code") or -1)
        temp_c = float(cur.get("temperature_2m") or 0.0)
        if code < 0 or temp_c == 0.0:
            return None
        daily = data.get("daily") or {}
        today = {
            "max_c": float((daily.get("temperature_2m_max") or [0.0])[0]),
            "min_c": float((daily.get("temperature_2m_min") or [0.0])[0]),
            "chance_of_rain": int((daily.get("precipitation_probability_max") or [0])[0] or 0),
        }
        return {
            "location": location,
            "condition": condition_from_wmo(code),
            "condition_text": f"WMO:{code}",
            "temp_c": round(temp_c, 1),
            "feels_like_c": round(float(cur.get("apparent_temperature") or temp_c), 1),
            "humidity": int(cur.get("relative_humidity_2m") or 0),
            "today": today,
            "provider": "open-meteo",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception:
        logger.debug("open-meteo parse failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# IP 定位（2026-10-16 萧哥拍板「跟着 IP 走」：他人不在宁波，她嘴里的天气得跟他走）
# ---------------------------------------------------------------------------
def _parse_ip_api_com(data: dict) -> dict | None:
    """ip-api.com 响应 → {city, lat, lon}；status 非 success/字段缺 → None。"""
    try:
        if str(data.get("status") or "") != "success":
            return None
        city = str(data.get("city") or "").strip()
        if not city:
            return None
        return {"city": city, "lat": float(data["lat"]), "lon": float(data["lon"])}
    except (KeyError, TypeError, ValueError):
        return None


def _parse_ipapi_co(data: dict) -> dict | None:
    """ipapi.co 响应 → {city, lat, lon}（city 英文，备案见上）。"""
    try:
        city = str(data.get("city") or "").strip()
        if not city:
            return None
        return {"city": city, "lat": float(data["latitude"]), "lon": float(data["longitude"])}
    except (KeyError, TypeError, ValueError):
        return None


_IP_GEO_SOURCES: tuple[tuple[str, object], ...] = (
    ("http://ip-api.com/json/?lang=zh-CN", _parse_ip_api_com),
    ("https://ipapi.co/json/", _parse_ipapi_co),
)


def _fetch_ip_geo() -> dict | None:
    """IP 定位源链：ip-api.com → ipapi.co；全失败 → None（绝不抛）。

    单源挂（被墙/被 Cloudflare 拦/限流）自动落下一源：位置是手段不是目的，
    但「唯一源静默失效」曾让定位整体退回宁波兜底且不报错（10-16 实测根因）。
    """
    for url, parse in _IP_GEO_SOURCES:
        data = _http_get_json(url, timeout=_IP_GEO_TIMEOUT)
        if not isinstance(data, dict):
            continue
        geo = parse(data)
        if geo:
            return geo
    return None


def _resolve_location(base_dir: Path | str | None, cache: dict) -> tuple[str, float, float, dict]:
    """位置解析单点：手动覆盖 → 缓存新鲜 → 联网刷新 → 缓存陈旧 → 宁波兜底。

    只在 fetch_weather 刷新天气时调用（天气 TTL 30min，定位 TTL 12h——
    定位网打得更少）。返回 (城市, 纬, 经, 可能更新过的 cache)。
    """
    lat_env = os.environ.get("DESKMATE_WEATHER_LAT")
    lon_env = os.environ.get("DESKMATE_WEATHER_LON")
    if lat_env and lon_env:
        try:
            lat, lon = float(lat_env), float(lon_env)
            city = (os.environ.get("DESKMATE_WEATHER_LOCATION") or "").strip() or f"{lat:.2f},{lon:.2f}"
            return city, lat, lon, cache
        except (TypeError, ValueError):
            pass
    env = (os.environ.get("DESKMATE_WEATHER_LOCATION") or "").strip()
    if env:
        lat, lon = _coords_of(cache)
        return env, lat, lon, cache

    geo = cache.get("geo") or {}
    geo_ts = float(geo.get("ts") or 0.0)
    if geo.get("city") and time.time() - geo_ts < _IP_GEO_TTL:
        return str(geo["city"]), float(geo["lat"]), float(geo["lon"]), cache

    fresh = _fetch_ip_geo()
    if fresh:
        cache = dict(cache)
        cache["geo"] = {**fresh, "ts": time.time()}
        save_cache(base_dir, cache)
        return fresh["city"], fresh["lat"], fresh["lon"], cache

    if geo.get("city"):   # 陈旧定位 > 没有定位（跨城当天仍准）
        return str(geo["city"]), float(geo["lat"]), float(geo["lon"]), cache
    return _DEFAULT_LOCATION, _DEFAULT_LAT, _DEFAULT_LON, cache


# ---------------------------------------------------------------------------
# 对外：取天气（缓存优先，双源兜底，全 fail-open）
# ---------------------------------------------------------------------------
def fetch_weather(base_dir: Path | str | None = None, *, force: bool = False) -> Optional[dict]:
    """当前天气（归一化 dict）；取不到返回 None。

    - 总开关关 → None
    - 缓存新鲜 → 直接返回（不打网络）
    - 否则刷新：先单点解析位置（IP 定位，手动覆盖优先），再 wttr → open-meteo；
      都失败 → 退回旧缓存（哪怕过期）；一无所有 → None
    """
    if not _enabled():
        return None
    cache = load_cache(base_dir)
    data = cache.get("data")
    if isinstance(data, dict) and data and not force and _fresh(cache):
        return data
    city, lat, lon, cache = _resolve_location(base_dir, cache)
    fresh = _fetch_wttr(city) or _fetch_open_meteo(lat, lon, city)
    if fresh:
        cache = dict(cache)
        cache["data"] = fresh
        cache["fetched_at"] = fresh.get("fetched_at")
        cache["fetched_at_ts"] = time.time()
        save_cache(base_dir, cache)
        return fresh
    return data if isinstance(data, dict) and data else None


# ---------------------------------------------------------------------------
# 对外：情绪影响（规则直驱，LLM 不碰参数）
# ---------------------------------------------------------------------------
def weather_mood_event(condition: str) -> Optional[str]:
    """天气条件 → 事件名（``weather_<condition>``）；她没口味/未知 → None。"""
    if condition in WEATHER_MOOD_DELTAS:
        return f"weather_{condition}"
    return None


def apply_weather_mood(base_dir: Path | str | None = None) -> Optional[dict]:
    """天气 → 她的参数（经 EmotionEngine 规则表，与 feeling_understood 同范式）。

    去重：同一天同一天气只影响一次（天气是慢变量，不按小时刷）。
    引擎侧还没有 ``weather_*`` 规则时（接线未落地）→ 静默跳过、不记去重，
    等规则补上即自动生效（能力探测，不抛错）。
    """
    if not _mood_enabled():
        return None
    try:
        wx = fetch_weather(base_dir)
        if not wx:
            return None
        condition = str(wx.get("condition") or "unknown")
        event = weather_mood_event(condition)
        if not event:
            return None
        # 能力探测：引擎规则表里有这条才落参（接线未落地时安静等）
        from deskmate.growth.emotion_engine import EmotionEngine

        if event not in EmotionEngine.EVENT_DIRECT_DELTAS:
            return {"skipped": "engine_rule_missing", "event": event}
        cache = load_cache(base_dir)
        last = cache.get("last_mood") or {}
        today = date.today().isoformat()
        if last.get("date") == today and last.get("condition") == condition:
            return None  # 同一天同一天气只算一次
        EmotionEngine(base_dir=base_dir).apply_event_direct(event)
        cache = dict(cache)
        cache["last_mood"] = {"date": today, "condition": condition}
        save_cache(base_dir, cache)
        return {"event": event, "condition": condition, "temp_c": wx.get("temp_c")}
    except Exception:
        logger.debug("apply_weather_mood failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 对外：聊天注入（她知道的窗外 + 报天气的素材）
# ---------------------------------------------------------------------------
def _format_weather_block(wx: dict) -> str:
    loc = wx.get("location") or _location()
    cond = wx.get("condition_text") or "未知"
    temp = wx.get("temp_c")
    hum = wx.get("humidity")
    today = wx.get("today") or {}
    parts = [f"{loc}现在 {temp:g}°C，{cond}"]
    if hum:
        parts.append(f"湿度 {hum}%")
    line = "，".join(parts) + "。"
    if today:
        bits = []
        if today.get("min_c") and today.get("max_c"):
            bits.append(f"今天 {today['min_c']:g}~{today['max_c']:g}°C")
        if today.get("chance_of_rain") is not None:
            bits.append(f"降水概率 {today['chance_of_rain']}%")
        if bits:
            line += "，".join(bits) + "。"
    return (
        "## 此刻窗外（真实世界的天气，你知道的）\n"
        f"{line}\n"
        "他问起天气，就用上面的真实数据回答，别编；"
        "也可以顺带说说你自己喜不喜欢这样的天。"
    )


def weather_context(user_message: str = "", base_dir: Path | str | None = None) -> Optional[str]:
    """聊天注入块：他问起窗外（关键词）或外面正下雨/雪/打雷（氛围行）时给出。

    - 关键词命中 → 完整天气块（气温/湿度/今日区间/降水概率）
    - 无关键词但正降水 → 一行氛围（「外面正下着雨（宁波）。」）——她置身其中
    - 其余（晴/阴且没人问）→ None：不是什么都值得说
    """
    if not _enabled():
        return None
    try:
        wx = fetch_weather(base_dir)
        if not wx:
            return None
        msg = user_message or ""
        if any(w in msg for w in _WEATHER_QUERY_WORDS):
            return _format_weather_block(wx)
        if str(wx.get("condition")) in _PRECIPITATION:
            phrase = _AMBIENT_PHRASE.get(str(wx.get("condition")), "外面天气有点动静")
            return f"{phrase}（{wx.get('location') or _location()}）。"
        return None
    except Exception:
        logger.debug("weather_context failed", exc_info=True)
        return None
