"""WeChatPadPro WebSocket 传输层。

连接 WeChatPadPro 的 ``/GetSyncMsg?key=<token>`` WebSocket 端点接收消息推送，
断线自动重连。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Awaitable, Callable, Optional

import websockets

logger = logging.getLogger("wechatpadpro_adapter.transport")

AsyncHook = Callable[[], Awaitable[None]]
PayloadHook = Callable[[dict], Awaitable[None]]

_DEFAULT_HEADERS = {"User-Agent": "MaiBot-WeChatPadPro-Adapter/0.1.0"}


class WeChatPadProTransport:
    """WeChatPadPro WebSocket 客户端。

    在独立守护线程中运行 ``websockets`` 客户端（线程内自建事件循环），
    避免阻塞插件主事件循环；消息通过 ``asyncio.run_coroutine_threadsafe``
    投递回主循环执行。
    """

    def __init__(
        self,
        ws_url: str,
        token: str,
        on_payload: PayloadHook,
        *,
        on_open: Optional[AsyncHook] = None,
        on_close: Optional[AsyncHook] = None,
        reconnect_delay: float = 5.0,
        ping_interval: float = 60.0,
        max_message_chars: int = 4 * 1024 * 1024,
    ) -> None:
        """初始化传输层。

        Args:
            ws_url: WebSocket 根地址（形如 ``ws://127.0.0.1:1238``）。
            token: WeChatPadPro 调用 token。
            on_payload: 收到消息推送时的异步回调（运行在主循环中）。
            on_open: 连接建立后的异步回调。
            on_close: 连接关闭后的异步回调。
            reconnect_delay: 重连等待秒数。
            ping_interval: WebSocket ping 间隔。
            max_message_chars: 单条消息最大字符数，超过则丢弃。
        """
        self._ws_url = ws_url.rstrip("/")
        self._token = token
        self._on_payload = on_payload
        self._on_open = on_open
        self._on_close = on_close
        self._reconnect_delay = reconnect_delay
        self._ping_interval = ping_interval
        self._max_message_chars = max_message_chars

        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected = False

    @property
    def connected(self) -> bool:
        """当前是否已连接。"""
        return self._connected

    def start(self, main_loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """以独立线程启动 WebSocket 连接。

        Args:
            main_loop: 插件主事件循环；不传则取当前运行中的循环。
        """
        if self._ws_thread is not None:
            return
        if main_loop is None:
            main_loop = asyncio.get_running_loop()
        self._main_loop = main_loop
        self._stop_event.clear()
        self._ws_thread = threading.Thread(
            target=self._run_loop,
            name="wechatpadpro-ws-thread",
            daemon=True,
        )
        self._ws_thread.start()

    def stop(self) -> None:
        """停止 WebSocket 连接并等待线程退出。"""
        self._stop_event.set()
        thread = self._ws_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)
        self._ws_thread = None
        self._connected = False
        self._main_loop = None

    def _run_loop(self) -> None:
        """在独立线程中运行的 WebSocket 主循环。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connect_loop())
        except Exception:
            logger.exception("WeChatPadPro WebSocket 线程异常退出")
        finally:
            loop.close()

    async def _connect_loop(self) -> None:
        """带重连的 WebSocket 连接循环。"""
        uri = f"{self._ws_url}/GetSyncMsg?key={self._token}"
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(
                    uri,
                    additional_headers=_DEFAULT_HEADERS,
                    ping_interval=self._ping_interval,
                    ping_timeout=20,
                    open_timeout=20,
                    close_timeout=10,
                    max_size=self._max_message_chars,
                ) as websocket:
                    self._connected = True
                    logger.info(f"WeChatPadPro WebSocket 已连接: {self._ws_url}")
                    if self._on_open is not None:
                        self._schedule_main(self._on_open)
                    try:
                        async for raw in websocket:
                            if self._stop_event.is_set():
                                break
                            self._handle_raw(raw)
                    finally:
                        self._connected = False
                        if self._on_close is not None:
                            self._schedule_main(self._on_close)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"WeChatPadPro WebSocket 连接异常: {str(exc)[:200]}")

            if self._stop_event.is_set():
                break
            # 等待重连间隔，期间可被 stop 唤醒
            self._stop_event.wait(timeout=self._reconnect_delay)

    def _handle_raw(self, raw: Any) -> None:
        """处理单条 WebSocket 消息。"""
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if len(raw) > self._max_message_chars:
                logger.warning("WeChatPadPro WebSocket 消息超过长度限制，已丢弃")
                return
            data = json.loads(raw)
            if not isinstance(data, dict):
                return
            self._schedule_main(self._on_payload, data)
        except json.JSONDecodeError:
            logger.warning("WeChatPadPro 收到非 JSON 消息，已丢弃")
        except Exception:
            logger.exception("WeChatPadPro WebSocket 消息处理异常")

    def _schedule_main(self, coro_factory: Callable[..., Awaitable[Any]], *args: Any) -> None:
        """把回调调度到插件主事件循环执行。"""
        loop = self._main_loop
        if loop is None or loop.is_closed():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(coro_factory(*args), loop)
        except RuntimeError:
            return

        def _done(f: Any) -> None:
            if f.cancelled():
                return
            try:
                f.result()
            except Exception:
                logger.exception("WeChatPadPro 回调执行失败")

        future.add_done_callback(_done)
