"""WeChatPadPro HTTP API 客户端。

直接与 WeChatPadPro 服务端通信，封装登录、消息发送、群成员查询等接口。
所有方法均为同步阻塞调用（内部使用 requests），调用方应通过 ``asyncio.to_thread``
调度，避免阻塞事件循环。
"""

from __future__ import annotations

from typing import Any

import json as json_module
import logging

import requests

logger = logging.getLogger("wechatpadpro_api.client")

_MAX_RESPONSE_BYTES = 16 * 1024 * 1024  # 16MB 上限，防止异常大响应撑爆内存
_DEFAULT_TIMEOUT = 60


def _read_response_limited(response: requests.Response) -> dict:
    """以流式方式读取响应，并限制最大字节数。"""
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_RESPONSE_BYTES:
                raise RuntimeError("WeChatPadPro response exceeds the runtime limit")
        except (TypeError, ValueError):
            pass
    body = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        body.extend(chunk)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("WeChatPadPro response exceeds the runtime limit")
    result = json_module.loads(body)
    if not isinstance(result, dict):
        raise RuntimeError("WeChatPadPro returned a non-object response")
    return result


def _post_json(base_url: str, token: str, data: dict | None = None) -> dict:
    """向 WeChatPadPro 发送 POST 请求。"""
    url = f"{base_url}?key={token}"
    try:
        with requests.post(
            url,
            json=data,
            headers={"Content-Type": "application/json"},
            timeout=_DEFAULT_TIMEOUT,
            stream=True,
        ) as response:
            response.raise_for_status()
            result = _read_response_limited(response)
        if result:
            return result
        raise RuntimeError("WeChatPadPro returned an empty response")
    except Exception as e:
        raise RuntimeError(f"WeChatPadPro request failed: {base_url}: {e}")


def _get_json(base_url: str, token: str) -> dict:
    """向 WeChatPadPro 发送 GET 请求。"""
    url = f"{base_url}?key={token}"
    try:
        with requests.get(
            url,
            headers={"Content-Type": "application/json"},
            timeout=_DEFAULT_TIMEOUT,
            stream=True,
        ) as response:
            response.raise_for_status()
            result = _read_response_limited(response)
        if result:
            return result
        raise RuntimeError("WeChatPadPro returned an empty response")
    except Exception as e:
        raise RuntimeError(f"WeChatPadPro request failed: {base_url}: {e}")


def is_token_invalid(result: dict) -> bool:
    """判断响应是否表示 token 无效 / 未绑定登录链路。

    服务端返回 ``Code == -2`` 且 Text 含“该链接不存在”时说明 key 已失效。
    """
    try:
        if int((result or {}).get("Code")) != -2:
            return False
    except (TypeError, ValueError):
        return False
    text = str((result or {}).get("Text") or "")
    return "该链接不存在" in text or "链接不存在" in text


def is_logged_in(result: dict) -> bool:
    """根据 GetLoginStatus 响应判断当前是否已登录。"""
    try:
        return int((result or {}).get("Code")) == 200
    except (TypeError, ValueError):
        return False


def scan_state(result: dict) -> int:
    """从 CheckLoginStatus 响应提取扫码状态码。

    返回值：
    - ``0`` 未扫码、``1`` 已扫码待确认、``2`` 已确认登录成功、``4`` 二维码已过期
    - ``-1`` 无有效状态（未绑定链路或响应异常）——调用方应重新获取二维码
    """
    try:
        data = (result or {}).get("Data") or {}
        return int(data.get("state"))
    except (TypeError, ValueError):
        return -1


def extract_auth_key(result: dict) -> str:
    """从 GenAuthKey1 响应中提取第一个 auth key。"""
    data = (result or {}).get("Data")
    if isinstance(data, dict):
        keys = data.get("authKeys") or []
        if keys:
            return str(keys[0])
    if isinstance(data, list) and data:
        return str(data[0])
    return ""


def gen_auth_key(admin_key: str, base_url: str, days: int = 365, count: int = 1) -> str:
    """使用 ADMIN_KEY 申请普通调用 token。

    Args:
        admin_key: 服务端配置的管理密钥。
        base_url: WeChatPadPro HTTP API 根地址。
        days: token 有效天数。
        count: 申请数量。

    Returns:
        str: 申请到的 token。
    """
    url = f"{base_url}/admin/GenAuthKey1"
    result = _post_json(url, admin_key, {"Count": count, "Days": days})
    key = extract_auth_key(result)
    if not key:
        raise RuntimeError(f"GenAuthKey1 未返回可用 key: {result}")
    return key


def get_login_status(base_url: str, token: str) -> dict:
    """获取当前登录状态。

    返回语义：
    - ``Code == 200``：已登录
    - ``Code == 300``：未登录 / 需要重新登录
    - ``Code == -2``（Text 含“该链接不存在”）：token 无效或未绑定登录链路
    """
    return _get_json(f"{base_url}/login/GetLoginStatus", token)


def check_login_status(base_url: str, token: str) -> dict:
    """轮询扫码登录状态。

    ``Data.state`` 含义：0 未扫码、1 已扫码待确认、2 已确认登录成功、
    4 二维码已过期。
    """
    return _get_json(f"{base_url}/login/CheckLoginStatus", token)


def get_login_qr(base_url: str, token: str, proxy: str = "") -> dict:
    """获取登录二维码并绑定登录链路。

    使用 ``GetLoginQrCodeNewX``（绕过验证码版本），调用后 token 与登录链路绑定，
    只有绑定后 ``CheckLoginStatus`` 才会返回有效状态。返回 ``Data`` 含
    ``QrCodeUrl``、``QrLink``、``qrCodeBase64``、``expiredTime``。
    """
    return _post_json(
        f"{base_url}/login/GetLoginQrCodeNewX",
        token,
        {"Proxy": proxy},
    )

def check_can_set_alias(base_url: str, token: str) -> dict:
    """检查当前账号是否处于可登录状态（探测链路是否可用）。"""
    return _get_json(f"{base_url}/login/CheckCanSetAlias", token)


def get_profile(base_url: str, token: str) -> dict:
    """获取当前账号个人资料（含 wxid / 昵称）。"""
    return _get_json(f"{base_url}/user/GetProfile", token)


def get_chatroom_member_detail(base_url: str, token: str, chatroom_name: str) -> dict:
    """获取群成员详情。"""
    return _post_json(
        f"{base_url}/group/GetChatroomMemberDetail",
        token,
        {"ChatRoomName": chatroom_name},
    )


def send_text_message(
    base_url: str,
    token: str,
    to_wxid: str,
    content: str,
    ats: list[str] | None = None,
) -> dict:
    """发送文本消息。"""
    json_data = {
        "MsgItem": [
            {
                "AtWxIDList": ats or [],
                "ImageContent": "",
                "MsgType": 0,
                "TextContent": content,
                "ToUserName": to_wxid,
            }
        ]
    }
    return _post_json(f"{base_url}/message/SendTextMessage", token, json_data)


def send_image_message(
    base_url: str,
    token: str,
    to_wxid: str,
    img_url: str,
    ats: list[str] | None = None,
) -> dict:
    """发送图片消息（指定图片 URL）。"""
    json_data = {
        "MsgItem": [
            {
                "AtWxIDList": ats or [],
                "ImageContent": img_url,
                "MsgType": 0,
                "TextContent": "",
                "ToUserName": to_wxid,
            }
        ]
    }
    return _post_json(f"{base_url}/message/SendImageMessage", token, json_data)


def send_voice_message(
    base_url: str,
    token: str,
    to_wxid: str,
    voice_data: str,
    voice_format: str,
    voice_duration: int,
) -> dict:
    """发送语音消息。"""
    json_data = {
        "ToUserName": to_wxid,
        "VoiceData": voice_data,
        "VoiceFormat": voice_format,
        "VoiceSecond": voice_duration,
    }
    return _post_json(f"{base_url}/message/SendVoice", token, json_data)


def send_emoji_message(base_url: str, token: str, to_wxid: str, emoji_md5: str, emoji_size: int = 0) -> dict:
    """发送 emoji 表情消息。"""
    json_data = {
        "EmojiList": [
            {"EmojiMd5": emoji_md5, "EmojiSize": emoji_size, "ToUserName": to_wxid}
        ]
    }
    return _post_json(f"{base_url}/message/SendEmojiMessage", token, json_data)


def send_app_message(base_url: str, token: str, to_wxid: str, xml_data: str, content_type: int = 0) -> dict:
    """发送 appmsg 消息（引用、链接、小程序等）。"""
    json_data = {
        "AppList": [
            {"ContentType": content_type, "ContentXML": xml_data, "ToUserName": to_wxid}
        ]
    }
    return _post_json(f"{base_url}/message/SendAppMessage", token, json_data)


def cdn_download(base_url: str, token: str, aeskey: str, file_type: int, file_url: str) -> dict:
    """CDN 下载（图片/语音等媒体）。"""
    json_data = {"AesKey": aeskey, "FileType": file_type, "FileURL": file_url}
    return _post_json(f"{base_url}/message/SendCdnDownload", token, json_data)


def get_msg_voice(base_url: str, token: str, buf_id: str, length: int, new_msg_id: str) -> dict:
    """下载语音消息。"""
    json_data = {"Bufid": buf_id, "Length": length, "NewMsgId": new_msg_id, "ToUserName": ""}
    return _post_json(f"{base_url}/message/GetMsgVoice", token, json_data)


def revoke_msg(base_url: str, token: str, to_wxid: str, msg_id: str, new_msg_id: str, create_time: int) -> dict:
    """撤回消息。"""
    param = {
        "ClientMsgId": msg_id,
        "CreateTime": create_time,
        "NewMsgId": new_msg_id,
        "ToUserName": to_wxid,
    }
    return _post_json(f"{base_url}/message/RevokeMsg", token, param)
