"""WeChatPadPro 适配器配置模型。"""

from __future__ import annotations

from typing import ClassVar

from maibot_sdk import Field, PluginConfigBase


class WeChatPadProServerConfig(PluginConfigBase):
    """WeChatPadPro 服务端连接配置。"""

    __ui_label__: ClassVar[str] = "服务端连接"
    __ui_order__: ClassVar[int] = 0

    wechatpad_url: str = Field(
        default="http://127.0.0.1:1238",
        description="WeChatPadPro HTTP API 地址（含协议与端口）。",
        json_schema_extra={
            "label": "HTTP API 地址",
            "hint": "形如 http://127.0.0.1:1238 ，若服务部署在其它机器请填写可访问地址。",
        },
    )
    wechatpad_ws: str = Field(
        default="ws://127.0.0.1:1238",
        description="WeChatPadPro WebSocket 推送地址（用于接收消息）。",
        json_schema_extra={
            "label": "WebSocket 地址",
            "hint": "形如 ws://127.0.0.1:1238 ，适配器会自动拼接 /GetSyncMsg?key=<token> 。",
        },
    )
    admin_key: str = Field(
        default="",
        description="WeChatPadPro 管理密钥（ADMIN_KEY），用于自动申请 token 。",
        json_schema_extra={
            "label": "管理密钥 AdminKey",
            "hint": "在服务端 .env 的 ADMIN_KEY 中查看；若直接填写 token 可留空。",
        },
    )
    token: str = Field(
        default="",
        description="WeChatPadPro 普通调用 token（可留空，启动时自动申请）。",
        json_schema_extra={
            "label": "调用 Token",
            "hint": "手动指定的 token 优先于 admin_key 自动申请。",
        },
    )
    connection_id: str = Field(
        default="default",
        description="当前连接标识，用于多实例区分。",
        json_schema_extra={"label": "连接 ID"},
    )
    login_check_interval: int = Field(
        default=30,
        description="未登录时的登录状态轮询间隔（秒）。",
        json_schema_extra={
            "label": "登录轮询间隔（秒）",
            "hint": "未登录时会按此间隔轮询 CheckLoginStatus，扫码成功后自动转入消息接收。",
        },
    )


class WeChatPadProFilterConfig(PluginConfigBase):
    """入站消息过滤配置。"""

    __ui_label__: ClassVar[str] = "消息过滤"
    __ui_order__: ClassVar[int] = 1

    ignore_self_message: bool = Field(
        default=True,
        description="忽略自己发出的消息。",
        json_schema_extra={"label": "忽略自发消息"},
    )
    ignore_official_account: bool = Field(
        default=True,
        description="忽略公众号（gh_ 开头）、微信团队等系统账号消息。",
        json_schema_extra={"label": "忽略公众号消息"},
    )
    # 私聊/群聊名单相对复杂，默认全部放行，后续按需扩展
    private_whitelist: list[str] = Field(
        default_factory=list,
        description="私聊白名单（wxid），为空表示不过滤。",
        json_schema_extra={"label": "私聊白名单"},
    )
    private_blacklist: list[str] = Field(
        default_factory=list,
        description="私聊黑名单（wxid）。",
        json_schema_extra={"label": "私聊黑名单"},
    )
    group_whitelist: list[str] = Field(
        default_factory=list,
        description="群聊白名单（群 ID，形如 xxx@chatroom），为空表示不过滤。",
        json_schema_extra={"label": "群聊白名单"},
    )
    group_blacklist: list[str] = Field(
        default_factory=list,
        description="群聊黑名单（群 ID）。",
        json_schema_extra={"label": "群聊黑名单"},
    )


class WeChatPadProPluginOptions(PluginConfigBase):
    """插件级配置。"""

    __ui_label__: ClassVar[str] = "插件设置"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=False,
        description="是否启用 WeChatPadPro 适配器。",
        json_schema_extra={
            "hint": "关闭后插件保持空闲，不会建立 WebSocket 连接。",
            "label": "启用适配器",
        },
    )
    config_version: str = Field(
        default="1",
        description="当前配置结构版本。",
        json_schema_extra={"disabled": True, "hidden": True, "label": "配置版本", "order": 99},
    )

    def should_connect(self) -> bool:
        """判断当前配置下是否应当启动连接。"""
        return self.enabled


class WeChatPadProPluginSettings(PluginConfigBase):
    """WeChatPadPro 适配器插件配置根模型。"""

    plugin: WeChatPadProPluginOptions = Field(
        default_factory=WeChatPadProPluginOptions,
        description="插件级开关配置。",
    )
    wechatpad_server: WeChatPadProServerConfig = Field(
        default_factory=WeChatPadProServerConfig,
        description="WeChatPadPro 服务端连接配置。",
    )
    filters: WeChatPadProFilterConfig = Field(
        default_factory=WeChatPadProFilterConfig,
        description="入站消息过滤配置。",
    )
