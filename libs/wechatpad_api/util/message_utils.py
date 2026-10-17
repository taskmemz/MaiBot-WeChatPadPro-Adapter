"""适配器复用的工具函数。"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Tuple
from xml.etree import ElementTree as ET


def is_group_message(message: Mapping[str, Any]) -> bool:
    """判断 WeChatPadPro 推送消息是否为群聊消息。

    Args:
        message: WeChatPadPro WebSocket 推送的原始消息字典。

    Returns:
        bool: 发送方以 ``@chatroom`` 结尾则视为群消息。
    """
    from_user_name = message.get("from_user_name")
    if isinstance(from_user_name, Mapping):
        return str(from_user_name.get("str", "")).endswith("@chatroom")
    return False


def extract_content_and_sender(raw_content: str) -> Tuple[str, Optional[str]]:
    """去除群消息 content 前面的发送者 wxid 前缀。

    群消息的 ``content.str`` 形如::

        "wxid_abc123:\\n实际消息内容"

    Args:
        raw_content: 原始 content 字符串。

    Returns:
        Tuple[str, Optional[str]]: (去除前缀后的内容, 发送者 wxid)。
    """
    try:
        regex = re.compile(r"^[a-zA-Z0-9_\-]{5,20}:")
        line_split = raw_content.split("\n")
        if len(line_split) > 0 and regex.match(line_split[0]):
            return "\n".join(line_split[1:]), line_split[0].strip(":")
    except Exception:
        pass
    return raw_content, None


def extract_at_targets(message: Mapping[str, Any]) -> list[str]:
    """从 ``msg_source`` 中解析被 @ 用户的 wxid 列表。

    Args:
        message: WeChatPadPro 推送的原始消息字典。

    Returns:
        list[str]: 被 @ 的 wxid 列表（不含为空的条目）。
    """
    at_targets: list[str] = []
    msg_source = str(message.get("msg_source") or "")
    if not msg_source:
        return at_targets
    try:
        root = ET.fromstring(msg_source)
        at_user_list = root.findtext("atuserlist") or ""
        if at_user_list:
            at_targets = [uid.strip() for uid in at_user_list.split(",") if uid.strip()]
    except Exception:
        pass
    return at_targets


def ats_bot(message: Mapping[str, Any], bot_wxid: str) -> bool:
    """判断当前消息是否 @ 了机器人。

    判定依据（与 LangBot wechatpad 适配器一致）：
    1. ``push_content`` 含“在群聊中@了你”；
    2. ``msg_source`` 的 ``atuserlist`` 含机器人 wxid；
    3. 引用消息（msg_type=49）的 ``refermsg.chatusr`` 等于机器人 wxid。

    Args:
        message: WeChatPadPro 推送的原始消息字典。
        bot_wxid: 机器人自身 wxid。

    Returns:
        bool: 是否 @ 了机器人。
    """
    if not bot_wxid:
        return False
    try:
        push_content = str(message.get("push_content") or "")
        if "在群聊中@了你" in push_content:
            return True

        msg_source = str(message.get("msg_source") or "")
        if msg_source:
            try:
                root = ET.fromstring(msg_source)
                at_user_list = root.findtext("atuserlist") or ""
                if bot_wxid in at_user_list:
                    return True
            except Exception:
                pass

        if str(message.get("msg_type") or 0) == "49":
            content = message.get("content")
            content_str = str(content.get("str", "") if isinstance(content, Mapping) else content or "")
            to_user_name = message.get("to_user_name")
            to_user_name_str = str(to_user_name.get("str", "") if isinstance(to_user_name, Mapping) else "")
            try:
                xml_data = ET.fromstring(content_str)
                appmsg_data = xml_data.find(".//appmsg")
                if appmsg_data is not None:
                    refermsg = appmsg_data.find(".//refermsg")
                    if refermsg is not None:
                        chatusr = refermsg.findtext(".//chatusr")
                        if chatusr and to_user_name_str and chatusr == to_user_name_str:
                            return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def extract_img_attrs(content_str: str) -> Tuple[Optional[str], Optional[str]]:
    """从图片消息 XML 中提取 aeskey 与 cdnthumburl。

    Args:
        content_str: 图片消息的 content（XML 字符串）。

    Returns:
        Tuple[Optional[str], Optional[str]]: (aeskey, cdnthumburl)，解析失败返回 (None, None)。
    """
    try:
        root = ET.fromstring(content_str)
        img_tag = root.find("img")
        if img_tag is not None:
            return img_tag.get("aeskey"), img_tag.get("cdnthumburl")
    except Exception:
        pass
    return None, None


def extract_voice_attrs(content_str: str) -> Tuple[Optional[str], Optional[int]]:
    """从语音消息 XML 中提取 bufid 与 voicelength。

    Args:
        content_str: 语音消息的 content（XML 字符串）。

    Returns:
        Tuple[Optional[str], Optional[int]]: (bufid, voicelength)，解析失败返回 (None, None)。
    """
    try:
        root = ET.fromstring(content_str)
        voicemsg = root.find("voicemsg")
        if voicemsg is not None:
            bufid = voicemsg.get("bufid")
            length = voicemsg.get("voicelength")
            return bufid, int(length) if length is not None else None
    except Exception:
        pass
    return None, None
