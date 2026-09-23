"""私聊对话增强模块（AI 正常回复用户后，概率在短时间内追发一条消息）。

设计参考 Luna-channel/astrbot_plugin_Conversa 的 enhancement 功能，但完全复用
本插件既有的生成与发送链路（_prepare_llm_request / _generate_llm_response /
_send_proactive_message / _finalize_and_reschedule），不引入第二套记忆或发送器。

触发链路：
    被动回复完成（框架 on_llm_response 钩子）
      → 概率掷骰（连续追发按 decay_rate 指数衰减）
      → 随机延迟 min~max 秒
      → 复查：用户在等待期间没来新消息、会话仍启用、不在免打扰时段
      → 用追发提示词模板走一次完整生成（记忆召回、分段、TTS 全部生效）
      → 发送后计入未回复计数并照常重排常规主动消息任务

防自触发：本插件的主动消息发送会通过 _notify_proactive_llm_response 手动发布
一次 on_llm_response 虚拟事件，其 message_str 恒为 PROACTIVE_HOOK_PLACEHOLDER
（"[主动消息]"）。据此区分「真实被动回复」与「插件自己发的主动消息」。
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

# （历史遗留清理：_FOLLOWUP_MAX_LIFETIME 与 _FOLLOWUP_PLACEHOLDERS 全库零引用，
#  已删除。任务超时由自身 finally 兜底；占位符由硬编码元组接管。）

# 单条占位文本注入模板时的最大长度，防止超长历史撑爆提示词
_FOLLOWUP_TEXT_MAX_CHARS = 200


class FollowupMixin:
    """私聊对话增强相关混入类。"""

    # 由 main.__init__ 初始化：会话 -> 待执行的追发任务
    followup_tasks: dict[str, asyncio.Task]

    # ------------------------------------------------------------------ #
    # 配置与状态小工具
    # ------------------------------------------------------------------ #

    def _followup_conf(self) -> dict[str, Any]:
        """读取 followup_settings 配置段，脏类型兜底为空 dict。"""
        raw = (self.config or {}).get("followup_settings", {}) or {}
        return raw if isinstance(raw, dict) else {}

    def _cancel_followup(self, session_id: str) -> bool:
        """取消指定会话待执行的追发任务。返回是否确实取消了任务。"""
        task = self.followup_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    def _cancel_all_followups(self) -> int:
        """插件终止时批量取消全部待发追发任务。返回取消数量。"""
        cancelled = 0
        for session_id in list(self.followup_tasks.keys()):
            if self._cancel_followup(session_id):
                cancelled += 1
        return cancelled

    def _get_followup_chain(self, session_id: str) -> int:
        """读取连续追发计数（用户真实发言时由 message_events 归零）。"""
        try:
            raw = self.session_data.get(session_id, {}).get("followup_chain_count", 0)
            return max(0, int(raw or 0))
        except Exception:
            return 0

    # ------------------------------------------------------------------ #
    # 触发入口（由 main.py 的 on_llm_response 钩子转发）
    # ------------------------------------------------------------------ #

    async def on_llm_response(self, event: AstrMessageEvent, response: Any) -> None:
        """AI 回复后检查是否应触发对话增强追发。任何异常都不影响主流程。"""
        try:
            conf = self._followup_conf()
            if not bool(conf.get("enable", False)):
                return

            umo = event.unified_msg_origin
            # 仅私聊生效
            if "GroupMessage" in umo or "GuildMessage" in umo:
                return

            # 跳过本插件主动消息发送时手动发布的虚拟事件
            if (event.message_str or "") == self.PROACTIVE_HOOK_PLACEHOLDER:
                return

            # 响应必须真的有内容（空响应没有「聊完一轮」的前提）
            reply_text = (getattr(response, "completion_text", "") or "").strip()
            if not reply_text:
                return

            session_id = self._normalize_session_id(umo)

            # 同一会话同时只允许一个待发追发任务
            pending = self.followup_tasks.get(session_id)
            if pending is not None and not pending.done():
                return

            # 会话未启用（或无配置）则不追发
            session_config = self._get_session_config(session_id)
            if not session_config or not session_config.get("enable", False):
                return

            # 概率掷骰：base_prob * decay ** chain，连续追发概率指数衰减
            chain = self._get_followup_chain(session_id)
            base_prob = self._parse_float(conf.get("probability", 20), 20)
            decay_rate = min(1.0, max(0.0, self._parse_float(conf.get("decay_rate", 0.1), 0.1)))
            effective_prob = max(0.0, base_prob * (decay_rate**chain))
            roll = random.random() * 100
            if roll >= effective_prob:
                if logger.isEnabledFor(10):  # DEBUG 级日志惰性化：级别不够时不求值 f-string（含 _get_session_log_str 配置链）
                    logger.debug(
                        f"[对话增强] {self._get_session_log_str(session_id)} 未触发喵 "
                        f"(有效概率={effective_prob:.2f}%，链={chain}，掷点={roll:.2f})"
                    )
                return

            # 随机延迟
            min_delay = int(max(0.0, self._parse_float(conf.get("min_delay_seconds", 30), 30)))
            max_delay = int(min(1800.0, max(0.0, self._parse_float(conf.get("max_delay_seconds", 600), 600))))
            if min_delay > max_delay:
                min_delay = max_delay
            delay = random.randint(min_delay, max_delay) if max_delay > 0 else 0

            self.followup_tasks[session_id] = asyncio.create_task(
                self._delayed_followup(session_id, conf, chain, delay)
            )
            logger.info(
                f"[对话增强] 触发喵 {self._get_session_log_str(session_id)} "
                f"(有效概率={effective_prob:.2f}%，链={chain}，掷点={roll:.2f})，"
                f"{delay} 秒后追发喵。"
            )
        except Exception as e:
            if logger.isEnabledFor(10):  # DEBUG 级日志惰性化：级别不够时不求值 f-string（含 _get_session_log_str 配置链）
                logger.debug(f"[对话增强] 触发检查异常喵（不影响主流程）: {e}")

    # ------------------------------------------------------------------ #
    # 延迟执行与生成
    # ------------------------------------------------------------------ #

    async def _delayed_followup(
        self, session_id: str, conf: dict[str, Any], chain_at_trigger: int, delay: int
    ) -> None:
        """等待随机延迟后复查条件并执行追发。"""
        try:
            trigger_user_ts = self.last_message_times.get(session_id, 0)
            if delay > 0:
                await asyncio.sleep(delay)

            # 等待期间用户发来新消息：用户已接管对话，不必追发
            if self.last_message_times.get(session_id, 0) > trigger_user_ts:
                logger.info(
                    f"[对话增强] {self._get_session_log_str(session_id)} 在等待期间发来新消息，取消本次追发喵。"
                )
                return

            # 会话禁用或免打扰时段（用户长时间没说话可能已进入深夜）
            allowed, reason = await self._is_chat_allowed(session_id)
            if not allowed:
                logger.info(f"[对话增强] {self._get_session_log_str(session_id)} 追发被阻断（{reason}），取消喵。")
                return

            await self._execute_followup(session_id, conf, chain_at_trigger)
        except asyncio.CancelledError:
            # 用户来消息等原因被取消，属正常路径
            raise
        except Exception as e:
            logger.warning(f"[对话增强] 追发任务异常喵: {e}")
        finally:
            self.followup_tasks.pop(session_id, None)

    async def _execute_followup(
        self, session_id: str, conf: dict[str, Any], chain_at_trigger: int
    ) -> None:
        """执行一次追发：生成 → 发送 → 存档 → 更新计数。"""
        session_config = self._get_session_config(session_id)
        if not session_config:
            logger.info(f"[对话增强] {self._get_session_log_str(session_id)} 无有效会话配置，放弃追发喵。")
            return

        # 尊重未回复次数上限：追发也占用「bot 说了话但用户没回」的额度
        async with self.data_lock:
            unanswered_count = int(
                self.session_data.get(session_id, {}).get("unanswered_count", 0) or 0
            )
        max_unanswered = int(
            session_config.get("schedule_settings", {}).get("max_unanswered_times", 3) or 0
        )
        if max_unanswered > 0 and unanswered_count >= max_unanswered:
            logger.info(
                f"[对话增强] {self._get_session_log_str(session_id)} 未回复次数 "
                f"({unanswered_count}) 已达上限 ({max_unanswered})，放弃追发喵。"
            )
            return

        # 复用主链路准备上下文（人格、历史、记忆召回配置）
        request_package = await self._prepare_llm_request(session_id)
        if not request_package:
            logger.info(f"[对话增强] {self._get_session_log_str(session_id)} 上下文准备失败，放弃追发喵。")
            return

        conv_id = request_package["conv_id"]
        history_messages = request_package["history"]
        system_prompt = request_package["system_prompt"]

        user_prompt = self._render_followup_prompt(conf, session_id, history_messages)
        logger.info(f"[对话增强] 开始生成追发消息喵，触发提示词：{user_prompt!r}")

        task_start_user_ts = self.last_message_times.get(session_id, 0)
        response_text, final_user_prompt = await self._generate_llm_response(
            session_id,
            session_config,
            history_messages,
            system_prompt,
            unanswered_count,
            prompt_override=user_prompt,
        )
        if not response_text:
            logger.info("[对话增强] 追发消息生成失败，放弃本次喵。")
            return

        # 生成期间用户新消息：内容已过时，丢弃
        if self.last_message_times.get(session_id, 0) > task_start_user_ts:
            logger.info("[对话增强] 生成期间用户发来新消息，丢弃本次追发喵。")
            return

        # 发送（复用主动消息的 TTS/分段/装饰钩子链路，会自动发布记忆钩子）
        await self._send_proactive_message(session_id, response_text)

        # 存档 + 未回复计数 +1 + 照常重排常规主动消息任务
        await self._finalize_and_reschedule(
            session_id, conv_id, final_user_prompt, response_text, unanswered_count
        )

        # 连续追发链 +1（用户下次真实发言时归零）
        async with self.data_lock:
            self.session_data.setdefault(session_id, {})["followup_chain_count"] = (
                chain_at_trigger + 1
            )
            await self._save_data_internal()

        logger.info(
            f"[对话增强] {self._get_session_log_str(session_id)} 追发完成喵，"
            f"连续追发链 -> {chain_at_trigger + 1}。"
        )

    # ------------------------------------------------------------------ #
    # 提示词模板渲染
    # ------------------------------------------------------------------ #

    def _render_followup_prompt(
        self, conf: dict[str, Any], session_id: str, history_messages: list
    ) -> str:
        """渲染追发提示词模板。占位符用显式替换而非 str.format，
        避免模板里出现未知花括号变量时直接抛 KeyError。"""
        templates = conf.get("prompt_templates") or []
        templates = [t for t in templates if isinstance(t, str) and t.strip()]
        template = random.choice(templates) if templates else (
            "（系统提示：你刚才和用户聊完一轮，过了一会儿还没有新消息。"
            "以当前人格自然地补一句简短的后续消息——可以补充刚才的话题、"
            "随口一问，或分享一个相关的小想法。保持口语化，只说一两句，"
            "不要重复之前说过的内容。这是系统任务，直接输出消息正文即可。）"
        )

        now_str = datetime.now(self.timezone).strftime("%Y年%m月%d日 %H:%M")
        last_user, last_ai = self._extract_followup_history_pair(history_messages)

        # 距离用户上一条消息的时长
        time_since_last_chat = "未知"
        try:
            last_ts = self.session_data.get(session_id, {}).get("last_message_time", 0) or 0
            if last_ts > 0:
                delta = max(0, int(time.time() - last_ts))
                if delta < 60:
                    time_since_last_chat = f"{delta} 秒"
                elif delta < 3600:
                    time_since_last_chat = f"{delta // 60} 分钟"
                else:
                    time_since_last_chat = f"{delta // 3600} 小时"
        except Exception:
            pass

        prompt = template
        for placeholder, value in (
            ("{now}", now_str),
            ("{last_user}", last_user),
            ("{last_ai}", last_ai),
            ("{time_since_last_chat}", time_since_last_chat),
            ("{umo}", session_id),
        ):
            prompt = prompt.replace(placeholder, value)
        return prompt

    def _extract_followup_history_pair(self, history_messages: list) -> tuple[str, str]:
        """从会话历史里取最近一轮 (用户消息, bot 消息) 文本。

        兼容 dict 与对象两种消息形态，content 兼容 str 与分段 list。
        主动消息的存档占位符（PROACTIVE_CONVERSATION_PLACEHOLDER）与
        动机提示词不是真实用户发言，跳过它们。
        """
        last_user = ""
        last_ai = ""

        def _msg_field(msg: Any, name: str) -> str:
            if isinstance(msg, dict):
                return str(msg.get(name, "") or "")
            return str(getattr(msg, name, "") or "")

        def _content_text(content: Any) -> str:
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts: list[str] = []
                for chunk in content:
                    if isinstance(chunk, str):
                        parts.append(chunk)
                    elif isinstance(chunk, dict):
                        parts.append(str(chunk.get("text", "") or ""))
                    else:
                        parts.append(str(getattr(chunk, "text", "") or ""))
                return " ".join(p for p in parts if p).strip()
            return str(content or "").strip()

        for msg in reversed(history_messages or []):
            role = _msg_field(msg, "role").lower()
            text = _content_text(_msg_field(msg, "content"))
            if not text:
                continue
            # 跳过主动消息占位与系统任务样式的假用户消息
            if role == "user" and (
                text.startswith("[主动消息]")
                or text.startswith("[系统任务")
                or text.startswith("[情景分析]")
            ):
                continue
            if not last_ai and role == "assistant":
                last_ai = text[:_FOLLOWUP_TEXT_MAX_CHARS]
            elif not last_user and role == "user":
                last_user = text[:_FOLLOWUP_TEXT_MAX_CHARS]
            if last_user and last_ai:
                break

        return last_user, last_ai
