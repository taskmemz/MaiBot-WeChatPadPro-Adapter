"""WeChatPadPro 协议包。"""

from .client import (
    cdn_download,
    check_can_set_alias,
    check_login_status,
    gen_auth_key,
    get_chatroom_member_detail,
    get_login_qr,
    get_login_status,
    get_msg_voice,
    get_profile,
    revoke_msg,
    send_app_message,
    send_emoji_message,
    send_image_message,
    send_text_message,
    send_voice_message,
)

__all__ = [
    "cdn_download",
    "check_can_set_alias",
    "check_login_status",
    "gen_auth_key",
    "get_chatroom_member_detail",
    "get_login_qr",
    "get_login_status",
    "get_msg_voice",
    "get_profile",
    "revoke_msg",
    "send_app_message",
    "send_emoji_message",
    "send_image_message",
    "send_text_message",
    "send_voice_message",
]
