"""时区安全的墙上时钟工具（移植自 Aurabot real-life 模块的 lib/clock.mjs）。

不依赖进程本地时区，重启或跨时区部署都不会算偏。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

DEFAULT_TZ = "Asia/Shanghai"
_HM_RE = re.compile(r"^(\d{1,2})\s*[:：]\s*(\d{1,2})$")
_YMD_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_EPOCH = date(1970, 1, 1)


def _js_round(value: float) -> int:
    """对齐 JS Math.round（.5 向正无穷取整）。"""
    return int(math.floor(float(value) + 0.5))


@lru_cache(maxsize=32)
def _zone(name: str):
    try:
        from zoneinfo import ZoneInfo
    except Exception:  # pragma: no cover - Python < 3.9
        return timezone(timedelta(hours=8))
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:
        try:
            return ZoneInfo(DEFAULT_TZ)
        except Exception:
            # Windows 上可能缺少 IANA 时区库（tzdata），退回固定 +8
            return timezone(timedelta(hours=8))


@dataclass
class WallTime:
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    ymd: str
    hhmm: str
    hour_float: float
    day_index: int


def wall_time(now_ms: int | None = None, time_zone: str = DEFAULT_TZ) -> WallTime:
    if now_ms is None:
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    dt = datetime.fromtimestamp(now_ms / 1000.0, tz=_zone(str(time_zone or DEFAULT_TZ)))
    year, month, day = dt.year, dt.month, dt.day
    hour, minute, second = dt.hour, dt.minute, dt.second
    ymd = f"{year:04d}-{month:02d}-{day:02d}"
    hhmm = f"{hour:02d}:{minute:02d}"
    return WallTime(
        year=year,
        month=month,
        day=day,
        hour=hour,
        minute=minute,
        second=second,
        ymd=ymd,
        hhmm=hhmm,
        hour_float=hour + minute / 60 + second / 3600,
        day_index=(dt.date() - _EPOCH).days,
    )


def parse_hm(text, fallback: float = 8) -> float:
    m = _HM_RE.match(str(text if text is not None else "").strip())
    if not m:
        return fallback
    h = int(m.group(1))
    minute = int(m.group(2))
    if h < 0 or h > 29 or minute < 0 or minute > 59:
        return fallback
    return h + minute / 60


def format_hm(hour_float) -> str:
    try:
        value = float(hour_float or 0)
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value):
        value = 0.0
    norm = ((value % 24) + 24) % 24
    total_min = _js_round(norm * 60) % (24 * 60)
    return f"{total_min // 60:02d}:{total_min % 60:02d}"


def is_valid_ymd(ymd) -> bool:
    if not isinstance(ymd, str):
        return False
    m = _YMD_RE.match(ymd.strip())
    if not m:
        return False
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        date(y, mo, d)
    except ValueError:
        return False
    return True


def days_between_ymd(start_ymd: str, end_ymd: str) -> int:
    if not is_valid_ymd(start_ymd) or not is_valid_ymd(end_ymd):
        return 0
    y1, m1, d1 = (int(x) for x in start_ymd.split("-"))
    y2, m2, d2 = (int(x) for x in end_ymd.split("-"))
    return (date(y2, m2, d2) - date(y1, m1, d1)).days


def shift_ymd(ymd: str, delta_days: float) -> str:
    y, m, d = (int(x) for x in str(ymd).split("-"))
    try:
        target = date(y, m, d) + timedelta(days=int(delta_days))
    except (ValueError, OverflowError):
        return str(ymd)
    return f"{target.year:04d}-{target.month:02d}-{target.day:02d}"


def daily_jitter_hours(ymd: str, salt: str, amplitude_minutes: float = 20) -> float:
    """每日确定性微浮动（小时）：同一天同一 key 结果固定，不随心跳乱跳。"""
    text = f"{ymd}:{salt}"
    h = 2166136261
    for ch in text:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    unit = (h % 2001) / 1000 - 1
    return (unit * amplitude_minutes) / 60


def in_hour_window(cur_h: float, start_h: float, end_h: float) -> bool:
    c = ((float(cur_h) % 24) + 24) % 24
    s = ((float(start_h) % 24) + 24) % 24
    e = ((float(end_h) % 24) + 24) % 24
    if abs(s - e) < 1e-6:
        return False
    return (s < e and s <= c < e) or (s >= e and (c >= s or c < e))
