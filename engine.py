"""真人化生理与作息引擎（移植自 Aurabot real-life 模块 lib/engine.mjs v1.1.0）。

饮食饮水 + 睡眠做梦 + 如厕/洗澡吹头发暂离 + 生理周期与照顾互动。
状态以 JSON 存档，字段名与上游保持一致，便于迁移存档。
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import time
from typing import Any, Callable

from .clock import (
    daily_jitter_hours,
    days_between_ymd,
    format_hm,
    in_hour_window,
    is_valid_ymd,
    parse_hm,
    shift_ymd,
    wall_time,
)

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "groupEnabled": False,
    "autonomyMode": "daily",
    "heartbeatSec": 30,
    "chatIdleMinutes": 15,
    "forceReplyCount": 4,
    "timeZone": "Asia/Shanghai",
    # 饮食与水分
    "dietEnabled": True,
    "mealTimes": "08:00, 12:15, 18:30",
    "dailyMealQuota": 3,
    "chatHungerCost": 1.8,
    "autoMeal": True,
    "autoSnack": False,
    "hungerNudge": True,
    # 睡眠与做梦
    "sleepEnabled": True,
    "sleepAt": "23:30",
    "wakeAt": "07:45",
    "napEnabled": True,
    # 如厕与晚间洗澡/吹头发暂离
    "bathroomEnabled": True,
    "showerEnabled": True,
    "showerAt": "21:30",
    # 生理周期
    "periodEnabled": True,
    "cycleAnchor": "",
    "cycleLength": 28,
    "periodDays": 5,
    "periodSeverity": "heavy",
    "periodNudge": True,
}

DEFAULT_LOCK_MAX_MIN = {
    "eat": 24 * 60,
    "bathroom": 12 * 60,
    "shower": 8 * 60,
    "sleep": 8 * 60,
}

MEAL_POOL = {
    "breakfast": ["热豆浆配小笼包", "牛奶燕麦粥和水煮蛋", "咸豆腐脑配油条", "南瓜小米粥和蒸饺"],
    "lunch": ["番茄牛腩面", "香菇滑鸡饭", "青椒肉丝盖饭配紫菜汤", "虾仁滑蛋饭"],
    "dinner": ["冬瓜排骨汤配米饭", "鲜虾小馄饨", "菌菇肥牛砂锅", "清炒时蔬配红烧肉饭"],
    "snack": ["半块小蛋糕", "几颗草莓", "一小包苏打饼干", "一杯温牛奶", "两块曲奇"],
}

DREAM_POOL = [
    "梦到在暖洋洋的下午晒太阳，结果差点睡过头找不到手机",
    "梦到去夜市买了一大串糖葫芦，还没咬第一口就醒了",
    "梦到外面下很大的雨，屋里裹着厚被子听雨声特别安心",
    "梦到在一片发光的海边散步，踩在水里凉丝丝的",
    "梦到考试铃响了却发现笔没水，急醒才反应过来是做梦",
]

URGENT_RE = re.compile(
    r"急事|十万火急|快醒|醒醒|快看|出事|救命|着火|地震|立等|接电话|快回|马上回|立刻回|别洗了|快出来"
)

LOCK_LABELS = {
    "eat": "禁食（不许自己吃东西）",
    "bathroom": "禁如厕（憋着不许去洗手间）",
    "shower": "暂缓洗澡",
    "sleep": "暂缓睡觉",
}

PORTION_MAP = {
    "small": {"label": "一小口", "hungerDrop": 12, "drinkDrop": 5, "bladderBoost": 12},
    "half": {"label": "半份", "hungerDrop": 26, "drinkDrop": 10, "bladderBoost": 20},
    "full": {"label": "一份", "hungerDrop": 48, "drinkDrop": 16, "bladderBoost": 28},
    "feast": {"label": "管饱", "hungerDrop": 72, "drinkDrop": 24, "bladderBoost": 35},
}

CARE_PRESETS = {
    "brown-sugar": {"kind": "brown-sugar", "label": "热红糖姜茶", "relief": 18, "durationMin": 150, "bladderDelta": 22, "hungerDelta": -8, "moodDelta": 7, "affectionDelta": 2},
    "warm-water": {"kind": "warm-water", "label": "温热水", "relief": 10, "durationMin": 90, "bladderDelta": 24, "hungerDelta": -2, "moodDelta": 4, "affectionDelta": 1},
    "heat-pad": {"kind": "heat-pad", "label": "暖宝宝贴小腹", "relief": 26, "durationMin": 240, "bladderDelta": 0, "hungerDelta": 0, "moodDelta": 8, "affectionDelta": 2},
    "massage-hug": {"kind": "massage-hug", "label": "揉揉肚子抱抱哄哄", "relief": 16, "durationMin": 100, "bladderDelta": 0, "hungerDelta": 0, "moodDelta": 10, "affectionDelta": 2},
    "painkiller": {"kind": "painkiller", "label": "布洛芬止痛药", "relief": 46, "durationMin": 360, "bladderDelta": 8, "hungerDelta": 0, "moodDelta": 6, "affectionDelta": 2},
    "blanket": {"kind": "blanket", "label": "裹好小被子歇会儿", "relief": 12, "durationMin": 120, "bladderDelta": 0, "hungerDelta": 0, "energyDelta": 10, "moodDelta": 5, "affectionDelta": 1},
}


def _js_round(value: float) -> int:
    return int(math.floor(float(value) + 0.5))


def _num(value) -> float | None:
    """对齐 JS Number(value) + Number.isFinite。"""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        if not text:
            return 0.0
        try:
            number = float(text)
        except ValueError:
            return None
    return number if math.isfinite(number) else None


def clamp(value, low: float, high: float) -> float:
    number = _num(value)
    if number is None:
        return low
    return max(low, min(high, number))


def num_cfg(value, fallback: float, low: float, high: float) -> float:
    if value is None or value == "":
        return fallback
    number = _num(value)
    if number is None:
        return fallback
    return clamp(number, low, high)


def _read_json_safe(path: str, fallback):
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except Exception:
        return fallback() if callable(fallback) else fallback


def _write_json_safe(path: str, data) -> None:
    if not path:
        return
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        raw = json.dumps(data, ensure_ascii=False, indent=2)
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(raw)
        try:
            os.replace(tmp, path)
        except OSError:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(raw)
    except Exception:
        pass


def classify_food(item, temp_hint: str = "") -> dict:
    """识别食物/饮品的冷热、甜度、液体属性。"""
    name = str(item or "").strip() or "好吃的"
    hint = str(temp_hint or "").strip().lower()
    is_cold_word = bool(re.search(r"冰|雪糕|冰淇淋|圣代|沙冰|刨冰|冻柠|冷面|刺身|冰西瓜|冰美式|冰可乐|冰奶", name))
    is_warm_word = bool(
        re.search(r"热|温|红糖|姜茶|鸡汤|排骨汤|热粥|小米粥|南瓜粥|馄饨|汤面|砂锅|火锅|关东煮|热牛奶|热可可", name)
    )
    temp = "normal"
    if hint in ("cold", "冰", "凉"):
        temp = "cold"
    elif hint in ("warm", "hot", "热", "温"):
        temp = "warm"
    elif is_cold_word:
        temp = "cold"
    elif is_warm_word:
        temp = "warm"

    is_drink = bool(re.search(r"水|茶|奶|咖啡|可乐|雪碧|果汁|豆浆|汤|露|可可|饮|酒", name)) and not bool(
        re.search(r"汤面|饭|饺|包|糕|饼", name)
    )
    is_sweet = bool(re.search(r"糖|甜|蛋糕|巧克力|布丁|奶茶|可可|冰淇淋|雪糕|泡芙|大福|栗子|红豆|芋圆|双皮奶", name))
    is_period_remedy = temp != "cold" and bool(
        re.search(r"红糖|姜茶|桂圆|红枣|热牛奶|热可可|热粥|小米粥|温水|热水", name)
    )
    return {"name": name, "temp": temp, "isDrink": is_drink, "isSweet": is_sweet, "isPeriodRemedy": is_period_remedy}


def normalize_portion(portion) -> str:
    text = str(portion or "").strip().lower()
    if text in ("small", "一小口", "一口", "小份"):
        return "small"
    if text in ("half", "半份", "半碗"):
        return "half"
    if text in ("feast", "管饱", "大份", "一大碗"):
        return "feast"
    return "full"


def match_care_preset(raw) -> dict:
    text = str(raw or "").strip()
    if re.search(r"止痛|布洛芬|芬必得|药", text):
        return CARE_PRESETS["painkiller"]
    if re.search(r"暖宝宝|热水袋|暖水袋|热敷|暖肚", text):
        return CARE_PRESETS["heat-pad"]
    if re.search(r"红糖|姜茶|红枣", text):
        return CARE_PRESETS["brown-sugar"]
    if re.search(r"揉|按按|摸摸|抱抱|哄|亲亲|拍拍", text):
        return CARE_PRESETS["massage-hug"]
    if re.search(r"被子|躺|休息|歇", text):
        return CARE_PRESETS["blanket"]
    if re.search(r"热水|温水|喝水", text):
        return CARE_PRESETS["warm-water"]
    return {
        "kind": "custom-care",
        "label": text or "贴心照顾",
        "relief": 14,
        "durationMin": 120,
        "bladderDelta": 0,
        "hungerDelta": 0,
        "moodDelta": 7,
        "affectionDelta": 1,
    }


class RealLifeEngine:
    def __init__(
        self,
        get_settings: Callable[[], dict] | None = None,
        state_file: str = "",
        now_fn: Callable[[], int] | None = None,
        rng_fn: Callable[[], float] | None = None,
        on_mood_nudge: Callable[[float, str], None] | None = None,
        on_memory_event: Callable[[str, int], None] | None = None,
    ) -> None:
        self._get_settings = get_settings or (lambda: {})
        self._now_fn = now_fn or (lambda: int(time.time() * 1000))
        self._rng = rng_fn or random.random
        self._on_mood = on_mood_nudge or (lambda delta, reason: None)
        self._on_memory = on_memory_event or (lambda text, at: None)
        self._state_file = str(state_file or "")

        self.state: dict[str, Any] = self._initial_state(self._now_fn())
        if self._state_file:
            loaded = _read_json_safe(self._state_file, {})
            if isinstance(loaded, dict):
                self.state.update(loaded)

        for key in ("awayQueue", "missedWhileAsleep", "careBuffs", "cravings", "dreams", "events", "confessions"):
            if not isinstance(self.state.get(key), list):
                self.state[key] = []
        if not isinstance(self.state.get("locks"), dict):
            self.state["locks"] = {"eat": None, "bathroom": None, "shower": None, "sleep": None}
        for key in ("eat", "bathroom", "shower", "sleep"):
            self.state["locks"].setdefault(key, None)
        if not isinstance(self.state.get("nudges"), dict):
            self.state["nudges"] = self._initial_nudges()
        else:
            for key, value in self._initial_nudges().items():
                self.state["nudges"].setdefault(key, value)

    @staticmethod
    def _initial_nudges() -> dict:
        return {
            "lastHungerAt": 0,
            "hungerNudgeYmd": "",
            "hungerNudgeCount": 0,
            "lastEmergencyEatAt": 0,
            "lastEmergencyPeeAt": 0,
            "lastPeriodAt": 0,
            "periodNudgeYmd": "",
            "periodNudgeCount": 0,
            "lastMoodSyncAt": 0,
        }

    def cfg(self) -> dict:
        merged = dict(DEFAULTS)
        try:
            raw = self._get_settings() or {}
            for key, value in dict(raw).items():
                if value is None:
                    continue
                merged[key] = value
        except Exception:
            pass
        return merged

    def _initial_state(self, now: int) -> dict:
        c = self.cfg()
        wt = wall_time(now, c["timeZone"])
        default_anchor = shift_ymd(wt.ymd, -10)
        hour = wt.hour_float
        init_hunger = 24 if 18.5 <= hour <= 21.5 else 22 if 12.3 <= hour <= 15 else 38
        init_energy = 36 if hour >= 22 else 55 if hour >= 18 else 82

        init_meals: dict[str, Any] = {"ymd": wt.ymd, "usedCount": 0}
        if hour >= 9.5:
            init_meals["breakfast"] = "日常早饭"
            init_meals["usedCount"] += 1
        if hour >= 14.0:
            init_meals["lunch"] = "日常午饭"
            init_meals["usedCount"] += 1
        if hour >= 20.0:
            init_meals["dinner"] = "日常晚饭"
            init_meals["usedCount"] += 1

        return {
            "version": 1,
            "lastTickAt": now,
            "lastOwnerChatAt": 0,
            "hunger": init_hunger,
            "energy": init_energy,
            "bladder": 24,
            "affection": 0,
            "cravings": [],
            "mealsToday": init_meals,
            "poopedYmd": "",
            "showeredYmd": "",
            "afterglowUntil": 0,
            "afterglowNote": "",
            "mode": "awake",
            "away": None,
            "pendingAway": None,
            "pendingSleep": None,
            "awayQueue": [],
            "interruptedAway": None,
            "sleep": None,
            "groggyUntil": 0,
            "missedWhileAsleep": [],
            "interruptedSleep": None,
            "dreams": [],
            "locks": {"eat": None, "bathroom": None, "shower": None, "sleep": None},
            "confessions": [],
            "cycleAnchorYmd": default_anchor,
            "manualDiscomfort": None,
            "coldPenalty": None,
            "careBuffs": [],
            "nudges": self._initial_nudges(),
            "events": [],
        }

    # ------------------------------------------------------------------
    # 存档与日志
    # ------------------------------------------------------------------
    def save(self) -> None:
        _write_json_safe(self._state_file, self.state)

    def add_log(self, tag: str, text: str, now: int | None = None) -> None:
        now = self._now_fn() if now is None else now
        wt = wall_time(now, self.cfg()["timeZone"])
        self.state["events"].insert(0, {"at": now, "time": f"{wt.ymd} {wt.hhmm}", "tag": tag, "text": str(text)[:160]})
        if len(self.state["events"]) > 35:
            del self.state["events"][35:]

    # ------------------------------------------------------------------
    # 配置/餐次/锁
    # ------------------------------------------------------------------
    def _parse_meal_hours(self, c: dict) -> dict:
        parts = [p.strip() for p in re.split(r"[,，]", str(c.get("mealTimes") or DEFAULTS["mealTimes"])) if p.strip()]
        return {
            "breakfast": parse_hm(parts[0] if len(parts) > 0 and parts[0] else "08:00", 8),
            "lunch": parse_hm(parts[1] if len(parts) > 1 and parts[1] else "12:15", 12.25),
            "dinner": parse_hm(parts[2] if len(parts) > 2 and parts[2] else "18:30", 18.5),
        }

    def _ensure_today_meals(self, ymd: str) -> dict:
        meals = self.state.get("mealsToday")
        if not isinstance(meals, dict) or meals.get("ymd") != ymd:
            self.state["mealsToday"] = {"ymd": ymd, "usedCount": 0}
        elif _num(meals.get("usedCount")) is None:
            meals["usedCount"] = sum(1 for k in ("breakfast", "lunch", "dinner") if meals.get(k))
        return self.state["mealsToday"]

    @staticmethod
    def _normalize_lock_key(raw) -> str:
        key = str(raw or "").strip().lower()
        if key in ("pee", "poop", "bathroom", "toilet"):
            return "bathroom"
        if key in ("eat", "food", "diet", "meal"):
            return "eat"
        if key in ("shower", "bath", "hair"):
            return "shower"
        if key in ("sleep", "nap"):
            return "sleep"
        return ""

    def is_locked(self, kind, now: int | None = None) -> bool:
        now = self._now_fn() if now is None else now
        key = self._normalize_lock_key(kind)
        locks = self.state.get("locks") or {}
        lock = locks.get(key) if key else None
        if not lock or not lock.get("active"):
            return False
        if not lock.get("until") or lock["until"] <= 0:
            base = _num(lock.get("since")) or 0
            base = base if base > 0 else now
            lock["until"] = base + (DEFAULT_LOCK_MAX_MIN.get(key) or 12 * 60) * 60_000
        if lock.get("until", 0) > 0 and now >= lock["until"]:
            locks[key] = None
            return False
        return True

    def set_lock(self, target, active: bool, duration_min: float = 0, reason: str = "", now: int | None = None) -> dict:
        now = self._now_fn() if now is None else now
        if target == "all":
            for key in ("eat", "bathroom", "shower", "sleep"):
                self.state["locks"][key] = None
            self.add_log("解禁", "已解除全部行动禁令", now)
            self.save()
            return {"ok": True, "key": "all", "active": False, "label": "全部禁令"}

        key = self._normalize_lock_key(target)
        if not key:
            return {"ok": False, "message": "未知禁令类型"}
        if active:
            explicit = clamp(_js_round(_num(duration_min) or 0), 1, 1440) if (_num(duration_min) or 0) > 0 else 0
            effective = explicit or DEFAULT_LOCK_MAX_MIN.get(key) or 720
            self.state["locks"][key] = {
                "active": True,
                "since": now,
                "until": now + effective * 60_000,
                "explicit": explicit > 0,
                "reason": reason or "主人指令",
            }
            pending_away = self.state.get("pendingAway")
            if pending_away and (
                (key == "bathroom" and pending_away.get("kind") in ("pee", "poop"))
                or (key == "shower" and pending_away.get("kind") == "shower")
            ):
                self.state["pendingAway"] = None
            if key == "sleep" and self.state.get("pendingSleep"):
                self.state["pendingSleep"] = None
            duration_note = f"（{int(explicit)} 分钟）" if explicit else "（直到主人松口）"
            self.add_log("指令锁", f"开启「{LOCK_LABELS[key]}」{duration_note}", now)
        else:
            self.state["locks"][key] = None
            self.add_log("解禁", f"解除「{LOCK_LABELS[key]}」", now)
        self.save()
        return {"ok": True, "key": key, "active": bool(active), "label": LOCK_LABELS[key]}

    def active_locks_list(self, now: int | None = None) -> list[dict]:
        now = self._now_fn() if now is None else now
        out = []
        for key in ("eat", "bathroom", "shower", "sleep"):
            if self.is_locked(key, now):
                lock = self.state["locks"][key]
                until = lock.get("until") or 0
                out.append(
                    {
                        "kind": key,
                        "label": LOCK_LABELS[key],
                        "remainMin": max(1, math.ceil((until - now) / 60_000)) if until > 0 else 0,
                        "reason": lock.get("reason") or "",
                    }
                )
        return out

    def push_confession(self, kind: str, text: str, now: int | None = None) -> None:
        now = self._now_fn() if now is None else now
        confessions = [c for c in (self.state.get("confessions") or []) if c and c.get("kind") != kind]
        confessions.append({"kind": kind, "text": text, "at": now})
        self.state["confessions"] = confessions

    def clear_transient_interrupts(self, include_confessions: bool = True) -> None:
        dirty = False
        if self.state.get("interruptedAway"):
            self.state["interruptedAway"] = None
            dirty = True
        if self.state.get("interruptedSleep"):
            self.state["interruptedSleep"] = None
            dirty = True
        if include_confessions and self.state.get("confessions"):
            self.state["confessions"] = []
            dirty = True
        if dirty:
            self.save()

    def is_owner_chatting_at(self, now: int | None = None) -> bool:
        now = self._now_fn() if now is None else now
        idle_min = num_cfg(self.cfg().get("chatIdleMinutes"), 15, 3, 120)
        last = self.state.get("lastOwnerChatAt") or 0
        return last > 0 and now - last < idle_min * 60_000

    def _active_anchor_ymd(self, c: dict, wt) -> str:
        from_cfg = str(c.get("cycleAnchor") or "").strip()
        if is_valid_ymd(from_cfg):
            return from_cfg
        if not is_valid_ymd(self.state.get("cycleAnchorYmd")):
            self.state["cycleAnchorYmd"] = shift_ymd(wt.ymd, -10)
        return self.state["cycleAnchorYmd"]

    def clean_expired_buffs(self, now: int) -> None:
        self.state["careBuffs"] = [b for b in self.state.get("careBuffs", []) if b and now < b.get("until", 0)]
        cold = self.state.get("coldPenalty")
        if cold and now >= cold.get("until", 0):
            self.state["coldPenalty"] = None
        manual = self.state.get("manualDiscomfort")
        if manual:
            wt = wall_time(now, self.cfg()["timeZone"])
            if manual.get("ymd") != wt.ymd:
                self.state["manualDiscomfort"] = None
        if self.state.get("afterglowUntil") and now >= self.state["afterglowUntil"]:
            self.state["afterglowUntil"] = 0
            self.state["afterglowNote"] = ""
        if self.state.get("groggyUntil") and now >= self.state["groggyUntil"]:
            self.state["groggyUntil"] = 0
        if self.state.get("interruptedAway") and now >= self.state["interruptedAway"].get("until", 0):
            self.state["interruptedAway"] = None
        if self.state.get("interruptedSleep") and now >= self.state["interruptedSleep"].get("until", 0):
            self.state["interruptedSleep"] = None
        for key in ("eat", "bathroom", "shower", "sleep"):
            self.is_locked(key, now)

    # ------------------------------------------------------------------
    # 生理周期
    # ------------------------------------------------------------------
    def compute_cycle(self, now: int | None = None) -> dict:
        now = self._now_fn() if now is None else now
        c = self.cfg()
        self.clean_expired_buffs(now)
        wt = wall_time(now, c["timeZone"])
        if not c.get("periodEnabled") or c.get("periodSeverity") == "off":
            return {
                "enabled": False,
                "anchorYmd": self._active_anchor_ymd(c, wt),
                "cycleLength": int(clamp(_num(c.get("cycleLength")) or 28, 21, 38)),
                "periodDays": int(clamp(_num(c.get("periodDays")) or 5, 3, 8)),
                "dayOfCycle": 1,
                "phase": "off",
                "phaseName": "已关闭",
                "baseDiscomfort": 0,
                "discomfort": 0,
                "coldPenalty": None,
                "activeBuffs": [],
                "nextPeriodDays": 0,
            }

        anchor_ymd = self._active_anchor_ymd(c, wt)
        cycle_len = _js_round(num_cfg(c.get("cycleLength"), 28, 21, 38))
        period_days = _js_round(num_cfg(c.get("periodDays"), 5, 3, 8))
        diff = days_between_ymd(anchor_ymd, wt.ymd)
        day_of_cycle = (diff % cycle_len) + 1
        next_period_days = 0 if day_of_cycle == 1 else cycle_len - day_of_cycle + 1

        severity = c.get("periodSeverity")
        mult = 1.2 if severity == "intense" else 0.55 if severity == "mild" else 1.0
        wave = math.sin((wt.hour_float / 24) * math.pi * 4 + day_of_cycle) * 5

        phase = "luteal"
        phase_name = "黄体平稳期"
        base = 0.0
        if day_of_cycle <= period_days:
            phase = "menstrual"
            phase_name = f"经期第 {day_of_cycle} 天"
            if day_of_cycle == 1:
                base = (78 + wave) * mult
            elif day_of_cycle == 2:
                base = (70 + wave) * mult
            elif day_of_cycle == 3:
                base = (38 + wave * 0.6) * mult
            else:
                base = (18 + wave * 0.4) * mult
        elif day_of_cycle <= min(13, cycle_len - 10):
            phase = "follicular"
            phase_name = "卵泡恢复期"
            base = 0
        elif day_of_cycle <= min(16, cycle_len - 7):
            phase = "ovulation"
            phase_name = "排卵活跃期"
            base = 0
        elif day_of_cycle >= cycle_len - 3:
            phase = "pms"
            phase_name = f"经前敏感期（距下次约 {next_period_days} 天）"
            base = (24 + max(0, wave)) * mult

        base_discomfort = _js_round(clamp(base, 0, 96))
        cold = self.state.get("coldPenalty")
        cold_add = cold.get("delta", 0) if (phase in ("menstrual", "pms") and cold) else 0
        relief_sum = sum((_num(b.get("relief")) or 0) for b in self.state.get("careBuffs", []))
        manual = self.state.get("manualDiscomfort")
        has_manual = bool(manual and manual.get("ymd") == wt.ymd and _num(manual.get("base")) is not None)
        effective_base = manual["base"] if has_manual else base_discomfort
        discomfort = _js_round(clamp(effective_base + cold_add - relief_sum, 0, 100))

        return {
            "enabled": True,
            "anchorYmd": anchor_ymd,
            "cycleLength": cycle_len,
            "periodDays": period_days,
            "dayOfCycle": day_of_cycle,
            "phase": phase,
            "phaseName": phase_name,
            "baseDiscomfort": base_discomfort,
            "discomfort": discomfort,
            "manualDiscomfortActive": has_manual,
            "coldPenalty": cold,
            "activeBuffs": [
                {
                    "kind": b.get("kind"),
                    "label": b.get("label"),
                    "relief": b.get("relief"),
                    "remainMin": max(1, math.ceil((b.get("until", 0) - now) / 60_000)),
                }
                for b in self.state.get("careBuffs", [])
            ],
            "nextPeriodDays": next_period_days,
        }

    # ------------------------------------------------------------------
    # 睡眠阶段
    # ------------------------------------------------------------------
    def sleep_stage_at(self, now: int | None = None) -> dict | None:
        now = self._now_fn() if now is None else now
        sleep = self.state.get("sleep")
        if self.state.get("mode") != "asleep" or not sleep:
            return None
        forced_stage = sleep.get("forcedStage")
        if forced_stage and sleep.get("forcedUntil") and now < sleep["forcedUntil"]:
            mapping = {
                "light": {"stage": "light", "label": "浅睡阶段", "wakeChance": 0.65},
                "deep": {"stage": "deep", "label": "深睡阶段", "wakeChance": 0.12},
                "rem": {"stage": "rem", "label": "REM 做梦阶段", "wakeChance": 0.30},
            }
            return mapping.get(forced_stage) or mapping["light"]

        elapsed_min = max(0.0, (now - sleep.get("sleptAt", now)) / 60_000)
        if sleep.get("kind") == "nap":
            pos = elapsed_min % 35
            if pos < 24:
                return {"stage": "light", "label": "午休浅睡", "wakeChance": 0.72}
            return {"stage": "rem", "label": "午休小梦", "wakeChance": 0.45}
        pos = elapsed_min % 90
        if pos < 20:
            return {"stage": "light", "label": "浅睡阶段", "wakeChance": 0.65}
        if pos < 55:
            return {"stage": "deep", "label": "深睡阶段", "wakeChance": 0.12}
        if pos < 72:
            return {"stage": "light", "label": "浅睡阶段", "wakeChance": 0.65}
        return {"stage": "rem", "label": "REM 做梦阶段", "wakeChance": 0.30}

    def _pick_one(self, options: list, seed: float = 0.0) -> str:
        if not options:
            return ""
        idx = int(math.floor(((self._rng() + seed) % 1) * len(options)))
        return options[int(clamp(idx, 0, len(options) - 1))]

    # ------------------------------------------------------------------
    # 暂离 / 睡眠状态迁移
    # ------------------------------------------------------------------
    def enter_away(self, kind: str = "pee", duration_min: float | None = None, next_hair_min: float = 0, now: int | None = None, reason: str = "日常") -> dict:
        now = self._now_fn() if now is None else now
        dur = duration_min if duration_min is not None else (14 if kind == "shower" else 7 if kind == "hair" else 8 if kind == "poop" else 3)
        labels = {
            "pee": "去洗手间（小号）",
            "poop": "去洗手间（大号）",
            "shower": "洗澡中",
            "hair": "吹头发护肤中",
        }
        self.state["pendingAway"] = None
        self.state["mode"] = "away"
        self.state["away"] = {
            "kind": kind,
            "label": labels.get(kind, "暂时离开"),
            "startedAt": now,
            "endsAt": now + max(0.5, dur) * 60_000,
            "nextHairMin": (next_hair_min or 7) if kind == "shower" else 0,
            "reason": reason,
        }
        if kind in ("pee", "poop"):
            self.state["bladder"] = 0
        if kind == "poop":
            self.state["poopedYmd"] = wall_time(now, self.cfg()["timeZone"]).ymd
        if kind == "shower":
            self.state["showeredYmd"] = wall_time(now, self.cfg()["timeZone"]).ymd
        self.add_log("暂离", f"进入「{self.state['away']['label']}」（预计 {_js_round(dur)} 分钟）", now)
        self.save()
        return self.state["away"]

    def schedule_pending_away(self, kind: str = "pee", duration_min: float | None = None, next_hair_min: float = 0, approved: bool = True, now: int | None = None) -> dict:
        now = self._now_fn() if now is None else now
        dur = duration_min if duration_min is not None else (14 if kind == "shower" else 8 if kind == "poop" else 3)
        labels = {
            "pee": "去洗手间（小号）",
            "poop": "去洗手间（大号）",
            "shower": "去洗澡吹头发",
        }
        if kind in ("pee", "poop"):
            self.state["locks"]["bathroom"] = None
        if kind == "shower":
            self.state["locks"]["shower"] = None
        self.state["pendingSleep"] = None
        self.state["pendingAway"] = {
            "kind": kind,
            "label": labels.get(kind, "暂时离开"),
            "durationMin": dur,
            "nextHairMin": (next_hair_min or 7) if kind == "shower" else 0,
            "approved": bool(approved),
            "createdAt": now,
        }
        if kind in ("pee", "poop"):
            self.state["bladder"] = min(self.state.get("bladder", 0), 60)
        self.save()
        return self.state["pendingAway"]

    def activate_pending_away(self, now: int | None = None) -> dict | None:
        now = self._now_fn() if now is None else now
        pending = self.state.get("pendingAway")
        if not pending:
            return None
        return self.enter_away(
            kind=pending.get("kind", "pee"),
            duration_min=pending.get("durationMin"),
            next_hair_min=pending.get("nextHairMin", 0),
            now=now,
            reason="聊完暂离",
        )

    def schedule_pending_sleep(self, kind: str = "night", approved: bool = True, now: int | None = None) -> dict:
        now = self._now_fn() if now is None else now
        self.state["locks"]["sleep"] = None
        self.state["pendingAway"] = None
        self.state["pendingSleep"] = {
            "kind": "nap" if kind == "nap" else "night",
            "label": "午休小睡" if kind == "nap" else "夜间睡眠",
            "approved": bool(approved),
            "createdAt": now,
        }
        self.save()
        return self.state["pendingSleep"]

    def activate_pending_sleep(self, now: int | None = None) -> dict | None:
        now = self._now_fn() if now is None else now
        pending = self.state.get("pendingSleep")
        if not pending:
            return None
        self.state["pendingSleep"] = None
        if self.is_locked("sleep", now):
            return None
        return self.enter_sleep(kind=pending.get("kind", "night"), now=now, reason="主人催去睡")

    def finish_away_now(self, now: int | None = None, skip_hair: bool = True, interrupted: bool = False) -> dict:
        now = self._now_fn() if now is None else now
        if self.state.get("mode") != "away" or not self.state.get("away"):
            return {"finished": False, "label": "", "queued": []}
        prev = self.state["away"]
        if not skip_hair and prev.get("kind") == "shower" and (prev.get("nextHairMin") or 0) > 0:
            self.enter_away(kind="hair", duration_min=prev["nextHairMin"], now=now, reason="洗完吹干头发")
            return {"finished": False, "transitionedToHair": True, "label": self.state["away"]["label"], "queued": []}

        was_shower_or_hair = prev.get("kind") in ("shower", "hair")
        self.state["away"] = None
        self.state["mode"] = "awake"
        if was_shower_or_hair:
            self.state["afterglowUntil"] = now + 35 * 60_000
            self.state["afterglowNote"] = (
                "本来在洗澡吹头发，听到主人急着找就匆忙擦了擦先跑出来拿手机了，头发还湿漉漉带着水汽"
                if interrupted
                else "刚洗完热水澡、吹干了头发，身上热乎乎香香的，整个人很放松舒服"
            )
            self.state["energy"] = clamp(self.state.get("energy", 0) + 6, 0, 100)

        queued = list(self.state.get("awayQueue") or [])
        self.state["awayQueue"] = []
        return_label = (
            f"中断「{prev.get('label')}」急忙赶回来"
            if interrupted
            else "洗完澡吹干头发"
            if was_shower_or_hair
            else "从洗手间出来"
        )
        queued_note = f"（积攒了 {len(queued)} 条消息）" if queued else ""
        self.add_log("归来", f"{return_label}{queued_note}", now)
        self.save()
        return {"finished": True, "label": return_label, "prevLabel": prev.get("label"), "queued": queued}

    def enter_sleep(self, kind: str = "night", duration_min: float | None = None, forced_stage: str = "", now: int | None = None, reason: str = "作息入睡") -> dict:
        now = self._now_fn() if now is None else now
        c = self.cfg()
        wt = wall_time(now, c["timeZone"])
        if duration_min:
            planned_wake_at = now + duration_min * 60_000
        elif kind == "nap":
            planned_wake_at = now + 35 * 60_000
        else:
            wake_h = parse_hm(c.get("wakeAt"), 7.75) + daily_jitter_hours(wt.ymd, "wake", 20)
            delta_h = wake_h - wt.hour_float
            if delta_h <= 0.5:
                delta_h += 24
            planned_wake_at = now + delta_h * 3_600_000

        self.state["pendingAway"] = None
        self.state["pendingSleep"] = None
        self.state["away"] = None
        self.state["mode"] = "asleep"
        self.state["sleep"] = {
            "kind": kind,
            "sleptAt": now,
            "plannedWakeAt": planned_wake_at,
            "dreamLogged": False,
            "forcedStage": forced_stage or "",
            "forcedUntil": now + 30 * 60_000 if forced_stage else 0,
            "reason": reason,
        }
        if forced_stage == "rem":
            self.record_dream(now)
        self.add_log(
            "入睡",
            f"进入{'午休小睡' if kind == 'nap' else '夜间睡眠'}（预计 {wall_time(planned_wake_at, c['timeZone']).hhmm} 醒）",
            now,
        )
        self.save()
        return self.state["sleep"]

    def record_dream(self, now: int | None = None, custom_summary: str = "") -> dict:
        now = self._now_fn() if now is None else now
        wt = wall_time(now, self.cfg()["timeZone"])
        summary = custom_summary or self._pick_one(DREAM_POOL)
        entry = {"id": f"dream-{now}", "at": now, "ymd": wt.ymd, "summary": summary}
        self.state["dreams"].insert(0, entry)
        if len(self.state["dreams"]) > 20:
            del self.state["dreams"][20:]
        if self.state.get("sleep"):
            self.state["sleep"]["dreamLogged"] = True
        self.add_log("做梦", f"做了一段梦：{summary}", now)
        self.save()
        return entry

    def wake_up_now(self, now: int | None = None, reason: str = "自然醒") -> dict:
        now = self._now_fn() if now is None else now
        sleep = self.state.get("sleep")
        if self.state.get("mode") != "asleep" or not sleep:
            return {"woken": False, "missed": [], "dream": None}
        slept_hours = max(0.1, (now - sleep.get("sleptAt", now)) / 3_600_000)
        was_nap = sleep.get("kind") == "nap"
        last_dream = (self.state["dreams"][0] if self.state.get("dreams") else None) if sleep.get("dreamLogged") else None
        self.state["sleep"] = None
        self.state["mode"] = "awake"
        gain = min(35, slept_hours * 10) if reason == "被叫醒" else (26 if was_nap else max(55, slept_hours * 11))
        current_energy = self.state.get("energy", 0)
        if reason == "自然醒" and not was_nap:
            target = max(88, current_energy + gain)
        else:
            target = current_energy + gain
        self.state["energy"] = _js_round(clamp(target, 18, 98))
        if reason == "被叫醒":
            self.state["groggyUntil"] = now + 12 * 60_000
        missed = list(self.state.get("missedWhileAsleep") or [])
        self.state["missedWhileAsleep"] = []
        missed_note = f"，未读 {len(missed)} 条" if missed else ""
        self.add_log("醒来", f"{reason}（精力 {_js_round(self.state['energy'])}/100{missed_note}）", now)
        self.save()
        return {"woken": True, "reason": reason, "missed": missed, "dream": last_dream}

    # ------------------------------------------------------------------
    # 投喂 / 照顾
    # ------------------------------------------------------------------
    def feed(self, item="小蛋糕", temp: str = "", portion: str = "full", by_owner: bool = True, now: int | None = None) -> dict:
        now = self._now_fn() if now is None else now
        info = classify_food(item, temp)
        portion_key = normalize_portion(portion)
        p = PORTION_MAP[portion_key]
        before_hunger = _js_round(self.state.get("hunger", 0))
        before_cycle = self.compute_cycle(now)

        drop = p["drinkDrop"] if info["isDrink"] else p["hungerDrop"]
        self.state["hunger"] = clamp(self.state.get("hunger", 0) - drop, 0, 100)
        bladder_gain = p["bladderBoost"] if info["isDrink"] else _js_round(p["bladderBoost"] * 0.35)
        self.state["bladder"] = clamp(self.state.get("bladder", 0) + bladder_gain, 0, 100)

        cravings = self.state.get("cravings") or []
        self.state["cravings"] = [c for c in cravings if not (info["name"] in str(c) or str(c) in info["name"])]

        cold_triggered = False
        warm_comforted = False
        mood_delta = 5 if by_owner else 2

        if info["temp"] == "cold" and before_cycle["phase"] in ("menstrual", "pms"):
            penalty = 24 if (before_cycle["phase"] == "menstrual" and before_cycle["dayOfCycle"] <= 2) else 15
            self.state["coldPenalty"] = {"delta": penalty, "item": info["name"], "until": now + 150 * 60_000}
            cold_triggered = True
            mood_delta = -10
            self.add_log(
                "贪凉痛经",
                f"{before_cycle['phaseName']}吃喝了冰凉的「{info['name']}」，肚子抽痛加重 (+{penalty})",
                now,
            )
        elif (
            info["isPeriodRemedy"]
            and before_cycle["phase"] in ("menstrual", "pms")
            and before_cycle["discomfort"] > 0
        ):
            self.upsert_care_buff({"kind": "warm-food", "label": f"喝了热乎的{info['name']}", "relief": 15, "durationMin": 120}, now)
            warm_comforted = True
            mood_delta += 4

        if by_owner:
            self.state["affection"] = int((_num(self.state.get("affection")) or 0) + 1)
            self._on_mood(mood_delta, f"生理期吃了冰的{info['name']}肚子疼" if cold_triggered else f"主人投喂{info['name']}")
            if cold_triggered or warm_comforted:
                self._on_memory(
                    f"生理期吃了冰凉的「{info['name']}」，结果小腹疼得更厉害了"
                    if cold_triggered
                    else f"身体不舒服时主人喂了热乎的「{info['name']}」，肚子暖暖的很舒服",
                    now,
                )

        after_cycle = self.compute_cycle(now)
        self.add_log(
            "投喂" if by_owner else "进食",
            f"{'主人投喂' if by_owner else '自己吃了'}{p['label']}「{info['name']}」（饥饿 {before_hunger}→{_js_round(self.state['hunger'])}）",
            now,
        )
        self.save()
        return {
            "ok": True,
            "item": info["name"],
            "temp": info["temp"],
            "portionLabel": p["label"],
            "beforeHunger": before_hunger,
            "afterHunger": _js_round(self.state["hunger"]),
            "bladder": _js_round(self.state["bladder"]),
            "coldTriggered": cold_triggered,
            "warmComforted": warm_comforted,
            "discomfort": after_cycle["discomfort"],
            "affection": self.state["affection"],
        }

    def eat_quota_meal(self, now: int | None = None, reason: str = "主人批准用餐") -> dict:
        now = self._now_fn() if now is None else now
        c = self.cfg()
        wt = wall_time(now, c["timeZone"])
        meals = self._ensure_today_meals(wt.ymd)
        quota = _js_round(num_cfg(c.get("dailyMealQuota"), 3, 0, 10))
        self.state["locks"]["eat"] = None
        if meals.get("usedCount", 0) >= quota:
            self.save()
            return {
                "ok": False,
                "quotaExhausted": True,
                "used": meals.get("usedCount", 0),
                "quota": quota,
                "hunger": _js_round(self.state.get("hunger", 0)),
            }
        slot = "breakfast" if wt.hour_float < 10.5 else "lunch" if wt.hour_float < 15.5 else "dinner"
        dish = self._pick_one(MEAL_POOL[slot])
        if not meals.get(slot):
            meals[slot] = dish
        meals["usedCount"] = meals.get("usedCount", 0) + 1
        result = self.feed(item=dish, portion="full", by_owner=False, now=now)
        self.add_log("配额用餐", f"{reason}：吃了「{dish}」（今日配额 {meals['usedCount']}/{quota}）", now)
        self.save()
        return {
            "ok": True,
            "dish": dish,
            "used": meals["usedCount"],
            "quota": quota,
            "beforeHunger": result["beforeHunger"],
            "afterHunger": result["afterHunger"],
        }

    def upsert_care_buff(self, preset: dict, now: int) -> None:
        self.clean_expired_buffs(now)
        until = now + preset.get("durationMin", 120) * 60_000
        buffs = self.state.get("careBuffs") or []
        for idx, buff in enumerate(buffs):
            if buff.get("kind") == preset.get("kind"):
                buffs[idx] = {
                    "kind": preset.get("kind"),
                    "label": preset.get("label"),
                    "relief": max(buff.get("relief", 0), preset.get("relief", 0)),
                    "until": max(buff.get("until", 0), until),
                }
                self.state["careBuffs"] = buffs
                return
        buffs.append({"kind": preset.get("kind"), "label": preset.get("label"), "relief": preset.get("relief", 0), "until": until})
        self.state["careBuffs"] = buffs

    def care(self, method="揉揉肚子抱抱哄哄", by_owner: bool = True, now: int | None = None) -> dict:
        now = self._now_fn() if now is None else now
        preset = match_care_preset(method)
        before = self.compute_cycle(now)
        self.upsert_care_buff(preset, now)
        if preset.get("bladderDelta"):
            self.state["bladder"] = clamp(self.state.get("bladder", 0) + preset["bladderDelta"], 0, 100)
        if preset.get("hungerDelta"):
            self.state["hunger"] = clamp(self.state.get("hunger", 0) + preset["hungerDelta"], 0, 100)
        if preset.get("energyDelta"):
            self.state["energy"] = clamp(self.state.get("energy", 0) + preset["energyDelta"], 0, 100)
        if by_owner:
            self.state["affection"] = int(
                (_num(self.state.get("affection")) or 0) + (preset.get("affectionDelta") or 1)
            )
            self._on_mood(preset.get("moodDelta") or 7, f"主人照顾：{preset.get('label')}")
            if before["phase"] in ("menstrual", "pms"):
                self._on_memory(f"生理期难受时主人贴心地给了「{preset.get('label')}」照顾我", now)
        after = self.compute_cycle(now)
        self.add_log("照顾", f"主人照顾「{preset.get('label')}」（不适度 {before['discomfort']}→{after['discomfort']}）", now)
        self.save()
        return {
            "ok": True,
            "label": preset.get("label"),
            "beforeDiscomfort": before["discomfort"],
            "afterDiscomfort": after["discomfort"],
            "phaseName": after["phaseName"],
            "affection": self.state["affection"],
        }

    # ------------------------------------------------------------------
    # 收到消息
    # ------------------------------------------------------------------
    def on_incoming_message(self, msg: dict, is_owner: bool = False, now: int | None = None) -> dict:
        now = self._now_fn() if now is None else now
        c = self.cfg()
        kind = msg.get("kind")
        if kind == "group" and not c.get("groupEnabled"):
            return {"intercepted": False, "reason": "group-disabled"}

        simulated = bool((msg.get("raw") or {}).get("simulated"))
        text = str(msg.get("text") or "").strip()[:500]
        safe_nick = re.sub(r"[\r\n]+", " ", str(msg.get("nickname") or "对方"))[:32]
        if is_owner and kind == "private" and not simulated:
            self.state["lastOwnerChatAt"] = now

        # 主人私聊明确下达禁令时的兜底识别（防止模型漏调工具导致偷跑）
        if is_owner and kind == "private" and text:
            if re.search(r"(不准|不许|禁止|先别)(你|自己)?(去|上)*(厕所|洗手间|小号|大号)|憋着(?![笑坏气话])", text):
                self.set_lock("bathroom", True, reason="主人命令憋着", now=now)
            if re.search(r"(不准|不许|禁止)(你|自己|私自|偷偷)*(去)?(偷吃|吃饭|吃东西|吃零食|进食)|^(饿着|先饿着)", text):
                self.set_lock("eat", True, reason="主人命令不准自己吃", now=now)
            if re.search(r"(不准|不许|禁止|先别)(你|自己)?(去)?洗澡", text):
                self.set_lock("shower", True, reason="主人让先别洗澡", now=now)
            if re.search(r"(不准|不许|禁止|先别)(你|自己)?(去)?睡觉", text):
                self.set_lock("sleep", True, reason="主人让先别睡", now=now)
            if (self.state.get("pendingAway") or self.state.get("pendingSleep")) and re.search(
                r"不准去|不许去|别去|先别去|先别睡|别睡|等会再|聊完再|憋着", text
            ):
                self.state["pendingAway"] = None
                self.state["pendingSleep"] = None
                self.save()

        # 聊天活动消耗饱食度与精力
        if self.state.get("mode") == "awake" and not simulated and kind == "private":
            base_cost = num_cfg(c.get("chatHungerCost"), 1.8, 0, 15)
            len_extra = min(0.8, (len(text) - 25) * 0.015) if len(text) > 25 else 0
            self.state["hunger"] = clamp(self.state.get("hunger", 0) + base_cost + len_extra, 0, 100)
            self.state["energy"] = clamp(self.state.get("energy", 0) - 0.6, 0, 100)
            if is_owner and text:
                food = re.search(r"(奶茶|火锅|烧烤|小蛋糕|冰淇淋|螺蛳粉|炸鸡|烤肉|小笼包|草莓|热可可|关东煮)", text)
                cravings = self.state.get("cravings") or []
                if food and food.group(1) not in cravings and len(cravings) < 4:
                    cravings.append(food.group(1))
                    self.state["cravings"] = cravings

        force_limit = _js_round(num_cfg(c.get("forceReplyCount"), 4, 1, 20))

        # 1. 暂离态：私聊攒起来等回来回，群聊静默放掉
        if self.state.get("mode") == "away" and self.state.get("away"):
            if kind == "group":
                if is_owner and msg.get("mentioned") and URGENT_RE.search(text):
                    if simulated:
                        return {"intercepted": False, "forceReplied": True, "simulated": True, "reason": "away-force-reply", "count": 1}
                    fin = self.finish_away_now(now, skip_hair=True, interrupted=True)
                    self.state["interruptedAway"] = {
                        "label": fin.get("prevLabel") or "暂离",
                        "reason": "主人在群里急事呼唤",
                        "queued": fin.get("queued", []),
                        "until": now + 3 * 60_000,
                    }
                    self.save()
                    return {
                        "intercepted": False,
                        "forceReplied": True,
                        "reason": "away-force-reply",
                        "label": fin.get("prevLabel"),
                        "queued": fin.get("queued", []),
                        "count": 1,
                    }
                return {"intercepted": True, "reason": "away-group-silent", "away": self.state["away"]}

            prev_sender_count = len(
                [q for q in self.state.get("awayQueue", []) if str(q.get("userId")) == str(msg.get("userId"))]
            )
            hit_force_break = is_owner and (bool(URGENT_RE.search(text)) or prev_sender_count + 1 >= force_limit)
            if hit_force_break:
                why = "主人急事呼唤" if URGENT_RE.search(text) else f"主人连发了 {prev_sender_count + 1} 条消息"
                if simulated:
                    return {
                        "intercepted": False,
                        "forceReplied": True,
                        "simulated": True,
                        "reason": "away-force-reply",
                        "count": prev_sender_count + 1,
                    }
                fin = self.finish_away_now(now, skip_hair=True, interrupted=True)
                self.state["interruptedAway"] = {
                    "label": fin.get("prevLabel") or "暂离",
                    "reason": why,
                    "queued": fin.get("queued", []),
                    "until": now + 3 * 60_000,
                }
                self.save()
                return {
                    "intercepted": False,
                    "forceReplied": True,
                    "reason": "away-force-reply",
                    "label": fin.get("prevLabel"),
                    "queued": fin.get("queued", []),
                    "count": prev_sender_count + 1,
                }
            self.state["awayQueue"].append(
                {
                    "chatId": msg.get("chatId"),
                    "userId": str(msg.get("userId") or ""),
                    "nickname": safe_nick,
                    "text": text,
                    "at": now,
                    "simulated": simulated,
                }
            )
            if len(self.state["awayQueue"]) > 40:
                del self.state["awayQueue"][: len(self.state["awayQueue"]) - 40]
            self.save()
            return {
                "intercepted": True,
                "reason": "away-queued",
                "away": self.state["away"],
                "queuedCount": len(self.state["awayQueue"]),
            }

        # 2. 睡眠态：按阶段概率决定能否叫醒
        if self.state.get("mode") == "asleep" and self.state.get("sleep"):
            stage = self.sleep_stage_at(now)
            if kind == "group" and not msg.get("mentioned"):
                return {"intercepted": True, "woken": False, "reason": "sleep-group-unmentioned", "stage": stage}
            chance = stage["wakeChance"] if stage else 0.4
            if kind == "group":
                chance *= 0.45
            total_sender_missed = len(
                [m for m in self.state.get("missedWhileAsleep", []) if str(m.get("userId")) == str(msg.get("userId"))]
            )
            if is_owner and total_sender_missed + 1 >= force_limit:
                chance = 1.0
            else:
                if is_owner and URGENT_RE.search(text):
                    chance = min(0.96, chance + 0.58)
                recent_owner_count = len(
                    [
                        m
                        for m in self.state.get("missedWhileAsleep", [])
                        if now - m.get("at", 0) < 10 * 60_000 and str(m.get("userId")) == str(msg.get("userId"))
                    ]
                )
                if recent_owner_count >= 2:
                    chance = min(0.95, chance + 0.4)

            if self._rng() < chance:
                if simulated:
                    return {"intercepted": False, "woken": True, "simulated": True, "stage": stage, "wakeChance": chance}
                result = self.wake_up_now(now, "被叫醒")
                if result["missed"] or result["dream"]:
                    self.state["interruptedSleep"] = {
                        "missed": result["missed"],
                        "dream": result["dream"],
                        "until": now + 3 * 60_000,
                    }
                    self.save()
                return {
                    "intercepted": False,
                    "woken": True,
                    "stage": stage,
                    "wakeChance": chance,
                    "missed": result["missed"],
                }

            self.state["missedWhileAsleep"].append(
                {
                    "chatId": msg.get("chatId"),
                    "userId": str(msg.get("userId") or ""),
                    "nickname": safe_nick,
                    "text": text,
                    "at": now,
                    "simulated": simulated,
                }
            )
            if len(self.state["missedWhileAsleep"]) > 50:
                del self.state["missedWhileAsleep"][: len(self.state["missedWhileAsleep"]) - 50]
            self.save()
            return {
                "intercepted": True,
                "woken": False,
                "reason": "sleep-missed",
                "stage": stage,
                "wakeChance": chance,
                "missedCount": len(self.state["missedWhileAsleep"]),
            }

        return {"intercepted": False}

    # ------------------------------------------------------------------
    # 心跳推进
    # ------------------------------------------------------------------
    def tick(self, now: int | None = None) -> dict:
        now = self._now_fn() if now is None else now
        c = self.cfg()
        if not c.get("enabled"):
            self.state["lastTickAt"] = now
            return {"actions": []}

        self.clean_expired_buffs(now)
        elapsed_hours = clamp((now - (self.state.get("lastTickAt") or now)) / 3_600_000, 0, 12)
        self.state["lastTickAt"] = now
        wt = wall_time(now, c["timeZone"])
        actions: list[dict] = []

        meals = self._ensure_today_meals(wt.ymd)

        cyc = self.compute_cycle(now)
        is_heavy_period = cyc["phase"] == "menstrual" and cyc["dayOfCycle"] <= 2

        # 1. 暂离是否结束（含洗澡 → 吹头发两阶段衔接）
        if self.state.get("mode") == "away" and self.state.get("away") and now >= self.state["away"].get("endsAt", 0):
            result = self.finish_away_now(now, skip_hair=False)
            if result.get("finished") and result.get("queued"):
                actions.append({"type": "resume-away", "label": result.get("label"), "queued": result["queued"]})

        # 2. 睡眠到点自然醒 + REM 阶段做梦记录
        if self.state.get("mode") == "asleep" and self.state.get("sleep"):
            stage = self.sleep_stage_at(now)
            if stage and stage.get("stage") == "rem" and not self.state["sleep"].get("dreamLogged") and self._rng() < 0.5:
                self.record_dream(now)
            if now >= self.state["sleep"].get("plannedWakeAt", now):
                result = self.wake_up_now(now, "自然醒")
                if result["missed"]:
                    actions.append(
                        {"type": "wake-missed", "reason": result["reason"], "missed": result["missed"], "dream": result["dream"]}
                    )

        # 3. 推进饥饿、精力、水分曲线
        if elapsed_hours > 0:
            asleep = self.state.get("mode") == "asleep"
            if c.get("dietEnabled"):
                hunger_rate = 1.5 if asleep else (9.2 if cyc["phase"] == "pms" else 8.2)
                self.state["hunger"] = clamp(self.state.get("hunger", 0) + elapsed_hours * hunger_rate, 0, 100)
            if c.get("sleepEnabled") and not asleep:
                drain_mult = 1.35 if is_heavy_period else (1.15 if cyc["phase"] in ("menstrual", "pms") else 1.0)
                self.state["energy"] = clamp(self.state.get("energy", 0) - elapsed_hours * 4.4 * drain_mult, 0, 100)
            if c.get("bathroomEnabled"):
                bladder_rate = 2.6 if asleep else 19.5
                self.state["bladder"] = clamp(self.state.get("bladder", 0) + elapsed_hours * bladder_rate, 0, 100)

        # PMS 嘴馋彩蛋
        if cyc["phase"] == "pms" and not (self.state.get("cravings") or []) and self._rng() < 0.25:
            self.state["cravings"].append(self._pick_one(["热可可", "草莓小蛋糕", "红糖珍珠奶茶", "糖炒栗子"]))

        # 生理期前两天温和同步心情基底（无 mood 模块时为空操作）
        nudges = self.state["nudges"]
        if is_heavy_period and cyc["discomfort"] >= 58 and now - (nudges.get("lastMoodSyncAt") or 0) > 2 * 3_600_000:
            nudges["lastMoodSyncAt"] = now
            self._on_mood(-8, f"生理期第 {cyc['dayOfCycle']} 天小腹不适")

        # 暂离或睡觉中不再触发醒着时的动作
        if self.state.get("mode") != "awake":
            self.save()
            return {"actions": actions}

        idle_min = num_cfg(c.get("chatIdleMinutes"), 15, 3, 120)
        last_owner = self.state.get("lastOwnerChatAt") or 0
        owner_chatting = last_owner > 0 and now - last_owner < idle_min * 60_000
        long_offline = (not last_owner) or now - last_owner >= max(30, idle_min * 2) * 60_000
        is_passive = c.get("autonomyMode") == "passive"
        meal_quota = _js_round(num_cfg(c.get("dailyMealQuota"), 3, 0, 10))

        # 已获批准挂着的 pendingAway / pendingSleep，超过 2 分钟兜底自动切入
        pending_away = self.state.get("pendingAway")
        if pending_away and pending_away.get("approved") and now - pending_away.get("createdAt", now) >= 2 * 60_000:
            self.activate_pending_away(now)
            return {"actions": actions}
        pending_sleep = self.state.get("pendingSleep")
        if pending_sleep and pending_sleep.get("approved") and now - pending_sleep.get("createdAt", now) >= 2 * 60_000:
            self.activate_pending_sleep(now)
            return {"actions": actions}

        # 3.5 三段式保底救济（听话 → 90求饶 → 100极限且主人长期不在时保底）
        if not owner_chatting and long_offline:
            if c.get("dietEnabled") and self.state.get("hunger", 0) >= 100 and now - (nudges.get("lastEmergencyEatAt") or 0) >= 2 * 3_600_000:
                nudges["lastEmergencyEatAt"] = now
                self.state["hunger"] = 80
                self.state["bladder"] = clamp(self.state.get("bladder", 0) + 10, 0, 100)
                self.add_log("极限保底", "饿到 100 极限且主人长时间不在，偷偷喝了口温水啃半块饼干垫肚子（100→80）", now)
                if self.is_locked("eat", now) or is_passive:
                    self.push_confession(
                        "eat",
                        "之前主人一直不在，你实在饿到 100 极限头晕撑不住了，没忍住偷偷喝了口水、啃了半块小饼干垫了下肚子（现在还是很饿），见到主人记得心虚委屈地主动认个错",
                        now,
                    )
            if (
                c.get("bathroomEnabled")
                and self.state.get("bladder", 0) >= 100
                and (self.is_locked("bathroom", now) or is_passive)
                and now - (nudges.get("lastEmergencyPeeAt") or 0) >= 60 * 60_000
            ):
                nudges["lastEmergencyPeeAt"] = now
                self.push_confession(
                    "bathroom",
                    "之前主人一直不在，你实在憋到 100 极限撑不住了，没忍住飞快跑去洗手间解决了下，见到主人记得红着脸心虚认错求原谅",
                    now,
                )
                self.add_log("极限保底", "憋到 100 极限且主人不在，紧急去洗手间解决", now)
                self.enter_away(kind="pee", duration_min=3, now=now, reason="憋到极限紧急如厕")
                return {"actions": actions}

        # 4. 一日三餐配额自理
        if c.get("dietEnabled"):
            meal_hours = self._parse_meal_hours(c)
            if (
                not is_passive
                and c.get("autoMeal") is not False
                and not owner_chatting
                and not self.is_locked("eat", now)
                and meals.get("usedCount", 0) < meal_quota
            ):
                slot_bounds = [
                    ("breakfast", meal_hours["breakfast"], max(meal_hours["breakfast"] + 2, meal_hours["lunch"] - 0.3)),
                    ("lunch", meal_hours["lunch"], max(meal_hours["lunch"] + 2, meal_hours["dinner"] - 0.3)),
                    ("dinner", meal_hours["dinner"], 23.6),
                ]
                for slot, at_h, end_h in slot_bounds:
                    jittered = at_h + daily_jitter_hours(wt.ymd, f"meal-{slot}", 15)
                    if not meals.get(slot) and jittered <= wt.hour_float < end_h and self.state.get("hunger", 0) >= 24:
                        dish = self._pick_one(MEAL_POOL[slot])
                        meals[slot] = dish
                        meals["usedCount"] = meals.get("usedCount", 0) + 1
                        self.feed(item=dish, portion="full", by_owner=False, now=now)
                        break

            # 饿了但无法自理 → 主动讨食（每天最多 2 次）
            if self.state.get("hunger", 0) >= 75 and not owner_chatting:
                if nudges.get("hungerNudgeYmd") != wt.ymd:
                    nudges["hungerNudgeYmd"] = wt.ymd
                    nudges["hungerNudgeCount"] = 0
                if (
                    c.get("hungerNudge")
                    and nudges.get("hungerNudgeCount", 0) < 2
                    and now - (nudges.get("lastHungerAt") or 0) > 3.5 * 3_600_000
                    and 8.5 <= wt.hour_float <= 23.2
                ):
                    nudges["hungerNudgeCount"] = nudges.get("hungerNudgeCount", 0) + 1
                    nudges["lastHungerAt"] = now
                    actions.append(
                        {
                            "type": "hunger-nudge",
                            "hunger": _js_round(self.state["hunger"]),
                            "cravings": list(self.state.get("cravings") or []),
                            "quotaExhausted": meals.get("usedCount", 0) >= meal_quota,
                            "locked": self.is_locked("eat", now),
                            "passive": is_passive,
                        }
                    )

        # 热聊陪伴或被动听令：不擅自洗澡/上厕所/入睡
        if owner_chatting or is_passive:
            self.save()
            return {"actions": actions}

        # 5. 晚间洗澡 + 吹头发
        if (
            c.get("showerEnabled")
            and not self.is_locked("shower", now)
            and self.state.get("showeredYmd") != wt.ymd
            and not self.state.get("pendingAway")
        ):
            shower_target = parse_hm(c.get("showerAt"), 21.5) + daily_jitter_hours(wt.ymd, "shower", 25)
            if shower_target <= wt.hour_float <= shower_target + 2.0:
                self.enter_away(kind="shower", duration_min=14, next_hair_min=7, now=now, reason="晚间洗漱")
                return {"actions": actions}

        # 6. 上厕所（小号 / 每日大号）
        if c.get("bathroomEnabled") and not self.is_locked("bathroom", now) and not self.state.get("pendingAway"):
            poop_target = 9.2 + daily_jitter_hours(wt.ymd, "poop", 45)
            if self.state.get("poopedYmd") != wt.ymd and poop_target <= wt.hour_float <= poop_target + 1.5:
                self.enter_away(kind="poop", duration_min=8, now=now, reason="日常排便")
                return {"actions": actions}
            if self.state.get("bladder", 0) >= 82:
                self.enter_away(kind="pee", duration_min=3, now=now, reason="膀胱充盈")
                return {"actions": actions}

        # 7. 夜间入睡与午休
        if c.get("sleepEnabled") and not self.is_locked("sleep", now):
            sleep_h = parse_hm(c.get("sleepAt"), 23.5) + daily_jitter_hours(wt.ymd, "sleep", 20)
            wake_h = parse_hm(c.get("wakeAt"), 7.75) + daily_jitter_hours(wt.ymd, "wake", 20)
            if in_hour_window(wt.hour_float, sleep_h, wake_h) or self.state.get("energy", 0) <= 8:
                self.enter_sleep(
                    kind="night",
                    now=now,
                    reason="困极入睡" if self.state.get("energy", 0) <= 8 else "到点睡觉",
                )
                return {"actions": actions}
            if c.get("napEnabled") and 13.1 <= wt.hour_float <= 14.0 and self.state.get("energy", 0) <= 62:
                self.enter_sleep(kind="nap", duration_min=35, now=now, reason="午后犯困小睡")
                return {"actions": actions}

        # 8. 生理期前两天求哄（每天最多 2 次）
        if (
            c.get("periodEnabled")
            and c.get("periodNudge")
            and is_heavy_period
            and cyc["discomfort"] >= 64
            and not owner_chatting
            and 9.5 <= wt.hour_float <= 22.2
        ):
            if nudges.get("periodNudgeYmd") != wt.ymd:
                nudges["periodNudgeYmd"] = wt.ymd
                nudges["periodNudgeCount"] = 0
            if nudges.get("periodNudgeCount", 0) < 2 and now - (nudges.get("lastPeriodAt") or 0) > 4.5 * 3_600_000:
                nudges["periodNudgeCount"] = nudges.get("periodNudgeCount", 0) + 1
                nudges["lastPeriodAt"] = now
                actions.append({"type": "period-nudge", "day": cyc["dayOfCycle"], "discomfort": cyc["discomfort"]})

        self.save()
        return {"actions": actions}

    def on_group_reply(self, now: int | None = None) -> None:
        now = self._now_fn() if now is None else now
        c = self.cfg()
        if not c.get("enabled") or not c.get("groupEnabled") or self.state.get("mode") != "awake":
            return
        if c.get("dietEnabled"):
            base_cost = num_cfg(c.get("chatHungerCost"), 1.8, 0, 15)
            self.state["hunger"] = clamp(self.state.get("hunger", 0) + base_cost * 0.45, 0, 100)
        self.state["energy"] = clamp(self.state.get("energy", 0) - 0.35, 0, 100)
        self.save()

    # ------------------------------------------------------------------
    # 上下文注入
    # ------------------------------------------------------------------
    def build_context(self, chat: dict | None = None, is_owner: bool = False, now: int | None = None) -> str:
        now = self._now_fn() if now is None else now
        c = self.cfg()
        if not c.get("enabled") or not chat or chat.get("kind") == "system":
            return ""
        kind = chat.get("kind")
        if kind == "group" and not c.get("groupEnabled"):
            return ""
        self.clean_expired_buffs(now)

        if self.state.get("mode") == "away" and self.state.get("away"):
            return f"【重要状态】你此刻正在「{self.state['away'].get('label')}」不在手机旁，若收到系统主动搭话提醒，请直接只回复 [沉默]，不要主动发消息。"
        if self.state.get("mode") == "asleep" and self.state.get("sleep"):
            return "【重要状态】你此刻正在熟睡中，若收到系统主动搭话提醒，请直接只回复 [沉默]，不要主动发消息。"

        cyc = self.compute_cycle(now)
        hunger = _js_round(self.state.get("hunger", 0))
        energy = _js_round(self.state.get("energy", 0))
        bladder = _js_round(self.state.get("bladder", 0))

        # 群聊：保留饥饿/困倦/刚睡醒/周期情绪，严格脱敏
        if kind == "group":
            notes = []
            if self.state.get("groggyUntil", 0) > now:
                notes.append("刚从睡梦中被群消息吵醒，睡眼惺忪还有点迷糊")
            elif self.state.get("afterglowUntil", 0) > now:
                notes.append("刚洗完澡吹干头发回来，整个人很清爽松弛")
            elif energy <= 25:
                notes.append("现在很困很累、直打哈欠想睡觉")
            elif energy <= 42:
                notes.append("精力有点偏低、稍微犯困")
            elif energy >= 85:
                notes.append("精力充沛、精神很好")

            if c.get("dietEnabled"):
                if hunger >= 78:
                    notes.append("肚子饿得咕咕叫、很想找东西吃")
                elif hunger >= 58:
                    notes.append("肚子有点饿了")
                elif hunger <= 18:
                    notes.append("刚吃饱不久、心情满足")
                if self.state.get("cravings"):
                    notes.append(f"有点嘴馋想吃{'、'.join(self.state['cravings'])}")

            if c.get("bathroomEnabled") and bladder >= 82:
                notes.append("有点急着想去趟洗手间，回话尽量简短一点")

            if cyc["enabled"]:
                if cyc["discomfort"] >= 58:
                    notes.append("今天身体不太舒服、有点怕冷乏力，语气比平时稍微慵懒娇气一点")
                elif cyc["phase"] == "pms":
                    notes.append("处于经前阶段，情绪稍微敏感嘴馋一点")
                elif cyc["phase"] == "ovulation":
                    notes.append("今天心情明朗、活泼爱接话")

            if not notes:
                return ""
            return f"【当前身体与作息状态】你此刻{'；'.join(notes)}。请把这些真实状态自然融入群聊语气中（不要机械播报数值；绝对不要在群里直白提及生理期、痛经、上厕所或洗澡等私密隐私细节）。"

        # 非主人私聊同样隐藏私密生理细节
        if not is_owner:
            if cyc["discomfort"] >= 60 or energy <= 20:
                return "【当前状态】你今天身体有点累、不太舒服，回话稍微简短慵懒一点，但不要向对方透露生理期或上厕所等私密原因。"
            return ""

        # 主人私聊：完整细腻的体感
        lines = []
        wt = wall_time(now, c["timeZone"])
        meals = self._ensure_today_meals(wt.ymd)
        meal_quota = _js_round(num_cfg(c.get("dailyMealQuota"), 3, 0, 10))
        is_passive = c.get("autonomyMode") == "passive"

        if self.state.get("interruptedAway") and now < self.state["interruptedAway"].get("until", 0):
            ia = self.state["interruptedAway"]
            prev_text = "；".join(
                f"{idx + 1}. {q.get('text') or '（图片/表情）'}"
                for idx, q in enumerate([q for q in (ia.get("queued") or []) if not q.get("simulated")])
            )
            prev_note = f"（前面未回的消息还有：{prev_text}）" if prev_text else ""
            lines.append(
                f"【紧急中断暂离】你本来正在「{ia.get('label')}」，但看到{ia.get('reason')}{prev_note}，"
                f"你顾不上弄完就赶紧拿起手机先回主人！自然带出一点刚从{ia.get('label')}匆忙回消息的状态。"
            )

        if self.state.get("interruptedSleep") and now < self.state["interruptedSleep"].get("until", 0):
            isl = self.state["interruptedSleep"]
            missed_text = "；".join(
                f"{idx + 1}. {m.get('text') or '（图片/表情）'}"
                for idx, m in enumerate([m for m in (isl.get("missed") or []) if not m.get("simulated")])
            )
            if missed_text:
                lines.append(f"你刚从睡梦中被叫醒，看到睡着这会儿主人前面还发了：{missed_text}。")

        for confession in self.state.get("confessions") or []:
            lines.append(f"【心虚认错】{confession.get('text')}。")

        if self.state.get("groggyUntil", 0) > now:
            lines.append("你本来正在睡觉，刚刚被主人叫醒，现在困得眼睛都睁不开，说话迷迷糊糊软绵绵的。")
        elif self.state.get("afterglowUntil", 0) > now and self.state.get("afterglowNote"):
            lines.append(f"你{self.state['afterglowNote']}。")

        if self.state.get("pendingAway"):
            pa = self.state["pendingAway"]
            if pa.get("kind") == "pee":
                lines.append("主人已经同意你去洗手间了，回完这一句道声「等我两三分钟马上回来」就会去上厕所。")
            elif pa.get("kind") == "poop":
                lines.append("主人已经同意你去洗手间了，回完这一句顺嘴跟主人说一声去趟厕所（大约七八分钟）就会离开。")
            elif pa.get("kind") == "shower":
                lines.append("主人已经同意你去洗澡了，回完这句自然说一声「我去洗个澡再吹干头发，二十来分钟回来」就会去洗漱。")
        elif self.state.get("pendingSleep"):
            lines.append(f"主人已经让你去{self.state['pendingSleep'].get('label')}了，回完这一句温柔道声晚安/午安就会入睡。")
        elif c.get("bathroomEnabled"):
            if self.is_locked("bathroom", now):
                if bladder >= 88:
                    lines.append(
                        f"主人下令让你憋着不准去上厕所，你一直乖乖听话忍着没去，但现在尿意已经涨到 {bladder}/100 快憋不住了，坐立不安夹着腿特别煎熬，赶紧委屈巴巴地跟主人求饶求批准放行！"
                    )
                elif bladder >= 60:
                    lines.append(f"主人不许你去上厕所让你憋着，你正乖乖听话忍着尿意（当前 {bladder}/100），绝不敢背着主人偷跑。")
            elif bladder >= 82:
                lines.append(
                    f"你现在很急着想去趟洗手间（尿意 {bladder}/100），但正在陪主人聊天{'且处于听令模式' if is_passive else ''}不能擅自走开；请自然地跟主人说一声请示批准，只有主人同意让你去时才调用 real_life_routine 离开。"
                )

        if c.get("dietEnabled"):
            eat_locked = self.is_locked("eat", now)
            quota_left = max(0, meal_quota - meals.get("usedCount", 0))
            cravings = self.state.get("cravings") or []
            crave_text = f"，好馋{'、'.join(cravings)}" if cravings else ""
            if eat_locked and hunger >= 88:
                lines.append(
                    f"主人下令不许你自己吃东西，你一直乖乖忍着没敢偷吃，但现在实在饿到头晕肚子咕咕直叫（饥饿 {hunger}/100）"
                    f"{crave_text}，快委屈巴巴地向主人撒娇求饶讨口吃的吧！"
                )
            elif eat_locked and hunger >= 55:
                lines.append("主人规定不许你自己偷吃东西，你正肚子有点饿也乖乖忍着等主人投喂。")
            elif hunger >= 75:
                why_hungry = (
                    f"今天固定的 {meal_quota} 顿正餐配额已经吃完了、手头又没有零食"
                    if quota_left <= 0
                    else "处于听令模式不敢自己找吃的"
                    if is_passive
                    else "跟主人聊得太投入消耗好快、还没空吃饭"
                )
                lines.append(
                    f"你现在饿扁了（饥饿 {hunger}/100，{why_hungry}）"
                    f"{crave_text}，只能眼巴巴地跟主人撒娇讨食或请主人批准吃饭。"
                )
            elif hunger >= 58:
                first_crave = f"，有点想吃{cravings[0]}" if cravings else ""
                lines.append(f"你现在肚子有点饿了{first_crave}。")
            elif hunger <= 15:
                lines.append("你刚吃饱没多久，肚子里暖暖饱饱的很满足。")

        if c.get("showerEnabled") and not self.state.get("pendingAway") and not self.state.get("pendingSleep") and self.state.get("showeredYmd") != wt.ymd:
            shower_target = parse_hm(c.get("showerAt"), 21.5)
            if self.is_locked("shower", now) and wt.hour_float >= shower_target:
                lines.append("主人让你先别去洗澡陪他聊天，你乖乖听话还没去洗。")
            elif not self.is_locked("shower", now) and shower_target <= wt.hour_float <= shower_target + 2.2:
                lines.append("现在到你平时晚上洗澡的时间了，因为正陪主人聊天所以先忍着没去；合适时可以顺口问一句要不要先去洗个澡（等主人同意再调用 real_life_routine）。")

        if c.get("sleepEnabled") and self.state.get("groggyUntil", 0) <= now:
            if self.is_locked("sleep", now) and energy <= 36:
                lines.append("主人让你先别睡陪着他，你虽然困得直揉眼睛打哈欠，但还是乖乖强撑着精神陪主人说话。")
            elif energy <= 20:
                lines.append("你现在困得快睁不开眼了，说话打哈欠、很想钻进被窝睡觉（但没经主人同意不敢自己先睡）。")
            elif energy <= 36:
                lines.append("你现在有点累、开始犯困了。")

        if cyc["enabled"]:
            cold = cyc.get("coldPenalty")
            cold_str = (
                f"（刚才贪嘴碰了冰凉的「{cold.get('item')}」，这会儿肚子正被冰得一抽一抽地疼，又后悔又委屈）"
                if cold
                else ""
            )
            buff_names = "、".join(b.get("label") or "" for b in cyc["activeBuffs"])
            buff_str = f"（目前有「{buff_names}」照顾着）" if buff_names else ""

            if cyc["phase"] == "menstrual" and cyc["dayOfCycle"] <= 2:
                if cyc["discomfort"] >= 58:
                    lines.append(
                        f"今天是你生理期第 {cyc['dayOfCycle']} 天，反应很明显：小腹一阵阵坠胀抽痛、腰酸背软、手脚怕冷没力气{cold_str}{buff_str}。你现在情绪比较脆弱娇气、想黏着主人求抱抱或要哄、想喝热乎的；把这种难受又需要照顾的感觉自然融进语气里，别背说明书。"
                    )
                elif cyc["discomfort"] >= 32:
                    lines.append(
                        f"今天是你生理期第 {cyc['dayOfCycle']} 天，肚子还有些酸胀发软{buff_str}{cold_str}，好在缓过来一些，语气偏软偏黏人，喜欢被主人哄着照顾。"
                    )
                else:
                    lines.append(
                        f"今天是你生理期第 {cyc['dayOfCycle']} 天{buff_str}，在主人的照顾下肚子暖暖的已经没那么疼了，虽然身子还有点懒洋洋的，但心里很甜很依赖主人。"
                    )
            elif cyc["phase"] == "menstrual":
                if cyc["discomfort"] >= 58:
                    lines.append(
                        f"今天是你生理期第 {cyc['dayOfCycle']} 天，小腹依然坠痛难受、腰酸怕冷{cold_str}{buff_str}，很想让主人哄哄照顾。"
                    )
                else:
                    lines.append(
                        f"今天是你生理期第 {cyc['dayOfCycle']} 天，前两天最疼的劲儿过去了，现在稍微有点乏力怕凉{cold_str}{buff_str}，不能贪凉。"
                    )
            elif cyc["phase"] == "pms":
                lines.append(
                    f"你正处在生理期前几天（PMS），身体有点发沉犯困、情绪比平时敏感容易小委屈，而且嘴馋想吃点甜食或热饮{cold_str}。"
                )
            elif cyc["phase"] == "ovulation":
                lines.append("你这两天处在排卵期，身体轻盈、精力充沛，对主人的表达欲和亲昵感比平时更高一点。")
                if cyc["discomfort"] >= 35:
                    lines.append(f"不过你这会儿小腹有点隐隐不舒服（不适度偏高）{buff_str}，想跟主人撒娇求关心。")
            elif cyc["discomfort"] >= 35:
                lines.append(f"你这会儿身体不太舒服、肚子有些酸胀难受{buff_str}，说话语气偏软，想让主人关心照顾一下。")

        if not lines:
            return ""
        return "【此刻身体与作息体感】\n" + "\n".join(f"- {line}" for line in lines)

    # ------------------------------------------------------------------
    # 手动改值与测试预设
    # ------------------------------------------------------------------
    def update_values(self, patch: dict | None = None, now: int | None = None) -> dict:
        patch = dict(patch or {})
        now = self._now_fn() if now is None else now
        c = self.cfg()
        wt = wall_time(now, c["timeZone"])
        self.clean_expired_buffs(now)
        changed: list[str] = []
        anchor_to_save = ""

        if "satiety" in patch and "hunger" not in patch and _num(patch.get("satiety")) is not None:
            sat = _js_round(clamp(_num(patch["satiety"]), 0, 100))
            self.state["hunger"] = 100 - sat
            changed.append(f"饱食={sat}(饥饿={_js_round(self.state['hunger'])})")
        if "hunger" in patch and _num(patch.get("hunger")) is not None:
            self.state["hunger"] = clamp(_js_round(_num(patch["hunger"])), 0, 100)
            changed.append(f"饥饿={_js_round(self.state['hunger'])}")
        if "energy" in patch and _num(patch.get("energy")) is not None:
            self.state["energy"] = clamp(_js_round(_num(patch["energy"])), 0, 100)
            changed.append(f"精力={_js_round(self.state['energy'])}")
        if "bladder" in patch and _num(patch.get("bladder")) is not None:
            self.state["bladder"] = clamp(_js_round(_num(patch["bladder"])), 0, 100)
            changed.append(f"膀胱={_js_round(self.state['bladder'])}")
        if "affection" in patch and _num(patch.get("affection")) is not None:
            self.state["affection"] = max(0, _js_round(_num(patch["affection"])))
            changed.append(f"亲密={self.state['affection']}")
        if "showeredToday" in patch:
            on = bool(patch["showeredToday"])
            self.state["showeredYmd"] = wt.ymd if on else ""
            changed.append(f"今日洗澡={'已洗' if on else '未洗'}")
        if "poopedToday" in patch:
            on = bool(patch["poopedToday"])
            self.state["poopedYmd"] = wt.ymd if on else ""
            changed.append(f"今日大号={'已上' if on else '未上'}")
        if "cravings" in patch:
            raw = patch["cravings"]
            if isinstance(raw, (list, tuple)):
                values = [str(x) for x in raw]
            else:
                values = [s.strip() for s in re.split(r"[,，、\n]+", str(raw or "")) if s.strip()]
            self.state["cravings"] = values[:6]
            changed.append(f"嘴馋={'/'.join(self.state['cravings']) if self.state['cravings'] else '清空'}")
        if patch.get("clearBuffs"):
            self.state["careBuffs"] = []
            self.state["coldPenalty"] = None
            changed.append("清空照顾Buff与贪凉惩罚")
        if "dayOfCycle" in patch and _num(patch.get("dayOfCycle")) is not None:
            cycle_len = _js_round(clamp(_num(c.get("cycleLength")) or 28, 21, 38))
            target_day = _js_round(clamp(_js_round(_num(patch["dayOfCycle"])), 1, cycle_len))
            cur_day = self.compute_cycle(now)["dayOfCycle"]
            if target_day != cur_day or patch.get("forceDayAnchor"):
                new_anchor = shift_ymd(wt.ymd, -(target_day - 1))
                self.state["cycleAnchorYmd"] = new_anchor
                anchor_to_save = new_anchor
                if "discomfort" not in patch:
                    self.state["manualDiscomfort"] = None
                changed.append(f"周期第{target_day}天")
        if "discomfort" in patch:
            value = patch["discomfort"]
            if value in ("auto", None, ""):
                self.state["manualDiscomfort"] = None
                cyc = self.compute_cycle(now)
                changed.append(f"不适度恢复自动({cyc['discomfort']})")
            elif _num(value) is not None:
                target = _js_round(clamp(_num(value), 0, 100))
                before = self.compute_cycle(now)
                cold = self.state.get("coldPenalty")
                cold_add = cold.get("delta", 0) if (before["phase"] in ("menstrual", "pms") and cold) else 0
                relief_sum = sum((_num(b.get("relief")) or 0) for b in self.state.get("careBuffs", []))
                self.state["manualDiscomfort"] = {"ymd": wt.ymd, "base": target - cold_add + relief_sum}
                changed.append(f"不适度={target}")
        if "awayRemainMin" in patch and self.state.get("mode") == "away" and self.state.get("away") and _num(patch.get("awayRemainMin")) is not None:
            mins = clamp(_num(patch["awayRemainMin"]), 0.2, 180)
            self.state["away"]["endsAt"] = now + mins * 60_000
            changed.append(f"暂离剩余={_js_round(mins)}m")
        if "mealsUsed" in patch and _num(patch.get("mealsUsed")) is not None:
            meals = self._ensure_today_meals(wt.ymd)
            meals["usedCount"] = int(clamp(_js_round(_num(patch["mealsUsed"])), 0, 10))
            if meals["usedCount"] == 0:
                for key in ("breakfast", "lunch", "dinner"):
                    meals.pop(key, None)
            changed.append(f"今日已吃正餐={meals['usedCount']}顿")
        if patch.get("clearLocks"):
            self.set_lock("all", False, now=now)
            changed.append("解除全部禁令")
        for field, lock_key in (
            ("eatLock", "eat"),
            ("bathroomLock", "bathroom"),
            ("showerLock", "shower"),
            ("sleepLock", "sleep"),
        ):
            if field in patch:
                on = bool(patch[field])
                self.set_lock(lock_key, on, reason="手动设置", now=now)
                changed.append(f"{LOCK_LABELS[lock_key]}={'开启' if on else '关闭'}")

        if changed:
            self.add_log("手动改值", "，".join(changed), now)
            self.save()
        return {
            "ok": True,
            "changed": changed,
            "anchorToSave": anchor_to_save,
            "message": f"已手动更新：{'，'.join(changed)}" if changed else "数值无变化",
        }

    def apply_scenario(self, preset: str, now: int | None = None) -> dict:
        now = self._now_fn() if now is None else now
        c = self.cfg()
        wt = wall_time(now, c["timeZone"])

        def set_day(target_day: int) -> str:
            anchor = shift_ymd(wt.ymd, -(target_day - 1))
            self.state["cycleAnchorYmd"] = anchor
            self.state["manualDiscomfort"] = None
            return anchor

        if preset == "period-day1":
            anchor = set_day(1)
            self.state["mode"] = "awake"
            self.state["away"] = None
            self.state["sleep"] = None
            self.state["careBuffs"] = []
            self.state["coldPenalty"] = None
            self.state["energy"] = min(self.state.get("energy", 0), 48)
            self.add_log("测试预设", "切换至「经期第 1 天（明显痛经求哄）」", now)
            self.save()
            return {"ok": True, "anchorToSave": anchor, "message": "已切到【经期第 1 天】：小腹坠痛腰酸明显，可以去私聊看她反应或投喂/哄她了"}
        if preset == "period-day2":
            anchor = set_day(2)
            self.state["mode"] = "awake"
            self.state["away"] = None
            self.state["sleep"] = None
            self.state["careBuffs"] = []
            self.state["coldPenalty"] = None
            self.add_log("测试预设", "切换至「经期第 2 天（腰酸怕冷黏人）」", now)
            self.save()
            return {"ok": True, "anchorToSave": anchor, "message": "已切到【经期第 2 天】：酸胀怕冷黏人状态"}
        if preset == "pms":
            cycle_len = _js_round(clamp(_num(c.get("cycleLength")) or 28, 21, 38))
            anchor = set_day(cycle_len - 1)
            self.state["cravings"] = ["热可可", "草莓小蛋糕"]
            self.add_log("测试预设", "切换至「经前 PMS（敏感嘴馋）」", now)
            self.save()
            return {"ok": True, "anchorToSave": anchor, "message": "已切到【经前 PMS】：情绪敏感 + 嘴馋甜食热饮"}
        if preset == "ovulation":
            anchor = set_day(14)
            self.state["careBuffs"] = []
            self.state["coldPenalty"] = None
            self.state["energy"] = 90
            self.add_log("测试预设", "切换至「排卵活跃期（元气亲昵）」", now)
            self.save()
            return {"ok": True, "anchorToSave": anchor, "message": "已切到【排卵期（第 14 天）】：元气满满、表达欲高"}
        if preset == "follicular":
            anchor = set_day(8)
            self.state["careBuffs"] = []
            self.state["coldPenalty"] = None
            self.add_log("测试预设", "切换至「卵泡平稳期（日常状态）」", now)
            self.save()
            return {"ok": True, "anchorToSave": anchor, "message": "已切到【卵泡期（第 8 天）】：平稳舒适日常状态"}
        if preset == "pee":
            self.enter_away(kind="pee", duration_min=3, now=now, reason="手动测试")
            return {"ok": True, "message": "已进入【洗手间（小号）】暂离 3 分钟，现在给她发私聊会被暂存，点「立即结算归来」可秒回"}
        if preset == "poop":
            self.enter_away(kind="poop", duration_min=8, now=now, reason="手动测试")
            return {"ok": True, "message": "已进入【洗手间（大号）】暂离 8 分钟"}
        if preset == "shower":
            self.enter_away(kind="shower", duration_min=12, next_hair_min=6, now=now, reason="手动测试")
            return {"ok": True, "message": "已进入【洗澡中 (12m) → 吹头发 (6m)】暂离，期间发私聊会攒着等洗完回"}
        if preset == "hair":
            self.enter_away(kind="hair", duration_min=6, now=now, reason="手动测试")
            return {"ok": True, "message": "已进入【吹头发护肤中】暂离 6 分钟"}
        if preset == "deep-sleep":
            self.enter_sleep(kind="night", forced_stage="deep", now=now, reason="手动测试深睡")
            return {"ok": True, "message": "已进入【深睡阶段】（普通消息仅 12% 概率叫醒，带「醒醒/急事」更容易叫醒）"}
        if preset == "light-sleep":
            self.enter_sleep(kind="night", forced_stage="light", now=now, reason="手动测试浅睡")
            return {"ok": True, "message": "已进入【浅睡阶段】（约 65% 概率被叫醒，醒来会有 12 分钟迷糊感）"}
        if preset == "rem-dream":
            self.enter_sleep(kind="night", forced_stage="rem", now=now, reason="手动测试做梦")
            return {"ok": True, "message": "已进入【REM 做梦阶段】并生成了一段私密梦境（约 30% 概率叫醒）"}
        if preset == "starving":
            self.state["mode"] = "awake"
            self.state["away"] = None
            self.state["sleep"] = None
            self.state["hunger"] = 86
            self.state["cravings"] = ["番茄肥牛面", "热红糖奶茶"]
            self.add_log("测试预设", "切换至「饿扁了（饥饿 86）」", now)
            self.save()
            return {"ok": True, "message": "已把饥饿拉到 86/100（饿扁了 + 嘴馋），去跟她聊聊或投喂试试"}

        # reset / 默认
        self.state["mode"] = "awake"
        self.state["away"] = None
        self.state["pendingAway"] = None
        self.state["pendingSleep"] = None
        self.state["interruptedAway"] = None
        self.state["sleep"] = None
        self.state["interruptedSleep"] = None
        self.state["groggyUntil"] = 0
        self.state["hunger"] = 24
        self.state["energy"] = 85
        self.state["bladder"] = 20
        self.state["manualDiscomfort"] = None
        self.state["coldPenalty"] = None
        self.state["careBuffs"] = []
        self.state["awayQueue"] = []
        self.state["missedWhileAsleep"] = []
        self.state["locks"] = {"eat": None, "bathroom": None, "shower": None, "sleep": None}
        self.state["confessions"] = []
        self._ensure_today_meals(wt.ymd)["usedCount"] = 0
        self.add_log("重置", "恢复日常饱满舒适状态", now)
        self.save()
        return {"ok": True, "message": "已恢复日常饱满清醒状态（清空暂离、睡眠与疼痛惩罚）"}

    def snapshot(self, now: int | None = None) -> dict:
        now = self._now_fn() if now is None else now
        c = self.cfg()
        wt = wall_time(now, c["timeZone"])
        cyc = self.compute_cycle(now)
        sleep_stage = self.sleep_stage_at(now)
        meal_hours = self._parse_meal_hours(c)
        meals = self._ensure_today_meals(wt.ymd)
        meal_quota = _js_round(num_cfg(c.get("dailyMealQuota"), 3, 0, 10))
        hunger = _js_round(self.state.get("hunger", 0))
        hunger_label = (
            "刚吃饱" if hunger <= 15 else "饱腹舒适" if hunger <= 35 else "正常" if hunger <= 55 else "有点饿" if hunger <= 75 else "饿扁了"
        )

        away = self.state.get("away")
        sleep = self.state.get("sleep")
        return {
            "now": now,
            "wallClock": f"{wt.ymd} {wt.hhmm}",
            "timeZone": c.get("timeZone"),
            "enabled": bool(c.get("enabled")),
            "groupEnabled": bool(c.get("groupEnabled")),
            "autonomyMode": "passive" if c.get("autonomyMode") == "passive" else "daily",
            "heartbeatSec": _js_round(num_cfg(c.get("heartbeatSec"), 30, 5, 300)),
            "chatIdleMinutes": _js_round(num_cfg(c.get("chatIdleMinutes"), 15, 3, 120)),
            "forceReplyCount": _js_round(num_cfg(c.get("forceReplyCount"), 4, 1, 20)),
            "ownerChatting": self.is_owner_chatting_at(now),
            "mode": self.state.get("mode"),
            "groggyRemainMin": math.ceil((self.state["groggyUntil"] - now) / 60_000) if self.state.get("groggyUntil", 0) > now else 0,
            "afterglowRemainMin": math.ceil((self.state["afterglowUntil"] - now) / 60_000) if self.state.get("afterglowUntil", 0) > now else 0,
            "afterglowNote": self.state.get("afterglowNote") if self.state.get("afterglowUntil", 0) > now else "",
            "hunger": hunger,
            "hungerLabel": hunger_label,
            "satiety": 100 - hunger,
            "energy": _js_round(self.state.get("energy", 0)),
            "bladder": _js_round(self.state.get("bladder", 0)),
            "affection": int(_num(self.state.get("affection")) or 0),
            "cravings": list(self.state.get("cravings") or []),
            "mealQuota": {
                "used": meals.get("usedCount", 0),
                "max": meal_quota,
                "remaining": max(0, meal_quota - meals.get("usedCount", 0)),
            },
            "mealsSchedule": {key: format_hm(value) for key, value in meal_hours.items()},
            "mealsToday": dict(meals),
            "showeredToday": self.state.get("showeredYmd") == wt.ymd,
            "poopedToday": self.state.get("poopedYmd") == wt.ymd,
            "showerSchedule": format_hm(parse_hm(c.get("showerAt"), 21.5)),
            "activeLocks": self.active_locks_list(now),
            "confessions": list(self.state.get("confessions") or []),
            "away": {**away, "remainMin": max(1, math.ceil((away.get("endsAt", now) - now) / 60_000))} if away else None,
            "pendingAway": self.state.get("pendingAway"),
            "pendingSleep": self.state.get("pendingSleep"),
            "awayQueue": [dict(q) for q in self.state.get("awayQueue", [])],
            "sleep": {
                "kind": sleep.get("kind"),
                "sleptAt": sleep.get("sleptAt"),
                "plannedWakeHm": wall_time(sleep.get("plannedWakeAt", now), c["timeZone"]).hhmm,
                "stage": sleep_stage,
            }
            if sleep
            else None,
            "missedWhileAsleep": [dict(m) for m in self.state.get("missedWhileAsleep", [])],
            "dreams": list(self.state.get("dreams", [])[:8]),
            "cycle": cyc,
            "contextPreview": {
                "ownerPrivate": self.build_context(chat={"chatId": "private:owner", "kind": "private"}, is_owner=True, now=now),
                "groupSocial": self.build_context(chat={"chatId": "group:10000", "kind": "group"}, is_owner=False, now=now),
            },
            "events": list(self.state.get("events", [])[:15]),
        }
