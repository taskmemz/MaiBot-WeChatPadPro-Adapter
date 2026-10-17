"""WeChatPadPro 消息网关插件主入口。

职责：
1. 作为 WebSocket 客户端连接 WeChatPadPro 服务端，接收微信消息推送；
2. 将入站消息转换为 Host 侧 ``MessageDict`` 并通过消息网关注入；
3. 将 Host 出站消息转换为 WeChatPadPro HTTP API 调用发送出去；
4. 未登录时自动进入「获取二维码 → 轮询扫码状态」流程，扫码成功自动转入消息接收；
5. 通过 ``@Tool`` 暴露登录状态、二维码、token 刷新等平台能力。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar, Dict, Mapping, Optional

from maibot_sdk import MaiBotPlugin, MessageGateway, PluginConfigBase, Tool

from .config import WeChatPadProPluginSettings
from .constants import GATEWAY_NAME
from .libs.wechatpad_api import client as wpp_client
from .libs.wechatpad_api.inbound_codec import WeChatPadProInboundCodec
from .libs.wechatpad_api.outbound_codec import WeChatPadProOutboundCodec
from .libs.wechatpad_api.transport import WeChatPadProTransport

logger = logging.getLogger("wechatpadpro_adapter.plugin")


class WeChatPadProAdapterPlugin(MaiBotPlugin):
    """WeChatPadPro 微信消息网关插件。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = WeChatPadProPluginSettings

    def __init__(self) -> None:
        """初始化插件实例。"""
        super().__init__()
        self._settings: Optional[WeChatPadProPluginSettings] = None
        self._token: str = ""
        self._bot_wxid: str = ""
        self._bot_nickname: str = ""
        self._inbound_codec: Optional[WeChatPadProInboundCodec] = None
        self._outbound_codec: Optional[WeChatPadProOutboundCodec] = None
        self._transport: Optional[WeChatPadProTransport] = None
        self._login_task: Optional[asyncio.Task] = None
        self._stop_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        """插件加载时按配置启动 WeChatPadPro 连接。"""
        self._stop_event.clear()
        settings = self._load_settings()
        if not settings.plugin.enabled:
            self.ctx.logger.info("WeChatPadPro 适配器未启用，保持空闲")
            return
        await self._start_connection()

    async def on_unload(self) -> None:
        """插件卸载时关闭连接。"""
        await self._stop_connection()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置更新后重启连接。"""
        if scope != "self":
            return
        self.set_plugin_config(config_data)
        if version:
            self.ctx.logger.debug(f"WeChatPadPro 适配器收到配置更新通知: {version}")
        await self._stop_connection()
        settings = self._load_settings()
        if settings.plugin.enabled:
            await self._start_connection()

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def _load_settings(self) -> WeChatPadProPluginSettings:
        """加载并缓存当前插件配置。"""
        raw = self.get_plugin_config_data() or {}
        try:
            self._settings = WeChatPadProPluginSettings(**raw)
        except Exception as exc:
            self.ctx.logger.warning(f"WeChatPadPro 配置解析失败，回退默认值: {exc}")
            self._settings = WeChatPadProPluginSettings()
        return self._settings

    @property
    def _base_url(self) -> str:
        return self._load_settings().wechatpad_server.wechatpad_url.rstrip("/")

    @property
    def _admin_key(self) -> str:
        return self._load_settings().wechatpad_server.admin_key.strip()

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    async def _start_connection(self) -> None:
        """建立与 WeChatPadPro 服务端的连接。"""
        server = self._load_settings().wechatpad_server
        if not server.wechatpad_url:
            raise RuntimeError("WeChatPadPro 服务端 HTTP 地址未配置")
        if not server.wechatpad_ws:
            raise RuntimeError("WeChatPadPro 服务端 WebSocket 地址未配置")

        self.ctx.logger.info("WeChatPadPro 适配器正在连接服务端…")

        # 1. 拿到可用 token
        token = server.token.strip()
        if token and not await self._token_valid(token):
            self.ctx.logger.warning("配置的 token 已失效，尝试用 AdminKey 重新申请")
            token = ""
        if not token:
            if not self._admin_key:
                raise RuntimeError(
                    "无可用 token，且未配置 AdminKey；请在 config.toml 的 "
                    "wechatpad_server.admin_key 或 token 中至少填一个"
                )
            token = await asyncio.to_thread(wpp_client.gen_auth_key, self._admin_key, self._base_url)
            self.ctx.logger.info("已通过 AdminKey 申请到新的调用 token")
        self._token = token

        # 2. 进入登录态检查
        status = await asyncio.to_thread(wpp_client.get_login_status, self._base_url, token)
        if wpp_client.is_token_invalid(status):
            self.ctx.logger.warning("token 未绑定登录链路，自动获取二维码以绑定")
            await self._refresh_token_if_admin_available()
            token = self._token
            status = await asyncio.to_thread(wpp_client.get_login_status, self._base_url, token)

        if wpp_client.is_logged_in(status):
            await self._on_logged_in()
            return

        self.ctx.logger.info("WeChatPadPro 当前未登录，进入扫码登录流程")
        await self._enter_login_flow()

    async def _token_valid(self, token: str) -> bool:
        """用 GetLoginStatus 探测 token 是否仍可用（区分「无效」与「未登录」）。"""
        try:
            result = await asyncio.to_thread(wpp_client.get_login_status, self._base_url, token)
        except Exception:
            return False
        return not wpp_client.is_token_invalid(result)

    async def _refresh_token_if_admin_available(self) -> None:
        """用 AdminKey 重新申请 token 并绑定登录链路。"""
        if not self._admin_key:
            return
        new_token = await asyncio.to_thread(wpp_client.gen_auth_key, self._admin_key, self._base_url)
        self._token = new_token
        await self._fetch_qr()

    async def _enter_login_flow(self) -> None:
        """启动后台任务：获取二维码 → 轮询扫码状态 → 登录成功后转入消息接收。"""
        if self._login_task is not None and not self._login_task.done():
            return
        self._login_task = asyncio.create_task(self._login_flow_loop(), name="wpp-login-flow")

    async def _login_flow_loop(self) -> None:
        """登录流程主循环，直到登录成功或插件停止。"""
        interval = max(5, self._load_settings().wechatpad_server.login_check_interval)
        try:
            await self._fetch_qr()
            while not self._stop_event.is_set():
                await asyncio.sleep(interval)
                if self._stop_event.is_set():
                    return
                try:
                    status = await asyncio.to_thread(
                        wpp_client.check_login_status, self._base_url, self._token
                    )
                except Exception as exc:
                    self.ctx.logger.warning(f"扫码状态轮询失败: {exc}")
                    continue
                if wpp_client.is_token_invalid(status):
                    self.ctx.logger.warning("token 失效，重新申请并重新获取二维码")
                    await self._refresh_token_if_admin_available()
                    continue
                state = wpp_client.scan_state(status)
                if state == 2:
                    self.ctx.logger.info("微信扫码成功，转入消息接收")
                    await self._on_logged_in()
                    return
                if state == 4:
                    self.ctx.logger.info("二维码已过期，重新生成")
                    await self._fetch_qr()
                else:
                    self.ctx.logger.debug(f"扫码状态: {state}")
        except Exception:
            self.ctx.logger.exception("登录流程异常终止")
        finally:
            self._login_task = None

    async def _fetch_qr(self) -> Optional[str]:
        """获取登录二维码并上报网关状态，返回二维码链接。"""
        try:
            result = await asyncio.to_thread(wpp_client.get_login_qr, self._base_url, self._token)
        except Exception as exc:
            self.ctx.logger.error(f"获取登录二维码失败: {exc}")
            return None
        if wpp_client.is_token_invalid(result):
            self.ctx.logger.warning("获取二维码时 token 已失效")
            return None
        data = (result or {}).get("Data") or {}
        if isinstance(data, Mapping):
            self.ctx.logger.info(
                f"WeChatPadPro 登录二维码已生成（有效期 {data.get('expiredTime')} 秒），请用微信扫码登录"
            )
            await self.ctx.gateway.update_state(
                gateway_name=GATEWAY_NAME,
                ready=False,
                platform="wechat",
                account_id="",
                scope=self._load_settings().wechatpad_server.connection_id,
                metadata={"login_qr_link": str(data.get("QrLink") or ""), "state": "waiting_scan"},
            )
            return str(data.get("QrLink") or "")
        return None

    async def _on_logged_in(self) -> None:
        """登录成功后的初始化：拉取资料、启动 WebSocket、上报状态。"""
        profile = await asyncio.to_thread(wpp_client.get_profile, self._base_url, self._token)
        if wpp_client.is_token_invalid(profile):
            raise RuntimeError(f"GetProfile 失败（token 失效）: {profile}")

        user_info = ((profile or {}).get("Data") or {}).get("userInfo") or {}
        self._bot_wxid = self._extract_str_field(user_info.get("userName"))
        self._bot_nickname = self._extract_str_field(user_info.get("nickName"))
        if not self._bot_wxid:
            raise RuntimeError(f"无法从 GetProfile 解析 wxid: {profile}")
        self.ctx.logger.info(f"WeChatPadPro 已登录: wxid={self._bot_wxid} nickname={self._bot_nickname}")

        self._inbound_codec = WeChatPadProInboundCodec(self._base_url, self._token, self._bot_wxid)
        self._outbound_codec = WeChatPadProOutboundCodec(self._base_url, self._token)

        ws_url = self._load_settings().wechatpad_server.wechatpad_ws.rstrip("/")
        self._transport = WeChatPadProTransport(
            ws_url=ws_url,
            token=self._token,
            on_payload=self._on_push_payload,
            on_open=self._on_ws_open,
            on_close=self._on_ws_close,
        )
        self._transport.start(asyncio.get_running_loop())

        await self.ctx.gateway.update_state(
            gateway_name=GATEWAY_NAME,
            ready=True,
            platform="wechat",
            account_id=self._bot_wxid,
            scope=self._load_settings().wechatpad_server.connection_id,
            metadata={"nickname": self._bot_nickname, "state": "connected"},
        )

    async def _stop_connection(self) -> None:
        """断开与 WeChatPadPro 服务端的连接。"""
        self._stop_event.set()
        task = self._login_task
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._login_task = None
        if self._transport is not None:
            self._transport.stop()
            self._transport = None
        self._inbound_codec = None
        self._outbound_codec = None
        self._bot_wxid = ""
        self._bot_nickname = ""
        try:
            await self.ctx.gateway.update_state(
                gateway_name=GATEWAY_NAME,
                ready=False,
                platform="wechat",
                account_id="",
                scope="",
                metadata={"state": "stopped"},
            )
        except Exception:
            pass
        self._stop_event.clear()

    @staticmethod
    def _extract_str_field(value: Any) -> str:
        """从 ``{"str": "..."}`` 或纯标量中提取字符串值。"""
        if isinstance(value, Mapping):
            return str(value.get("str") or "")
        return "" if value is None else str(value)

    # ------------------------------------------------------------------
    # WebSocket 回调
    # ------------------------------------------------------------------

    async def _on_ws_open(self) -> None:
        """WebSocket 连接建立回调。"""
        self.ctx.logger.info("WeChatPadPro 消息推送已就绪")
        await self.ctx.gateway.update_state(
            gateway_name=GATEWAY_NAME,
            ready=True,
            platform="wechat",
            account_id=self._bot_wxid,
            scope=self._load_settings().wechatpad_server.connection_id,
            metadata={"nickname": self._bot_nickname, "state": "connected"},
        )

    async def _on_ws_close(self) -> None:
        """WebSocket 连接关闭回调。"""
        self.ctx.logger.warning("WeChatPadPro 消息推送已断开，等待重连")
        await self.ctx.gateway.update_state(
            gateway_name=GATEWAY_NAME,
            ready=False,
            platform="wechat",
            account_id=self._bot_wxid,
            scope=self._load_settings().wechatpad_server.connection_id,
            metadata={"state": "reconnecting"},
        )

    async def _on_push_payload(self, data: dict) -> None:
        """处理 WebSocket 推送的原始消息。

        WeChatPadPro 推送的单条消息字典可能直接是消息体，也可能被
        ``{"type": ..., "data": {...}}`` 包一层，这里做一次归一化。
        """
        if not isinstance(data, dict):
            return
        payload: Mapping[str, Any] = data.get("data") if isinstance(data.get("data"), Mapping) else data
        codec = self._inbound_codec
        if codec is None:
            return
        try:
            message_dict = await codec.build_message_dict(payload)
        except Exception:
            self.ctx.logger.exception("WeChatPadPro 入站消息转换失败")
            return
        if message_dict is None:
            return

        if not self._is_inbound_allowed(message_dict, self._load_settings()):
            return

        external_message_id = str(message_dict.get("message_id") or "").strip()
        accepted = await self.ctx.gateway.route_message(
            gateway_name=GATEWAY_NAME,
            message=message_dict,
            external_message_id=external_message_id,
            dedupe_key=external_message_id,
        )
        if not accepted:
            self.ctx.logger.debug(
                f"Host 丢弃了 WeChatPadPro 入站消息: {external_message_id or '无消息 ID'}"
            )

    def _is_inbound_allowed(
        self, message_dict: Mapping[str, Any], settings: WeChatPadProPluginSettings
    ) -> bool:
        """按名单过滤入站消息。"""
        message_info = message_dict.get("message_info", {})
        if not isinstance(message_info, Mapping):
            return True
        additional_config = message_info.get("additional_config", {})
        if not isinstance(additional_config, Mapping):
            additional_config = {}
        filters = settings.filters
        group_id = str(additional_config.get("platform_io_target_group_id") or "").strip()
        user_info = message_info.get("user_info")
        user_id = str(user_info.get("user_id", "") if isinstance(user_info, Mapping) else "").strip()

        if group_id:
            if filters.group_whitelist and group_id not in filters.group_whitelist:
                return False
            if group_id in filters.group_blacklist:
                return False
        else:
            if filters.private_whitelist and user_id not in filters.private_whitelist:
                return False
            if user_id in filters.private_blacklist:
                return False
        return True

    # ------------------------------------------------------------------
    # 出站网关
    # ------------------------------------------------------------------

    @MessageGateway(
        name=GATEWAY_NAME,
        route_type="duplex",
        platform="wechat",
        protocol="wechatpadpro",
        description="WeChatPadPro 微信双工消息网关",
    )
    async def handle_wechatpadpro_gateway(
        self,
        message: Dict[str, Any],
        route: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """处理 Host 出站消息并发送到 WeChatPadPro。"""
        del metadata, kwargs

        if self._outbound_codec is None:
            return {"success": False, "error": "WeChatPadPro 适配器尚未登录，暂无法发送消息"}

        try:
            plan = self._outbound_codec.build_send_plan(message, route or {})
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        external_message_id = ""
        errors: list[str] = []
        for step in plan:
            op = step["op"]
            params = step["params"]
            try:
                result = await asyncio.to_thread(
                    self._dispatch_send_op, op, params
                )
            except Exception as exc:
                errors.append(f"{op}: {exc}")
                continue
            if wpp_client.is_token_invalid(result):
                errors.append("token 已失效，请检查配置后重试")
                break
            if int((result or {}).get("Code") or 0) != 200:
                errors.append(f"{op}: {result.get('Text') or result.get('Code')}")
                continue
            external_message_id = self._extract_msg_id(result) or external_message_id

        if errors:
            return {"success": False, "error": "; ".join(errors)}
        return {
            "success": True,
            "external_message_id": external_message_id or None,
            "metadata": {"steps": len(plan)},
        }

    def _dispatch_send_op(self, op: str, params: Mapping[str, Any]) -> Dict[str, Any]:
        """同步分派单条发送操作到对应的 WeChatPadPro 端点。"""
        token = self._token
        if op == "text":
            return wpp_client.send_text_message(
                self._base_url, token, params["to_wxid"], params["content"], params.get("ats") or []
            )
        if op == "image":
            return wpp_client.send_image_message(
                self._base_url, token, params["to_wxid"], params["img_url"], params.get("ats") or []
            )
        if op == "voice":
            return wpp_client.send_voice_message(
                self._base_url,
                token,
                params["to_wxid"],
                params["voice_url"],
                params["voice_format"],
                params["voice_duration"],
            )
        if op == "emoji":
            return wpp_client.send_emoji_message(
                self._base_url, token, params["to_wxid"], params["emoji_md5"], params.get("emoji_size") or 0
            )
        if op == "app":
            return wpp_client.send_app_message(
                self._base_url, token, params["to_wxid"], params["xml_data"], params.get("content_type") or 0
            )
        raise ValueError(f"未知发送操作: {op}")

    @staticmethod
    def _extract_msg_id(result: Mapping[str, Any]) -> str:
        """从发送接口响应中提取消息 ID。"""
        try:
            data = (result or {}).get("Data")
            if isinstance(data, Mapping):
                for key in ("MsgId", "msg_id", "MsgID", "MsgItem"):
                    value = data.get(key)
                    if value:
                        return str(value)
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------
    # 公开 Tool
    # ------------------------------------------------------------------

    @Tool(
        "get_login_status",
        description="获取当前 WeChatPadPro 微信账号的登录状态。",
        visibility="visible",
    )
    async def tool_get_login_status(self, **kwargs: Any) -> Dict[str, Any]:
        """获取登录状态。"""
        del kwargs
        if not self._token:
            return {"success": False, "error": "适配器尚未启动"}
        try:
            result = await asyncio.to_thread(wpp_client.get_login_status, self._base_url, self._token)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "content": result,
            "logged_in": wpp_client.is_logged_in(result),
            "wxid": self._bot_wxid,
        }

    @Tool(
        "get_login_qr",
        description="获取 WeChatPadPro 登录二维码（未登录时使用），返回二维码图片 base64 与链接。",
        visibility="visible",
    )
    async def tool_get_login_qr(self, **kwargs: Any) -> Dict[str, Any]:
        """获取登录二维码。"""
        del kwargs
        if not self._token:
            return {"success": False, "error": "适配器尚未启动"}
        try:
            result = await asyncio.to_thread(wpp_client.get_login_qr, self._base_url, self._token)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        data = (result or {}).get("Data") or {}
        return {
            "success": True,
            "qr_link": str(data.get("QrLink") or ""),
            "qr_code_base64": str(data.get("qrCodeBase64") or ""),
            "expired_time": data.get("expiredTime"),
        }

    @Tool(
        "refresh_token",
        description="使用 AdminKey 重新申请调用 token 并重新绑定登录链路。",
        visibility="visible",
    )
    async def tool_refresh_token(self, **kwargs: Any) -> Dict[str, Any]:
        """重新申请 token。"""
        del kwargs
        if not self._admin_key:
            return {"success": False, "error": "未配置 admin_key"}
        try:
            new_token = await asyncio.to_thread(wpp_client.gen_auth_key, self._admin_key, self._base_url)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        self._token = new_token
        qr_link = await self._fetch_qr()
        return {"success": True, "qr_link": qr_link or ""}


def create_plugin() -> WeChatPadProAdapterPlugin:
    """创建插件实例（Runner 入口）。"""
    return WeChatPadProAdapterPlugin()
