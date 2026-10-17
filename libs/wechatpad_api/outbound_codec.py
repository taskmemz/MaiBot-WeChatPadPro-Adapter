"""WeChatPadPro 出站消息编解码器。

将 Host 出站 ``MessageDict`` 转换为 WeChatPadPro HTTP API 调用。
与 NapCat 适配器不同，WeChatPadPro 没有统一的 ``call_action`` 接口，
出站直接调用具体的 HTTP 端点（由 ``client.py`` 封装）。
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .client import (
    cdn_download,
    send_app_message,
    send_emoji_message,
    send_image_message,
    send_text_message,
    send_voice_message,
)

logger = logging.getLogger("wechatpadpro_adapter.outbound_codec")


class WeChatPadProOutboundCodec:
    """WeChatPadPro 出站消息编码器。"""

    def __init__(self, base_url: str, token: str) -> None:
        """初始化出站消息编码器。

        Args:
            base_url: WeChatPadPro HTTP API 根地址。
            token: WeChatPadPro 调用 token。
        """
        self._base_url = base_url
        self._token = token

    def build_send_plan(
        self,
        message: Mapping[str, Any],
        route: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        """为 Host 出站消息构造发送计划。

        与 NapCat 适配器的 ``build_outbound_action`` 对应，但因
        WeChatPadPro 需要按消息类型分别调用不同端点，这里返回
        ``[{"op": "text"|"image"|"voice"|"emoji"|"app", "params": {...}}, ...]``
        形式的发送步骤列表，由 transport 依次执行。

        Args:
            message: Host 侧标准 ``MessageDict``。
            route: Platform IO 路由信息（预留，目前未使用）。

        Returns:
            List[Dict[str, Any]]: 发送步骤列表。
        """
        del route  # 预留

        message_info = message.get("message_info", {})
        if not isinstance(message_info, Mapping):
            message_info = {}
        group_info = message_info.get("group_info", {})
        if not isinstance(group_info, Mapping):
            group_info = {}
        additional_config = message_info.get("additional_config", {})
        if not isinstance(additional_config, Mapping):
            additional_config = {}

        to_wxid = str(
            group_info.get("group_id") or additional_config.get("platform_io_target_group_id") or ""
        ).strip()
        if not to_wxid:
            to_wxid = str(
                additional_config.get("platform_io_target_user_id")
                or additional_config.get("target_user_id")
                or ""
            ).strip()
        if not to_wxid:
            raise ValueError("WeChatPadPro 出站消息缺少目标 wxid")

        raw_message = message.get("raw_message")
        if not isinstance(raw_message, list):
            raw_message = []

        ats: List[str] = []
        plan: List[Dict[str, Any]] = []
        for item in raw_message:
            if not isinstance(item, Mapping):
                continue
            item_type = str(item.get("type") or "").strip()
            if item_type == "at":
                ats.extend(self._build_at_list(item))
                continue
            if item_type == "text":
                text_value = str(item.get("data") or "")
                if text_value:
                    plan.append({"op": "text", "params": {"content": text_value}})
                continue
            if item_type == "image":
                image_params = self._build_image_params(item)
                if image_params:
                    plan.append({"op": "image", "params": image_params})
                continue
            if item_type == "voice":
                voice_params = self._build_voice_params(item)
                if voice_params:
                    plan.append({"op": "voice", "params": voice_params})
                continue
            if item_type == "emoji":
                emoji_params = self._build_emoji_params(item)
                if emoji_params:
                    plan.append({"op": "emoji", "params": emoji_params})
                continue
            if item_type == "forward":
                forward_params = self._build_forward_params(item)
                if forward_params:
                    plan.append({"op": "app", "params": forward_params})
                continue
            if item_type == "reply":
                continue  # 引用消息单独通过 app 消息实现，暂不自动拼接
            # 未知类型降级为占位文本
            plan.append({"op": "text", "params": {"content": f"[unsupported:{item_type or 'unknown'}]"}})

        if not plan:
            plan.append({"op": "text", "params": {"content": ""}})

        # @ 列表附带给每一条文本步骤
        if ats:
            for step in plan:
                if step["op"] == "text":
                    step["params"]["ats"] = list(ats)

        for step in plan:
            step["params"]["to_wxid"] = to_wxid
        return plan

    @staticmethod
    def _build_at_list(item: Mapping[str, Any]) -> List[str]:
        """从 @ 段提取目标 wxid 列表。"""
        item_data = item.get("data")
        if not isinstance(item_data, Mapping):
            return []
        target_user_id = str(item_data.get("target_user_id") or "").strip()
        if not target_user_id:
            return []
        if target_user_id == "all":
            return ["notify@all"]
        return [target_user_id]

    @staticmethod
    def _build_image_params(item: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """构造图片发送参数。

        优先使用 URL；其次使用 base64（先解码再上传 CDN 由服务端处理，
        但 WeChatPadPro 的 SendImageMessage 只接受 URL，故 base64 场景
        降级为占位提示）。
        """
        item_data = item.get("data")
        data_url = ""
        if isinstance(item_data, Mapping):
            data_url = str(item_data.get("url") or item_data.get("image_url") or "")
        else:
            data_url = str(item_data or "")

        if data_url.startswith("http://") or data_url.startswith("https://"):
            return {"img_url": data_url}

        # data:image/...;base64,xxx 形式：WeChatPadPro 不直接支持，降级
        return None

    @staticmethod
    def _build_voice_params(item: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """构造语音发送参数。"""
        item_data = item.get("data")
        data_url = ""
        if isinstance(item_data, Mapping):
            data_url = str(item_data.get("url") or item_data.get("voice_url") or "")
        else:
            data_url = str(item_data or "")

        if data_url.startswith("http://") or data_url.startswith("https://"):
            return {"voice_url": data_url, "voice_format": "amr", "voice_duration": 0}

        # base64 形式：需要转成可发送的形式，服务端要求 VoiceData 为可下载 URL
        return None

    @staticmethod
    def _build_emoji_params(item: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """构造表情发送参数。"""
        item_data = item.get("data")
        if not isinstance(item_data, Mapping):
            return None
        emoji_md5 = str(item_data.get("emoji_md5") or "").strip()
        if not emoji_md5:
            return None
        return {"emoji_md5": emoji_md5, "emoji_size": int(item_data.get("emoji_size") or 0)}

    @staticmethod
    def _build_forward_params(item: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """构造链接/转发消息发送参数（appmsg）。"""
        item_data = item.get("data")
        if not isinstance(item_data, Mapping):
            return None
        title = str(item_data.get("title") or "")
        url = str(item_data.get("url") or "")
        if not url:
            return None
        description = str(item_data.get("description") or "")
        xml = (
            '<appmsg appid="" sdkver="0">'
            f"<title>{title}</title>"
            f"<des>{description}</des>"
            f'<type>5</type>'
            f"<url>{url}</url>"
            "</appmsg>"
        )
        return {"xml_data": xml, "content_type": 0}
