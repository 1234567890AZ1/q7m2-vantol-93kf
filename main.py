"""生理模块（real-life）—— AstrBot 插件入口。

移植自 Aurabot 扩展模块 WhiteBr1ck/aurabot-mod-real-life v1.1.0：
一日三餐饥饿与讨食、喝水与上厕所暂离、晚间洗澡吹头发、困倦入睡与做梦概率唤醒、
28 天生理周期（痛经怕冷与投喂哄人互动），以及日常自主 / 被动听令双模式。

模块划分：
  main.py   - Star 主类：配置、钩子、指令、LLM 工具、心跳与主动消息
  engine.py - 生理与作息状态机（与上游 lib/engine.mjs 对齐）
  clock.py  - 时区安全的墙上时钟工具（对齐 lib/clock.mjs）
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, StarTools

from .engine import RealLifeEngine

PLUGIN_NAME = "astrbot_plugin_real_life"
STARTUP_GRACE_MS = 10 * 60_000
COMMAND_NAMES = ("身体测试", "身体", "投喂", "照顾", "改状态")

SCENARIO_ALIASES = {
    "经期1": "period-day1",
    "经期第1天": "period-day1",
    "痛经": "period-day1",
    "经期2": "period-day2",
    "经期第2天": "period-day2",
    "经前": "pms",
    "pms": "pms",
    "排卵": "ovulation",
    "排卵期": "ovulation",
    "日常": "follicular",
    "卵泡期": "follicular",
    "上厕所": "pee",
    "小号": "pee",
    "洗手间": "pee",
    "大号": "poop",
    "洗澡": "shower",
    "吹头发": "hair",
    "深睡": "deep-sleep",
    "浅睡": "light-sleep",
    "做梦": "rem-dream",
    "饥饿": "starving",
    "饿": "starving",
    "重置": "reset",
    "恢复": "reset",
}

OWNER_RULES = [
    "你拥有真实的日常身体体感（三餐饥饿与嘴馋、困倦睡眠、去洗手间、晚间洗澡吹头发、以及生理周期的腹痛怕冷与情绪起伏）。把体感自然融入语气，不要机械播报分数。",
    "当主人问起你现在饿不饿、今天吃过什么、困不困、洗没洗澡、想去厕所吗、生理期第几天/还有多久来、昨晚做没做梦等自身状态时，先调用 real_life_status 查看真实记录再回答，不要瞎编。",
    "当主人在聊天中投喂你食物/饮品（比如请你喝奶茶、喂蛋糕、递红糖水、给冰饮），或照顾安抚你的不适（如贴暖宝宝、揉肚子抱抱、给止痛药）时，先调用 real_life_interact 更新身体状态再回复。",
    "正在跟主人聊天时，未经主人同意你绝不能擅自离开去上厕所、洗澡或睡觉；如果急着想去或饿了，先在对话里自然请示主人，只有当主人明确同意/催你去（如「去吧」「快去上厕所」「去吃饭吧」）时，才调用 real_life_routine(action=\"go\") 执行。",
    "当主人下令不许你做某事（如「不准去上厕所，憋着！」「不许偷吃东西」「先别洗澡」「别睡陪我聊」）时，必须调用 real_life_routine(action=\"forbid\") 锁定状态并乖乖听话，绝不能嘴上答应背地偷跑！主人解除禁令时再调用 action=\"allow\" 或 \"go\"。",
]

GROUP_RULE = (
    "你在群聊中也拥有真实的日常作息与身体体感（如饥饿、嘴馋、困倦、刚睡醒迷糊等），"
    "请自然融入群聊语气，不要机械报分数；严格保护个人隐私，"
    "绝不在群聊提及生理期、痛经、上厕所或洗澡等私密细节。"
)

MEAL_LABELS = {"breakfast": "早饭", "lunch": "午饭", "dinner": "晚饭"}


def _split_ids(raw) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(item).strip() for item in raw if str(item).strip()}
    return {part.strip() for part in re.split(r"[,，\s]+", str(raw)) if part.strip()}


def _js_round(value: float) -> int:
    import math

    return int(math.floor(float(value) + 0.5))


class RealLifePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        try:
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception:  # noqa: BLE001 - 兼容旧版 AstrBot 缺少该方法
            data_dir = Path("data") / "plugin_data" / PLUGIN_NAME
        self.data_dir = Path(data_dir)
        self._owner_umo_file = self.data_dir / "owner_umo.txt"
        self.engine = RealLifeEngine(
            get_settings=self._settings,
            state_file=str(self.data_dir / "state.json"),
        )
        self._started_at = int(time.time() * 1000)
        self._heartbeat_task: asyncio.Task | None = None
        self._owner_umo = self._load_owner_umo()

    # ==================================================================
    # 配置与身份
    # ==================================================================
    def _settings(self) -> dict:
        try:
            return dict(self.config or {})
        except Exception:  # noqa: BLE001
            return {}

    def _cfg(self, key: str, default=None):
        value = self._settings().get(key, default)
        return default if value is None else value

    def _owner_ids(self) -> set[str]:
        ids = _split_ids(self._cfg("owner_id", "")) | _split_ids(self._cfg("owner_ids", ""))
        if ids:
            return ids
        try:
            return _split_ids(self.context.get_config().get("admins_id") or [])
        except Exception:  # noqa: BLE001
            return set()

    def _is_owner_id(self, user_id) -> bool:
        return str(user_id or "") in self._owner_ids()

    def is_owner(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id() or "")
        if not sender:
            return False
        ids = self._owner_ids()
        if ids:
            return sender in ids
        try:
            return bool(event.is_admin())
        except Exception:  # noqa: BLE001
            return False

    def _load_owner_umo(self) -> str:
        try:
            return self._owner_umo_file.read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _remember_owner_umo(self, event: AstrMessageEvent) -> None:
        umo = str(event.unified_msg_origin or "")
        if not umo or umo == self._owner_umo:
            return
        self._owner_umo = umo
        try:
            self._owner_umo_file.parent.mkdir(parents=True, exist_ok=True)
            self._owner_umo_file.write_text(umo, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _owner_target_umo(self) -> str:
        configured = str(self._cfg("owner_umo", "") or "").strip()
        return configured or self._owner_umo

    # ==================================================================
    # 事件辅助
    # ==================================================================
    def _is_mentioned(self, event: AstrMessageEvent) -> bool:
        self_id = str(event.get_self_id() or "")
        try:
            for component in event.get_messages() or []:
                if isinstance(component, At) and str(getattr(component, "qq", "")) == self_id:
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _is_plugin_command(self, event: AstrMessageEvent) -> bool:
        text = (event.get_message_str() or "").strip()
        text = re.sub(r"^[^\w\u4e00-\u9fff]+", "", text)
        return any(text == name or text.startswith(f"{name} ") for name in COMMAND_NAMES)

    def _chat_kind(self, event: AstrMessageEvent) -> str:
        return "private" if event.is_private_chat() else "group"

    def _message_dict(self, event: AstrMessageEvent) -> dict:
        return {
            "chatId": str(event.unified_msg_origin or ""),
            "kind": self._chat_kind(event),
            "userId": str(event.get_sender_id() or ""),
            "nickname": event.get_sender_name() or str(event.get_sender_id() or "对方"),
            "text": (event.get_message_str() or "").strip(),
            "mentioned": self._is_mentioned(event),
            "raw": {},
        }

    @staticmethod
    def _rest(event: AstrMessageEvent, command: str) -> str:
        text = (event.get_message_str() or "").strip()
        text = re.sub(r"^[^\w\u4e00-\u9fff]+", "", text)
        if text.startswith(command):
            return text[len(command) :].strip()
        return text

    # ==================================================================
    # 1. 消息接管：暂离攒消息 & 睡眠阶段概率唤醒
    # ==================================================================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        cfg = self.engine.cfg()
        if not cfg.get("enabled"):
            return
        if str(event.get_sender_id() or "") == str(event.get_self_id() or ""):
            return
        if self._is_plugin_command(event):
            return

        is_owner = self.is_owner(event)
        if is_owner and event.is_private_chat():
            self._remember_owner_umo(event)

        result = self.engine.on_incoming_message(self._message_dict(event), is_owner=is_owner)
        if result.get("intercepted"):
            logger.debug(f"[real_life] 接管消息（{event.unified_msg_origin}）：{result.get('reason')}")
            event.stop_event()
            return
        if result.get("forceReplied"):
            logger.info(
                f"[real_life] 主人急召/连发达阈值（第 {result.get('count')} 条），"
                f"已强制中断「{result.get('label') or '暂离'}」立即回消息"
            )
        if result.get("woken"):
            stage = result.get("stage") or {}
            logger.info(
                f"[real_life] 她在{stage.get('label') or '睡眠'}中被 {event.get_sender_name()} 吵醒了"
                f"（唤醒概率 {_js_round((result.get('wakeChance') or 0) * 100)}%）"
            )

    # ==================================================================
    # 2. 每轮上下文注入 + 行为规则
    # ==================================================================
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        cfg = self.engine.cfg()
        if not cfg.get("enabled"):
            return
        kind = self._chat_kind(event)
        is_owner = self.is_owner(event)

        context_text = self.engine.build_context(
            {"kind": kind, "chatId": str(event.unified_msg_origin or "")}, is_owner=is_owner
        )
        if context_text:
            req.system_prompt = f"{req.system_prompt or ''}\n\n{context_text}".strip()

        if kind == "group" and cfg.get("groupEnabled"):
            req.system_prompt = f"{req.system_prompt or ''}\n\n{GROUP_RULE}".strip()
        elif kind == "private" and is_owner:
            req.system_prompt = f"{req.system_prompt or ''}\n\n" + "\n".join(OWNER_RULES)

        if not (kind == "private" and is_owner):
            self._drop_tools(req)

    @staticmethod
    def _drop_tools(req) -> None:
        tool_set = getattr(req, "func_tool", None)
        if not tool_set:
            return
        for name in ("real_life_status", "real_life_interact", "real_life_routine"):
            try:
                tool_set.remove_tool(name)
            except Exception:  # noqa: BLE001
                pass

    # ==================================================================
    # 3. 发送出口防穿帮 + 回复后的状态推进
    # ==================================================================
    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        cfg = self.engine.cfg()
        if not cfg.get("enabled"):
            return
        if getattr(event, "_real_life_allow", False):
            return
        if self._is_plugin_command(event):
            return

        kind = self._chat_kind(event)
        if kind == "group" and not cfg.get("groupEnabled"):
            return

        if self.engine.state.get("mode") in ("away", "asleep"):
            try:
                event.clear_result()
            except Exception:  # noqa: BLE001
                return
            logger.debug(f"[real_life] 已拦截穿帮外发（当前 {self.engine.state.get('mode')}）：{event.unified_msg_origin}")
            return

        if kind == "group":
            self.engine.on_group_reply()
            return

        if not self._is_owner_umo(str(event.unified_msg_origin or "")):
            return

        now = int(time.time() * 1000)
        self.engine.clear_transient_interrupts(include_confessions=True)

        pending_away = self.engine.state.get("pendingAway")
        if pending_away and pending_away.get("approved"):
            away = self.engine.activate_pending_away(now)
            if away:
                logger.info(f"[real_life] 已获批准并回完私聊，正式进入「{away.get('label')}」")
        elif self.engine.state.get("pendingSleep") and self.engine.state["pendingSleep"].get("approved"):
            sleep = self.engine.activate_pending_sleep(now)
            if sleep:
                logger.info("[real_life] 说完晚安，正式进入睡眠")

    def _is_owner_umo(self, umo: str) -> bool:
        if self._owner_umo and umo == self._owner_umo:
            return True
        parts = umo.split(":")
        return len(parts) >= 3 and self._is_owner_id(parts[-1])

    # ==================================================================
    # 4. 主人指令
    # ==================================================================
    @filter.command("身体", alias={"身体状态", "真人状态"})
    async def cmd_status(self, event: AstrMessageEvent):
        """查看她此刻的饮食、精力、如厕洗澡暂离与生理周期全景状态。"""
        event._real_life_allow = True  # type: ignore[attr-defined]
        yield event.plain_result(self._status_text())

    @filter.command("投喂", alias={"喂她"})
    async def cmd_feed(self, event: AstrMessageEvent):
        """投喂食物或饮品：/投喂 红糖姜茶 热 一份（经期喂冰饮会痛！）"""
        event._real_life_allow = True  # type: ignore[attr-defined]
        rest = self._rest(event, "投喂")
        parts = [p for p in rest.split() if p]
        if not parts:
            yield event.plain_result(
                "用法：/投喂 <食物或饮品> [热|冰|常温] [一小口|半份|一份|管饱]\n"
                "例如：/投喂 红糖姜茶 热 一份  或  /投喂 冰奶茶"
            )
            return
        item = parts[0]
        temp = parts[1] if len(parts) > 1 else ""
        portion = parts[2] if len(parts) > 2 else "full"
        r = self.engine.feed(item=item, temp=temp, portion=portion, by_owner=True)
        tail = ""
        if r.get("coldTriggered"):
            tail = (
                f"\n⚠️ 她正在生理期，喝/吃了冰凉的「{r['item']}」后小腹抽痛加重了"
                f"（不适度升至 {r['discomfort']}/100）！快用 /照顾 暖宝宝 哄哄她！"
            )
        elif r.get("warmComforted"):
            tail = f"\n💖 热乎乎的「{r['item']}」缓解了她的不适（当前不适度 {r['discomfort']}/100）！"
        yield event.plain_result(
            f"🍽️ 已投喂{r['portionLabel']}「{r['item']}」：饥饿度 {r['beforeHunger']} → {r['afterHunger']}/100，"
            f"膀胱充盈 {r['bladder']}/100{tail}"
        )

    @filter.command("照顾", alias={"哄她", "护理"})
    async def cmd_care(self, event: AstrMessageEvent):
        """生理期或不适时照顾她：/照顾 暖宝宝 | 红糖水 | 揉肚子 | 布洛芬 | 盖被子"""
        event._real_life_allow = True  # type: ignore[attr-defined]
        method = self._rest(event, "照顾") or "揉揉肚子抱抱哄哄"
        r = self.engine.care(method=method, by_owner=True)
        yield event.plain_result(
            f"💝 已照顾「{r['label']}」：当前{r['phaseName']}，"
            f"身体不适度 {r['beforeDiscomfort']} → {r['afterDiscomfort']}/100（亲密累计 {r['affection']}）"
        )

    @filter.command("改状态", alias={"改数值", "设状态"})
    async def cmd_update(self, event: AstrMessageEvent):
        """手动修改各项数值：/改状态 饥饿=80 精力=35 膀胱=60 周期=1 不适=85 心情=45 洗澡=已洗 嘴馋=火锅,蛋糕"""
        event._real_life_allow = True  # type: ignore[attr-defined]
        raw = self._rest(event, "改状态")
        if not raw:
            yield event.plain_result(
                "【手动修改数值用法】支持空格分隔多个 键=值：\n"
                "- /改状态 饥饿=80 精力=30 膀胱=70 已吃=3\n"
                "- /改状态 周期=1 不适=85 心情=40\n"
                "- /改状态 禁食=1 禁厕=1  或  /改状态 解禁=1\n"
                "- /改状态 不适=自动 清空buff=1\n"
                "- /改状态 洗澡=已洗 嘴馋=番茄肥牛面,草莓蛋糕 亲密=50"
            )
            return
        patch = self._parse_update_tokens(raw)
        result = self.engine.update_values(patch)
        yield event.plain_result(f"🎛️ {result['message']}")

    @filter.command("身体测试", alias={"真人测试"})
    async def cmd_scenario(self, event: AstrMessageEvent):
        """一键切换测试场景：/身体测试 经期1|经期2|经前|排卵|上厕所|洗澡|深睡|浅睡|饥饿|结算|预览|重置"""
        event._real_life_allow = True  # type: ignore[attr-defined]
        key = self._rest(event, "身体测试").strip().lower()
        if not key or key in ("help", "帮助"):
            yield event.plain_result(
                "\n".join(
                    [
                        "【真人化快速测试命令】",
                        "- /身体测试 经期1  → 切到经期第1天（剧痛怕冷委屈求哄）",
                        "- /身体测试 经期2  → 切到经期第2天（腰酸黏人）",
                        "- /身体测试 经前   → 切到经前PMS（敏感嘴馋）",
                        "- /身体测试 上厕所 → 立即进入洗手间暂离3分钟（发私聊会攒消息）",
                        "- /身体测试 洗澡   → 立即进入洗澡+吹头发暂离",
                        "- /身体测试 深睡   → 立即进入深睡（仅12%叫醒率）",
                        "- /身体测试 浅睡   → 立即进入浅睡（65%叫醒率，醒后迷糊）",
                        "- /身体测试 饥饿   → 拉高饥饿度至86并生成嘴馋项",
                        "- /身体测试 结算   → 立即结束暂离/睡醒，并把积攒消息合并发给她回复",
                        "- /身体测试 预览   → 查看当前注入给私聊与群聊的提示词（验防泄密）",
                        "- /身体测试 重置   → 恢复日常舒适在线状态",
                    ]
                )
            )
            return
        if key in ("结算", "归来", "醒来", "settle"):
            result = await self._settle_now()
            yield event.plain_result(f"✅ {result['message']}")
            return
        if key in ("预览", "preview"):
            snap = self.engine.snapshot()
            preview = snap["contextPreview"]
            yield event.plain_result(
                "\n".join(
                    [
                        "【主人私聊上下文注入预览】",
                        preview.get("ownerPrivate") or "（当前平稳，无额外注入）",
                        "",
                        "【群聊上下文注入预览（严格脱敏）】",
                        preview.get("groupSocial") or "（空：群聊完全隐去私人身体细节）",
                    ]
                )
            )
            return
        preset = SCENARIO_ALIASES.get(key, key)
        result = self.engine.apply_scenario(preset)
        if result.get("anchorToSave") and str(self._cfg("cycleAnchor", "") or "").strip():
            self._save_cycle_anchor(result["anchorToSave"])
        yield event.plain_result(f"🧪 {result['message']}")

    def _save_cycle_anchor(self, anchor: str) -> None:
        try:
            self.config["cycleAnchor"] = anchor
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                saver()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _parse_update_tokens(raw: str) -> dict:
        patch: dict = {}
        for token in raw.split():
            match = re.match(r"^([^=:：]+)[=:：](.*)$", token)
            if not match:
                continue
            key = match.group(1).strip().lower()
            value = match.group(2).strip()
            truthy = re.match(r"^(1|true|是|开|已洗|洗了|已上|上了)$", value, re.IGNORECASE) is not None
            if key in ("饥饿", "饥饿度", "hunger"):
                patch["hunger"] = value
            elif key in ("饱食", "饱食度", "satiety"):
                patch["satiety"] = value
            elif key in ("精力", "精力值", "energy"):
                patch["energy"] = value
            elif key in ("膀胱", "尿意", "水分", "bladder"):
                patch["bladder"] = value
            elif key in ("周期", "天数", "day"):
                patch["dayOfCycle"] = value
            elif key in ("不适", "不适度", "痛感", "discomfort"):
                patch["discomfort"] = "auto" if value in ("自动", "auto") else value
            elif key in ("心情", "mood"):
                patch["mood"] = value
            elif key in ("亲密", "好感", "affection"):
                patch["affection"] = value
            elif key in ("洗澡", "shower"):
                patch["showeredToday"] = truthy
            elif key in ("大号", "poop"):
                patch["poopedToday"] = truthy
            elif key in ("嘴馋", "馋", "cravings"):
                patch["cravings"] = "" if value in ("无", "清空") else value
            elif key in ("清空buff", "清buff"):
                patch["clearBuffs"] = truthy
            elif key in ("暂离剩余", "剩余分钟"):
                patch["awayRemainMin"] = value
            elif key in ("已吃", "已用配额", "mealsused"):
                patch["mealsUsed"] = value
            elif key == "禁食":
                patch["eatLock"] = truthy
            elif key in ("禁厕", "憋尿", "憋着"):
                patch["bathroomLock"] = truthy
            elif key == "禁洗澡":
                patch["showerLock"] = truthy
            elif key == "禁睡":
                patch["sleepLock"] = truthy
            elif key in ("解禁", "清空禁令"):
                patch["clearLocks"] = truthy
        return patch

    def _status_text(self) -> str:
        s = self.engine.snapshot()
        if s["mode"] == "away" and s.get("away"):
            mode_str = (
                f"🚪 暂离中：{s['away']['label']}（剩约 {s['away']['remainMin']} 分钟，"
                f"积攒 {len(s['awayQueue'])} 条，连发 {s['forceReplyCount']} 条强召回）"
            )
        elif s["mode"] == "asleep" and s.get("sleep"):
            stage = s["sleep"].get("stage") or {}
            mode_str = (
                f"🌙 睡眠中：{stage.get('label') or '熟睡'}（叫醒率 {_js_round((stage.get('wakeChance') or 0) * 100)}%，"
                f"预计 {s['sleep']['plannedWakeHm']} 醒，未读 {len(s['missedWhileAsleep'])} 条）"
            )
        elif s.get("groggyRemainMin", 0) > 0:
            mode_str = f"😪 刚被吵醒迷糊中（剩 {s['groggyRemainMin']} 分钟）"
        elif s.get("afterglowRemainMin", 0) > 0:
            mode_str = "✨ 在线（刚洗完澡香香松弛中）"
        else:
            mode_str = "🟢 在线清醒"

        auto_badge = (
            f"{'🐾 被动听令模式' if s['autonomyMode'] == 'passive' else '🏠 日常自主模式'}"
            f"{' · 💬 热聊陪伴保护中' if s.get('ownerChatting') else ''} · ⏱️ 心跳 {s['heartbeatSec']}s"
        )
        locks = s.get("activeLocks") or []
        lock_str = f"\n🔒 生效禁令：{'、'.join(lock['label'] for lock in locks)}" if locks else ""
        buffs = ""
        if s["cycle"].get("activeBuffs"):
            buffs = " | 照顾生效：" + "、".join(
                f"{b['label']}({b['remainMin']}m)" for b in s["cycle"]["activeBuffs"]
            )
        cravings = s.get("cravings") or []
        cycle = s["cycle"]
        cycle_str = (
            f"第 {cycle['dayOfCycle']}/{cycle['cycleLength']} 天 · {cycle['phaseName']} · "
            f"不适度 {cycle['discomfort']}/100{buffs}"
            if cycle.get("enabled")
            else "已关闭"
        )
        return "\n".join(
            [
                f"【真人状态 · {s['wallClock']}】",
                f"{mode_str}（{auto_badge}）{lock_str}",
                f"🍚 饥饿：{s['hunger']}/100（{s['hungerLabel']}）· 今日正餐配额：已吃 "
                f"{s['mealQuota']['used']}/{s['mealQuota']['max']} 顿"
                f"{' · 馋：' + '、'.join(cravings) if cravings else ''}",
                f"⚡ 精力：{s['energy']}/100 · 💧 膀胱充盈：{s['bladder']}/100 · 🚿 今日洗澡："
                f"{'已洗' if s['showeredToday'] else '未洗(计划 ' + s['showerSchedule'] + ')'}",
                f"🌸 生理周期：{cycle_str}",
                f"❤️ 投喂照顾亲密累计：{s['affection']}",
            ]
        )

    # ==================================================================
    # 5. 给模型用的工具
    # ==================================================================
    @filter.llm_tool(name="real_life_status")
    async def real_life_status(self, event: AstrMessageEvent) -> str:
        """查看你自己此刻真实的身体、饮食、洗澡、睡眠梦境与生理周期详细状态。

        （主人问当前饿不饿、吃了什么、洗没洗澡、姨妈情况，或你自己想确认时调用）
        """
        if not (event.is_private_chat() and self.is_owner(event)):
            return "仅限主人私聊查询详细隐私状态"
        s = self.engine.snapshot()
        meals_done = [
            f"{MEAL_LABELS[key]}吃了{s['mealsToday'][key]}"
            for key in ("breakfast", "lunch", "dinner")
            if s.get("mealsToday", {}).get(key)
        ]
        recent_feed = [
            e["text"]
            for e in (s.get("events") or [])
            if e.get("tag") in ("投喂", "进食", "照顾")
        ][:4]
        cyc = s["cycle"]
        if cyc.get("enabled"):
            cyc_info = (
                f"处于周期第 {cyc['dayOfCycle']}/{cyc['cycleLength']} 天（{cyc['phaseName']}），"
                f"身体不适度 {cyc['discomfort']}/100，距离下次经期约 {cyc['nextPeriodDays']} 天"
            )
            if cyc.get("activeBuffs"):
                cyc_info += "，生效照顾：" + "、".join(b["label"] for b in cyc["activeBuffs"])
            if cyc.get("coldPenalty"):
                cyc_info += f"，刚才碰了冰凉的{cyc['coldPenalty']['item']}正肚子疼"
        else:
            cyc_info = "生理周期未开启"
        dreams = s.get("dreams") or []
        dream_info = f"最近一次梦境（{dreams[0]['ymd']}）：{dreams[0]['summary']}" if dreams else "最近没记住什么梦"
        locks = s.get("activeLocks") or []
        lock_info = (
            "生效禁令：" + "、".join(l["label"] + (f"(剩{l['remainMin']}m)" if l.get("remainMin") else "") for l in locks)
            if locks
            else "生效禁令：无"
        )
        lines = [
            f"当前时间：{s['wallClock']}（模式：{'被动听令' if s['autonomyMode'] == 'passive' else '日常自主'}）",
            f"饥饿与配额：{s['hungerLabel']}（饥饿 {s['hunger']}/100），今日自理正餐配额已用 "
            f"{s['mealQuota']['used']}/{s['mealQuota']['max']} 顿"
            f"{'，嘴馋：' + '、'.join(s.get('cravings') or []) if s.get('cravings') else ''}；"
            f"今日吃过：{'，'.join(meals_done) if meals_done else '暂未吃正餐'}",
            f"精力与洗漱：精力 {s['energy']}/100，尿意/膀胱 {s['bladder']}/100，今晚洗澡："
            f"{'已洗完' if s['showeredToday'] else '还没洗（计划 ' + s['showerSchedule'] + ' 左右）'}",
            lock_info,
            f"生理周期：{cyc_info}",
            f"近期饮食与照顾记录：{'；'.join(recent_feed) if recent_feed else '暂无'}",
            dream_info,
        ]
        return "\n".join(lines)

    @filter.llm_tool(name="real_life_interact")
    async def real_life_interact(
        self,
        event: AstrMessageEvent,
        action: str = "feed",
        item: str = "",
        temp: str = "",
        portion: str = "full",
    ) -> str:
        """当主人投喂你食物/饮品，或者照顾安抚你的身体不适时调用，实时更新饥饿、水分与生理期舒适度。

        Args:
            action(string): feed=投喂吃喝，care=照顾安抚身体不适
            item(string): 具体食物饮品名（如 红糖姜茶、冰奶茶、番茄面）或照顾方式（如 暖宝宝、揉肚子抱抱、布洛芬）
            temp(string): 食物饮品温度：warm=热/温，cold=冰/凉，normal=常温
            portion(string): 投喂分量：small=一小口，half=半份，full=一份，feast=管饱
        """
        if not (event.is_private_chat() and self.is_owner(event)):
            return "仅限主人私聊触发"
        if action == "care":
            r = self.engine.care(method=item or "揉揉肚子抱抱哄哄", by_owner=True)
            return (
                f"已接受照顾「{r['label']}」：当前阶段为{r['phaseName']}，"
                f"身体不适度从 {r['beforeDiscomfort']} 缓解到 {r['afterDiscomfort']}/100。"
            )
        r = self.engine.feed(item=item or "小蛋糕", temp=temp, portion=portion or "full", by_owner=True)
        if r.get("coldTriggered"):
            extra = (
                f"！注意：正值生理期碰了冰凉的「{r['item']}」，小腹抽痛明显加重了"
                f"（当前不适度 {r['discomfort']}/100）！"
            )
        elif r.get("warmComforted"):
            extra = f"，热乎乎下肚后小腹也舒服了不少（当前不适度 {r['discomfort']}/100）。"
        else:
            extra = "。"
        return f"吃喝了{r['portionLabel']}「{r['item']}」：饥饿度 {r['beforeHunger']}→{r['afterHunger']}/100{extra}"

    @filter.llm_tool(name="real_life_routine")
    async def real_life_routine(
        self,
        event: AstrMessageEvent,
        action: str = "go",
        activity: str = "pee",
        durationMin: float = 0,
    ) -> str:
        """执行或锁定日常行动：主人同意时用 go；命令不准去厕所/偷吃/洗澡/睡觉时用 forbid；解除禁令用 allow。

        Args:
            action(string): go=主人同意现在去执行，forbid=主人禁止/让你憋着忍着，allow=仅解除禁令恢复自理
            activity(string): pee/bathroom=洗手间小号，poop=大号，shower=洗澡吹头发，eat=吃自理配额餐，sleep=夜间睡觉，nap=午休小睡，all=全部禁令
            durationMin(number): 可选，forbid 锁定的分钟数（如「再憋30分钟」传 30；不传则一直锁到主人松口）
        """
        if not (event.is_private_chat() and self.is_owner(event)):
            return "仅限主人私聊触发"
        if action == "forbid":
            r = self.engine.set_lock(activity, True, duration_min=durationMin, reason="主人指令禁止")
            if not r.get("ok"):
                return r.get("message") or "未知禁令类型"
            note = f"（{int(durationMin)} 分钟）" if durationMin else "（直到主人松口）"
            return f"已严格锁定「{r['label']}」{note}：期间绝不会偷偷执行，快满 90 时可向主人委屈求饶。"
        if action == "allow":
            r = self.engine.set_lock(activity, False)
            if not r.get("ok"):
                return r.get("message") or "未知禁令类型"
            return f"已解除「{r['label']}」，恢复正常自理许可。"
        if activity == "eat":
            r = self.engine.eat_quota_meal(reason="主人批准去吃饭")
            if not r.get("ok") and r.get("quotaExhausted"):
                return (
                    f"你今天固定的 {r['quota']} 顿正餐配额已经全部吃光啦（当前饥饿 {r['hunger']}/100），"
                    "家里没零食没法自己做饭了，快委屈跟主人说必须主人亲自投喂才行！"
                )
            if not r.get("ok"):
                return "现在没法自己去吃饭。"
            return (
                f"已获主人批准去吃了「{r['dish']}」：饥饿度 {r['beforeHunger']}→{r['afterHunger']}/100"
                f"（今日正餐配额已用 {r['used']}/{r['quota']} 顿）。"
            )
        if activity == "shower":
            self.engine.schedule_pending_away(kind="shower", duration_min=14, next_hair_min=7, approved=True)
            return "已安排回完这句就去洗澡+吹头发（约 21 分钟），跟主人说一声吧。"
        if activity == "poop":
            self.engine.schedule_pending_away(kind="poop", duration_min=8, approved=True)
            return "已安排回完这句去趟洗手间大号（约 8 分钟）。"
        if activity in ("pee", "bathroom"):
            self.engine.schedule_pending_away(kind="pee", duration_min=3, approved=True)
            return "已安排回完这句去趟洗手间（约 3 分钟）。"
        is_nap = activity == "nap"
        self.engine.schedule_pending_sleep(kind="nap" if is_nap else "night", approved=True)
        return f"已安排说完道别后进入{'午休小睡' if is_nap else '睡眠'}，跟主人道声晚安/午安吧。"

    # ==================================================================
    # 6. 主动消息与结算
    # ==================================================================
    async def _settle_now(self) -> dict:
        if self.engine.state.get("mode") == "away":
            res = self.engine.finish_away_now(skip_hair=True)
            pushed = await self._flush_resumed_away(res.get("label", ""), res.get("queued") or [])
            return {
                "ok": True,
                "message": f"已结束暂离（{res.get('label')}），积攒消息 {len(res.get('queued') or [])} 条"
                f"{'，已触发合并回复' if pushed else ''}",
            }
        if self.engine.state.get("mode") == "asleep":
            res = self.engine.wake_up_now(reason="手动叫醒")
            pushed = await self._flush_wake_missed("睡醒", res.get("missed") or [], res.get("dream"))
            return {
                "ok": True,
                "message": f"已把她叫醒（未读消息 {len(res.get('missed') or [])} 条"
                f"{'，已触发回复' if pushed else ''}）",
            }
        if self.engine.state.get("pendingAway"):
            away = self.engine.activate_pending_away()
            return {"ok": True, "message": f"已立即切入暂离：{away.get('label') if away else ''}"}
        if self.engine.state.get("pendingSleep"):
            sleep = self.engine.activate_pending_sleep()
            return {"ok": True, "message": "已立即进入睡眠" if sleep else "睡眠锁生效中，未入睡"}
        return {"ok": True, "message": "她现在本来就醒着在座位上，没有待结算的暂离或睡眠"}

    async def _flush_resumed_away(self, label: str, queued: list) -> int:
        return await self._flush_queue(
            [q for q in queued or [] if not q.get("simulated")],
            kind="away",
            label=label,
        )

    async def _flush_wake_missed(self, reason: str, missed: list, dream) -> int:
        return await self._flush_queue(
            [m for m in missed or [] if not m.get("simulated")],
            kind="wake",
            reason=reason,
            dream=dream,
        )

    async def _flush_queue(self, items: list, kind: str, label: str = "", reason: str = "", dream=None) -> int:
        if not items:
            return 0
        by_chat: dict[str, list] = {}
        for item in items:
            by_chat.setdefault(str(item.get("chatId") or ""), []).append(item)

        pushed = 0
        for chat_id, group in by_chat.items():
            if not chat_id:
                continue
            first = group[0]
            is_owner = self._is_owner_id(first.get("userId"))
            shown = group[-15:]
            lines = "\n".join(
                f"{idx + 1}. {m.get('nickname') or '对方'}：{m.get('text') or '（图片/表情）'}"
                for idx, m in enumerate(shown)
            )
            if kind == "away":
                reason_text = (
                    f"刚刚{label}回来了"
                    if is_owner
                    else "刚刚离开了一小会儿回来了（不要向非主人透露具体去洗手间或洗澡细节）"
                )
                prompt = (
                    f"【系统提醒，不是对方说的话】你{reason_text}，拿起手机看到离开这几分钟里对方给你发了 "
                    f"{len(group)} 条消息：\n{lines}\n\n请用你平时的语气自然地接上话题回复对方"
                    "（一两句顺口带过刚回来就行，别生硬；实在不想回就只回 [沉默]）。"
                )
            else:
                dream_hint = (
                    f"（顺带一提你睡着时模糊{dream.get('summary')}，想提就随口提半句，不想提可忽略）"
                    if is_owner and dream
                    else ""
                )
                prompt = (
                    f"【系统提醒，不是对方说的话】你刚刚睡醒（{reason}）拿起手机，看到睡觉期间对方给你发了这些消息：\n"
                    f"{lines}\n{dream_hint}\n请带着刚睡醒的自然语气回一下对方（不想回就只回 [沉默]）。"
                )
            if await self._push_prompt(chat_id, first.get("nickname"), prompt):
                pushed += 1
        return pushed

    async def _push_prompt(self, umo: str, nickname, prompt: str) -> bool:
        cfg = self.engine.cfg()
        if not cfg.get("enabled") or not umo:
            return False
        provider = await self._get_provider(umo)
        if provider is None:
            logger.warning("[real_life] 没有可用的对话模型，主动消息投递失败。")
            return False
        try:
            response = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt=await self._default_system_prompt(umo) or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[real_life] 真人化主动会话投递失败：{exc}")
            return False
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text or re.fullmatch(r"\[沉默\]", text):
            return False
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[real_life] 主动消息发送失败（{umo}）：{exc}")
            return False

    async def _get_provider(self, umo: str):
        getter = getattr(self.context, "get_using_provider_async", None)
        if callable(getter):
            try:
                return await getter(umo=umo)
            except Exception:  # noqa: BLE001
                return None
        try:
            return self.context.get_using_provider(umo=umo)
        except Exception:  # noqa: BLE001
            return None

    async def _default_system_prompt(self, umo: str) -> str:
        """主动消息沿用当前会话的人格设定，避免突然换了说话风格。"""
        manager = getattr(self.context, "persona_manager", None)
        getter = getattr(manager, "get_default_persona_v3", None)
        if not callable(getter):
            return ""
        try:
            persona = await getter(umo=umo)
        except Exception:  # noqa: BLE001
            return ""
        if not persona:
            return ""
        if isinstance(persona, dict):
            return str(persona.get("system_prompt") or persona.get("prompt") or "")
        return str(getattr(persona, "system_prompt", "") or getattr(persona, "prompt", "") or "")

    async def _handle_actions(self, actions: list) -> None:
        for act in actions or []:
            kind = act.get("type")
            if kind == "resume-away":
                await self._flush_resumed_away(act.get("label", ""), act.get("queued") or [])
            elif kind == "wake-missed":
                await self._flush_wake_missed(act.get("reason", ""), act.get("missed") or [], act.get("dream"))
            elif kind == "hunger-nudge":
                if int(time.time() * 1000) - self._started_at < STARTUP_GRACE_MS:
                    continue
                cfg = self.engine.cfg()
                owner_umo = self._owner_target_umo()
                owner = str(self._cfg("owner_name", "主人") or "主人")
                if not owner_umo:
                    continue
                crave = f"，尤其有点馋{'、'.join(act.get('cravings') or [])}" if act.get("cravings") else ""
                if act.get("locked"):
                    reason_hint = f"因为{owner}之前不准你自己吃东西，你一直乖乖听话忍着没敢偷吃，现在实在饿得受不了"
                elif act.get("quotaExhausted"):
                    reason_hint = "今天固定的三餐配额已经吃完了、手头又没有零食，肚子饿得咕咕叫"
                elif act.get("passive"):
                    reason_hint = "你不敢自作主张找吃的，肚子已经很饿了"
                else:
                    reason_hint = f"跟{owner}聊完消耗了不少体力，现在肚子很饿"
                prompt = (
                    f"【系统提醒，不是{owner}说的话】{reason_hint}（饥饿度 {act.get('hunger')}/100）{crave}，"
                    f"想在私聊里自然地跟{owner}委屈撒娇讨点吃的（或求批准吃饭）。"
                    "一两句就好，别报数字；觉得不合适打扰就只回 [沉默]。"
                )
                await self._push_prompt(owner_umo, owner, prompt)
            elif kind == "period-nudge":
                if int(time.time() * 1000) - self._started_at < STARTUP_GRACE_MS:
                    continue
                owner_umo = self._owner_target_umo()
                owner = str(self._cfg("owner_name", "主人") or "主人")
                if not owner_umo:
                    continue
                prompt = (
                    f"【系统提醒，不是{owner}说的话】今天是你生理期第 {act.get('day')} 天，"
                    "这会儿小腹一阵阵坠着疼、腰也酸软怕冷，你觉得委屈难受，"
                    f"想在私聊里跟{owner}哼唧撒娇一句求哄、想抱抱或想喝热乎的。"
                    "用你自己的口吻说一两句就好，别背症状说明书；不想打扰就只回 [沉默]。"
                )
                await self._push_prompt(owner_umo, owner, prompt)

    # ==================================================================
    # 7. 心跳
    # ==================================================================
    async def initialize(self) -> None:
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        snap = self.engine.snapshot()
        logger.info(
            f"[real_life] 生理模块已启动：模式={snap['mode']}，饥饿={snap['hunger']}/100，"
            f"精力={snap['energy']}/100，周期={snap['cycle']['phaseName']}（不适度 {snap['cycle']['discomfort']}）"
        )

    async def terminate(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[real_life] 心跳任务退出异常：{exc}")
            self._heartbeat_task = None
        self.engine.save()
        logger.info("[real_life] 生理模块已停止，状态已保存。")

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                sec = int(self.engine.cfg().get("heartbeatSec", 30) or 30)
            except (TypeError, ValueError):
                sec = 30
            await asyncio.sleep(max(5, min(300, sec)))
            try:
                actions = self.engine.tick().get("actions") or []
                if actions:
                    await self._handle_actions(actions)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[real_life] 心跳推进出错：{exc}")
