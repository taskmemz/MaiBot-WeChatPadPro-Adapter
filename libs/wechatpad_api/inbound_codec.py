"""WeChatPadPro 入站消息编解码器。

将 WeChatPadPro WebSocket 推送的消息转换为 Host 侧可接受的 ``MessageDict``。
消息体结构与 LangBot 的 wechatpad 适配器保持一致，但输出目标为
MaiBot SDK 的消息网关格式。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from typing import Any, Dict, List, Mapping, Optional
from uuid import uuid4
from xml.etree import ElementTree as ET

from .util.message_utils import (
    ats_bot,
    extract_at_targets,
    extract_content_and_sender,
    extract_img_attrs,
    extract_voice_attrs,
    is_group_message,
)
from .client import cdn_download, get_msg_voice, get_chatroom_member_detail

logger = logging.getLogger("wechatpadpro_adapter.inbound_codec")

# msg_type -> 处理器名称
_HANDLER_MAP = {
    1: "text",
    3: "image",
    34: "voice",
    49: "compound",
}


class WeChatPadProInboundCodec:
    """WeChatPadPro 入站消息编码器。"""

    def __init__(self, base_url: str, token: str, bot_wxid: str = "") -> None:
        """初始化入站消息编码器。

        Args:
            base_url: WeChatPadPro HTTP API 根地址。
            token: WeChatPadPro 调用 token。
            bot_wxid: 机器人自身 wxid，用于判定 @ 机器人。
        """
        self._base_url = base_url
        self._token = token
        self._bot_wxid = bot_wxid
        # 群成员信息缓存：chatroom_id -> {wxid -> nickname}
        self._member_cache: Dict[str, Dict[str, str]] = {}
        self._member_cache_at: Dict[str, float] = {}
        self._member_cache_ttl = 300.0  # 5 分钟

    def set_bot_wxid(self, bot_wxid: str) -> None:
        """更新机器人 wxid。"""
        self._bot_wxid = bot_wxid

    @property
    def bot_wxid(self) -> str:
        """返回当前机器人 wxid。"""
        return self._bot_wxid

    @staticmethod
    def _build_text_segment(text: str) -> Dict[str, Any]:
        """构造一条纯文本 Host 消息段。"""
        return {"type": "text", "data": text}

    @staticmethod
    def _get_content_str(message: Mapping[str, Any]) -> str:
        """安全提取 ``content.str``。"""
        content = message.get("content")
        if isinstance(content, Mapping):
            return str(content.get("str") or "")
        return str(content or "")

    async def build_message_dict(self, payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """将 WeChatPadPro 推送消息转换为 Host ``MessageDict``。

        Args:
            payload: WeChatPadPro WebSocket 推送的单条消息字典。

        Returns:
            Optional[Dict[str, Any]]: 转换成功返回 ``MessageDict``，否则返回 ``None``。
        """
        from_user_name = payload.get("from_user_name")
        from_user_id = str(from_user_name.get("str", "") if isinstance(from_user_name, Mapping) else "")

        # 基本过滤：公众号 / 系统账号 / 自己发的消息
        if from_user_id.startswith("gh_") or from_user_id in ("weixin", "newsapp"):
            return None
        if self._bot_wxid and from_user_id == self._bot_wxid:
            return None

        is_group = is_group_message(payload)
        content = self._get_content_str(payload)
        content_no_prefix = content
        sender_wxid = ""

        if is_group:
            content_no_prefix, sender_wxid = extract_content_and_sender(content)
            if not sender_wxid:
                sender_wxid = from_user_id

        msg_type = payload.get("msg_type")
        try:
            msg_type = int(msg_type)
        except (TypeError, ValueError):
            msg_type = 0

        handler_name = _HANDLER_MAP.get(msg_type)
        message_segments: List[Dict[str, Any]] = []
        is_at = False

        if is_group:
            is_at = ats_bot(payload, self._bot_wxid)
            if "@所有人" in content_no_prefix:
                message_segments.append({"type": "at", "data": {"target_user_id": "all"}})
            if is_at and self._bot_wxid:
                message_segments.append({"type": "at", "data": {"target_user_id": self._bot_wxid}})
            for target_id in extract_at_targets(payload):
                if target_id != self._bot_wxid:
                    message_segments.append({"type": "at", "data": {"target_user_id": target_id}})

        # 分派到具体的 msg_type 处理器
        if handler_name == "text":
            message_segments.extend(await self._handle_text(payload, content_no_prefix, is_group))
        elif handler_name == "image":
            message_segments.extend(await self._handle_image(payload, content_no_prefix))
        elif handler_name == "voice":
            message_segments.extend(await self._handle_voice(payload, content_no_prefix))
        elif handler_name == "compound":
            message_segments.extend(await self._handle_compound(payload, content_no_prefix))
        else:
            message_segments.append(self._build_text_segment(f"[未知消息类型 msg_type:{msg_type}]"))

        if not message_segments:
            message_segments = [self._build_text_segment("[unsupported]")]

        plain_text = self._build_plain_text(message_segments)
        timestamp_seconds = payload.get("create_time")
        if not isinstance(timestamp_seconds, (int, float)):
            timestamp_seconds = time.time()

        # 构造 user_info / group_info
        user_nickname = sender_wxid or from_user_id
        if is_group:
            user_nickname = await self._resolve_group_member_nickname(from_user_id, sender_wxid) or sender_wxid

        additional_config: Dict[str, Any] = {
            "self_id": self._bot_wxid,
            "wechatpadpro_message_type": "group" if is_group else "private",
        }
        if is_group:
            additional_config["platform_io_target_group_id"] = from_user_id
        else:
            additional_config["platform_io_target_user_id"] = from_user_id

        message_info: Dict[str, Any] = {
            "user_info": {
                "user_id": sender_wxid or from_user_id,
                "user_nickname": user_nickname,
                "user_cardname": None,
            },
            "additional_config": additional_config,
        }
        if is_group:
            message_info["group_info"] = {
                "group_id": from_user_id,
                "group_name": from_user_id,
            }

        message_id = str(payload.get("msg_id") or payload.get("new_msg_id") or f"wechatpad-{uuid4().hex}").strip()
        return {
            "message_id": message_id,
            "timestamp": str(float(timestamp_seconds)),
            "platform": "wechat",
            "message_info": message_info,
            "raw_message": message_segments,
            "is_mentioned": is_at,
            "is_at": is_at,
            "is_emoji": False,
            "is_picture": any(item.get("type") == "image" for item in message_segments),
            "is_command": plain_text.startswith("/"),
            "is_notify": False,
            "session_id": "",
            "processed_plain_text": plain_text,
            "display_message": plain_text,
        }

    def _build_plain_text(self, segments: List[Dict[str, Any]]) -> str:
        """从消息段列表提取纯文本（用于命令判定与展示）。"""
        parts: List[str] = []
        for item in segments:
            item_type = str(item.get("type") or "")
            item_data = item.get("data")
            if item_type == "text":
                parts.append(str(item_data or ""))
            elif item_type == "at" and isinstance(item_data, Mapping):
                at_name = str(item_data.get("target_user_nickname") or item_data.get("target_user_id") or "")
                if at_name:
                    parts.append(f"@{at_name}")
            elif item_type == "reply":
                parts.append("[reply]")
            elif item_type == "forward":
                parts.append("[forward]")
            elif item_type in {"image", "emoji", "voice"}:
                parts.append(f"[{item_type}]")
        return "".join(p for p in parts if p).strip() or "[unsupported]"

    # ------------------------------------------------------------------
    # msg_type 处理器
    # ------------------------------------------------------------------

    async def _handle_text(
        self,
        message: Mapping[str, Any],
        content_no_prefix: str,
        is_group: bool,
    ) -> List[Dict[str, Any]]:
        """处理文本消息 (msg_type=1)。"""
        text = content_no_prefix
        if is_group:
            text = re.sub(r"@\S{1,20}", "", text)
        return [self._build_text_segment(text)]

    async def _handle_image(self, message: Mapping[str, Any], content_no_prefix: str) -> List[Dict[str, Any]]:
        """处理图片消息 (msg_type=3)。"""
        try:
            if not content_no_prefix:
                return [self._build_text_segment("[图片内容为空]")]
            aeskey, cdnthumburl = extract_img_attrs(content_no_prefix)
            if not aeskey or not cdnthumburl:
                return [self._build_text_segment("[图片解析失败]")]

            image_data = await asyncio.to_thread(
                cdn_download, self._base_url, self._token, aeskey, 1, cdnthumburl
            )
            base64_str = str(((image_data or {}).get("Data") or {}).get("FileData") or "")
            if not base64_str:
                image_data = await asyncio.to_thread(
                    cdn_download, self._base_url, self._token, aeskey, 2, cdnthumburl
                )
                base64_str = str(((image_data or {}).get("Data") or {}).get("FileData") or "")
            if not base64_str:
                return [self._build_text_segment("[图片下载失败]")]

            return [
                {
                    "type": "image",
                    "data": f"data:image/png;base64,{base64_str}",
                    "binary_data_base64": base64_str,
                }
            ]
        except Exception as e:
            logger.warning(f"处理图片消息失败: {e}")
            return [self._build_text_segment("[图片处理失败]")]

    async def _handle_voice(self, message: Mapping[str, Any], content_no_prefix: str) -> List[Dict[str, Any]]:
        """处理语音消息 (msg_type=34)。"""
        try:
            if not content_no_prefix:
                return [self._build_text_segment("[语音内容为空]")]
            buf_id, voice_length = extract_voice_attrs(content_no_prefix)
            if not buf_id:
                return [self._build_text_segment("[语音解析失败]")]

            new_msg_id = str(message.get("new_msg_id") or message.get("msg_id") or "")
            voice_data = await asyncio.to_thread(
                get_msg_voice, self._base_url, self._token, str(buf_id), int(voice_length or 0), new_msg_id
            )
            audio_base64 = str(((voice_data or {}).get("Data") or {}).get("Base64") or "")
            if not audio_base64:
                return [self._build_text_segment("[语音内容为空]")]

            return [
                {
                    "type": "voice",
                    "data": f"data:audio/silk;base64,{audio_base64}",
                    "binary_data_base64": audio_base64,
                }
            ]
        except Exception as e:
            logger.warning(f"处理语音消息失败: {e}")
            return [self._build_text_segment("[语音处理失败]")]

    async def _handle_compound(self, message: Mapping[str, Any], content_no_prefix: str) -> List[Dict[str, Any]]:
        """处理复合消息 (msg_type=49)，按 appmsg.type 二次分派。"""
        try:
            if not content_no_prefix:
                return [self._build_text_segment("[复合消息内容为空]")]
            xml_data = ET.fromstring(content_no_prefix)
            appmsg_data = xml_data.find(".//appmsg")
            if appmsg_data is None:
                return [self._build_text_segment(content_no_prefix)]

            data_type = appmsg_data.findtext(".//type", "")
            if data_type == "57":
                return await self._handle_compound_quote(xml_data, appmsg_data)
            if data_type == "5":
                return self._handle_compound_link(appmsg_data)
            if data_type in ("6", "74"):
                return self._handle_compound_unsupported("[文件消息]")
            if data_type in ("33", "36"):
                return self._handle_compound_unsupported("[小程序消息]")
            if data_type == "2000":
                return self._handle_compound_unsupported("[转账消息]")
            if data_type == "2001":
                return self._handle_compound_unsupported("[红包消息]")
            if data_type == "51":
                return self._handle_compound_unsupported("[视频号消息]")
            return self._handle_compound_unsupported(f"[复合消息 type={data_type}]")
        except Exception as e:
            logger.warning(f"解析复合消息失败: {e}")
            return [self._build_text_segment("[复合消息解析失败]")]

    async def _handle_compound_quote(
        self, xml_data: ET.Element, appmsg_data: ET.Element
    ) -> List[Dict[str, Any]]:
        """处理引用消息 (appmsg.type=57)。"""
        segments: List[Dict[str, Any]] = []
        sender_id = xml_data.findtext(".//fromusername") or ""
        user_data = appmsg_data.findtext(".//title") or ""
        refermsg = appmsg_data.find(".//refermsg")
        quote_content = refermsg.findtext(".//content") if refermsg is not None else None

        if user_data:
            user_data = re.sub(r"@\S{1,20}", "", user_data)
            if user_data.strip():
                segments.append(self._build_text_segment(user_data))

        if quote_content:
            segments.append(
                {
                    "type": "reply",
                    "data": {
                        "target_message_id": "",
                        "target_user_id": sender_id,
                        "target_message_content": quote_content,
                    },
                }
            )
        return segments

    def _handle_compound_link(self, appmsg_data: ET.Element) -> List[Dict[str, Any]]:
        """处理链接消息 (appmsg.type=5)。"""
        title = appmsg_data.findtext(".//title") or ""
        description = appmsg_data.findtext(".//des") or ""
        url = appmsg_data.findtext(".//url") or ""
        if not url:
            return [self._build_text_segment(f"[链接消息] {title}")]
        return [
            {
                "type": "forward",
                "data": {
                    "title": title or "[链接]",
                    "description": description,
                    "url": url,
                },
            }
        ]

    def _handle_compound_unsupported(self, text: str) -> List[Dict[str, Any]]:
        """不支持的复合消息统一转化为占位文本。"""
        return [self._build_text_segment(text)]

    # ------------------------------------------------------------------
    # 群成员昵称解析（用于 user_nickname 更可读）
    # ------------------------------------------------------------------

    async def _resolve_group_member_nickname(self, chatroom_id: str, member_wxid: str) -> str:
        """解析群成员昵称，带 5 分钟缓存。"""
        if not chatroom_id or not member_wxid:
            return ""
        now = time.time()
        cached_at = self._member_cache_at.get(chatroom_id, 0)
        members = self._member_cache.get(chatroom_id) if now - cached_at < self._member_cache_ttl else None
        if members is None:
            members = {}
            try:
                result = await asyncio.to_thread(
                    get_chatroom_member_detail, self._base_url, self._token, chatroom_id
                )
                data = ((result or {}).get("Data") or {}).get("member_data") or {}
                member_list = data.get("chatroom_member_list") or []
                for member in member_list:
                    wxid = str(member.get("user_name") or "")
                    nick = str(member.get("nick_name") or member.get("display_name") or "")
                    if wxid:
                        members[wxid] = nick
                self._member_cache[chatroom_id] = members
                self._member_cache_at[chatroom_id] = now
            except Exception as e:
                logger.debug(f"获取群成员详情失败 ({chatroom_id}): {e}")
        return members.get(member_wxid, "")
