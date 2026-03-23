"""
@input: websockets、asyncio、tomllib、WechatAPIClient、PluginBase 与装饰器；base64/xml 解析用于图片/链接文章与引用上下文；OpenClaw 斜杠命令直通与方法速查；微信路由信息到 OpenClaw `to/groupId/accountId/sessionKey` 的结构化透传，以及通过 `channels.status` 发现网关当前真实支持的消息渠道
@output: ClawPlugin，提供 OpenClaw Gateway WebSocket 连接、RPC 调用与文本/AT/图片/语音/视频/文件/引用消息转发；向网关透传会话/发送者标识；`sessionKey` 保持微信桥接所需的 `agent:<agent>:<channel>:direct|group:<peer>` 规范形态，并仅在渠道已被当前 OpenClaw 版本识别时附带 `agent.channel/replyChannel`；握手时默认声明 `tool-events` 能力以接收实时工具事件；支持后台触发不阻塞插件链路、同会话 pending-run 防堆积、WS 事件回推并默认仅终态回写完整回复（可选开启流式增量回写），并用 agent.wait + chat.history 兜底终态拉取；管理员 slash 中“真实网关方法”走 RPC，“OpenClaw 原生命令”走 `agent` 并复用当前 sessionKey；当未配置固定 `to-wxids` 时，仅把带 `runId/sessionKey` 的网关事件回推到对应微信会话；唤醒词帮助在本地列出常用命令与功能；图片/语音/视频/文件消息统一优先走网关原生 WS `attachments`（base64 + mimeType + fileName），不再向网关注入本地路径 `MEDIA:<path>` 指令；显式错误只在可恢复场景自动重试，`chat` 无文本终态优先等待兜底收敛，401/权限/账号停用等硬错误不再回打网关；不自动下载或回传网关附件
@position: plugins/Claw 的核心实现文件，负责消息路由、OpenClaw 会话命名、媒体附件/指令封装、入站媒体落盘与网关通信编排
@auto-doc: Update header and folder INDEX.md when this file changes
"""

import asyncio
import base64
import hashlib
import inspect
import json
import mimetypes
import os
import glob
import platform
import re
import time
import subprocess
import tempfile
import tomllib
import uuid
import urllib.parse
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

import websockets
from loguru import logger

from WechatAPI import WechatAPIClient
from utils.decorators import (
    on_article_message,
    on_at_message,
    on_file_message,
    on_image_message,
    on_quote_message,
    on_text_message,
    on_video_message,
    on_voice_message,
)
from utils.plugin_base import PluginBase


def _safe_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("string", "str", "text"):
            text_value = value.get(key)
            if isinstance(text_value, str):
                return text_value
        return ""
    if value is None:
        return ""
    return str(value)


def _compact_json(payload: Any, limit: int) -> str:
    try:
        content = json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception:
        content = str(payload)
    if len(content) <= limit:
        return content
    return f"{content[:limit]}...(已截断)"


def _dump_json(payload: Any) -> str:
    """面向用户输出：不做截断，交给发送侧分片处理。"""
    try:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception:
        return str(payload)


def _mask_device_signature_payload(payload: Any) -> str:
    text = _safe_text(payload).strip()
    if not text:
        return ""
    parts = text.split("|")
    if len(parts) >= 9 and parts[0] in {"v2", "v3"}:
        parts[7] = "<masked-token>"
        return "|".join(parts)
    return text


def _normalize_gateway_caps(value: Any) -> list[str]:
    caps: list[str] = []
    if isinstance(value, list):
        for item in value:
            text = _safe_text(item).strip()
            if text and text not in caps:
                caps.append(text)
    if "tool-events" not in caps:
        caps.append("tool-events")
    return caps


_OPENCLAW_CHANNEL_ALIASES = {
    "wx-869": "wechat",
    "wx869": "wechat",
    "wechat-869": "wechat",
}

_OPENCLAW_DELIVERABLE_CHANNELS = {
    "telegram",
    "whatsapp",
    "discord",
    "irc",
    "googlechat",
    "slack",
    "signal",
    "imessage",
    "webchat",
}


@dataclass
class PendingRequest:
    future: asyncio.Future
    expect_final: bool
    method: str


@dataclass
class WatchRoute:
    route_id: str
    to_wxid: str
    sender_wxid: str
    sender_name: str
    is_group: bool

    def session_id(self) -> str:
        """用于网关会话命名：私聊用 wxid，群聊用 chatroom id。"""
        return self.to_wxid


@dataclass
class TriggerMatch:
    word: str
    mode: str


class OpenClawGatewayClient:
    def __init__(
        self,
        *,
        ws_url: str,
        token: str,
        password: str,
        device_token: str,
        role: str,
        scopes: list[str],
        caps: list[str],
        command_claims: list[str],
        permissions: dict[str, bool],
        client_id: str,
        client_mode: str,
        client_version: str,
        client_platform: str,
        client_display_name: str,
        connect_timeout_seconds: int,
        request_timeout_seconds: int,
        challenge_timeout_seconds: int,
        auto_reconnect: bool,
        event_callback: Optional[Callable[[dict], Awaitable[None] | None]] = None,
        device_auth_enable: bool = False,
        device_state_dir: str = "",
        client_device_family: str = "",
    ):
        self.ws_url = ws_url
        self.token = token
        self.password = password
        self.device_token = device_token
        self.role = role
        self.scopes = scopes
        self.caps = caps
        self.command_claims = command_claims
        self.permissions = permissions
        self.client_id = client_id
        self.client_mode = client_mode
        self.client_version = client_version
        self.client_platform = client_platform
        self.client_display_name = client_display_name
        self.connect_timeout_seconds = connect_timeout_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.challenge_timeout_seconds = challenge_timeout_seconds
        self.auto_reconnect = auto_reconnect
        self.event_callback = event_callback
        self.device_auth_enable = device_auth_enable
        self.device_state_dir = device_state_dir
        self.client_device_family = self._sanitize_device_family(client_device_family)

        self._protocol_version = 3
        self._ws = None
        self._runner_task: Optional[asyncio.Task] = None
        self._challenge_task: Optional[asyncio.Task] = None
        self._connected_event = asyncio.Event()
        self._pending: Dict[str, PendingRequest] = {}
        self._send_lock = asyncio.Lock()
        self._handshake_lock = asyncio.Lock()

        self._running = False
        self._connect_sent = False
        self._last_error = ""
        self._last_event: Optional[dict] = None
        self._hello_ok: dict = {}
        self._last_challenge_nonce = ""
        self._device_identity: Optional[dict] = None
        self._device_lock = asyncio.Lock()
        self._pairing_pause_until = 0.0
        self._last_connect_error_details: Optional[dict] = None
        self._last_device_auth_debug_log_at = 0.0
        self._device_auth_payload_version = "v3"
        self._default_agent_id = ""
        self._supported_gateway_channels: set[str] = set()

    async def start(self):
        if self._running and self._runner_task and not self._runner_task.done():
            return
        self._running = True
        self._runner_task = asyncio.create_task(self._run(), name="claw-gateway-runner")

    async def _cancel_task_safe(self, task: Optional[asyncio.Task]) -> None:
        if not task or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _shutdown_cleanup(self) -> None:
        self._connected_event.clear()
        await self._close_ws()
        self._fail_all_pending(RuntimeError("OpenClaw 连接已停止"))

    async def stop(self):
        self._running = False

        current_loop = asyncio.get_running_loop()
        runner_task = self._runner_task
        challenge_task = self._challenge_task
        runner_loop = runner_task.get_loop() if runner_task else None
        challenge_loop = challenge_task.get_loop() if challenge_task else None

        # stop() 可能被不同事件循环触发（例如后台消息 loop vs 管理后台 uvicorn loop）。
        # 这里避免跨 loop 直接 await 旧 Task/Future，改为把取消与清理调度回它们所属的 loop。
        if runner_loop and runner_loop is not current_loop:
            try:
                cancel_future = asyncio.run_coroutine_threadsafe(
                    self._cancel_task_safe(runner_task), runner_loop
                )
                await asyncio.wait_for(asyncio.wrap_future(cancel_future), timeout=3)
            except Exception:
                try:
                    runner_loop.call_soon_threadsafe(runner_task.cancel)
                except Exception:
                    pass
        else:
            await self._cancel_task_safe(runner_task)

        if challenge_task:
            if challenge_loop and challenge_loop is not current_loop:
                try:
                    challenge_loop.call_soon_threadsafe(challenge_task.cancel)
                except Exception:
                    pass
            else:
                await self._cancel_task_safe(challenge_task)

        self._runner_task = None
        self._challenge_task = None

        cleanup_loop = runner_loop or current_loop
        if cleanup_loop is current_loop:
            await self._shutdown_cleanup()
            return

        try:
            cleanup_future = asyncio.run_coroutine_threadsafe(self._shutdown_cleanup(), cleanup_loop)
            await asyncio.wait_for(asyncio.wrap_future(cleanup_future), timeout=3)
        except Exception:
            try:
                cleanup_loop.call_soon_threadsafe(lambda: asyncio.create_task(self._shutdown_cleanup()))
            except Exception:
                pass

    async def ensure_connected(self, timeout_seconds: Optional[int] = None):
        if self._connected_event.is_set() and self._is_ws_open():
            return
        await self.start()
        timeout = timeout_seconds or self.connect_timeout_seconds
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            detail = self._last_error or "连接超时"
            raise RuntimeError(f"OpenClaw 连接失败: {detail}") from exc

    async def request(
        self,
        method: str,
        params: Optional[dict] = None,
        *,
        expect_final: bool = False,
        timeout_seconds: Optional[int] = None,
    ) -> Any:
        await self.ensure_connected()
        return await self._request_internal(
            method=method,
            params=params,
            expect_final=expect_final,
            timeout_seconds=timeout_seconds or self.request_timeout_seconds,
        )

    def status_snapshot(self) -> dict:
        methods = self._hello_ok.get("features", {}).get("methods", [])
        events = self._hello_ok.get("features", {}).get("events", [])
        return {
            "connected": self._connected_event.is_set() and self._is_ws_open(),
            "ws_url": self.ws_url,
            "protocol": self._hello_ok.get("protocol"),
            "server": self._hello_ok.get("server", {}),
            "methods_count": len(methods) if isinstance(methods, list) else 0,
            "events_count": len(events) if isinstance(events, list) else 0,
            "last_error": self._last_error,
            "last_event": self._last_event.get("event") if isinstance(self._last_event, dict) else "",
            "has_challenge_nonce": bool(self._last_challenge_nonce),
            "default_agent_id": self._default_agent_id,
            "supported_gateway_channels": sorted(self._supported_gateway_channels),
        }

    def list_methods(self) -> list[str]:
        methods = self._hello_ok.get("features", {}).get("methods", [])
        return methods if isinstance(methods, list) else []

    def list_events(self) -> list[str]:
        events = self._hello_ok.get("features", {}).get("events", [])
        return events if isinstance(events, list) else []

    def default_agent_id(self) -> str:
        return self._default_agent_id

    def supports_message_channel(self, channel: str) -> bool:
        normalized = _safe_text(channel).strip().lower()
        if not normalized:
            return False
        if normalized == "webchat":
            return True
        if self._supported_gateway_channels:
            return normalized in self._supported_gateway_channels
        return normalized in _OPENCLAW_DELIVERABLE_CHANNELS

    async def ensure_default_agent_id(self) -> str:
        if self._default_agent_id:
            return self._default_agent_id
        try:
            payload = await self.request(method="health", params={}, expect_final=False, timeout_seconds=6)
        except Exception:
            return ""
        if isinstance(payload, dict):
            default_agent = _safe_text(payload.get("defaultAgentId")).strip()
            if default_agent:
                self._default_agent_id = default_agent
        return self._default_agent_id

    async def refresh_supported_message_channels(self) -> set[str]:
        if "channels.status" not in self.list_methods():
            return set(self._supported_gateway_channels)
        try:
            payload = await self.request(
                method="channels.status",
                params={"probe": False, "timeoutMs": 2000},
                expect_final=False,
                timeout_seconds=6,
            )
        except Exception:
            return set(self._supported_gateway_channels)

        if not isinstance(payload, dict):
            return set(self._supported_gateway_channels)

        channels = payload.get("channels")
        if not isinstance(channels, dict):
            return set(self._supported_gateway_channels)

        normalized: set[str] = set()
        for key in channels.keys():
            text = _safe_text(key).strip().lower()
            if text:
                normalized.add(text)
        if normalized:
            self._supported_gateway_channels = normalized
        return set(self._supported_gateway_channels)

    def last_event(self) -> Optional[dict]:
        return self._last_event

    async def _run(self):
        backoff_seconds = 1
        while self._running:
            now = time.time()
            if self._pairing_pause_until > now:
                await asyncio.sleep(self._pairing_pause_until - now)
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=6,
                    max_size=25 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    self._connect_sent = False
                    self._connected_event.clear()
                    self._last_error = ""
                    self._challenge_task = asyncio.create_task(
                        self._connect_after_timeout(), name="claw-connect-fallback"
                    )
                    logger.info("[Claw] 已连接 OpenClaw Gateway: {}", self.ws_url)
                    handshake_established = False
                    async for raw_message in ws:
                        await self._handle_raw_message(raw_message)
                        if not handshake_established and self._connected_event.is_set():
                            handshake_established = True
                            backoff_seconds = 1
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._last_error:
                    self._last_error = str(exc)
                logger.warning("[Claw] OpenClaw 连接异常: {}", exc)
            finally:
                self._connected_event.clear()
                if self._challenge_task and not self._challenge_task.done():
                    self._challenge_task.cancel()
                self._challenge_task = None
                self._ws = None
                self._connect_sent = False
                self._fail_all_pending(RuntimeError("OpenClaw 连接已断开"))

            if not self._running or not self.auto_reconnect:
                break
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, 30)

    async def _handle_raw_message(self, raw_message: str):
        if isinstance(raw_message, (bytes, bytearray)):
            raw_message = raw_message.decode("utf-8", errors="ignore")
        try:
            frame = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.debug("[Claw] 忽略非 JSON 帧: {}", raw_message)
            return

        if not isinstance(frame, dict):
            return

        frame_type = frame.get("type")
        if frame_type == "event":
            event_name = _safe_text(frame.get("event"))
            if event_name == "connect.challenge":
                payload = frame.get("payload")
                nonce = ""
                if isinstance(payload, dict):
                    nonce = _safe_text(payload.get("nonce")).strip()
                self._last_challenge_nonce = nonce
                asyncio.create_task(self._send_connect(nonce), name="claw-send-connect")
                return

            self._last_event = frame
            if event_name == "health":
                payload = frame.get("payload")
                if isinstance(payload, dict):
                    default_agent = _safe_text(payload.get("defaultAgentId")).strip()
                    if default_agent:
                        self._default_agent_id = default_agent
            callback = self.event_callback
            if callback:
                try:
                    callback_result = callback(frame)
                    if inspect.isawaitable(callback_result):
                        asyncio.create_task(callback_result)
                except Exception as exc:
                    logger.warning("[Claw] 事件回调失败: {}", exc)
            return

        if frame_type != "res":
            return

        request_id = _safe_text(frame.get("id"))
        if not request_id:
            return

        pending = self._pending.get(request_id)
        if not pending:
            payload = frame.get("payload")
            if isinstance(payload, dict):
                status = _safe_text(payload.get("status")).strip().lower()
                run_id = _safe_text(payload.get("runId") or payload.get("run_id")).strip()
                if status in {"ok", "error"} and run_id:
                    callback = self.event_callback
                    if callback:
                        try:
                            callback_result = callback({"type": "event", "event": "agent", "payload": payload})
                            if inspect.isawaitable(callback_result):
                                asyncio.create_task(callback_result)
                        except Exception as exc:
                            logger.warning("[Claw] 事件回调失败(res->event): {}", exc)
            return

        payload = frame.get("payload")
        if pending.expect_final and isinstance(payload, dict) and payload.get("status") == "accepted":
            return

        self._pending.pop(request_id, None)
        if frame.get("ok") is True:
            if not pending.future.done():
                pending.future.set_result(payload)
            return

        error_message = "unknown error"
        error_detail = frame.get("error")
        if isinstance(error_detail, dict):
            error_message = _safe_text(error_detail.get("message")) or error_message
            details = error_detail.get("details")
            if isinstance(details, dict):
                detail_parts: list[str] = []
                code = details.get("code")
                reason = details.get("reason")
                auth_reason = details.get("authReason")
                payload_version = details.get("payloadVersion")
                payload_v3 = details.get("payloadV3")
                payload_v2 = details.get("payloadV2")
                if code:
                    detail_parts.append(str(code))
                if reason:
                    detail_parts.append(str(reason))
                if auth_reason:
                    detail_parts.append(str(auth_reason))
                if pending.method == "connect" and any(
                    value is not None for value in (payload_version, payload_v3, payload_v2)
                ):
                    sanitized = {
                        "payloadVersion": payload_version,
                        "payloadV3": _mask_device_signature_payload(payload_v3),
                        "payloadV2": _mask_device_signature_payload(payload_v2),
                    }
                    self._last_connect_error_details = sanitized
                    detail_parts.append(f"details={_compact_json(sanitized, 900)}")
                if detail_parts:
                    error_message = f"{error_message} ({', '.join(detail_parts)})"
        if not pending.future.done():
            pending.future.set_exception(RuntimeError(error_message))

    async def _send_connect(self, nonce: str):
        async with self._handshake_lock:
            if self._connect_sent or not self._is_ws_open():
                return
            self._connect_sent = True

            connect_params = {
                "minProtocol": self._protocol_version,
                "maxProtocol": self._protocol_version,
                "client": self._build_client_info(),
                "caps": self.caps,
                "commands": self.command_claims if self.command_claims else None,
                "permissions": self.permissions if self.permissions else None,
                "role": self.role,
                "scopes": self.scopes,
                "auth": self._build_auth(),
            }
            connect_params = {k: v for k, v in connect_params.items() if v is not None}

            if self.device_auth_enable:
                nonce = (nonce or "").strip()
                if not nonce:
                    self._last_error = "缺少 connect.challenge.nonce，无法进行 device-auth"
                    await self._close_ws()
                    return
                device = await self._build_device_identity(connect_params, nonce)
                connect_params["device"] = device

            try:
                payload = await self._request_internal(
                    method="connect",
                    params=connect_params,
                    expect_final=False,
                    timeout_seconds=self.connect_timeout_seconds,
                )
                if not isinstance(payload, dict) or payload.get("type") != "hello-ok":
                    raise RuntimeError(f"connect 返回异常: {payload}")
                self._hello_ok = payload
                self._connected_event.set()
                logger.info("[Claw] OpenClaw 握手成功，协议版本: {}", payload.get("protocol"))
                if "channels.status" in self.list_methods():
                    asyncio.create_task(
                        self.refresh_supported_message_channels(),
                        name="claw-refresh-gateway-channels",
                    )
            except Exception as exc:
                self._last_error = str(exc)
                logger.error("[Claw] OpenClaw 握手失败: {}", exc)
                if self.device_auth_enable and "device signature invalid" in self._last_error:
                    if self._device_auth_payload_version != "v2":
                        self._device_auth_payload_version = "v2"
                        logger.warning("[Claw] 网关拒绝 v3 device-auth 签名，已自动降级为 v2 payload（下一次重连生效）")
                    now = time.time()
                    if now - self._last_device_auth_debug_log_at >= 60:
                        self._last_device_auth_debug_log_at = now
                        identity = self._device_identity if isinstance(self._device_identity, dict) else {}
                        payload = _mask_device_signature_payload(identity.get("debug_payload"))
                        signature = _safe_text(identity.get("debug_signature"))
                        if len(signature) > 18:
                            signature = f"{signature[:18]}...(已截断)"
                        logger.error(
                            "[Claw] device-auth 调试：deviceId={} publicKey={} payload={} signature={}",
                            _safe_text(identity.get("device_id")),
                            _safe_text(identity.get("public_key_b64url")),
                            payload,
                            signature,
                        )
                        details = self._last_connect_error_details
                        if isinstance(details, dict):
                            logger.error("[Claw] gateway 返回：{}", _compact_json(details, 900))
                if self.device_auth_enable and "pairing required" in self._last_error:
                    identity = self._device_identity if isinstance(self._device_identity, dict) else {}
                    device_id = _safe_text(identity.get("device_id"))
                    public_key = _safe_text(identity.get("public_key_b64url"))
                    if device_id:
                        logger.error(
                            "[Claw] OpenClaw 需要在网关侧配对该设备后才能连接：deviceId={} publicKey={}",
                            device_id,
                            public_key,
                        )
                    self._pairing_pause_until = time.time() + 30
                await self._close_ws()

    def _build_client_info(self) -> dict:
        platform_value = self._safe_string(self.client_platform, fallback="linux")
        client = {
            "id": self._safe_string(self.client_id, fallback="gateway-client"),
            "displayName": self.client_display_name or None,
            "version": self._safe_string(self.client_version, fallback="allbot-claw-1.0.0"),
            "platform": platform_value,
            "mode": self._safe_string(self.client_mode, fallback="backend"),
            "deviceFamily": self.client_device_family,
        }
        return {k: v for k, v in client.items() if v is not None}

    def _safe_string(self, value: Any, fallback: str = "") -> str:
        text = _safe_text(value).strip()
        return text or fallback

    def _sanitize_device_family(self, value: Any) -> str:
        text = self._safe_string(value).lower()
        if text:
            return text
        normalized_platform = self._safe_string(self.client_platform).lower()
        if normalized_platform in {"android", "ios", "mobile"}:
            return "mobile"
        return "desktop"

    async def _build_device_identity(self, connect_params: dict, nonce: str) -> dict:
        async with self._device_lock:
            if self._device_identity is None:
                self._device_identity = await asyncio.to_thread(self._load_or_create_device_identity)
            identity = self._device_identity

        scopes = connect_params.get("scopes") if isinstance(connect_params.get("scopes"), list) else []
        scopes_csv = ",".join(str(item) for item in scopes)
        token = ""
        auth = connect_params.get("auth")
        if isinstance(auth, dict):
            token = str(auth.get("token") or auth.get("deviceToken") or "")

        signed_at_ms = int(time.time() * 1000)
        platform_value = self._normalize_ascii_lower(connect_params.get("client", {}).get("platform"))
        device_family_value = self._normalize_ascii_lower(connect_params.get("client", {}).get("deviceFamily"))
        if self._device_auth_payload_version == "v2":
            payload_parts = [
                "v2",
                identity["device_id"],
                str(connect_params.get("client", {}).get("id") or ""),
                str(connect_params.get("client", {}).get("mode") or ""),
                str(connect_params.get("role") or ""),
                scopes_csv,
                str(signed_at_ms),
                token,
                nonce,
            ]
        else:
            payload_parts = [
                "v3",
                identity["device_id"],
                str(connect_params.get("client", {}).get("id") or ""),
                str(connect_params.get("client", {}).get("mode") or ""),
                str(connect_params.get("role") or ""),
                scopes_csv,
                str(signed_at_ms),
                token,
                nonce,
                platform_value,
                device_family_value,
            ]
        payload = "|".join(payload_parts)
        signature = await asyncio.to_thread(self._sign_payload, identity["private_key_pem_path"], payload)
        identity["debug_payload"] = payload
        identity["debug_signature"] = signature
        return {
            "id": identity["device_id"],
            "publicKey": identity["public_key_b64url"],
            "signature": signature,
            "signedAt": signed_at_ms,
            "nonce": nonce,
        }

    def _normalize_ascii_lower(self, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        out = []
        for ch in raw:
            if "A" <= ch <= "Z":
                out.append(chr(ord(ch) + 32))
            else:
                out.append(ch)
        return "".join(out)

    def _load_or_create_device_identity(self) -> dict:
        state_dir = self.device_state_dir or os.path.join(os.path.dirname(__file__), "state")
        os.makedirs(state_dir, exist_ok=True)
        key_path = os.path.join(state_dir, "device_key_ed25519.pem")
        pub_der_path = os.path.join(state_dir, "device_pub_ed25519.der")

        if not os.path.exists(key_path):
            self._run_openssl(["genpkey", "-algorithm", "Ed25519", "-out", key_path])
            try:
                os.chmod(key_path, 0o600)
            except Exception:
                pass
        if not os.path.exists(pub_der_path):
            self._run_openssl(["pkey", "-in", key_path, "-pubout", "-outform", "DER", "-out", pub_der_path])

        pub_der = open(pub_der_path, "rb").read()
        prefix = bytes.fromhex("302a300506032b6570032100")
        if not pub_der.startswith(prefix) or len(pub_der) < len(prefix) + 32:
            raise RuntimeError("device public key der 格式异常，无法提取 Ed25519 raw key")
        pub_raw = pub_der[len(prefix) : len(prefix) + 32]
        device_id = hashlib.sha256(pub_raw).hexdigest()
        public_key_b64url = urlsafe_b64encode(pub_raw).decode().rstrip("=")
        return {
            "device_id": device_id,
            "public_key_b64url": public_key_b64url,
            "private_key_pem_path": key_path,
        }

    def _sign_payload(self, private_key_pem_path: str, payload: str) -> str:
        with tempfile.NamedTemporaryFile("wb", delete=False) as f_in:
            f_in.write(payload.encode("utf-8"))
            in_path = f_in.name
        with tempfile.NamedTemporaryFile("wb", delete=False) as f_out:
            out_path = f_out.name
        try:
            self._run_openssl(
                ["pkeyutl", "-sign", "-inkey", private_key_pem_path, "-rawin", "-in", in_path, "-out", out_path]
            )
            sig = open(out_path, "rb").read()
            return urlsafe_b64encode(sig).decode().rstrip("=")
        finally:
            for p in (in_path, out_path):
                try:
                    os.remove(p)
                except Exception:
                    pass

    def _run_openssl(self, args: list[str]) -> None:
        cmd = ["openssl", *args]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"openssl 失败: {' '.join(cmd)}: {proc.stderr.strip() or proc.stdout.strip()}")

    def _build_auth(self) -> Optional[dict]:
        auth = {
            "token": self.token or None,
            "deviceToken": self.device_token or None,
            "password": self.password or None,
        }
        compact_auth = {key: value for key, value in auth.items() if value}
        return compact_auth or None

    async def _request_internal(
        self,
        *,
        method: str,
        params: Optional[dict],
        expect_final: bool,
        timeout_seconds: int,
    ) -> Any:
        if not self._is_ws_open():
            raise RuntimeError("gateway not connected")

        loop = asyncio.get_running_loop()
        request_id = uuid.uuid4().hex
        response_future = loop.create_future()
        self._pending[request_id] = PendingRequest(
            future=response_future, expect_final=expect_final, method=method
        )

        frame = {
            "type": "req",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            frame["params"] = params

        try:
            await self._send_frame(frame)
            return await asyncio.wait_for(response_future, timeout=timeout_seconds)
        except Exception:
            self._pending.pop(request_id, None)
            raise

    async def _send_frame(self, frame: dict):
        if not self._is_ws_open():
            raise RuntimeError("gateway not connected")
        async with self._send_lock:
            await self._ws.send(json.dumps(frame, ensure_ascii=False))

    async def _connect_after_timeout(self):
        try:
            await asyncio.sleep(self.challenge_timeout_seconds)
            if self._connect_sent or not self._is_ws_open():
                return
            await self._send_connect("")
        except asyncio.CancelledError:
            return

    async def _close_ws(self):
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:
            pass

    def _fail_all_pending(self, error: Exception):
        if not self._pending:
            return
        pending_list = list(self._pending.values())
        self._pending.clear()
        for pending in pending_list:
            if not pending.future.done():
                pending.future.set_exception(error)

    def _is_ws_open(self) -> bool:
        return self._ws is not None and not getattr(self._ws, "closed", False)


class ClawPlugin(PluginBase):
    description = "OpenClaw 网关通信插件"
    author = "allbot"
    version = "1.1.0"
    _DEFERRED_REPLY = "__claw_deferred_reply__"
    _MAX_GATEWAY_MEDIA_ITEMS = 20
    _MAX_EXPLICIT_ERROR_RETRIES = 1

    def __init__(self):
        super().__init__()
        self.bot: Optional[WechatAPIClient] = None
        self._session_routes: Dict[str, WatchRoute] = {}
        self._route_locks: Dict[str, asyncio.Lock] = {}
        self._pending_run_routes: Dict[str, tuple[WatchRoute, float]] = {}
        self._pending_run_meta: Dict[str, dict] = {}
        self._pending_run_texts: Dict[str, str] = {}
        self._pending_run_stream_sent_texts: Dict[str, str] = {}
        self._pending_run_stream_sent_at: Dict[str, float] = {}
        self._pending_run_media_fingerprints: Dict[str, set[str]] = {}
        self._pending_run_finalize_locks: Dict[str, asyncio.Lock] = {}
        self._pending_run_watchdog_task: Optional[asyncio.Task] = None
        self._disable_reason = ""

        config_path = os.path.join(os.path.dirname(__file__), "config.toml")
        with open(config_path, "rb") as f:
            plugin_config = tomllib.load(f).get("Claw", {})

        event_forward_config = plugin_config.get("EventForward", {})
        self.enable = bool(plugin_config.get("enable", False))
        self.max_reply_chars = int(plugin_config.get("max-reply-chars", 1800))
        self.stream_reply_enable = bool(plugin_config.get("stream-reply-enable", False))

        self.default_agent_id = _safe_text(plugin_config.get("default-agent-id")).strip()
        self.auto_trigger_enable = bool(plugin_config.get("auto-trigger-enable", True))
        trigger_words = plugin_config.get("trigger-words", ["龙虾"])
        self.trigger_words = [
            str(item).strip()
            for item in trigger_words
            if str(item).strip()
        ]
        self.trigger_keys = sorted(self.trigger_words, key=len, reverse=True)
        self.trigger_match_mode = _safe_text(plugin_config.get("trigger-match-mode")).strip().lower() or "prefix"
        self.trigger_strip_word = bool(plugin_config.get("trigger-strip-word", True))
        self.trigger_expect_final = bool(plugin_config.get("trigger-expect-final", True))
        self.trigger_timeout_seconds = int(plugin_config.get("trigger-timeout-seconds", 45))
        pending_run_ttl_seconds = plugin_config.get("pending-run-ttl-seconds")
        if pending_run_ttl_seconds is None:
            pending_run_ttl_seconds = max(self.trigger_timeout_seconds * 6, 600)
        self.pending_run_ttl_seconds = max(int(pending_run_ttl_seconds), 60)
        self.pending_run_watchdog_enable = bool(plugin_config.get("pending-run-watchdog-enable", True))
        self.pending_run_watchdog_seconds = max(int(plugin_config.get("pending-run-watchdog-seconds", 60)), 10)
        self.pending_run_watchdog_interval_seconds = max(
            float(plugin_config.get("pending-run-watchdog-interval-seconds", 5.0) or 0.0),
            1.0,
        )
        self.trigger_session_prefix = _safe_text(plugin_config.get("trigger-session-prefix")).strip() or "allbot"
        configured_gateway_channel = _safe_text(
            plugin_config.get("gateway-channel") or plugin_config.get("default-channel")
        ).strip()
        if not configured_gateway_channel:
            configured_gateway_channel = (
                self.trigger_session_prefix
                if self.trigger_session_prefix not in {"allbot", "wechat"}
                else "wx-869"
            )
        self.gateway_channel = configured_gateway_channel
        self.gateway_account_id = _safe_text(
            plugin_config.get("gateway-account-id") or plugin_config.get("default-account-id")
        ).strip()
        self.trigger_use_session_key = bool(plugin_config.get("trigger-use-session-key", True))
        self.trigger_agent_id = _safe_text(plugin_config.get("trigger-agent-id")).strip()
        self.trigger_auto_default_agent = bool(plugin_config.get("trigger-auto-default-agent", True))
        self.trigger_reply_prefix = _safe_text(plugin_config.get("trigger-reply-prefix")).strip()
        self.private_auto_forward_enable = bool(plugin_config.get("private-auto-forward-enable", False))
        self.at_auto_forward_enable = bool(plugin_config.get("at-auto-forward-enable", False))
        self.image_auto_forward_enable = bool(plugin_config.get("image-auto-forward-enable", True))
        self.slash_command_forward_enable = bool(plugin_config.get("slash-command-forward-enable", True))
        self.propagate_to_other_plugins = bool(plugin_config.get("propagate-to-other-plugins", True))
        self.retry_hint_to_gateway_enable = bool(plugin_config.get("retry-hint-to-gateway-enable", True))

        # 去重兜底：框架侧可能对同一 MsgId 触发多次事件回调（例如 @ / 引用消息预检查），
        # 防止重复发起网关请求导致“二次请求/回复不全/多次回写”。
        self.dedup_enable = bool(plugin_config.get("dedup-enable", True))
        self.dedup_window_seconds = float(plugin_config.get("dedup-window-seconds", 3.0) or 0.0)
        self._dedup_seen_at: Dict[str, float] = {}
        self._dedup_last_gc_at = 0.0

        method_help_keywords = plugin_config.get(
            "method-help-keywords",
            ["帮助", "命令", "命令列表", "常用命令"],
        )
        if isinstance(method_help_keywords, str):
            method_help_keywords = [method_help_keywords]
        self.method_help_keywords = sorted(
            {
                _safe_text(item).strip().lower()
                for item in method_help_keywords
                if _safe_text(item).strip()
            }
        )
        self._global_admins = self._load_global_admins()
        logger.info(
            "[Claw] 已加载全局管理员 {} 项: {}",
            len(self._global_admins),
            ",".join(sorted(self._global_admins)) if self._global_admins else "<empty>",
        )
        self.image_forward_mode = _safe_text(plugin_config.get("image-forward-mode")).strip().lower() or "summary"
        self.image_base64_max_chars = int(plugin_config.get("image-base64-max-chars", 12000))
        self.image_path_forward_enable = bool(plugin_config.get("image-path-forward-enable", True))
        self.image_host_path_prefix = _safe_text(plugin_config.get("image-host-path-prefix")).strip()
        self.image_public_base_url = _safe_text(plugin_config.get("image-public-base-url")).strip().rstrip("/")
        self.image_public_route_prefix = _safe_text(plugin_config.get("image-public-route-prefix")).strip() or "/files"
        if not self.image_public_route_prefix.startswith("/"):
            self.image_public_route_prefix = f"/{self.image_public_route_prefix}"
        self.quote_include_enable = bool(plugin_config.get("quote-include-enable", True))
        media_url_bases = plugin_config.get("media-url-bases", [])
        if isinstance(media_url_bases, str):
            media_url_bases = [media_url_bases]
        self.media_url_bases = [
            str(item).strip()
            for item in media_url_bases
            if str(item).strip()
        ]
        media_local_dirs = plugin_config.get("media-local-dirs", [])
        if isinstance(media_local_dirs, str):
            media_local_dirs = [media_local_dirs]
        self.media_local_dirs = [
            str(item).strip()
            for item in media_local_dirs
            if str(item).strip()
        ]

        ws_url = _safe_text(plugin_config.get("ws-url")).strip()
        gateway_token = _safe_text(plugin_config.get("gateway-token")).strip()
        gateway_password = _safe_text(plugin_config.get("gateway-password")).strip()
        device_token = _safe_text(plugin_config.get("device-token")).strip()
        role = _safe_text(plugin_config.get("role")).strip() or "operator"
        scopes = plugin_config.get("scopes", ["operator.admin"])
        caps = _normalize_gateway_caps(plugin_config.get("caps", []))
        command_claims = plugin_config.get("commands-claims", [])
        permissions = plugin_config.get("permissions", {})

        connect_timeout_seconds = int(plugin_config.get("connect-timeout-seconds", 12))
        request_timeout_seconds = int(plugin_config.get("request-timeout-seconds", 20))
        challenge_timeout_seconds = int(plugin_config.get("challenge-timeout-seconds", 2))
        auto_reconnect = bool(plugin_config.get("auto-reconnect", True))
        self.auto_connect = bool(plugin_config.get("auto-connect", False))

        client_id = _safe_text(plugin_config.get("client-id")).strip() or "gateway-client"
        client_mode = _safe_text(plugin_config.get("client-mode")).strip() or "backend"
        client_version = _safe_text(plugin_config.get("client-version")).strip() or "allbot-claw-1.0.0"
        client_platform = _safe_text(plugin_config.get("client-platform")).strip() or platform.system().lower()
        client_display_name = _safe_text(plugin_config.get("client-display-name")).strip() or "AllBot Claw"
        client_device_family = _safe_text(plugin_config.get("client-device-family")).strip()

        device_auth_enable = bool(plugin_config.get("device-auth-enable", True))
        device_state_dir = _safe_text(plugin_config.get("device-state-dir")).strip()

        if self.enable and not ws_url:
            self._disable_reason = "OpenClaw 网关配置为空：ws-url 未配置"
            self.enable = False
            self.auto_connect = False
            logger.warning("[Claw] {}", self._disable_reason)

        self.event_forward_enable = bool(event_forward_config.get("enable", False))
        self.event_forward_allowed = set(
            str(item).strip()
            for item in event_forward_config.get("allowed-events", [])
            if str(item).strip()
        )
        self.event_forward_to_wxids = [
            str(item).strip()
            for item in event_forward_config.get("to-wxids", [])
            if str(item).strip()
        ]
        self.event_mention_in_group = bool(event_forward_config.get("mention-in-group", True))

        self.gateway = OpenClawGatewayClient(
            ws_url=ws_url,
            token=gateway_token,
            password=gateway_password,
            device_token=device_token,
            role=role,
            scopes=[str(item) for item in scopes] if isinstance(scopes, list) else ["operator.admin"],
            caps=caps,
            command_claims=[str(item) for item in command_claims] if isinstance(command_claims, list) else [],
            permissions=permissions if isinstance(permissions, dict) else {},
            client_id=client_id,
            client_mode=client_mode,
            client_version=client_version,
            client_platform=client_platform,
            client_display_name=client_display_name,
            connect_timeout_seconds=connect_timeout_seconds,
            request_timeout_seconds=request_timeout_seconds,
            challenge_timeout_seconds=challenge_timeout_seconds,
            auto_reconnect=auto_reconnect,
            event_callback=self._on_gateway_event,
            device_auth_enable=device_auth_enable,
            device_state_dir=device_state_dir,
            client_device_family=client_device_family,
        )

    async def on_enable(self, bot=None):
        await super().on_enable(bot)
        self.bot = bot
        if self.enable and self.auto_connect:
            await self.gateway.start()
        if self.enable and self.pending_run_watchdog_enable and self._pending_run_watchdog_task is None:
            self._pending_run_watchdog_task = asyncio.create_task(
                self._pending_run_watchdog_loop(), name="claw-pending-run-watchdog"
            )

    async def on_disable(self):
        task = self._pending_run_watchdog_task
        self._pending_run_watchdog_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.gateway.stop()
        await super().on_disable()

    def _iter_main_config_candidates(self) -> list[str]:
        roots = [
            os.getcwd(),
            "/app",
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
        ]
        candidates: list[str] = []
        for root in roots:
            root_text = _safe_text(root).strip()
            if not root_text:
                continue
            candidate = os.path.join(root_text, "main_config.toml")
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def _load_global_admins(self) -> set[str]:
        """读取全局管理员列表（main_config.toml 的 [XYBot].admins 或顶层 admins）。"""
        for candidate in self._iter_main_config_candidates():
            if not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "rb") as f:
                    cfg = tomllib.load(f)
            except Exception:
                continue

            admins = None
            if isinstance(cfg.get("XYBot"), dict):
                admins = cfg["XYBot"].get("admins")
            if admins is None:
                admins = cfg.get("admins")
            if isinstance(admins, list):
                return {str(item).strip() for item in admins if str(item).strip()}
        return set()

    def _match_admin(self, sender_wxid: str, admin_set: set[str]) -> bool:
        if not sender_wxid or not admin_set:
            return False
        if sender_wxid in admin_set:
            return True
        sender_lower = sender_wxid.lower()
        return any(sender_lower == item.lower() for item in admin_set)

    def _refresh_global_admins(self) -> set[str]:
        latest_admins = self._load_global_admins()
        if latest_admins != self._global_admins:
            logger.info(
                "[Claw] 全局管理员列表已刷新: {} -> {}",
                ",".join(sorted(self._global_admins)) if self._global_admins else "<empty>",
                ",".join(sorted(latest_admins)) if latest_admins else "<empty>",
            )
            self._global_admins = latest_admins
        return self._global_admins

    def _is_global_admin(self, message: dict) -> bool:
        sender_wxid = _safe_text(message.get("SenderWxid")).strip()
        if not sender_wxid:
            return False
        if self._match_admin(sender_wxid, self._global_admins):
            return True
        latest_admins = self._refresh_global_admins()
        return self._match_admin(sender_wxid, latest_admins)

    async def async_init(self):
        return

    def _dedup_key(self, event_name: str, message: dict) -> str:
        msg_id = _safe_text(message.get("MsgId")).strip()
        if msg_id:
            return f"{event_name}:{msg_id}"
        route_id = _safe_text(message.get("FromWxid")).strip()
        sender = _safe_text(message.get("SenderWxid")).strip()
        content = _safe_text(message.get("Content")).strip()
        created = _safe_text(message.get("Createtime")).strip()
        digest = hashlib.sha1(f"{route_id}|{sender}|{created}|{content}".encode("utf-8")).hexdigest()[:16]
        return f"{event_name}:h:{digest}"

    def _should_skip_duplicate(self, event_name: str, message: dict) -> bool:
        if not self.dedup_enable:
            return False
        window = float(self.dedup_window_seconds or 0.0)
        if window <= 0:
            return False

        now = time.time()
        if now - float(self._dedup_last_gc_at or 0.0) > max(10.0, window * 4):
            deadline = now - max(30.0, window * 6)
            expired = [key for key, seen_at in self._dedup_seen_at.items() if seen_at <= deadline]
            for key in expired:
                self._dedup_seen_at.pop(key, None)
            self._dedup_last_gc_at = now

        key = self._dedup_key(event_name, message)
        seen_at = self._dedup_seen_at.get(key)
        if seen_at is not None and (now - seen_at) <= window:
            logger.debug("[Claw] 去重命中，跳过重复事件 key={}", key)
            return True
        self._dedup_seen_at[key] = now
        return False

    @on_text_message(priority=45)
    async def handle_text(self, bot: WechatAPIClient, message: dict):
        if self._should_skip_duplicate("text_message", message):
            return bool(self.propagate_to_other_plugins)
        if await self._maybe_handle_slash_command(bot, message, strip_at_prefix=False):
            return bool(self.propagate_to_other_plugins)
        return await self._handle_trigger(bot, message)

    @on_at_message(priority=45)
    async def handle_at(self, bot: WechatAPIClient, message: dict):
        if self._should_skip_duplicate("at_message", message):
            return bool(self.propagate_to_other_plugins)
        if await self._maybe_handle_slash_command(bot, message, strip_at_prefix=True):
            return bool(self.propagate_to_other_plugins)
        return await self._handle_trigger(
            bot,
            message,
            bypass_trigger=self.at_auto_forward_enable,
            strip_at_prefix=True,
        )

    @on_quote_message(priority=45)
    async def handle_quote(self, bot: WechatAPIClient, message: dict):
        if self._should_skip_duplicate("quote_message", message):
            return bool(self.propagate_to_other_plugins)
        if await self._maybe_handle_slash_command(bot, message, strip_at_prefix=bool(message.get("Ats"))):
            return bool(self.propagate_to_other_plugins)
        is_at_current_bot = self._is_at_current_bot(message, bot=bot)
        return await self._handle_trigger(
            bot,
            message,
            bypass_trigger=is_at_current_bot,
            strip_at_prefix=bool(message.get("Ats")),
        )

    @on_image_message(priority=45)
    async def handle_image(self, bot: WechatAPIClient, message: dict):
        route = self._build_route(message)
        if route and route.is_group:
            return True
        return await self._handle_trigger(bot, message, bypass_trigger=self.image_auto_forward_enable)

    @on_voice_message(priority=45)
    async def handle_voice(self, bot: WechatAPIClient, message: dict):
        route = self._build_route(message)
        if route and route.is_group:
            return True
        return await self._handle_trigger(bot, message, bypass_trigger=self.image_auto_forward_enable)

    @on_video_message(priority=45)
    async def handle_video(self, bot: WechatAPIClient, message: dict):
        route = self._build_route(message)
        if route and route.is_group:
            return True
        return await self._handle_trigger(bot, message, bypass_trigger=self.image_auto_forward_enable)

    @on_file_message(priority=45)
    async def handle_file(self, bot: WechatAPIClient, message: dict):
        route = self._build_route(message)
        if route and route.is_group:
            return True
        return await self._handle_trigger(bot, message, bypass_trigger=self.image_auto_forward_enable)

    @on_article_message(priority=45)
    async def handle_article(self, bot: WechatAPIClient, message: dict):
        route = self._build_route(message)
        if route and route.is_group:
            return True
        return await self._handle_trigger(bot, message, bypass_trigger=True)

    async def _handle_trigger(
        self,
        bot: WechatAPIClient,
        message: dict,
        *,
        bypass_trigger: bool = False,
        strip_at_prefix: bool = False,
        allow_private_auto_forward: bool = True,
    ):
        if not self.enable or not self.auto_trigger_enable:
            return True

        route = self._build_route(message)
        if not route:
            return True

        user_text = self._extract_user_text(message, strip_at_prefix=strip_at_prefix)
        if route.is_group and self._looks_like_group_slash_text(user_text):
            return True
        match = self._match_trigger(user_text) if user_text else None
        should_bypass = bool(bypass_trigger) or (
            allow_private_auto_forward
            and not route.is_group
            and self.private_auto_forward_enable
        )
        if not match and not should_bypass:
            return True

        self._cleanup_pending_run_routes()
        existing_run_id = self._find_pending_run_id_for_route(route)
        if existing_run_id:
            logger.warning(
                "[Claw] 当前会话已有 pending run，跳过本次触发 route_id={} run_id={}",
                route.route_id,
                existing_run_id,
            )
            return bool(self.propagate_to_other_plugins)

        self._create_task_safe(
            self._trigger_forward_in_background(
                bot,
                message,
                route,
                user_text,
                match_word=(match.word if match else ""),
            ),
            name=f"claw-trigger:{route.route_id}:{_safe_text(message.get('MsgId')).strip() or uuid.uuid4().hex[:8]}",
        )
        return bool(self.propagate_to_other_plugins)

    def _find_pending_run_id_for_route(self, route: WatchRoute) -> str:
        if not self._pending_run_routes or not route:
            return ""
        for run_id, (pending_route, _expires_at) in self._pending_run_routes.items():
            if pending_route.route_id == route.route_id:
                return run_id
        return ""

    def _remember_session_route(self, session_key: str, route: Optional[WatchRoute]) -> None:
        key = _safe_text(session_key).strip()
        if not key or not route or not route.to_wxid:
            return
        self._session_routes[key] = route

    def _strip_trigger_prompt(self, user_text: str, match_word: str) -> str:
        text = _safe_text(user_text).strip()
        trigger = _safe_text(match_word).strip()
        if not text:
            return ""
        if self.trigger_strip_word and trigger and text.startswith(trigger):
            stripped = text[len(trigger) :].strip()
            if stripped:
                return stripped
        return text

    def _extract_group_slash_text(self, bot: WechatAPIClient, message: dict, user_text: str) -> str:
        if not self._is_at_current_bot(message, bot=bot):
            return ""
        match = self._match_trigger(user_text)
        if not match:
            return ""
        stripped = self._strip_trigger_prompt(user_text, match.word).strip()
        if not stripped.startswith("/"):
            return ""
        return stripped

    def _looks_like_group_slash_text(self, user_text: str) -> bool:
        text = _safe_text(user_text).strip()
        if not text:
            return False
        if text.startswith("/"):
            return True
        match = self._match_trigger(text)
        if not match:
            return False
        return self._strip_trigger_prompt(text, match.word).strip().startswith("/")

    def _create_task_safe(self, coro: Awaitable[Any], *, name: str) -> None:
        task = asyncio.create_task(coro, name=name)

        def _done(t: asyncio.Task):
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                return
            except Exception as inner_exc:
                logger.warning("[Claw] 后台任务异常(name={}): {}", name, inner_exc)
                return
            if exc:
                logger.warning("[Claw] 后台任务失败(name={}): {}", name, exc)

        task.add_done_callback(_done)

    async def _trigger_forward_in_background(
        self,
        bot: WechatAPIClient,
        message: dict,
        route: WatchRoute,
        user_text: str,
        *,
        match_word: str,
    ) -> None:
        lock = self._route_locks.get(route.route_id)
        if lock is None:
            lock = asyncio.Lock()
            self._route_locks[route.route_id] = lock

        async with lock:
            if int(message.get("MsgType") or 0) == 3 and self.image_forward_mode == "base64":
                await self._ensure_image_base64(bot, message)
            attachments = self._build_gateway_attachments(message)
            if not attachments:
                await self._ensure_media_local_path(message)
                attachments = self._build_gateway_attachments(message)

            prompt_text = self._strip_trigger_prompt(user_text, match_word)
            if match_word and (not prompt_text or self._is_method_help_query(prompt_text)):
                await self._send_to_route(route, self._format_method_help())
                return

            prompt = self._build_openclaw_prompt(
                message,
                user_text=prompt_text,
                use_gateway_attachments=bool(attachments),
            )
            if not prompt:
                return

            try:
                reply_text = await self._forward_to_openclaw(prompt, route, attachments=attachments)
            except Exception as exc:
                if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                    logger.warning("[Claw] OpenClaw 请求超时")
                    await self._send_to_route(route, "OpenClaw gateway timeout, please retry.")
                else:
                    logger.exception("[Claw] 触发词转发失败")
                    await self._send_to_route(route, f"OpenClaw 调用失败: {exc}")
                return

            if reply_text == self._DEFERRED_REPLY:
                return

            if self.trigger_reply_prefix:
                reply_text = f"{self.trigger_reply_prefix}{reply_text}"

            await self._send_openclaw_reply(route, reply_text)

    async def _maybe_handle_slash_command(self, bot: WechatAPIClient, message: dict, *, strip_at_prefix: bool) -> bool:
        if not self.enable or not self.slash_command_forward_enable:
            return False
        if not self._is_global_admin(message):
            return False

        route = self._build_route(message)
        if not route:
            return False

        user_text = self._extract_user_text(message, strip_at_prefix=strip_at_prefix)
        slash_text = user_text
        if route.is_group:
            slash_text = self._extract_group_slash_text(bot, message, user_text)
            if not slash_text:
                return False
        elif not slash_text.startswith("/"):
            return False

        method, params, expect_final = self._parse_openclaw_slash_command(slash_text)
        method = self._normalize_gateway_method_name(method)
        if not method:
            return False

        if not self._is_openclaw_slash_command(slash_text, message):
            return False

        self._create_task_safe(
            self._execute_slash_command_in_background(
                bot,
                message,
                method,
                params,
                expect_final,
                slash_text,
            ),
            name=f"claw-slash:{method}:{_safe_text(message.get('MsgId')).strip() or uuid.uuid4().hex[:8]}",
        )
        return True

    def _normalize_gateway_method_name(self, method: str) -> str:
        """将用户输入的 method 规范化为网关声明的 canonical 名称（大小写/别名对齐）。"""
        raw = _safe_text(method).strip()
        if not raw:
            return ""
        methods = [str(item).strip() for item in (self.gateway.list_methods() or []) if str(item).strip()]
        if not methods:
            return raw
        canonical = {name.lower(): name for name in methods}
        return canonical.get(raw.lower(), raw)

    def _is_openclaw_slash_command(self, user_text: str, message: dict) -> bool:
        if not self.enable or not self.slash_command_forward_enable:
            return False
        if not self._is_global_admin(message):
            return False
        text = _safe_text(user_text).strip()
        if not text.startswith("/"):
            return False

        first, _, _rest = text[1:].partition(" ")
        method = first.strip()
        if not method:
            return False

        methods = [str(item).strip() for item in (self.gateway.list_methods() or []) if str(item).strip()]
        if methods:
            canonical = {name.lower(): name for name in methods}
            if method.lower() in canonical:
                return True
            if method.lower() in {"health", "help", "new", "reset"}:
                return True
            # 只要是管理员“/命令”且不是本插件命令，都优先按 OpenClaw slash 处理，
            # 避免被当作普通文本转发到 agent。
            return True

        # hello-ok 未包含 methods（或尚未拉取到方法列表）时，为避免把 “/xxx” 当普通文本转发，
        # 这里放宽判定：只要是管理员的 “/命令”，就尝试走 OpenClaw slash 直通。
        return True

    def _parse_openclaw_slash_command(self, raw: str) -> tuple[str, Optional[dict], bool]:
        text = _safe_text(raw).strip()
        if not text.startswith("/"):
            return "", None, False

        first, _, rest = text[1:].partition(" ")
        method = first.strip()
        if not method:
            return "", None, False

        rest = rest.strip()
        if not rest:
            return method, None, True

        if rest.startswith("{") or rest.startswith("["):
            try:
                parsed = json.loads(rest)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                return method, parsed, True
            return method, None, True

        if method == "agent":
            return method, {"message": rest, "deliver": False}, True

        return method, {"message": rest}, True

    def _slash_uses_gateway_rpc(self, method: str) -> bool:
        """仅真实存在的网关方法走原始 RPC，其余 slash 视为 OpenClaw 原生命令。"""
        raw = _safe_text(method).strip()
        if not raw:
            return False

        methods = [str(item).strip() for item in (self.gateway.list_methods() or []) if str(item).strip()]
        if not methods:
            return True

        canonical = {name.lower(): name for name in methods}
        return raw.lower() in canonical

    async def _execute_slash_command_in_background(
        self,
        bot: WechatAPIClient,
        message: dict,
        method: str,
        params: Optional[dict],
        expect_final: bool,
        raw_text: str,
    ) -> None:
        route = self._build_route(message)
        if not route:
            return

        if not self.gateway.status_snapshot().get("connected"):
            await self._reply(bot, message, "OpenClaw 未连接，请检查 Claw 网关配置和连接状态。")
            return

        if not self._slash_uses_gateway_rpc(method):
            logger.info(
                "[Claw] 执行 native slash via agent method={} sender={} group={}",
                method,
                _safe_text(message.get("SenderWxid")).strip() or "-",
                bool(message.get("IsGroup")),
            )
            try:
                reply_text = await self._forward_to_openclaw(raw_text, route)
            except Exception as exc:
                logger.warning("[Claw] native slash 失败 method={} error={}", method, exc)
                await self._reply(bot, message, f"OpenClaw 命令执行失败: {exc}")
                return

            if reply_text and reply_text != self._DEFERRED_REPLY:
                await self._send_openclaw_reply(route, reply_text)
            return

        logger.info(
            "[Claw] 执行 slash 命令 method={} expect_final={} sender={} group={}",
            method,
            expect_final,
            _safe_text(message.get("SenderWxid")).strip() or "-",
            bool(message.get("IsGroup")),
        )

        try:
            payload = await self.gateway.request(
                method=method,
                params=params,
                expect_final=expect_final,
                timeout_seconds=max(6, min(self.trigger_timeout_seconds, 20)),
            )
        except Exception as exc:
            logger.warning("[Claw] slash 命令失败 method={} error={}", method, exc)
            error_text = str(exc)
            await self._reply(bot, message, f"OpenClaw 命令执行失败: {error_text}")
            return

        logger.info("[Claw] slash 命令完成 method={}", method)
        await self._reply(bot, message, f"/{method} 返回：\n{_dump_json(payload)}")

    async def _send_openclaw_reply(self, route: WatchRoute, reply_text: str) -> None:
        """发送 OpenClaw 回复到微信：仅转发文本，不自动下载网关媒体/附件。"""
        text = _safe_text(reply_text).strip()
        if text:
            await self._send_to_route(route, text)

    def _normalize_media_ref(self, ref: str) -> str:
        text = _safe_text(ref).strip()
        if not text:
            return ""

        text = text.strip("`'\"<>[](){}")
        if text.lower().startswith("file://"):
            text = text[7:].strip()
        if not text:
            return ""

        text = text.split()[0].strip()
        text = text.rstrip("，。！？；：,.;!?)】》\"'")
        if text.startswith("./"):
            text = text[2:]
        return text.strip()

    def _is_probable_media_ref(self, ref: str) -> bool:
        text = self._normalize_media_ref(ref)
        if not text or text in {"/", ".", ".."}:
            return False
        if text.startswith("http://") or text.startswith("https://"):
            return self._looks_like_remote_url(text)
        if re.match(r"^[A-Za-z]:[\\/]", text):
            return bool(os.path.basename(text.rstrip("/\\")))
        if text.startswith("/"):
            base_name = os.path.basename(text.rstrip("/\\"))
            return bool(base_name)
        return bool(text)

    def _extract_media_directives(self, content: str) -> tuple[str, list[str]]:
        lines = content.splitlines()
        media: list[str] = []
        kept: list[str] = []
        media_inline = re.compile(r"(?i)(?<![A-Za-z0-9_])MEDIA\s*:\s*([^\s\"'<>]+)")
        md_image_angle = re.compile(r"!\[[^\]]*\]\(<([^>]+)>\)")
        md_image_plain = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
        for line in lines:
            candidate = line
            for match in md_image_angle.finditer(line):
                ref = self._normalize_media_ref(match.group(1))
                if self._is_probable_media_ref(ref):
                    media.append(ref)
            candidate = md_image_angle.sub("", candidate)

            for match in md_image_plain.finditer(candidate):
                ref = self._normalize_media_ref(match.group(1))
                if self._is_probable_media_ref(ref):
                    media.append(ref)
            candidate = md_image_plain.sub("", candidate)

            def _replace_media_inline(match: re.Match) -> str:
                ref = self._normalize_media_ref(match.group(1))
                if self._is_probable_media_ref(ref):
                    media.append(ref)
                    return ""
                return match.group(0)

            candidate = media_inline.sub(_replace_media_inline, candidate)

            candidate = re.sub(r"\s{2,}", " ", candidate).strip()
            if candidate:
                kept.append(candidate)
        return "\n".join(kept).strip(), media

    def _build_media_rel_candidates(self, ref: str) -> list[str]:
        """把 MEDIA 引用归一化为可下载的相对路径候选列表（按优先级排序）。"""
        text = _safe_text(ref).strip()
        if not text:
            return []
        text = text.split("?", 1)[0].split("#", 1)[0].strip()
        if not text:
            return []

        normalized = text.replace("\\", "/").rstrip("/")
        candidates: list[str] = []

        base_name = os.path.basename(normalized).strip()
        if base_name:
            candidates.append(base_name)

        lower = normalized.lower()
        marker = "/.openclaw/media/"
        marker_index = lower.find(marker)
        if marker_index != -1:
            tail = normalized[marker_index + len(marker) :].lstrip("/").strip()
            if tail:
                candidates.append(tail)
                parts = [p for p in tail.split("/") if p]
                if len(parts) >= 2:
                    candidates.append("/".join(parts[-2:]))

        seen: set[str] = set()
        filtered: list[str] = []
        for item in candidates:
            item = item.strip().lstrip("/")
            if not item:
                continue
            if ".." in item or "\\" in item:
                continue
            if item in seen:
                continue
            seen.add(item)
            filtered.append(item)
        return filtered

    async def _send_media_ref_to_route(self, route: WatchRoute, ref: str) -> bool:
        """处理 MEDIA:/xxx 或 MEDIA:https://...；下载并按类型回写微信。"""
        ref = ref.strip()
        if not ref:
            return False

        if ref.startswith("http://") or ref.startswith("https://"):
            return await self._send_media_url_to_route(route, ref, ref)

        rel_candidates = self._build_media_rel_candidates(ref)
        if not rel_candidates:
            return False

        for safe_rel in rel_candidates:
            for base_dir in self.media_local_dirs:
                candidate = os.path.join(base_dir, safe_rel)
                if os.path.exists(candidate) and os.path.isfile(candidate):
                    try:
                        source = {
                            "media_type": self._infer_media_type(
                                node_type="",
                                parent_key="path",
                                mime_type="",
                                value_hint=candidate,
                                file_name=os.path.basename(candidate),
                            ),
                            "transport": "path",
                            "value": candidate,
                            "file_name": os.path.basename(candidate),
                            "mime_type": mimetypes.guess_type(candidate)[0] or "",
                        }
                        media_bytes = await self._resolve_openclaw_media_bytes(source)
                        if media_bytes:
                            await self._send_gateway_media_to_wechat(route, source, media_bytes)
                            return True
                    except Exception as exc:
                        logger.warning("[Claw] MEDIA 本地读取失败(path={}): {}", candidate, exc)

            for base_url in self.media_url_bases:
                base_url = base_url.rstrip("/") + "/"
                full_url = base_url + safe_rel
                if await self._send_media_url_to_route(route, full_url, ref):
                    return True

        return False

    async def _send_media_url_to_route(self, route: WatchRoute, url: str, ref: str) -> bool:
        url = url.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return False
        if re.match(r"^https?://$", url):
            return False

        file_name = os.path.basename(urllib.parse.urlparse(url).path) or self._file_name_from_source({"value": ref})
        guessed_mime = mimetypes.guess_type(file_name)[0] or ""
        media_type = self._infer_media_type(
            node_type="",
            parent_key="url",
            mime_type=guessed_mime,
            value_hint=url,
            file_name=file_name,
        )
        source = {
            "media_type": media_type,
            "transport": "url",
            "value": url,
            "file_name": file_name,
            "mime_type": guessed_mime,
        }

        try:
            media_bytes = await self._resolve_openclaw_media_bytes(source)
        except Exception as exc:
            logger.warning("[Claw] MEDIA 下载失败(url={}): {}", url, exc)
            return False

        if not media_bytes:
            return False
        await self._send_gateway_media_to_wechat(route, source, media_bytes)
        return True

    def _is_at_current_bot(self, message: dict, *, bot: Optional[WechatAPIClient] = None) -> bool:
        ats = message.get("Ats")
        if not isinstance(ats, list) or not ats:
            return False
        bot_wxid = _safe_text(
            getattr(bot, "wxid", None) or getattr(self.bot, "wxid", None)
        ).strip()
        if not bot_wxid:
            return False
        return bot_wxid in ats

    async def _forward_to_openclaw(
        self,
        prompt: str,
        route: WatchRoute,
        *,
        attachments: Optional[list[dict[str, Any]]] = None,
    ) -> str:
        session_key = ""
        idempotency_key = uuid.uuid4().hex
        params = {
            "message": prompt,
            "deliver": False,
            "idempotencyKey": idempotency_key,
        }
        if attachments:
            params["attachments"] = attachments
        params.update(self._build_openclaw_agent_context(route))
        agent_id = self._resolve_agent_id()
        if not agent_id and self.trigger_auto_default_agent:
            agent_id = await self.gateway.ensure_default_agent_id()
        if agent_id:
            params["agentId"] = agent_id

        session_key = self._resolve_session_key(route, agent_id=agent_id)
        if session_key:
            params["sessionKey"] = session_key
            self._remember_session_route(session_key, route)

        expect_final = bool(self.trigger_expect_final)
        timeout_seconds = max(6, int(self.trigger_timeout_seconds))

        try:
            payload = await self.gateway.request(
                method="agent",
                params=params,
                expect_final=expect_final,
                timeout_seconds=timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[Claw] agent RPC 超时，尝试幂等重试 idempotencyKey={}",
                idempotency_key[:10],
            )
            payload = await self.gateway.request(
                method="agent",
                params=params,
                expect_final=expect_final,
                timeout_seconds=timeout_seconds,
            )

        run_id = self._extract_openclaw_run_id(payload)
        if expect_final and self._is_accepted_payload(payload) and run_id:
            self._bind_pending_run(
                run_id,
                route,
                session_key=session_key,
                request_params=params,
                retry_count=0,
            )
            logger.info(
                "[Claw] OpenClaw 返回 accepted(run_id={})，继续等待 final/事件回推 sessionKey={}",
                run_id,
                session_key or "-",
            )
            return self._DEFERRED_REPLY

        reply_text = self._extract_openclaw_reply_text(payload)
        if reply_text:
            return reply_text

        if run_id:
            self._bind_pending_run(
                run_id,
                route,
                session_key=session_key,
                request_params=params,
                retry_count=0,
            )
            logger.info(
                "[Claw] OpenClaw 已受理 run_id={} sessionKey={}（等待 WS 实时事件回推）",
                run_id,
                session_key or "-",
            )

            return self._DEFERRED_REPLY

        logger.warning("[Claw] OpenClaw 返回 payload 不含 runId，无法绑定实时回推: {}", _compact_json(payload, 800))
        return "(OpenClaw 已受理，但未返回 runId)"

    def _is_accepted_payload(self, payload: Any) -> bool:
        seen: set[int] = set()

        def walk(node: Any, depth: int = 0) -> bool:
            if depth > 6:
                return False
            if isinstance(node, dict):
                node_id = id(node)
                if node_id in seen:
                    return False
                seen.add(node_id)
                for key in ("status", "state"):
                    value = _safe_text(node.get(key)).strip().lower()
                    if value == "accepted":
                        return True
                for value in node.values():
                    if walk(value, depth + 1):
                        return True
                return False
            if isinstance(node, list):
                for item in node[:40]:
                    if walk(item, depth + 1):
                        return True
                return False
            return False

        return walk(payload)

    def _build_session_key(self, route: WatchRoute, *, agent_id: str = "") -> str:
        agent_part = _safe_text(agent_id).strip() or "main"
        channel = self._resolve_session_channel(route)
        peer_kind = "group" if route and route.is_group else "direct"
        scope = self._build_session_scope(route)
        return f"agent:{agent_part}:{channel}:{peer_kind}:{scope}"

    def _build_session_scope(self, route: WatchRoute) -> str:
        raw_scope = ""
        if route:
            if route.is_group:
                raw_scope = route.to_wxid
            else:
                raw_scope = route.sender_wxid or route.to_wxid
        scope = _safe_text(raw_scope).strip()
        if not scope:
            return "unknown"

        normalized: list[str] = []
        for char in scope:
            if char.isalnum() or char in {"@", "_", "-", ".", ":"}:
                normalized.append(char)
            else:
                normalized.append("_")
        sanitized = "".join(normalized).strip("._:-") or "unknown"
        if len(sanitized) <= 96:
            return sanitized

        digest = hashlib.sha1(scope.encode("utf-8")).hexdigest()[:12]
        return f"{sanitized[:64]}:{digest}"

    def _resolve_openclaw_channel(self, route: Optional[WatchRoute] = None) -> str:
        channel = _safe_text(self.gateway_channel).strip()
        if channel:
            normalized = channel.lower()
            return _OPENCLAW_CHANNEL_ALIASES.get(normalized, normalized)
        prefix = _safe_text(self.trigger_session_prefix).strip()
        if prefix:
            normalized = prefix.lower()
            return _OPENCLAW_CHANNEL_ALIASES.get(normalized, normalized)
        return "wechat"

    def _resolve_session_channel(self, route: Optional[WatchRoute] = None) -> str:
        channel = _safe_text(self.gateway_channel).strip()
        if channel:
            return channel.lower()
        prefix = _safe_text(self.trigger_session_prefix).strip()
        if prefix:
            return prefix.lower()
        return "wx-869"

    def _build_openclaw_agent_context(self, route: Optional[WatchRoute]) -> dict[str, Any]:
        if not route:
            return {}

        channel = self._resolve_openclaw_channel(route)
        params: dict[str, Any] = {}
        if self.gateway.supports_message_channel(channel):
            params["channel"] = channel
            params["replyChannel"] = channel
        if self.gateway_account_id:
            params["accountId"] = self.gateway_account_id
            params["replyAccountId"] = self.gateway_account_id

        if route.is_group:
            sender_target = _safe_text(route.sender_wxid).strip()
            if sender_target:
                params["to"] = sender_target
            else:
                params["to"] = route.to_wxid
            params["groupId"] = route.to_wxid
        else:
            params["to"] = route.to_wxid

        return params

    def _resolve_session_key(self, route: WatchRoute, *, agent_id: str) -> str:
        """获取当前 route 的 sessionKey。"""
        if not self.trigger_use_session_key:
            return ""
        return self._build_session_key(route, agent_id=agent_id)

    def _clone_json_payload(self, payload: Any) -> Any:
        try:
            return json.loads(json.dumps(payload, ensure_ascii=False))
        except Exception:
            if isinstance(payload, dict):
                return dict(payload)
            return payload

    def _update_pending_run_meta(self, run_id: str, **updates: Any) -> None:
        current = self._pending_run_meta.get(run_id) or {}
        current.update(updates)
        self._pending_run_meta[run_id] = current

    def _bind_pending_run(
        self,
        run_id: str,
        route: WatchRoute,
        *,
        session_key: str,
        request_params: Optional[dict] = None,
        retry_count: int = 0,
    ) -> None:
        self._pending_run_texts.pop(run_id, None)
        self._pending_run_stream_sent_at.pop(run_id, None)
        expires_at = time.time() + self.pending_run_ttl_seconds
        self._pending_run_routes[run_id] = (route, expires_at)
        now = time.time()
        meta = {
            "sessionKey": session_key,
            "retryCount": int(retry_count or 0),
            "acceptedAt": now,
            "lastProgressAt": now,
            "watchdogTriggered": False,
            # 避免 WS 事件 / agent.wait 兜底等并发路径重复回写终态。
            "finalSent": False,
            # 记录流式回写次数：当回写过于碎片化时，终态会重发完整回复以收敛一致性。
            "streamFlushCount": 0,
            # accepted 后兜底：用 agent.wait + chat.history 收敛终态文本，避免 WS 事件丢失导致“回复不全/看起来超时”。
            "finalizerStarted": True,
        }
        if session_key:
            self._remember_session_route(session_key, route)
        if isinstance(request_params, dict):
            meta["requestParams"] = self._clone_json_payload(request_params)
        self._pending_run_meta[run_id] = meta
        self._create_task_safe(
            self._await_pending_run_final(run_id),
            name=f"claw-run-finalizer:{run_id[:8]}",
        )

    async def _retry_pending_run_via_gateway(
        self,
        run_id: str,
        route: WatchRoute,
        error_text: str,
        *,
        error_kind: str = "unknown",
    ) -> bool:
        meta = self._pending_run_meta.get(run_id) or {}
        retry_count = int(meta.get("retryCount") or 0)
        request_params = meta.get("requestParams")
        if retry_count >= self._MAX_EXPLICIT_ERROR_RETRIES or not isinstance(request_params, dict):
            return False

        pending_text = self._pending_run_texts.get(run_id, "").strip()
        if pending_text and not self._classify_model_failure_text(pending_text):
            logger.info(
                "[Claw] run 已有正常累计文本，跳过自动重试 run_id={} chars={}",
                run_id,
                len(pending_text),
            )
            return False

        if self._is_non_retryable_run_error(error_kind, error_text):
            logger.warning(
                "[Claw] 命中不可重试错误，停止自动重试 run_id={} kind={} error={}",
                run_id,
                error_kind or "-",
                error_text or "-",
            )
            return False

        session_key = _safe_text(meta.get("sessionKey")).strip()
        retry_params = self._clone_json_payload(request_params) or {}
        retry_params["idempotencyKey"] = uuid.uuid4().hex
        next_retry_count = retry_count + 1

        retry_hint = self._build_gateway_retry_hint(error_kind, error_text)
        # 用户要求：自动重试只向网关发送“错误/重试提示”，不附带本次对话内容（原 prompt）。
        # 网关应基于同 sessionKey 的上下文自行重试上一轮请求。
        retry_notice = retry_hint if (self.retry_hint_to_gateway_enable and retry_hint) else ""
        if not retry_notice:
            retry_notice = "Retry this request once and return a complete response."
        retry_params["message"] = f"[Gateway Retry Notice]\n{retry_notice}".strip()

        logger.warning(
            "[Claw] 显式错误触发自动重试 old_run_id={} retry={} error={}",
            run_id,
            next_retry_count,
            error_text or "-",
        )
        self._clear_pending_run(run_id)

        try:
            payload = await self.gateway.request(
                method="agent",
                params=retry_params,
                expect_final=False,
                timeout_seconds=max(6, min(self.trigger_timeout_seconds, 20)),
            )
        except Exception as exc:
            logger.warning("[Claw] 自动重试发起失败 old_run_id={} error={}", run_id, exc)
            return False

        reply_text = self._extract_openclaw_reply_text(payload).strip()
        if reply_text:
            failure_kind = self._classify_model_failure_text(reply_text)
            if failure_kind:
                logger.warning(
                    "[Claw] 自动重试返回模型失败文本，抑制回写 old_run_id={} kind={} text={}",
                    run_id,
                    failure_kind,
                    _safe_text(reply_text)[:160].replace("\n", "\\n"),
                )
                return True
            await self._send_openclaw_reply(route, reply_text)
            return True

        new_run_id = self._extract_openclaw_run_id(payload)
        if not new_run_id:
            logger.warning("[Claw] 自动重试未返回 runId old_run_id={} payload={}", run_id, _compact_json(payload, 800))
            return False

        self._bind_pending_run(
            new_run_id,
            route,
            session_key=session_key,
            request_params=request_params,
            retry_count=next_retry_count,
        )
        logger.info(
            "[Claw] 自动重试已受理 old_run_id={} new_run_id={} sessionKey={}",
            run_id,
            new_run_id,
            session_key or "-",
        )
        return True

    async def _await_pending_run_final(self, run_id: str) -> None:
        """accepted 后兜底：用 agent.wait 等待完成并收敛终态文本。"""
        await asyncio.sleep(0)

        meta = self._pending_run_meta.get(run_id) or {}
        session_key = _safe_text(meta.get("sessionKey")).strip()
        if not session_key:
            return

        timeout_seconds = max(int(self.pending_run_watchdog_seconds), int(self.pending_run_ttl_seconds))
        try:
            payload = await self.gateway.request(
                method="agent.wait",
                params={"runId": run_id},
                expect_final=True,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            logger.warning("[Claw] agent.wait 兜底失败 run_id={} error={}", run_id, exc)
            return

        pending = self._pending_run_routes.get(run_id)
        if not pending:
            return
        route, _ = pending

        pending_text = self._pending_run_texts.get(run_id, "").strip()
        wait_text = self._extract_openclaw_reply_text(payload).strip()
        reply_text = pending_text
        if wait_text and len(wait_text) > len(reply_text):
            reply_text = wait_text

        if not reply_text:
            history_text = await self._fetch_assistant_reply_via_chat_history(session_key)
            if history_text and len(history_text) > len(reply_text):
                reply_text = history_text

            if not reply_text:
                await asyncio.sleep(0.6)
                history_text = await self._fetch_assistant_reply_via_chat_history(session_key)
                if history_text:
                    reply_text = history_text

        if reply_text:
            failure_kind = self._classify_model_failure_text(reply_text)
            if failure_kind:
                if self._is_terminal_failure_text(reply_text):
                    logger.warning(
                        "[Claw] agent.wait 收到模型失败终止提示，抑制回写 run_id={} kind={}",
                        run_id,
                        failure_kind,
                    )
                    self._clear_pending_run(run_id)
                    return
                if self._is_non_retryable_run_error(failure_kind, reply_text):
                    logger.warning(
                        "[Claw] agent.wait 收到不可重试模型失败文本，停止自动重试 run_id={} kind={}",
                        run_id,
                        failure_kind,
                    )
                    self._clear_pending_run(run_id)
                    return
                handled = await self._retry_pending_run_via_gateway(
                    run_id,
                    route,
                    reply_text,
                    error_kind=failure_kind,
                )
                if handled:
                    return
                logger.warning(
                    "[Claw] agent.wait 收到模型失败文本且自动重试失败 run_id={} kind={}",
                    run_id,
                    failure_kind,
                )
                self._clear_pending_run(run_id)
                return

            await self._finalize_pending_run_once(
                run_id,
                route,
                reply_text,
                source="agent.wait",
            )
            return

        status = ""
        if isinstance(payload, dict):
            status = _safe_text(payload.get("status")).strip().lower()

        error_text = self._extract_openclaw_error_text(payload) or status or "empty response"
        error_kind = self._classify_run_error(error_text, payload if isinstance(payload, dict) else {})
        if error_kind == "unknown":
            if not status or status == "ok":
                error_kind = "empty_model"
            else:
                error_kind = "model_error"
        if self._is_non_retryable_run_error(error_kind, error_text, payload):
            logger.warning(
                "[Claw] agent.wait 收到不可重试错误，停止自动重试 run_id={} kind={} error={}",
                run_id,
                error_kind,
                error_text or "-",
            )
            self._clear_pending_run(run_id)
            return
        handled = await self._retry_pending_run_via_gateway(
            run_id,
            route,
            error_text or "model error",
            error_kind=error_kind,
        )
        if handled:
            return

        logger.warning("[Claw] agent.wait 已完成但未取到终态文本 run_id={} status={}", run_id, status or "-")
        # 用户要求：模型超时/重试失败等不要回写到微信，避免污染会话。
        self._clear_pending_run(run_id)

    def _extract_openclaw_run_id(self, payload: Any) -> str:
        seen: set[int] = set()

        def walk(node: Any, depth: int = 0) -> str:
            if depth > 6:
                return ""
            if isinstance(node, dict):
                node_id = id(node)
                if node_id in seen:
                    return ""
                seen.add(node_id)
                for key in ("runId", "run_id"):
                    value = _safe_text(node.get(key)).strip()
                    if value:
                        return value
                for key in ("data", "payload", "result", "message", "messages", "items", "payloads"):
                    if key in node:
                        found = walk(node.get(key), depth + 1)
                        if found:
                            return found
                for value in node.values():
                    found = walk(value, depth + 1)
                    if found:
                        return found
                return ""
            if isinstance(node, list):
                for item in node:
                    found = walk(item, depth + 1)
                    if found:
                        return found
            return ""

        return walk(payload)

    def _extract_run_id_from_event(self, frame: Any) -> str:
        if not isinstance(frame, dict):
            return ""
        for key in ("runId", "run_id"):
            value = _safe_text(frame.get(key)).strip()
            if value:
                return value
        return self._extract_openclaw_run_id(frame.get("payload"))

    def _extract_session_key_from_payload(self, payload: Any) -> str:
        seen: set[int] = set()

        def walk(node: Any, depth: int = 0) -> str:
            if depth > 8:
                return ""
            if isinstance(node, dict):
                node_id = id(node)
                if node_id in seen:
                    return ""
                seen.add(node_id)
                for key in ("sessionKey", "session_key"):
                    value = _safe_text(node.get(key)).strip()
                    if value:
                        return value
                for key in ("data", "payload", "result", "message", "messages", "items", "payloads"):
                    if key in node:
                        found = walk(node.get(key), depth + 1)
                        if found:
                            return found
                for value in node.values():
                    found = walk(value, depth + 1)
                    if found:
                        return found
                return ""
            if isinstance(node, list):
                for item in node:
                    found = walk(item, depth + 1)
                    if found:
                        return found
            return ""

        return walk(payload)

    def _resolve_event_route(self, frame: dict, run_id: str) -> Optional[WatchRoute]:
        if run_id:
            pending = self._pending_run_routes.get(run_id)
            if pending:
                return pending[0]
            meta = self._pending_run_meta.get(run_id) or {}
            session_key = _safe_text(meta.get("sessionKey")).strip()
            if session_key:
                route = self._session_routes.get(session_key)
                if route:
                    return route

        session_key = self._extract_session_key_from_payload(frame)
        if session_key:
            return self._session_routes.get(session_key)
        return None

    def _extract_openclaw_reply_text(self, payload: Any) -> str:
        parts: list[str] = []
        seen_texts: set[str] = set()
        assistant_roles = {"assistant", "bot"}

        def add(text: Any):
            value = _safe_text(text).strip()
            if not value or value in seen_texts:
                return
            seen_texts.add(value)
            parts.append(value)

        def walk(node: Any, depth: int = 0, *, from_message: bool = False):
            if depth > 7:
                return
            if isinstance(node, str):
                if from_message:
                    add(node)
                return
            if isinstance(node, dict):
                role = _safe_text(node.get("role")).strip().lower()
                is_assistant_message = bool(role) and role in assistant_roles
                if not role or is_assistant_message:
                    for key in ("text", "delta"):
                        if key in node:
                            add(node.get(key))

                # 只有在“明确是 assistant 消息”或“已处于消息内容上下文”时，
                # 才把 content/message/payloads 当作可直接抽取文本的消息体，避免误把 user prompt 当回复。
                if role and role not in assistant_roles:
                    message_context = False
                else:
                    message_context = bool(from_message) or is_assistant_message

                content = node.get("content")
                if content is not None:
                    walk(content, depth + 1, from_message=message_context)

                message = node.get("message")
                if message is not None:
                    walk(message, depth + 1, from_message=message_context)

                payloads = node.get("payloads")
                if payloads is not None:
                    walk(payloads, depth + 1, from_message=message_context)

                # 常见容器字段：不同 RPC/事件会把消息列表放在 messages/items/output 等字段里。
                for key in ("data", "payload", "result", "response", "messages", "items", "output", "outputs"):
                    if key in node:
                        walk(node.get(key), depth + 1, from_message=from_message)

                return
            if isinstance(node, list):
                for item in node:
                    walk(item, depth + 1, from_message=from_message)

        walk(payload)
        return "\n".join(parts).strip()

    def _is_explicit_run_error(self, event_name: str, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False

        event_name = _safe_text(event_name).strip().lower()
        status = _safe_text(payload.get("status")).strip().lower()
        state = _safe_text(payload.get("state")).strip().lower()
        stream = _safe_text(payload.get("stream")).strip().lower()
        reason = _safe_text(payload.get("reason") or payload.get("stopReason") or payload.get("stop_reason")).strip().lower()
        error_message = _safe_text(payload.get("errorMessage") or payload.get("error_message")).strip()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        phase = _safe_text(data.get("phase")).strip().lower()

        error_states = {"error", "failed", "failure", "timeout", "timed_out", "cancelled", "canceled"}
        if status in error_states or state in error_states or phase in error_states:
            return True
        if error_message:
            return True
        if reason in {"network_error", "model_error", "empty_model", "timeout"}:
            return True
        if event_name == "agent" and stream == "lifecycle" and phase in error_states:
            return True
        if event_name == "chat" and state in error_states:
            return True
        if payload.get("error"):
            return True
        extracted_error = self._extract_openclaw_error_text(payload)
        if self._classify_run_error(extracted_error, payload) != "unknown":
            return True
        return False

    def _extract_openclaw_error_text(self, payload: Any) -> str:
        texts: list[str] = []
        seen: set[str] = set()

        def add(value: Any):
            text = _safe_text(value).strip()
            if not text or text in seen:
                return
            seen.add(text)
            texts.append(text)

        def walk(node: Any, depth: int = 0):
            if depth > 6:
                return
            if isinstance(node, str):
                add(node)
                return
            if isinstance(node, dict):
                for key in (
                    "message",
                    "error",
                    "errorMessage",
                    "error_message",
                    "reason",
                    "detail",
                    "details",
                    "stopReason",
                    "stop_reason",
                    "statusText",
                    "finishReason",
                    "finish_reason",
                    "phase",
                    "code",
                ):
                    if key in node:
                        walk(node.get(key), depth + 1)
                for key in ("data", "payload", "result"):
                    if key in node:
                        walk(node.get(key), depth + 1)
                return
            if isinstance(node, list):
                for item in node:
                    walk(item, depth + 1)

        walk(payload)
        return " | ".join(texts[:4]).strip()

    def _classify_run_error(self, error_text: str, payload: Any = None) -> str:
        parts = [_safe_text(error_text).strip().lower()]
        if payload is not None:
            try:
                parts.append(_safe_text(_compact_json(payload, 1600)).strip().lower())
            except Exception:
                pass
        merged = " ".join(part for part in parts if part)

        timeout_keywords = {
            "timeout",
            "timed_out",
            "timed out",
            "llm request timed out",
            "network_error",
            "deadline exceeded",
            "deadline_exceeded",
            "econnreset",
            "etimedout",
            "connection reset",
            "超时",
            "请求超时",
        }
        empty_keywords = {
            "empty_model",
            "empty response",
            "empty reply",
            "blank response",
            "no response",
            "no output",
            "empty output",
            "模型空回复",
            "空回复",
            "无回复",
        }
        model_keywords = {
            "model_error",
            "provider_error",
            "rate limit",
            "rate_limit",
            "rate_limited",
            "too many requests",
            "insufficient quota",
            "insufficient_quota",
            "quota exceeded",
            "quota_exceeded",
            "context length",
            "context_length",
            "context_length_exceeded",
            "maximum context length",
            "max context length",
            "invalid request",
            "invalid_request",
            "bad request",
            "unauthorized",
            "forbidden",
            "permission denied",
            "access denied",
            "service unavailable",
            "server overloaded",
            "overloaded",
            "invalid model",
            "model not found",
            "model unavailable",
            "unavailable",
            "模型错误",
            "模型不可用",
            "限流",
            "频率限制",
            "配额",
            "额度不足",
            "余额不足",
            "上下文长度",
            "最大上下文",
            "无权限",
            "未授权",
            "禁止访问",
            "服务不可用",
            "服务器过载",
            "过载",
            "failovererror",
            "an error occurred while processing your request",
            "help.openai.com",
            "help center",
        }

        if any(keyword in merged for keyword in timeout_keywords):
            return "timeout"
        if any(keyword in merged for keyword in empty_keywords):
            return "empty_model"
        if any(keyword in merged for keyword in model_keywords):
            return "model_error"
        return "unknown"

    def _classify_model_failure_text(self, reply_text: str) -> str:
        """判断 assistant 文本是否更像“模型失败提示”而非正常回答。"""
        text = _safe_text(reply_text).strip()
        if not text:
            return "empty_model"

        lowered = text.lower()
        # 正常回答可能会讨论 timeout/模型/错误等关键词；这里用长度 + 关键短语做降误判。
        if len(lowered) > 280:
            strong_markers = (
                "an error occurred while processing your request",
                "help.openai.com",
                "request id",
                "retry failed",
                "model timeout retry failed",
                "model error persisted",
                "model returned empty",
                "failovererror",
            )
            if not any(marker in lowered for marker in strong_markers):
                return ""

        kind = self._classify_run_error(text)
        if kind == "unknown":
            return ""

        if kind == "timeout":
            if any(marker in lowered for marker in ("model", "retry", "failed", "please try again later", "error occurred", "重试", "失败")):
                return kind
            return ""

        if kind == "model_error":
            if any(
                marker in lowered
                for marker in (
                    "model",
                    "provider",
                    "error",
                    "failed",
                    "invalid",
                    "unavailable",
                    "rate limit",
                    "too many requests",
                    "quota",
                    "unauthorized",
                    "forbidden",
                    "permission",
                    "access denied",
                    "please try again later",
                    "help center",
                    "重试",
                    "失败",
                    "限流",
                    "配额",
                    "额度",
                    "无权限",
                )
            ):
                return kind
            return ""

        if kind == "empty_model":
            if any(marker in lowered for marker in ("empty", "blank", "no response", "no output", "空回复", "无回复")):
                return kind
            return ""

        return ""

    def _is_non_retryable_run_error(self, error_kind: str, error_text: str, payload: Any = None) -> bool:
        if error_kind != "model_error":
            return False

        parts = [_safe_text(error_text).strip().lower()]
        if payload is not None:
            try:
                parts.append(_safe_text(_compact_json(payload, 1600)).strip().lower())
            except Exception:
                pass
        merged = " ".join(part for part in parts if part)
        if not merged:
            return False

        hard_markers = (
            "account has been deactivated",
            "has been deactivated",
            "deactivated",
            "unauthorized",
            "forbidden",
            "permission denied",
            "access denied",
            "invalid api key",
            "incorrect api key",
            "invalid_api_key",
            "insufficient quota",
            "quota exceeded",
            "billing",
            "payment required",
            "model not found",
            "no such model",
            "invalid model",
            "invalid request",
            "invalid_request",
            "unsupported model",
            "unsupported",
            "未授权",
            "无权限",
            "禁止访问",
            "权限不足",
            "账号已停用",
            "账号被停用",
            "额度不足",
            "配额不足",
        )
        return any(marker in merged for marker in hard_markers)

    def _is_terminal_failure_text(self, reply_text: str) -> bool:
        """识别“已重试仍失败/请稍后再试”一类终止提示，避免继续重试或回写到微信。"""
        text = _safe_text(reply_text).strip().lower()
        if not text:
            return False
        terminal_markers = (
            "retry failed",
            "after retry",
            "please try again later",
            "contact us",
            "help center",
            "help.openai.com",
            "重试失败",
            "请稍后再试",
            "稍后再试",
        )
        return any(marker in text for marker in terminal_markers)

    def _build_gateway_retry_hint(self, error_kind: str, error_text: str) -> str:
        if error_kind == "timeout":
            return "Model timed out. Retry this request once and return a complete response."
        if error_kind == "empty_model":
            return "Model returned an empty response. Retry this request once and return a complete response."
        if error_kind == "model_error":
            return "Model failed to respond correctly. Retry this request once and return a complete response."
        detail = _safe_text(error_text).strip()
        if detail:
            return f"Retry this request once due to model failure. Last error: {detail}"
        return "Retry this request once due to model failure and return a complete response."

    def _build_retry_agent_params(self, request_params: dict, *, error_kind: str, error_text: str) -> dict:
        retry_params = self._clone_json_payload(request_params) or {}
        retry_params["idempotencyKey"] = uuid.uuid4().hex

        retry_hint = self._build_gateway_retry_hint(error_kind, error_text)
        retry_notice = retry_hint if (self.retry_hint_to_gateway_enable and retry_hint) else ""
        if not retry_notice:
            retry_notice = "Retry this request once and return a complete response."
        # 用户要求：自动重试只向网关发送“错误/重试提示”，不附带本次对话内容（原 prompt）。
        # 网关应基于同 sessionKey 的上下文自行重试上一轮请求。
        retry_params["message"] = f"[Gateway Retry Notice]\n{retry_notice}".strip()
        return retry_params

    def _build_final_model_error_message(self, error_kind: str, error_text: str) -> str:
        if error_kind == "timeout":
            return "Model timeout retry failed. Please try again later."
        if error_kind == "empty_model":
            return "Model returned empty output after retry. Please try again later."
        if error_kind == "model_error":
            return "Model error persisted after retry. Please try again later."
        detail = _safe_text(error_text).strip()
        if detail:
            return f"Model error was not recognized after retry: {detail}"
        return "Model error was not recognized after retry."

    def _extract_text_from_chat_history_message(self, message: dict) -> str:
        if not isinstance(message, dict):
            return ""
        parts: list[str] = []

        content = message.get("content")
        if isinstance(content, list):
            seg_parts: list[str] = []
            for segment in content:
                if isinstance(segment, dict):
                    seg_text = _safe_text(segment.get("text")).strip()
                else:
                    seg_text = _safe_text(segment).strip()
                if seg_text:
                    seg_parts.append(seg_text)
            if seg_parts:
                parts.append("".join(seg_parts))
        else:
            content_text = _safe_text(content).strip()
            if content_text:
                parts.append(content_text)

        direct_text = _safe_text(message.get("text")).strip()
        if direct_text:
            parts.append(direct_text)

        return "\n".join([p for p in parts if p]).strip()

    def _extract_assistant_reply_from_chat_history(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return ""

        assistant_roles = {"assistant", "bot"}
        user_roles = {"user", "human"}

        last_user_index = -1
        fallback_boundary_index = -1
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            role = _safe_text(message.get("role")).strip().lower()
            if role in user_roles:
                last_user_index = index
            if role and role not in assistant_roles:
                fallback_boundary_index = index

        if last_user_index < 0:
            last_user_index = fallback_boundary_index

        if last_user_index < 0:
            return ""

        start = last_user_index + 1
        parts: list[str] = []
        for message in messages[start:]:
            if not isinstance(message, dict):
                continue
            role = _safe_text(message.get("role")).strip().lower()
            if role and role not in assistant_roles:
                continue
            text = self._extract_text_from_chat_history_message(message)
            if text:
                parts.append(text)

        return "\n".join(parts).strip()

    async def _fetch_assistant_reply_via_chat_history(self, session_key: str) -> str:
        session_key = _safe_text(session_key).strip()
        if not session_key:
            return ""
        try:
            payload = await self.gateway.request(
                method="chat.history",
                params={"sessionKey": session_key},
                expect_final=False,
                timeout_seconds=10,
            )
        except Exception as exc:
            logger.warning("[Claw] chat.history 获取失败 sessionKey={} error={}", session_key, exc)
            return ""
        return self._extract_assistant_reply_from_chat_history(payload).strip()

    async def _maybe_finalize_run_via_chat_history(
        self,
        run_id: str,
        route: WatchRoute,
        session_key: str,
        *,
        reason: str,
        min_chars: int = 1,
    ) -> bool:
        if run_id not in self._pending_run_routes:
            return True
        history_text = await self._fetch_assistant_reply_via_chat_history(session_key)
        if not history_text or len(history_text) < int(min_chars):
            return False
        logger.info("[Claw] 通过 chat.history 收敛终态 run_id={} reason={}", run_id, reason)
        sent = await self._finalize_pending_run_once(
            run_id,
            route,
            history_text,
            source=f"chat.history:{_safe_text(reason).strip() or '-'}",
        )
        return sent or run_id not in self._pending_run_routes

    async def _on_gateway_event(self, frame: dict):
        if not isinstance(frame, dict):
            return

        event_name = _safe_text(frame.get("event") or frame.get("name") or frame.get("method")).strip()
        payload = frame.get("payload", {})
        run_id = self._extract_run_id_from_event(frame)

        if run_id:
            self._cleanup_pending_run_routes()
            pending = self._pending_run_routes.get(run_id)
            if pending:
                self._update_pending_run_meta(run_id, lastProgressAt=time.time(), watchdogTriggered=False)
            if isinstance(payload, dict):
                update_mode, stream_text = self._extract_stream_text_update(event_name, payload)
                if stream_text:
                    if update_mode == "append":
                        self._pending_run_texts[run_id] = f"{self._pending_run_texts.get(run_id, '')}{stream_text}".strip()
                    else:
                        self._pending_run_texts[run_id] = stream_text
                    if pending and self.stream_reply_enable:
                        route, _ = pending
                        await self._maybe_send_stream_update_to_route(run_id, route)
            if pending:
                route, _ = pending
                if not isinstance(payload, dict):
                    return

                media_sent = await self._maybe_send_gateway_media(run_id, route, payload)
                meta = self._pending_run_meta.get(run_id) or {}
                session_key = _safe_text(meta.get("sessionKey")).strip()

                if not self._is_run_completion_event(event_name, payload):
                    return

                # 终态 payload 有时仅携带最后一段 delta（或部分字段），但我们在流式阶段已累计完整文本。
                # 为避免“只发到第一段/看起来截断”，这里优先使用更长的累计结果。
                reply_text = self._extract_openclaw_reply_text(payload).strip()
                pending_text = self._pending_run_texts.get(run_id, "").strip()
                if pending_text and len(pending_text) > len(reply_text):
                    reply_text = pending_text
                elif not reply_text:
                    reply_text = pending_text
                reply_failure_kind = self._classify_model_failure_text(reply_text) if reply_text else ""

                if self._is_explicit_run_error(event_name, payload):
                    error_text = self._extract_openclaw_error_text(payload) or reply_text
                    if not error_text:
                        error_text = "unknown model error"
                    error_kind = self._classify_run_error(error_text, payload)
                    if error_kind == "unknown":
                        error_kind = "model_error"
                    if reply_text and not reply_failure_kind:
                        logger.warning(
                            "[Claw] 显式错误事件附带正常文本，按文本终态回写 run_id={} event={}",
                            run_id,
                            event_name or "-",
                        )
                        await self._finalize_pending_run_once(
                            run_id,
                            route,
                            reply_text,
                            source=f"event:{event_name or '-'}:text-wins",
                        )
                        return
                    if self._is_non_retryable_run_error(error_kind, error_text, payload):
                        logger.warning(
                            "[Claw] 显式错误事件命中不可重试错误，停止自动重试 run_id={} kind={} error={}",
                            run_id,
                            error_kind,
                            error_text or "-",
                        )
                        self._clear_pending_run(run_id)
                        return
                    handled = await self._retry_pending_run_via_gateway(
                        run_id,
                        route,
                        error_text,
                        error_kind=error_kind,
                    )
                    if handled:
                        return
                    logger.warning("[Claw] 显式错误事件终止且自动重试失败 run_id={} error={}", run_id, error_text)
                    self._clear_pending_run(run_id)
                    return

                if event_name == "agent" and session_key and not reply_text:
                    logger.debug("[Claw] agent 完成事件已收到但无文本，等待 agent.wait 兜底(run_id={})", run_id)
                    return

                if reply_text:
                    failure_kind = reply_failure_kind
                    if failure_kind:
                        if self._is_terminal_failure_text(reply_text):
                            logger.warning(
                                "[Claw] 终态返回模型失败终止提示，抑制回写 run_id={} kind={}",
                                run_id,
                                failure_kind,
                            )
                            self._clear_pending_run(run_id)
                            return
                        if self._is_non_retryable_run_error(failure_kind, reply_text):
                            logger.warning(
                                "[Claw] 终态返回不可重试模型失败文本，停止自动重试 run_id={} kind={}",
                                run_id,
                                failure_kind,
                            )
                            self._clear_pending_run(run_id)
                            return
                        handled = await self._retry_pending_run_via_gateway(
                            run_id,
                            route,
                            reply_text,
                            error_kind=failure_kind,
                        )
                        if handled:
                            return
                        logger.warning(
                            "[Claw] 终态返回模型失败文本且自动重试失败 run_id={} kind={}",
                            run_id,
                            failure_kind,
                        )
                        self._clear_pending_run(run_id)
                        return
                    await self._finalize_pending_run_once(
                        run_id,
                        route,
                        reply_text,
                        source=f"event:{event_name or '-'}",
                    )
                    return

                if event_name == "chat" and not media_sent:
                    if session_key:
                        finalized = await self._maybe_finalize_run_via_chat_history(
                            run_id,
                            route,
                            session_key,
                            reason="chat-empty-complete",
                            min_chars=max(1, len(pending_text)),
                        )
                        if finalized:
                            return
                        logger.info(
                            "[Claw] chat 完成事件无文本，等待 agent.wait/chat.history 兜底 run_id={}",
                            run_id,
                        )
                        return
                    error_text = self._extract_openclaw_error_text(payload) or "empty response"
                    error_kind = self._classify_run_error(error_text, payload)
                    if error_kind == "unknown":
                        error_kind = "empty_model"
                    handled = await self._retry_pending_run_via_gateway(
                        run_id,
                        route,
                        error_text,
                        error_kind=error_kind,
                    )
                    if handled:
                        return
                    logger.warning("[Claw] chat 完成无文本且重试失败 run_id={} error={}", run_id, error_text)
                    self._clear_pending_run(run_id)
                    return

                if event_name == "chat":
                    logger.info("[Claw] chat 完成事件无文本，结束等待(run_id={})", run_id)
                    self._clear_pending_run(run_id)
                    return

                if session_key:
                    logger.debug("[Claw] 非 chat 终态事件无文本，继续等待实时事件(run_id={})", run_id)
                    return

                self._clear_pending_run(run_id)
                return

        # 原始网关事件体只允许转发到显式配置的固定目标，
        # 不能再按“当前会话”回推到微信，避免把 payload 直接发给用户。
        if not self.event_forward_enable or not self.event_forward_to_wxids:
            return

        if self.event_forward_allowed and event_name not in self.event_forward_allowed:
            return

        message_text = f"[ClawEvent] {event_name}\n{_dump_json(frame.get('payload', {}))}"

        for target_wxid in self.event_forward_to_wxids:
            try:
                await self.bot.send_text_message(target_wxid, message_text)
            except Exception as exc:
                logger.warning("[Claw] 事件转发失败(to_wxid={}): {}", target_wxid, exc)

    async def _pending_run_watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(self.pending_run_watchdog_interval_seconds)
            try:
                await self._tick_pending_run_watchdog()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[Claw] pending-run watchdog 异常")

    async def _tick_pending_run_watchdog(self) -> None:
        if not self.enable or not self.pending_run_watchdog_enable:
            return
        if not self._pending_run_routes:
            return

        self._cleanup_pending_run_routes()
        if not self._pending_run_routes:
            return

        now = time.time()
        stalled: list[tuple[str, WatchRoute, str]] = []

        for run_id, (route, _expires_at) in list(self._pending_run_routes.items()):
            meta = self._pending_run_meta.get(run_id) or {}
            if not meta:
                continue
            if bool(meta.get("watchdogTriggered")):
                continue

            accepted_at = float(meta.get("acceptedAt") or 0.0) or now
            last_progress_at = float(meta.get("lastProgressAt") or 0.0)
            last_stream_at = float(self._pending_run_stream_sent_at.get(run_id) or 0.0)
            last_progress_at = max(last_progress_at, last_stream_at, accepted_at)

            stalled_for = now - last_progress_at
            if stalled_for < float(self.pending_run_watchdog_seconds):
                continue

            stalled.append((run_id, route, f"stalled_for={int(stalled_for)}s"))

        for run_id, route, detail in stalled:
            logger.warning("[Claw] pending run watchdog 触发但未取到终态 run_id={} {}", run_id, detail)
            self._update_pending_run_meta(run_id, watchdogTriggered=True)
            # 用户要求：模型超时/重试失败等不要回写到微信，避免污染会话。
            # 同时不在这里清理 run：让 agent.wait 兜底任务继续等待终态，避免“模型慢但最终有回复”被误清理。

    def _get_pending_run_finalize_lock(self, run_id: str) -> asyncio.Lock:
        lock = self._pending_run_finalize_locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._pending_run_finalize_locks[run_id] = lock
        return lock

    async def _finalize_pending_run_once(
        self,
        run_id: str,
        route: WatchRoute,
        reply_text: str,
        *,
        source: str,
    ) -> bool:
        text = _safe_text(reply_text).strip()
        if not text:
            return False

        lock = self._get_pending_run_finalize_lock(run_id)
        async with lock:
            if run_id not in self._pending_run_routes:
                return False
            meta = self._pending_run_meta.get(run_id) or {}
            if bool(meta.get("finalSent")):
                return False
            self._update_pending_run_meta(
                run_id,
                finalSent=True,
                finalSentAt=time.time(),
                finalSentBy=_safe_text(source).strip() or "-",
            )
            await self._send_final_reply_without_duplicate(run_id, route, text)
            self._clear_pending_run(run_id)
            return True

    def _cleanup_pending_run_routes(self):
        if not self._pending_run_routes:
            return
        now = time.time()
        expired = [run_id for run_id, (_, expires_at) in self._pending_run_routes.items() if expires_at <= now]
        for run_id in expired:
            self._clear_pending_run(run_id)

    def _clear_pending_run(self, run_id: str) -> None:
        """清理 run 关联缓存。"""
        self._pending_run_routes.pop(run_id, None)
        self._pending_run_meta.pop(run_id, None)
        self._pending_run_texts.pop(run_id, None)
        self._pending_run_stream_sent_texts.pop(run_id, None)
        self._pending_run_stream_sent_at.pop(run_id, None)
        self._pending_run_media_fingerprints.pop(run_id, None)
        self._pending_run_finalize_locks.pop(run_id, None)

    async def _maybe_send_gateway_media(self, run_id: str, route: WatchRoute, payload: dict) -> bool:
        # 用户要求：不自动下载/回传网关返回的任何媒体/附件，避免插件侧下载阻塞与误触发。
        return False

    def _mask_media_source_debug(self, source: dict) -> dict:
        value = _safe_text(source.get("value")).strip()
        transport = _safe_text(source.get("transport")).strip().lower()

        preview = ""
        if transport == "data_uri":
            head, _, _tail = value.partition(",")
            preview = f"{head},<base64...>"
        elif transport == "base64":
            preview = f"{value[:24]}...(len={len(value)})"
        else:
            preview = value[:140]

        return {
            "media_type": _safe_text(source.get("media_type")).strip(),
            "transport": transport,
            "file_name": _safe_text(source.get("file_name")).strip(),
            "mime_type": _safe_text(source.get("mime_type")).strip(),
            "value_preview": preview,
        }

    def _extract_openclaw_media_sources(self, payload: Any) -> list[dict]:
        """从 OpenClaw 返回中提取媒体来源（image/video/audio/file）。"""
        sources: list[dict] = []

        def add_source(media_type: str, transport: str, value: str, *, fingerprint: str, file_name: str = "", mime_type: str = ""):
            if not value:
                return
            sources.append(
                {
                    "media_type": media_type,
                    "transport": transport,
                    "value": value,
                    "file_name": file_name,
                    "mime_type": mime_type,
                    "fingerprint": fingerprint,
                }
            )

        def walk(node: Any, *, parent_key: str = "", depth: int = 0):
            if depth > 7:
                return
            if isinstance(node, dict):
                node_type = _safe_text(node.get("type")).strip().lower()
                mime_type = _safe_text(
                    node.get("mimeType")
                    or node.get("mime_type")
                    or node.get("contentType")
                    or node.get("content_type")
                ).strip().lower()
                file_name = _safe_text(
                    node.get("fileName")
                    or node.get("filename")
                    or node.get("name")
                    or node.get("title")
                ).strip()

                url = _safe_text(node.get("url") or node.get("src") or node.get("href")).strip()
                if url and self._looks_like_remote_url(url):
                    media_type = self._infer_media_type(
                        node_type=node_type,
                        parent_key=parent_key,
                        mime_type=mime_type,
                        value_hint=url,
                        file_name=file_name,
                    )
                    add_source(media_type, "url", url, fingerprint=f"url:{url}", file_name=file_name, mime_type=mime_type)

                image_obj = node.get("image")
                if isinstance(image_obj, dict):
                    url = _safe_text(image_obj.get("url") or image_obj.get("src")).strip()
                    if url and self._looks_like_remote_url(url):
                        media_type = self._infer_media_type(
                            node_type="image",
                            parent_key=parent_key,
                            mime_type=_safe_text(image_obj.get("mimeType") or image_obj.get("mime_type")).strip().lower() or "image/*",
                            value_hint=url,
                            file_name=file_name,
                        )
                        add_source(media_type, "url", url, fingerprint=f"url:{url}", file_name=file_name, mime_type=mime_type)
                    data_uri = _safe_text(image_obj.get("dataUri") or image_obj.get("data_uri")).strip()
                    if data_uri.startswith("data:") and ";base64," in data_uri:
                        parsed_mime = data_uri.split(";", 1)[0].removeprefix("data:").strip().lower()
                        media_type = self._infer_media_type(
                            node_type="image",
                            parent_key=parent_key,
                            mime_type=parsed_mime,
                            value_hint="",
                            file_name=file_name,
                        )
                        add_source(
                            media_type,
                            "data_uri",
                            data_uri,
                            fingerprint=f"data_uri:{hashlib.sha1(data_uri.encode('utf-8')).hexdigest()[:12]}",
                            file_name=file_name,
                            mime_type=parsed_mime,
                        )
                    b64 = _safe_text(image_obj.get("base64") or image_obj.get("b64")).strip()
                    if b64 and len(b64) > 128 and self._looks_like_base64_blob(b64):
                        add_source(
                            "image",
                            "base64",
                            b64,
                            fingerprint=f"b64:{hashlib.sha1(b64.encode('utf-8')).hexdigest()[:12]}",
                            file_name=file_name,
                            mime_type=mime_type or "image/*",
                        )

                for data_key in ("dataUri", "data_uri"):
                    data_uri = _safe_text(node.get(data_key)).strip()
                    if data_uri.startswith("data:") and ";base64," in data_uri:
                        parsed_mime = data_uri.split(";", 1)[0].removeprefix("data:").strip().lower()
                        media_type = self._infer_media_type(
                            node_type=node_type,
                            parent_key=parent_key,
                            mime_type=parsed_mime,
                            value_hint="",
                            file_name=file_name,
                        )
                        add_source(
                            media_type,
                            "data_uri",
                            data_uri,
                            fingerprint=f"data_uri:{hashlib.sha1(data_uri.encode('utf-8')).hexdigest()[:12]}",
                            file_name=file_name,
                            mime_type=parsed_mime,
                        )
                        break

                for b64_key in ("base64", "b64", "bytes", "fileBase64", "file_base64"):
                    b64 = _safe_text(node.get(b64_key)).strip()
                    if b64 and len(b64) > 128 and self._looks_like_base64_blob(b64):
                        media_type = self._infer_media_type(
                            node_type=node_type,
                            parent_key=parent_key,
                            mime_type=mime_type,
                            value_hint="",
                            file_name=file_name,
                        )
                        add_source(
                            media_type,
                            "base64",
                            b64,
                            fingerprint=f"b64:{hashlib.sha1(b64.encode('utf-8')).hexdigest()[:12]}",
                            file_name=file_name,
                            mime_type=mime_type,
                        )
                        break

                content_value = node.get("content")
                if isinstance(content_value, str):
                    content_text = content_value.strip()
                    if content_text.startswith("data:") and ";base64," in content_text:
                        parsed_mime = content_text.split(";", 1)[0].removeprefix("data:").strip().lower()
                        media_type = self._infer_media_type(
                            node_type=node_type,
                            parent_key=parent_key or "content",
                            mime_type=parsed_mime or mime_type,
                            value_hint=file_name,
                            file_name=file_name,
                        )
                        add_source(
                            media_type,
                            "data_uri",
                            content_text,
                            fingerprint=f"data_uri:{hashlib.sha1(content_text.encode('utf-8')).hexdigest()[:12]}",
                            file_name=file_name,
                            mime_type=parsed_mime or mime_type,
                        )
                    elif self._is_attachment_like_node(node, parent_key) and len(content_text) > 128 and self._looks_like_base64_blob(content_text):
                        media_type = self._infer_media_type(
                            node_type=node_type,
                            parent_key=parent_key or "content",
                            mime_type=mime_type,
                            value_hint=file_name,
                            file_name=file_name,
                        )
                        add_source(
                            media_type,
                            "base64",
                            content_text,
                            fingerprint=f"b64:{hashlib.sha1(content_text.encode('utf-8')).hexdigest()[:12]}",
                            file_name=file_name,
                            mime_type=mime_type,
                        )
                    elif self._is_attachment_like_node(node, parent_key) and self._looks_like_remote_url(content_text):
                        media_type = self._infer_media_type(
                            node_type=node_type,
                            parent_key=parent_key or "content",
                            mime_type=mime_type,
                            value_hint=content_text,
                            file_name=file_name,
                        )
                        add_source(
                            media_type,
                            "url",
                            content_text,
                            fingerprint=f"url:{content_text}",
                            file_name=file_name,
                            mime_type=mime_type,
                        )
                    elif self._is_attachment_like_node(node, parent_key) and os.path.exists(content_text):
                        media_type = self._infer_media_type(
                            node_type=node_type,
                            parent_key=parent_key or "content",
                            mime_type=mime_type,
                            value_hint=content_text,
                            file_name=file_name,
                        )
                        add_source(
                            media_type,
                            "path",
                            content_text,
                            fingerprint=f"path:{content_text}",
                            file_name=file_name,
                            mime_type=mime_type,
                        )

                for path_key in ("path", "file", "filepath", "file_path", "filePath", "localPath"):
                    path_value = _safe_text(node.get(path_key)).strip()
                    if path_value and os.path.exists(path_value):
                        media_type = self._infer_media_type(
                            node_type=node_type,
                            parent_key=parent_key,
                            mime_type=mime_type,
                            value_hint=path_value,
                            file_name=file_name,
                        )
                        add_source(media_type, "path", path_value, fingerprint=f"path:{path_value}", file_name=file_name, mime_type=mime_type)
                        break

                for key, value in list(node.items()):
                    walk(value, parent_key=str(key), depth=depth + 1)
                return

            if isinstance(node, list):
                for item in node[:40]:
                    walk(item, parent_key=parent_key, depth=depth + 1)
                return

            if isinstance(node, str):
                text = node.strip()
                media_refs = self._extract_media_ref_candidates(text)
                for media_ref in media_refs:
                    for source in self._build_media_sources_from_ref(media_ref):
                        add_source(
                            source["media_type"],
                            source["transport"],
                            source["value"],
                            fingerprint=source["fingerprint"],
                            file_name=source.get("file_name", ""),
                            mime_type=source.get("mime_type", ""),
                        )
                if text.startswith("data:") and ";base64," in text:
                    parsed_mime = text.split(";", 1)[0].removeprefix("data:").strip().lower()
                    media_type = self._infer_media_type(
                        node_type="",
                        parent_key=parent_key,
                        mime_type=parsed_mime,
                        value_hint="",
                        file_name="",
                    )
                    add_source(
                        media_type,
                        "data_uri",
                        text,
                        fingerprint=f"data_uri:{hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]}",
                        mime_type=parsed_mime,
                    )
                    return
                if text.startswith("file://"):
                    parsed = urllib.parse.urlparse(text)
                    file_path = parsed.path or ""
                    if file_path and os.path.exists(file_path):
                        media_type = self._infer_media_type(
                            node_type="",
                            parent_key=parent_key,
                            mime_type="",
                            value_hint=file_path,
                            file_name=os.path.basename(file_path),
                        )
                        add_source(media_type, "path", file_path, fingerprint=f"path:{file_path}")
                        return
                if self._looks_like_remote_url(text):
                    media_type = self._infer_media_type(
                        node_type="",
                        parent_key=parent_key,
                        mime_type="",
                        value_hint=text,
                        file_name="",
                    )
                    add_source(media_type, "url", text, fingerprint=f"url:{text}")
                    return
                if parent_key.lower() in {"path", "file", "filepath", "file_path"} and os.path.exists(text):
                    media_type = self._infer_media_type(
                        node_type="",
                        parent_key=parent_key,
                        mime_type="",
                        value_hint=text,
                        file_name="",
                    )
                    add_source(media_type, "path", text, fingerprint=f"path:{text}")
                return

        walk(payload)
        return sources

    def _looks_like_remote_url(self, value: str) -> bool:
        text = value.strip()
        if not (text.startswith("http://") or text.startswith("https://")):
            return False
        parsed = urllib.parse.urlparse(text)
        return bool(parsed.netloc)

    def _extract_media_ref_candidates(self, text: str) -> list[str]:
        """提取文本中的 MEDIA: 引用，例如 MEDIA:/baidu.png。"""
        refs: list[str] = []
        for match in re.finditer(r"(?i)(?<![A-Za-z0-9_])MEDIA\s*:\s*([^\s\"'<>]+)", text):
            candidate = self._normalize_media_ref(match.group(1))
            if self._is_probable_media_ref(candidate):
                refs.append(candidate)
        return refs

    def _build_media_sources_from_ref(self, ref: str) -> list[dict]:
        """把 MEDIA: 引用扩展为可下载 source 列表（url/path）。"""
        ref = ref.strip()
        if not ref:
            return []
        items: list[dict] = []

        def build_source(value: str, transport: str) -> dict:
            file_name = os.path.basename(urllib.parse.urlparse(value).path) if transport == "url" else os.path.basename(value)
            mime_type = mimetypes.guess_type(file_name)[0] or ""
            media_type = self._infer_media_type(
                node_type="",
                parent_key=transport,
                mime_type=mime_type,
                value_hint=value,
                file_name=file_name,
            )
            return {
                "media_type": media_type,
                "transport": transport,
                "value": value,
                "file_name": file_name,
                "mime_type": mime_type,
                "fingerprint": f"{transport}:{value}",
            }

        if ref.startswith("http://") or ref.startswith("https://"):
            if self._looks_like_remote_url(ref):
                items.append(build_source(ref, "url"))
            return items

        rel_candidates = self._build_media_rel_candidates(ref)
        if not rel_candidates:
            return items

        for safe_rel in rel_candidates:
            for base_dir in self.media_local_dirs:
                candidate = os.path.join(base_dir, safe_rel)
                if os.path.exists(candidate) and os.path.isfile(candidate):
                    items.append(build_source(candidate, "path"))

            for base_url in self.media_url_bases:
                full_url = base_url.rstrip("/") + "/" + safe_rel
                if self._looks_like_remote_url(full_url):
                    items.append(build_source(full_url, "url"))

        return items

    def _looks_like_base64_blob(self, value: str) -> bool:
        text = value.strip()
        if len(text) < 128:
            return False
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r")
        return all(ch in allowed for ch in text[:512])

    def _infer_media_type(
        self,
        *,
        node_type: str,
        parent_key: str,
        mime_type: str,
        value_hint: str,
        file_name: str,
    ) -> str:
        check = " ".join(
            part for part in [node_type.lower(), parent_key.lower(), mime_type.lower()] if part
        )
        if "image" in check or "photo" in check or "sticker" in check:
            return "image"
        if "video" in check:
            return "video"
        if "audio" in check or "voice" in check or "music" in check:
            return "audio"

        hint = (value_hint or file_name or "").strip()
        ext = ""
        if hint:
            hint_base = hint.split("?", 1)[0].split("#", 1)[0]
            if "." in hint_base:
                ext = hint_base.rsplit(".", 1)[-1].lower()

        if ext in {"png", "jpg", "jpeg", "webp", "gif", "bmp"}:
            return "image"
        if ext in {"mp4", "mov", "mkv", "avi", "webm", "3gp"}:
            return "video"
        if ext in {"amr", "wav", "mp3", "m4a", "ogg", "aac"}:
            return "audio"
        return "file"

    def _voice_format_from_source(self, source: dict) -> str:
        file_name = _safe_text(source.get("file_name")).strip()
        mime_type = _safe_text(source.get("mime_type")).strip().lower()
        ext = ""
        if file_name and "." in file_name:
            ext = file_name.rsplit(".", 1)[-1].lower()
        if not ext:
            value = _safe_text(source.get("value")).strip()
            value_base = value.split("?", 1)[0].split("#", 1)[0]
            if "." in value_base:
                ext = value_base.rsplit(".", 1)[-1].lower()
        if ext in {"wav", "mp3", "amr"}:
            return ext
        if "wav" in mime_type:
            return "wav"
        if "mpeg" in mime_type or "mp3" in mime_type:
            return "mp3"
        return "amr"

    def _file_name_from_source(self, source: dict) -> str:
        file_name = _safe_text(source.get("file_name")).strip()
        if file_name:
            return file_name
        value = _safe_text(source.get("value")).strip()
        if value.startswith("http://") or value.startswith("https://"):
            parsed = urllib.parse.urlparse(value)
            name = os.path.basename(parsed.path)
            if name:
                return name
        if os.path.exists(value):
            return os.path.basename(value)
        media_type = _safe_text(source.get("media_type")).strip() or "file"
        mime_type = _safe_text(source.get("mime_type")).strip().lower()
        ext = mimetypes.guess_extension(mime_type) or ""
        ext = ext if ext.startswith(".") else f".{ext}" if ext else ""
        if media_type == "image":
            ext = ext or ".jpg"
        elif media_type == "video":
            ext = ext or ".mp4"
        elif media_type == "audio":
            ext = ext or ".amr"
        return f"gateway_{media_type}{ext}"

    def _is_send_result_failed(self, payload: Any) -> bool:
        if payload is None:
            return False

        if isinstance(payload, tuple):
            return len(payload) >= 3 and all(int(item or 0) == 0 for item in payload[:3])

        if isinstance(payload, list):
            if not payload:
                return False
            return all(self._is_send_result_failed(item) for item in payload)

        if isinstance(payload, dict):
            if "isSendSuccess" in payload and payload.get("isSendSuccess") is not None:
                return payload.get("isSendSuccess") is False
            if "IsSendSuccess" in payload and payload.get("IsSendSuccess") is not None:
                return payload.get("IsSendSuccess") is False
            if "Success" in payload and isinstance(payload.get("Success"), bool):
                return payload.get("Success") is False
            if "success" in payload and isinstance(payload.get("success"), bool):
                return payload.get("success") is False
            if "Data" in payload:
                return self._is_send_result_failed(payload.get("Data"))
            for key in ("List", "list", "MsgItem"):
                if key in payload:
                    return self._is_send_result_failed(payload.get(key))
            return False

        return False

    async def _send_gateway_media_to_wechat(self, route: WatchRoute, source: dict, media_bytes: bytes) -> None:
        if not self.bot:
            return
        media_type = _safe_text(source.get("media_type")).strip().lower()
        file_name = self._file_name_from_source(source)
        logger.info(
            "[Claw] 回传媒体 type={} file={} size={}B to={}",
            media_type or "file",
            file_name,
            len(media_bytes or b""),
            route.to_wxid,
        )
        if media_type == "image":
            try:
                result = await self.bot.send_image_message(route.to_wxid, media_bytes)
                if not self._is_send_result_failed(result):
                    return
                logger.warning("[Claw] 图片回传返回失败，降级为文件发送 result={}", _compact_json(result, 240))
            except Exception as exc:
                logger.warning("[Claw] 图片回传异常，降级为文件发送 error={}", exc)
        if media_type == "video":
            try:
                result = await self.bot.send_video_message(route.to_wxid, media_bytes, b"")
                if not self._is_send_result_failed(result):
                    return
                logger.warning("[Claw] 视频回传返回失败，降级为文件发送 result={}", _compact_json(result, 240))
            except Exception as exc:
                logger.warning("[Claw] 视频回传异常，降级为文件发送 error={}", exc)
        if media_type == "audio":
            voice_format = self._voice_format_from_source(source)
            try:
                await self.bot.send_voice_message(route.to_wxid, media_bytes, format=voice_format)
                return
            except Exception:
                pass
        await self.bot.send_file_message(route.to_wxid, media_bytes, file_name=file_name)

    async def _resolve_openclaw_media_bytes(self, source: dict) -> bytes:
        transport = _safe_text(source.get("transport")).strip().lower()
        value = _safe_text(source.get("value")).strip()
        if not transport or not value:
            return b""

        if transport == "path":
            if not os.path.exists(value):
                return b""
            return open(value, "rb").read()

        if transport == "data_uri":
            prefix, _, b64 = value.partition(",")
            if not b64 or "base64" not in prefix:
                return b""
            try:
                import base64

                data = base64.b64decode(b64)
                return data
            except Exception:
                return b""

        if transport == "base64":
            try:
                import base64

                data = base64.b64decode(value)
                return data
            except Exception:
                return b""

        if transport == "url":
            return await self._download_media_url(value)

        return b""

    async def _download_media_url(self, url: str) -> bytes:
        try:
            import aiohttp
        except Exception as exc:
            raise RuntimeError(f"aiohttp 不可用: {exc}")

        def _looks_like_complete_image(blob: bytes, content_type: str, file_name: str) -> bool:
            if not blob:
                return False
            ct = (content_type or "").split(";", 1)[0].strip().lower()
            name = (file_name or "").strip().lower()

            is_png = ct == "image/png" or name.endswith(".png") or blob.startswith(b"\x89PNG\r\n\x1a\n")
            if is_png:
                return blob.startswith(b"\x89PNG\r\n\x1a\n") and blob.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")

            is_jpeg = ct in {"image/jpeg", "image/jpg"} or name.endswith((".jpg", ".jpeg")) or blob.startswith(b"\xff\xd8")
            if is_jpeg:
                return blob.startswith(b"\xff\xd8") and blob.endswith(b"\xff\xd9")

            is_gif = ct == "image/gif" or name.endswith(".gif") or blob.startswith((b"GIF87a", b"GIF89a"))
            if is_gif:
                return blob.startswith((b"GIF87a", b"GIF89a")) and blob.endswith(b";")

            return True

        retry_statuses = {404, 409, 425, 429, 500, 502, 503, 504}
        delays = (0.0, 0.6, 1.2, 2.4)

        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            last_exc: Optional[BaseException] = None
            for attempt, delay in enumerate(delays, start=1):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    async with session.get(url) as resp:
                        if resp.status in retry_statuses and attempt < len(delays):
                            await resp.release()
                            continue
                        resp.raise_for_status()
                        content = await resp.read()
                        content_length = resp.headers.get("Content-Length")
                        if content_length and content_length.isdigit():
                            expected = int(content_length)
                            if expected > 0 and len(content) < expected and attempt < len(delays):
                                last_exc = RuntimeError(f"下载内容不足(url={url}): got={len(content)} expected={expected}")
                                continue
                        content_type = _safe_text(resp.headers.get("Content-Type")).strip()
                        file_name = os.path.basename(urllib.parse.urlparse(url).path)
                        if not _looks_like_complete_image(content, content_type, file_name) and attempt < len(delays):
                            last_exc = RuntimeError(f"下载媒体疑似未写完(url={url}): len={len(content)} type={content_type or '-'}")
                            continue
                        return content
                except aiohttp.ClientResponseError as exc:
                    last_exc = exc
                    if getattr(exc, "status", None) in retry_statuses and attempt < len(delays):
                        continue
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    last_exc = exc
                    if attempt < len(delays):
                        continue
                    raise RuntimeError(f"下载失败(url={url}): {exc}") from exc

            if last_exc:
                raise RuntimeError(f"下载失败(url={url}): {last_exc}") from last_exc
        return b""

    def _extract_stream_text_update(self, event_name: str, payload: dict) -> tuple[str, str]:
        if not isinstance(payload, dict):
            return "", ""
        if event_name == "agent":
            data = payload.get("data")
            if isinstance(data, dict):
                full_text = _safe_text(data.get("text")).strip()
                if full_text:
                    return "replace", full_text
                delta_text = _safe_text(data.get("delta")).strip()
                if delta_text:
                    return "append", delta_text
        if event_name == "chat":
            message = payload.get("message")
            if isinstance(message, dict):
                role = _safe_text(message.get("role")).strip().lower()
                if role and role not in {"assistant", "bot"}:
                    return "", ""
                content = message.get("content")
                if isinstance(content, list):
                    texts: list[str] = []
                    for segment in content:
                        if isinstance(segment, dict):
                            segment_text = _safe_text(segment.get("text")).strip()
                        else:
                            segment_text = _safe_text(segment).strip()
                        if segment_text:
                            texts.append(segment_text)
                    if texts:
                        return "replace", "".join(texts).strip()
                content_text = _safe_text(content).strip()
                if content_text:
                    return "replace", content_text
        return "", ""

    def _compute_unsent_stream_suffix(self, run_id: str, full_text: str) -> tuple[str, str]:
        current_text = _safe_text(full_text).strip()
        sent_text = self._pending_run_stream_sent_texts.get(run_id, "")
        if not current_text:
            return "", sent_text
        if sent_text and current_text.startswith(sent_text):
            return current_text[len(sent_text):], sent_text
        if sent_text and sent_text.startswith(current_text):
            return "", sent_text
        return current_text, sent_text

    async def _maybe_send_stream_update_to_route(self, run_id: str, route: WatchRoute) -> None:
        full_text = self._pending_run_texts.get(run_id, "")
        suffix, _ = self._compute_unsent_stream_suffix(run_id, full_text)
        if not suffix:
            return

        now = time.time()
        last_sent_at = float(self._pending_run_stream_sent_at.get(run_id) or 0.0)
        # 微信端/869 在短时间内大量小段消息时，存在静默丢段/乱序风险。
        # 因此这里做更强的“字数 + 时间”节流：宁可少发，也不要碎片化刷屏。
        min_flush_chars = 120
        max_flush_interval_seconds = 2.5
        if len(suffix) < 8 and now - last_sent_at < max_flush_interval_seconds:
            return
        should_flush = len(suffix) >= min_flush_chars or now - last_sent_at >= max_flush_interval_seconds
        if not should_flush:
            return

        await self._send_to_route(route, suffix)
        self._pending_run_stream_sent_texts[run_id] = _safe_text(full_text).strip()
        self._pending_run_stream_sent_at[run_id] = now
        meta = self._pending_run_meta.get(run_id) or {}
        flush_count = int(meta.get("streamFlushCount") or 0) + 1
        self._update_pending_run_meta(run_id, streamFlushCount=flush_count)

    async def _send_final_reply_without_duplicate(self, run_id: str, route: WatchRoute, reply_text: str) -> None:
        final_text = _safe_text(reply_text).strip()
        if not final_text:
            return

        meta = self._pending_run_meta.get(run_id) or {}
        flush_count = int(meta.get("streamFlushCount") or 0)
        sent_text = self._pending_run_stream_sent_texts.get(run_id, "")

        # 只要流式阶段曾经回写过（哪怕只有 1 次），微信侧就可能出现丢段/乱序，
        # 为保证最终一致性：终态直接重发完整文本进行“收敛”。
        if sent_text and flush_count >= 1:
            await self._send_to_route(route, final_text)
        else:
            suffix, _ = self._compute_unsent_stream_suffix(run_id, final_text)
            if suffix:
                await self._send_to_route(route, suffix)
        self._pending_run_stream_sent_texts[run_id] = final_text
        self._pending_run_stream_sent_at[run_id] = time.time()

    async def _maybe_send_media_directives_from_text(self, run_id: str, route: WatchRoute, content: str) -> None:
        """保留占位：不再自动下载/回传网关回复中的 MEDIA 指令。"""
        return

    def _is_run_completion_event(self, event_name: str, payload: dict) -> bool:
        if not isinstance(payload, dict):
            return False
        status = _safe_text(payload.get("status")).strip().lower()
        if status in {"ok", "error"}:
            return True
        if event_name == "agent":
            stream = _safe_text(payload.get("stream")).strip().lower()
            if stream == "lifecycle":
                data = payload.get("data")
                if isinstance(data, dict):
                    phase = _safe_text(data.get("phase")).strip().lower()
                    if phase in {"end", "done", "completed", "complete", "stop", "stopped", "failed", "error"}:
                        return True
        if event_name == "chat":
            state = _safe_text(payload.get("state")).strip().lower()
            if state in {"final", "done", "completed", "complete", "failed", "error"}:
                return True
        return False

    async def _send_to_route(self, route: WatchRoute, content: str):
        if not self.bot:
            return
        chunks = self._split_reply_chunks(content)
        if not chunks:
            return

        chunk_total = len(chunks)
        if chunk_total > 1:
            logger.info("[Claw] 分片发送(to_wxid={}): chunks={}", route.to_wxid, chunk_total)

        mentioned = False
        for index, chunk in enumerate(chunks, start=1):
            try:
                if not mentioned and route.is_group and route.sender_wxid and self.event_mention_in_group:
                    mention_text = await self._build_group_mention_text(self.bot, route, chunk)
                    await self.bot.send_text_message(route.to_wxid, mention_text, [route.sender_wxid])
                    mentioned = True
                    if index < chunk_total:
                        await asyncio.sleep(0.25)
                    continue
            except Exception as exc:
                logger.warning("[Claw] 发送@消息失败(to_wxid={}): {}", route.to_wxid, exc)
            try:
                await self.bot.send_text_message(route.to_wxid, chunk)
            except Exception as exc:
                logger.warning("[Claw] 发送文本失败(to_wxid={}): {}", route.to_wxid, exc)
            if index < chunk_total:
                await asyncio.sleep(0.25)

    async def _reply(self, bot: WechatAPIClient, message: dict, content: str):
        route = self._build_route(message)
        if not route:
            return

        chunks = self._split_reply_chunks(content)
        if not chunks:
            return

        chunk_total = len(chunks)
        if chunk_total > 1:
            logger.info("[Claw] reply 分片发送(to_wxid={}): chunks={}", route.to_wxid, chunk_total)

        mentioned = False
        for index, chunk in enumerate(chunks, start=1):
            try:
                if not mentioned and route.is_group and route.sender_wxid:
                    mention_text = await self._build_group_mention_text(bot, route, chunk)
                    await bot.send_text_message(route.to_wxid, mention_text, [route.sender_wxid])
                    mentioned = True
                    if index < chunk_total:
                        await asyncio.sleep(0.25)
                    continue
            except Exception as exc:
                logger.warning("[Claw] reply @发送失败(to_wxid={}): {}", route.to_wxid, exc)
            try:
                await bot.send_text_message(route.to_wxid, chunk)
            except Exception as exc:
                logger.warning("[Claw] reply 文本发送失败(to_wxid={}): {}", route.to_wxid, exc)
            if index < chunk_total:
                await asyncio.sleep(0.25)

    def _build_route(self, message: dict) -> Optional[WatchRoute]:
        to_wxid = _safe_text(message.get("FromWxid")).strip()
        if not to_wxid:
            from_user = message.get("FromUserName")
            to_wxid = _safe_text(from_user).strip()
            if isinstance(from_user, dict):
                to_wxid = _safe_text(from_user.get("string")).strip()
        if not to_wxid:
            return None

        is_group = bool(message.get("IsGroup")) or to_wxid.endswith("@chatroom")
        sender_wxid = self._extract_sender_wxid(message, is_group=is_group)
        sender_name = self._extract_sender_name(message, sender_wxid=sender_wxid, is_group=is_group)
        route_id = to_wxid
        return WatchRoute(
            route_id=route_id,
            to_wxid=to_wxid,
            sender_wxid=sender_wxid,
            sender_name=sender_name,
            is_group=is_group,
        )

    def _extract_sender_wxid(self, message: dict, *, is_group: bool) -> str:
        for key in ("SenderWxid", "ActualUserWxid", "sender_wxid", "actual_user_wxid"):
            value = _safe_text(message.get(key)).strip()
            if value:
                return value

        raw_content = _safe_text(message.get("Content")).strip()
        if is_group:
            for marker in (":\n", ":"):
                if marker not in raw_content:
                    continue
                sender_part, _ = raw_content.split(marker, 1)
                sender_part = sender_part.strip()
                if sender_part and " " not in sender_part and len(sender_part) <= 96:
                    return sender_part

        return ""

    def _extract_sender_name(self, message: dict, *, sender_wxid: str, is_group: bool) -> str:
        candidates = [
            _safe_text(message.get("SenderName")).strip(),
            _safe_text(message.get("sender_name")).strip(),
            _safe_text(message.get("DisplayName")).strip(),
            _safe_text(message.get("display_name")).strip(),
            _safe_text(message.get("NickName")).strip(),
            _safe_text(message.get("nickname")).strip(),
        ]

        push_content = _safe_text(message.get("PushContent")).strip()
        if is_group and push_content:
            for marker in (" : ", ":", "\n"):
                if marker in push_content:
                    prefix, _ = push_content.split(marker, 1)
                    candidates.append(prefix.strip())
                    break

        for candidate in candidates:
            if candidate and not self._looks_like_wxid_text(candidate, wxid=sender_wxid):
                return candidate
        return ""

    def _extract_message_content(self, message: dict) -> str:
        content = _safe_text(message.get("Content")).replace("\u2005", " ").strip()
        if ":\n" in content and (
            bool(message.get("IsGroup")) or _safe_text(message.get("FromWxid")).endswith("@chatroom")
        ):
            _, content = content.split(":\n", 1)
            content = content.strip()
        return content

    def _strip_leading_mentions(self, content: str) -> str:
        text = content.strip()
        while text.startswith("@"):
            _, _, rest = text.partition(" ")
            if not rest.strip():
                return ""
            text = rest.strip()
        return text

    def _extract_user_text(self, message: dict, *, strip_at_prefix: bool) -> str:
        msg_type = int(message.get("MsgType") or 0)
        if msg_type == 3:
            return ""
        text = self._extract_message_content(message)
        if msg_type == 49 and text.lstrip().startswith("<"):
            return ""
        if strip_at_prefix:
            text = self._strip_leading_mentions(text)
        return text.strip()

    def _match_trigger(self, text: str) -> Optional[TriggerMatch]:
        content = _safe_text(text).strip()
        if not content:
            return None

        match_mode = self.trigger_match_mode or "prefix"
        if match_mode not in {"prefix", "contains", "exact"}:
            match_mode = "prefix"

        for word in self.trigger_keys:
            trigger = _safe_text(word).strip()
            if not trigger:
                continue

            if match_mode == "exact" and content == trigger:
                return TriggerMatch(word=trigger, mode=match_mode)
            if match_mode == "contains" and trigger in content:
                return TriggerMatch(word=trigger, mode=match_mode)
            if match_mode == "prefix" and content.startswith(trigger):
                return TriggerMatch(word=trigger, mode=match_mode)

        return None

    def _is_method_help_query(self, text: str) -> bool:
        if not self.method_help_keywords:
            return False
        lowered = _safe_text(text).strip().lower()
        if not lowered:
            return False
        for keyword in self.method_help_keywords:
            if keyword and keyword in lowered:
                return True
        return False

    def _describe_openclaw_method(self, method_name: str) -> str:
        name = _safe_text(method_name).strip().lower()
        exact_map = {
            "health": "查询网关健康状态与默认 agent。",
            "help": "获取网关帮助信息。",
            "agent": "调用默认/指定 agent 进行对话。",
            "chat.send": "向指定会话发送消息。",
            "chat.history": "查询会话历史消息。",
            "operator.info": "查询当前 operator 身份信息。",
            "operator.keys.list": "列出授权码/密钥。",
            "operator.keys.create": "创建新的授权码/密钥。",
            "operator.keys.delete": "删除授权码/密钥。",
            "device.pair.request": "发起设备配对请求。",
            "device.pair.confirm": "确认设备配对。",
            "device.pair.reject": "拒绝设备配对。",
        }
        if name in exact_map:
            return exact_map[name]

        prefix_map = [
            ("chat.", "会话相关方法（发送、历史等）。"),
            ("agent.", "Agent 相关方法（会话、配置、执行等）。"),
            ("operator.", "Operator 管理方法（权限、密钥、账号等）。"),
            ("device.", "设备管理方法（配对、认证、状态等）。"),
            ("auth.", "认证授权方法（登录、校验、刷新等）。"),
            ("media.", "媒体方法（上传、下载、资源处理）。"),
            ("file.", "文件方法（上传、下载、管理）。"),
            ("tool.", "工具类方法（辅助能力调用）。"),
        ]
        for prefix, desc in prefix_map:
            if name.startswith(prefix):
                return desc
        return "网关方法（参数请按 OpenClaw 文档/返回提示）。"

    def _format_method_help(self) -> str:
        lines: list[str] = [
            "龙虾常用命令：",
            "- /new：开启新会话，清空当前聊天对象的上下文",
            "- /reset：重置当前会话，效果与 /new 接近",
            "- /model：查看或切换当前会话模型",
            "- /think <level>：设置思考强度，例如 low/medium/high",
            "- /verbose <level>：调整输出详细程度",
            "- /reasoning <level>：调整推理强度",
            "- /compact：压缩当前会话上下文，减少历史占用",
            "- /status：查看当前状态",
            "- /help：查看 OpenClaw 内置帮助",
            "- /stop：停止当前正在进行的回复",
            "",
            "使用方式：",
            "- 私聊直接发送：/命令",
            "- 群聊管理员命令：@机器人 龙虾 /命令",
            "- 对话帮助：发送“龙虾 帮助”或“龙虾 命令”",
            "- 普通提问：发送“龙虾 你的问题”",
            "",
            "说明：",
            "- 上述命令默认作用于当前聊天对象对应的会话",
            "- 私聊按对方 wxid 记忆上下文，群聊按群 id 记忆上下文",
            "- 群聊中的 /命令 不会再直接透传；只有管理员同时满足 @机器人 + 唤醒词 才会执行",
        ]
        return "\n".join(lines).strip()

    def _extract_article_meta(self, message: dict) -> dict:
        xml_text = _safe_text(message.get("Content")).strip()
        if not xml_text or not xml_text.lstrip().startswith("<"):
            return {}
        try:
            import xml.etree.ElementTree as ET

            root = ET.fromstring(xml_text)
            appmsg = root.find("appmsg")
            if appmsg is None:
                return {}
            type_element = appmsg.find("type")
            if type_element is None:
                return {}
            if int(type_element.text or "0") != 5:
                return {}
            title = _safe_text(appmsg.findtext("title")).strip()
            url = _safe_text(appmsg.findtext("url")).strip()
            description = _safe_text(appmsg.findtext("des")).strip()
            thumburl = _safe_text(appmsg.findtext("thumburl")).strip()
            return {"title": title, "url": url, "description": description, "thumburl": thumburl}
        except Exception:
            return {}

    def _format_article_prompt(self, message: dict) -> str:
        meta = self._extract_article_meta(message)
        if not meta:
            return ""
        title = _safe_text(meta.get("title")).strip()
        url = _safe_text(meta.get("url")).strip()
        description = _safe_text(meta.get("description")).strip()
        thumburl = _safe_text(meta.get("thumburl")).strip()

        first_line_parts = ["[链接文章]"]
        if title:
            first_line_parts.append(title)
        if url:
            first_line_parts.append(url)

        lines = [" ".join(first_line_parts).strip()]
        if description:
            lines.append(f"- 描述: {description}")
        if thumburl:
            lines.append(f"- 封面: {thumburl}")
        return "\n".join(lines).strip()

    def _build_openclaw_prompt(
        self,
        message: dict,
        *,
        user_text: str,
        use_gateway_attachments: bool = False,
    ) -> str:
        msg_type = int(message.get("MsgType") or 0)
        route = self._build_route(message)
        identity_header = self._format_gateway_identity_header(message, route)
        if msg_type == 3:
            image_payload = self._format_image_attachment_prompt(message)
            return f"{identity_header}\n{image_payload}".strip() if identity_header else image_payload
        if msg_type in {34, 43}:
            media_label = "语音" if msg_type == 34 else "视频"
            media_payload = (
                self._format_binary_media_attachment_prompt(message, media_label=media_label)
                if use_gateway_attachments
                else self._format_binary_media_prompt(message, media_label=media_label)
            )
            return f"{identity_header}\n{media_payload}".strip() if identity_header else media_payload
        prompt = user_text.strip()
        if msg_type == 49:
            file_prompt = (
                self._format_file_attachment_prompt(message)
                if use_gateway_attachments
                else self._format_file_prompt(message)
            )
            if file_prompt:
                prompt = f"{prompt}\n\n{file_prompt}".strip() if prompt else file_prompt
                return f"{identity_header}\n{prompt}".strip() if identity_header else prompt
            article_prompt = self._format_article_prompt(message)
            if article_prompt:
                prompt = f"{prompt}\n\n{article_prompt}".strip() if prompt else article_prompt
        quote = message.get("Quote")
        if self.quote_include_enable and isinstance(quote, dict):
            prompt = self._append_quote_context(prompt, quote)
        return f"{identity_header}\n{prompt}".strip() if identity_header else prompt

    def _format_gateway_identity_header(self, message: dict, route: Optional[WatchRoute]) -> str:
        """给网关的 prompt 附带来源标识，便于网关侧识别会话与发送者。"""
        if not route:
            return ""

        sender_wxid = route.sender_wxid or ""
        sender_name = self._extract_sender_name(message, sender_wxid=sender_wxid, is_group=route.is_group)
        if not sender_name and route.sender_name:
            sender_name = route.sender_name
        if not sender_name and sender_wxid:
            sender_name = self._lookup_contact_display(sender_wxid)
        chat_name = self._lookup_contact_display(route.to_wxid) if route.to_wxid else ""
        msg_id = _safe_text(message.get("MsgId")).strip()

        lines: list[str] = [
            "[WeChatRoute]",
            f"- chat_id: {route.to_wxid}",
            f"- is_group: {route.is_group}",
        ]
        if chat_name:
            lines.append(f"- chat_name: {chat_name}")
        if sender_wxid:
            lines.append(f"- sender_wxid: {sender_wxid}")
        if sender_name:
            lines.append(f"- sender_name: {sender_name}")
        if msg_id:
            lines.append(f"- msg_id: {msg_id}")
        return "\n".join(lines)

    def _lookup_contact_display(self, wxid: str) -> str:
        wxid = _safe_text(wxid).strip()
        if not wxid:
            return ""
        try:
            from database.contacts_db import get_contact_from_db

            contact = get_contact_from_db(wxid)
            if not isinstance(contact, dict):
                return ""
            remark = _safe_text(contact.get("remark")).strip()
            nickname = _safe_text(contact.get("nickname")).strip()
            display = remark or nickname
            return "" if self._looks_like_wxid_text(display, wxid=wxid) else display
        except Exception:
            return ""

    def _looks_like_wxid_text(self, text: str, *, wxid: str = "") -> bool:
        value = _safe_text(text).strip()
        wxid_text = _safe_text(wxid).strip()
        if not value:
            return True
        lowered = value.lower()
        if wxid_text and value == wxid_text:
            return True
        if lowered.startswith("wxid_"):
            return True
        if lowered.endswith("@chatroom"):
            return True
        if re.fullmatch(r"[A-Za-z0-9_@.-]{12,}", value):
            return True
        return False

    def _lookup_group_member_display(self, bot: WechatAPIClient, route: WatchRoute) -> str:
        sender_wxid = _safe_text(route.sender_wxid).strip()
        if not route.is_group or not sender_wxid:
            return ""
        if route.sender_name and not self._looks_like_wxid_text(route.sender_name, wxid=sender_wxid):
            return route.sender_name

        getter = getattr(bot, "get_local_nickname", None)
        if callable(getter):
            try:
                nickname = _safe_text(getter(sender_wxid, route.to_wxid)).strip()
                if nickname and not self._looks_like_wxid_text(nickname, wxid=sender_wxid):
                    return nickname
            except Exception:
                pass
        return self._lookup_contact_display(sender_wxid)

    async def _build_group_mention_text(self, bot: WechatAPIClient, route: WatchRoute, content: str) -> str:
        sender_wxid = _safe_text(route.sender_wxid).strip()
        if not route.is_group or not sender_wxid:
            return content

        nickname = self._lookup_group_member_display(bot, route)
        if not nickname:
            try:
                fetched = await bot.get_nickname(sender_wxid)
            except Exception:
                fetched = ""
            nickname = _safe_text(fetched).strip()
        if self._looks_like_wxid_text(nickname, wxid=sender_wxid):
            return content
        return f"@{nickname}\u2005{content}"

    def _extract_md5_from_img_xml(self, xml_text: str) -> str:
        raw = _safe_text(xml_text).strip()
        if not raw:
            return ""
        try:
            import html
            import xml.etree.ElementTree as ET

            unescaped = html.unescape(raw)
            root = ET.fromstring(unescaped)
            img = root.find("img")
            if img is None:
                return ""
            return (_safe_text(img.get("md5")) or "").strip()
        except Exception:
            match = re.search(r'md5="([^"]+)"', raw)
            return (match.group(1) if match else "").strip()

    async def _ensure_image_base64(self, bot: WechatAPIClient, message: dict) -> None:
        raw = _safe_text(message.get("Content")).strip()
        if self._is_probably_base64(raw):
            return
        if raw.startswith("<?xml") or raw.startswith("<msg"):
            aeskey, file_nos = self._extract_image_cdn_info_from_xml(raw)
            if not aeskey or not file_nos:
                return
            for file_no in file_nos:
                try:
                    image_bytes = await bot.get_msg_image(aeskey, file_no)
                except Exception:
                    image_bytes = b""
                if image_bytes:
                    import base64

                    message["Content"] = base64.b64encode(image_bytes).decode("utf-8")
                    return
        fallback_path = self._find_existing_image_path(message)
        if fallback_path:
            try:
                file_size = os.path.getsize(fallback_path)
                if file_size > 256:
                    import base64

                    with open(fallback_path, "rb") as f:
                        message["Content"] = base64.b64encode(f.read()).decode("utf-8")
            except Exception:
                pass

    def _extract_image_cdn_info_from_xml(self, xml_text: str) -> tuple[str, list[str]]:
        try:
            import xml.etree.ElementTree as ET
        except Exception:
            return "", []
        try:
            root = ET.fromstring(xml_text)
        except Exception:
            return "", []
        img = root.find("img")
        if img is None:
            return "", []
        aeskey = (img.get("aeskey") or "").strip()
        candidates = [
            (img.get("cdnbigimgurl") or "").strip(),
            (img.get("cdnmidimgurl") or "").strip(),
            (img.get("cdnthumburl") or "").strip(),
        ]
        file_nos = [value for value in candidates if value]
        return aeskey, file_nos

    def _format_image_prompt(self, message: dict) -> str:
        md5_value = _safe_text(message.get("ImageMD5")).strip()
        image_path = _safe_text(message.get("ImagePath")).strip()
        local_path = self._find_existing_image_path(message)
        if local_path and not image_path:
            image_path = local_path
        if not image_path and md5_value:
            image_path = f"/app/files/{md5_value}.jpg"
        gateway_path = ""
        if self.image_path_forward_enable and image_path:
            gateway_path = self._map_path_for_gateway(image_path)
        else:
            gateway_path = image_path
        raw_content = _safe_text(message.get("Content")).strip()
        base64_payload = raw_content
        if raw_content.startswith("<?xml") or raw_content.startswith("<msg"):
            base64_payload = ""
        approx_bytes = int(len(base64_payload) * 3 / 4) if base64_payload else 0

        parts = ["[图片] 已接收"]
        if md5_value:
            parts.append(f"md5={md5_value}")
        if gateway_path:
            parts.append(f"path={gateway_path}")
        if approx_bytes:
            parts.append(f"bytes≈{approx_bytes}")
        media_directive = self._build_gateway_media_directive(gateway_path=gateway_path)

        if self.image_forward_mode == "base64" and base64_payload:
            data_uri_prefix = "data:image;base64,"
            max_chars = self.image_base64_max_chars
            path_block = f"\n\n[图片路径] {gateway_path}" if (self.image_path_forward_enable and gateway_path) else ""
            directive_block = f"\n{media_directive}" if media_directive else ""
            if max_chars <= 0:
                return f"{' '.join(parts)}{directive_block}\n\n[图片] {data_uri_prefix}{base64_payload}{path_block}"
            preview = base64_payload[:max_chars]
            suffix = "" if len(base64_payload) <= max_chars else "...(已截断)"
            return f"{' '.join(parts)}{directive_block}\n\n[图片] {data_uri_prefix}{preview}{suffix}{path_block}"

        if self.image_path_forward_enable and image_path:
            if self.image_forward_mode == "path":
                directive_block = f"\n{media_directive}" if media_directive else ""
                return f"{' '.join(parts)}{directive_block}\n\n[图片路径] {gateway_path}"
            directive_block = f"\n{media_directive}" if media_directive else ""
            return f"{' '.join(parts)}{directive_block}\n\n[图片路径] {gateway_path}"
        if media_directive:
            return f"{' '.join(parts)}\n{media_directive}"
        return " ".join(parts)

    def _format_image_attachment_prompt(self, message: dict) -> str:
        md5_value = _safe_text(message.get("ImageMD5")).strip()
        raw_content = _safe_text(message.get("Content")).strip()
        approx_bytes = 0
        if raw_content and not (raw_content.startswith("<?xml") or raw_content.startswith("<msg")):
            try:
                approx_bytes = len(base64.b64decode(raw_content, validate=False))
            except Exception:
                approx_bytes = 0
        parts = ["[图片] 已接收"]
        if md5_value:
            parts.append(f"md5={md5_value}")
        if approx_bytes:
            parts.append(f"bytes={approx_bytes}")
        return " ".join(parts).strip()

    def _build_gateway_attachments(self, message: dict) -> list[dict[str, Any]]:
        msg_type = int(message.get("MsgType") or 0)
        if msg_type == 3:
            payload = self._extract_image_attachment_payload(message)
            if not payload:
                return []
            mime_type = self._guess_image_attachment_mime_type(message, payload)
            file_name = self._guess_image_attachment_file_name(message, mime_type)
            return [self._build_gateway_attachment(type_name="image", mime_type=mime_type, file_name=file_name, payload=payload)]
        return []

    def _build_binary_gateway_attachments(self, message: dict, *, media_type: str) -> list[dict[str, Any]]:
        payload = self._extract_binary_attachment_payload(message)
        if not payload:
            return []

        mime_type = self._guess_binary_attachment_mime_type(message, payload, media_type=media_type)
        file_name = self._guess_binary_attachment_file_name(message, mime_type, media_type=media_type)
        return [
            self._build_gateway_attachment(
                type_name=media_type,
                mime_type=mime_type,
                file_name=file_name,
                payload=payload,
            )
        ]

    def _build_gateway_attachment(
        self,
        *,
        type_name: str,
        mime_type: str,
        file_name: str,
        payload: str,
    ) -> dict[str, Any]:
        return {
            "type": type_name,
            "mimeType": mime_type,
            "fileName": file_name,
            "content": payload,
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": payload,
            },
        }

    def _extract_image_attachment_payload(self, message: dict) -> str:
        raw_content = _safe_text(message.get("Content")).strip()
        if self._is_probably_base64(raw_content):
            return raw_content

        image_path = self._find_existing_image_path(message)
        if image_path and os.path.isfile(image_path):
            try:
                with open(image_path, "rb") as f:
                    return base64.b64encode(f.read()).decode("utf-8")
            except Exception:
                return ""
        return ""

    def _guess_image_attachment_mime_type(self, message: dict, payload_base64: str) -> str:
        image_path = _safe_text(message.get("ImagePath")).strip()
        guessed_from_path, _encoding = mimetypes.guess_type(image_path)
        if guessed_from_path and guessed_from_path.startswith("image/"):
            return guessed_from_path
        try:
            header = base64.b64decode(payload_base64[:128], validate=False)
        except Exception:
            header = b""
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if header.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if header.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if header.startswith(b"RIFF") and b"WEBP" in header[:16]:
            return "image/webp"
        return "image/jpeg"

    def _guess_image_attachment_file_name(self, message: dict, mime_type: str) -> str:
        image_path = _safe_text(message.get("ImagePath")).strip()
        base_name = os.path.basename(image_path) if image_path else ""
        if base_name:
            return base_name
        md5_value = _safe_text(message.get("ImageMD5")).strip().lower()
        extension = mimetypes.guess_extension(mime_type) or ".jpg"
        stem = md5_value or (_safe_text(message.get("MsgId")).strip() or uuid.uuid4().hex[:12])
        return f"{stem}{extension}"

    def _extract_binary_attachment_payload(self, message: dict) -> str:
        payload = self._extract_inbound_media_payload(message)
        if payload:
            return base64.b64encode(payload).decode("utf-8")

        local_path = self._resolve_media_local_path(message)
        if local_path and os.path.isfile(local_path):
            try:
                with open(local_path, "rb") as f:
                    return base64.b64encode(f.read()).decode("utf-8")
            except Exception:
                return ""
        return ""

    def _guess_binary_attachment_mime_type(self, message: dict, payload_base64: str, *, media_type: str) -> str:
        try:
            header = base64.b64decode(payload_base64[:256], validate=False)
        except Exception:
            header = b""

        if media_type == "audio":
            if header.startswith(b"RIFF"):
                return "audio/wav"
            if header.startswith(b"#!SILK_V3"):
                return "audio/silk"
            if header.startswith(b"ID3") or (len(header) >= 2 and header[:2] == b"\xff\xfb"):
                return "audio/mpeg"
            file_name = _safe_text(message.get("FileName") or message.get("Filename")).strip()
            local_path = self._resolve_media_local_path(message)
            guessed_from_name, _encoding = mimetypes.guess_type(file_name or local_path or "")
            return guessed_from_name or "application/octet-stream"

        if media_type == "video":
            if len(header) >= 12 and header[4:8] == b"ftyp":
                return "video/mp4"
            file_name = _safe_text(message.get("FileName") or message.get("Filename")).strip()
            local_path = self._resolve_media_local_path(message)
            guessed_from_name, _encoding = mimetypes.guess_type(file_name or local_path or "")
            return guessed_from_name or "application/octet-stream"

        file_name = _safe_text(message.get("FileName") or message.get("Filename")).strip()
        local_path = self._resolve_media_local_path(message)
        guessed_from_name, _encoding = mimetypes.guess_type(file_name or local_path or "")
        if guessed_from_name:
            return guessed_from_name
        if header.startswith(b"%PDF-"):
            return "application/pdf"
        if header.startswith((b"{", b"[")):
            return "application/json"
        return "application/octet-stream"

    def _guess_binary_attachment_file_name(self, message: dict, mime_type: str, *, media_type: str) -> str:
        local_path = self._resolve_media_local_path(message)
        source_name = _safe_text(message.get("FileName") or message.get("Filename")).strip()
        if not source_name and local_path:
            source_name = os.path.basename(local_path)
        if source_name:
            return self._sanitize_media_filename(
                source_name,
                fallback_stem=f"{media_type}-{_safe_text(message.get('MsgId')).strip() or 'media'}",
            )

        md5_value = _safe_text(
            message.get("md5") or message.get("FileMd5") or message.get("ImageMD5")
        ).strip().lower()
        extension = mimetypes.guess_extension(mime_type) or {
            "audio": ".bin",
            "video": ".bin",
            "file": ".bin",
        }.get(media_type, ".bin")
        stem = md5_value or (_safe_text(message.get("MsgId")).strip() or uuid.uuid4().hex[:12])
        return f"{stem}{extension}"

    def _build_gateway_media_directive(self, *, gateway_path: str = "", public_url: str = "") -> str:
        candidate = _safe_text(gateway_path).strip()
        if not candidate:
            return ""
        return f"MEDIA:{candidate}"

    async def _ensure_media_local_path(self, message: dict) -> str:
        local_path = self._resolve_media_local_path(message)
        if local_path:
            return local_path

        payload = self._extract_inbound_media_payload(message)
        if not payload:
            return ""

        file_path = self._persist_inbound_media_payload(message, payload)
        if not file_path:
            return ""

        msg_type = int(message.get("MsgType") or 0)
        message["ResourcePath"] = file_path
        if msg_type == 3:
            message["ImagePath"] = file_path
        elif msg_type == 34:
            message["voice_path"] = file_path
        elif msg_type == 43:
            message["video_path"] = file_path
        elif msg_type == 49:
            message["FilePath"] = file_path
        return file_path

    def _extract_inbound_media_payload(self, message: dict) -> bytes:
        msg_type = int(message.get("MsgType") or 0)
        candidates: list[Any] = []
        if msg_type == 3:
            candidates.append(message.get("Content"))
        elif msg_type == 34:
            candidates.append(message.get("Content"))
        elif msg_type == 43:
            candidates.append(message.get("Video"))
        elif msg_type == 49:
            candidates.append(message.get("File"))

        for candidate in candidates:
            payload = self._coerce_media_payload_bytes(candidate)
            if payload:
                return payload
        return b""

    def _coerce_media_payload_bytes(self, payload: Any) -> bytes:
        if isinstance(payload, memoryview):
            return payload.tobytes()
        if isinstance(payload, bytearray):
            return bytes(payload)
        if isinstance(payload, bytes):
            return payload

        raw = _safe_text(payload).strip()
        if not raw:
            return b""
        if raw.startswith("<?xml") or raw.startswith("<msg"):
            return b""
        if raw.startswith("data:") and ";base64," in raw:
            raw = raw.split(";base64,", 1)[1].strip()
        try:
            return base64.b64decode(raw, validate=False)
        except Exception:
            return b""

    def _persist_inbound_media_payload(self, message: dict, payload: bytes) -> str:
        if not payload:
            return ""

        target_dir = self._get_gateway_media_store_dir()
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception as exc:
            logger.warning("[Claw] 创建媒体落盘目录失败 dir={} error={}", target_dir, exc)
            return ""

        file_name = self._build_inbound_media_file_name(message, payload)
        if not file_name:
            return ""

        file_path = os.path.join(target_dir, file_name)
        try:
            if not os.path.isfile(file_path):
                with open(file_path, "wb") as f:
                    f.write(payload)
            return file_path
        except Exception as exc:
            logger.warning("[Claw] 媒体落盘失败 path={} error={}", file_path, exc)
            return ""

    def _get_gateway_media_store_dir(self) -> str:
        root = "/app" if os.path.isdir("/app") else os.getcwd()
        return os.path.join(root, "files", "claw-media")

    def _build_inbound_media_file_name(self, message: dict, payload: bytes) -> str:
        msg_type = int(message.get("MsgType") or 0)
        md5_value = _safe_text(message.get("md5") or message.get("FileMd5") or message.get("ImageMD5")).strip().lower()
        msg_id = _safe_text(message.get("MsgId")).strip()

        if msg_type == 49:
            file_name = _safe_text(message.get("FileName") or message.get("Filename")).strip()
            file_ext = _safe_text(message.get("FileExtend")).strip().lstrip(".")
            if file_name and not os.path.splitext(file_name)[1] and file_ext:
                file_name = f"{file_name}.{file_ext}"
            suffix = os.path.splitext(file_name)[1] if file_name else ""
            if not suffix and file_ext:
                suffix = f".{file_ext}"
            if not suffix:
                suffix = ".bin"
            if md5_value:
                return f"{md5_value}{suffix.lower()}"
            if file_name:
                return self._sanitize_media_filename(file_name, fallback_stem=f"file-{msg_id or 'media'}")
            return f"file-{msg_id or hashlib.sha256(payload).hexdigest()[:16]}{suffix.lower()}"

        if msg_type == 34:
            stem = md5_value or msg_id or hashlib.sha256(payload).hexdigest()[:16]
            return f"{stem}.wav"

        if msg_type == 43:
            stem = md5_value or msg_id or hashlib.sha256(payload).hexdigest()[:16]
            return f"{stem}.mp4"

        if msg_type == 3:
            stem = md5_value or msg_id or hashlib.sha256(payload).hexdigest()[:16]
            return f"{stem}.jpg"

        return ""

    def _sanitize_media_filename(self, file_name: str, *, fallback_stem: str) -> str:
        safe_name = os.path.basename(_safe_text(file_name).strip())
        safe_name = re.sub(r'[\\/*?:"<>|]+', "_", safe_name).strip(" .")
        if not safe_name:
            safe_name = fallback_stem
        stem, suffix = os.path.splitext(safe_name)
        stem = stem[:160] or fallback_stem
        suffix = suffix[:32]
        return f"{stem}{suffix}"

    def _resolve_media_local_path(self, message: dict) -> str:
        for key in ("ResourcePath", "FilePath", "ImagePath", "video_path", "voice_path"):
            candidate = _safe_text(message.get(key)).strip()
            if candidate and os.path.isfile(candidate):
                return candidate

        content_xml = _safe_text(message.get("Content")).strip()
        resource_path = self._extract_resource_path_from_media_xml(content_xml)
        if resource_path and os.path.isfile(resource_path):
            return resource_path

        md5_value = _safe_text(message.get("md5") or message.get("ImageMD5")).strip().lower()
        file_name = _safe_text(message.get("FileName") or message.get("Filename")).strip()
        return self._find_existing_file_path(md5_value=md5_value, file_name=file_name)

    def _format_binary_media_prompt(self, message: dict, *, media_label: str) -> str:
        local_path = self._resolve_media_local_path(message)
        file_name = _safe_text(message.get("FileName") or message.get("Filename")).strip()
        if not file_name and local_path:
            file_name = os.path.basename(local_path)
        md5_value = _safe_text(message.get("md5") or message.get("ImageMD5")).strip().lower()
        public_url = self._build_public_media_url(local_path, md5_value=md5_value, file_name=file_name)
        parts = [f"[{media_label}] 已接收"]
        if file_name:
            parts.append(file_name)
        if md5_value:
            parts.append(f"md5={md5_value}")
        if public_url:
            parts.append(f"url={public_url}")

        blocks = [" ".join(parts).strip()]
        if public_url:
            blocks.append(f"[{media_label}链接] {public_url}")
        return "\n\n".join(blocks).strip()

    def _format_binary_media_attachment_prompt(self, message: dict, *, media_label: str) -> str:
        local_path = self._resolve_media_local_path(message)
        file_name = _safe_text(message.get("FileName") or message.get("Filename")).strip()
        if not file_name and local_path:
            file_name = os.path.basename(local_path)
        md5_value = _safe_text(message.get("md5") or message.get("ImageMD5")).strip().lower()
        file_size = ""
        if local_path and os.path.isfile(local_path):
            try:
                file_size = str(os.path.getsize(local_path))
            except Exception:
                file_size = ""

        parts = [f"[{media_label}] 已接收"]
        if file_name:
            parts.append(file_name)
        if file_size:
            parts.append(f"bytes={file_size}")
        if md5_value:
            parts.append(f"md5={md5_value}")
        return " ".join(parts).strip()

    def _format_file_prompt(self, message: dict) -> str:
        file_name = _safe_text(message.get("FileName") or message.get("Filename")).strip()
        file_size = _safe_text(message.get("FileSize")).strip()
        md5_value = _safe_text(message.get("md5") or message.get("FileMd5")).strip().lower()
        local_path = self._resolve_media_local_path(message)
        if not file_name and local_path:
            file_name = os.path.basename(local_path)
        if not (file_name or local_path or md5_value):
            return ""

        public_url = self._build_public_media_url(local_path, md5_value=md5_value, file_name=file_name)
        parts = ["[文件] 已接收"]
        if file_name:
            parts.append(file_name)
        if file_size:
            parts.append(f"size={file_size}")
        if md5_value:
            parts.append(f"md5={md5_value}")
        if public_url:
            parts.append(f"url={public_url}")

        blocks = [" ".join(parts).strip()]
        if public_url:
            blocks.append(f"[文件链接] {public_url}")
        return "\n\n".join(blocks).strip()

    def _format_file_attachment_prompt(self, message: dict) -> str:
        file_name = _safe_text(message.get("FileName") or message.get("Filename")).strip()
        file_size = _safe_text(message.get("FileSize")).strip()
        md5_value = _safe_text(message.get("md5") or message.get("FileMd5")).strip().lower()
        local_path = self._resolve_media_local_path(message)
        if not file_name and local_path:
            file_name = os.path.basename(local_path)
        if not file_size and local_path and os.path.isfile(local_path):
            try:
                file_size = str(os.path.getsize(local_path))
            except Exception:
                file_size = ""
        if not (file_name or local_path or md5_value):
            return ""

        parts = ["[文件] 已接收"]
        if file_name:
            parts.append(file_name)
        if file_size:
            parts.append(f"size={file_size}")
        if md5_value:
            parts.append(f"md5={md5_value}")
        return " ".join(parts).strip()

    def _find_existing_image_path(self, message: dict) -> str:
        image_path = _safe_text(message.get("ImagePath")).strip()
        if image_path and os.path.exists(image_path):
            return image_path

        md5_value = _safe_text(message.get("ImageMD5")).strip()
        if not md5_value:
            return ""

        roots = [os.getcwd(), "/app"]
        candidates: list[str] = []
        for root in roots:
            pattern = os.path.join(root, "files", f"{md5_value}.*")
            candidates.extend(glob.glob(pattern))

        existing = [path for path in candidates if os.path.isfile(path)]
        if not existing:
            return ""
        existing.sort(key=lambda path: os.path.getsize(path), reverse=True)
        return existing[0]

    def _map_path_for_gateway(self, path: str) -> str:
        normalized = path
        if self.image_host_path_prefix and normalized.startswith("/app"):
            suffix = normalized[len("/app") :]
            return f"{self.image_host_path_prefix}{suffix}"
        return normalized

    def _build_public_media_url(self, path: str, *, md5_value: str = "", file_name: str = "") -> str:
        base_url = self.image_public_base_url
        if not base_url:
            return ""
        resolved_name = os.path.basename(path.strip()) if path else ""
        if not resolved_name:
            resolved_name = os.path.basename(_safe_text(file_name).strip()) if file_name else ""
        if not resolved_name and md5_value:
            resolved_name = f"{md5_value}.jpg"
        if not resolved_name:
            return ""
        encoded_name = urllib.parse.quote(resolved_name)
        route = self.image_public_route_prefix.rstrip("/") or "/files"
        return f"{base_url}{route}/{encoded_name}"

    def _is_probably_base64(self, value: str) -> bool:
        if not value:
            return False
        if value.startswith("<?xml") or value.startswith("<msg"):
            return False
        if len(value) < 64:
            return False
        allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r"
        for ch in value[:512]:
            if ch not in allowed:
                return False
        return True

    def _extract_md5_from_media_xml(self, xml_text: str) -> str:
        raw = _safe_text(xml_text).strip()
        if not raw:
            return ""
        try:
            import html

            raw = html.unescape(raw)
        except Exception:
            pass

        match = re.search(r'\\bmd5="([0-9a-fA-F]{16,64})"', raw)
        if match:
            return match.group(1).lower()

        match = re.search(r"<md5>([^<]+)</md5>", raw, re.IGNORECASE)
        if match:
            return _safe_text(match.group(1)).strip().lower()
        return ""

    def _extract_resource_path_from_media_xml(self, xml_text: str) -> str:
        raw = _safe_text(xml_text).strip()
        if not raw:
            return ""
        try:
            import html

            raw = html.unescape(raw)
        except Exception:
            pass

        for key in (
            "resource_path",
            "resourcepath",
            "filepath",
            "file_path",
            "fullpath",
            "videopath",
            "video_path",
            "voicepath",
            "voice_path",
        ):
            match = re.search(rf'\\b{re.escape(key)}="([^"]+)"', raw, re.IGNORECASE)
            if match:
                return _safe_text(match.group(1)).strip()
        return ""

    def _find_existing_file_path(self, *, md5_value: str = "", file_name: str = "") -> str:
        roots = [os.getcwd(), "/app"]
        safe_name = os.path.basename(_safe_text(file_name).strip()) if file_name else ""
        candidates: list[str] = []
        for root in roots:
            if safe_name:
                candidate = os.path.join(root, "files", safe_name)
                if os.path.isfile(candidate):
                    return candidate
                nested_pattern = os.path.join(root, "files", "**", safe_name)
                candidates.extend(glob.glob(nested_pattern, recursive=True))

        md5_value = _safe_text(md5_value).strip()
        if not md5_value:
            existing = [path for path in candidates if os.path.isfile(path)]
            if not existing:
                return ""
            existing.sort(key=lambda path: (os.path.getmtime(path), os.path.getsize(path)), reverse=True)
            return existing[0]
        for root in roots:
            pattern = os.path.join(root, "files", f"{md5_value}.*")
            nested_pattern = os.path.join(root, "files", "**", f"{md5_value}.*")
            candidates.extend(glob.glob(pattern))
            candidates.extend(glob.glob(nested_pattern, recursive=True))
        existing = [path for path in candidates if os.path.isfile(path)]
        if not existing:
            return ""
        existing.sort(key=lambda path: (os.path.getmtime(path), os.path.getsize(path)), reverse=True)
        return existing[0]

    def _append_quote_context(self, prompt: str, quote: dict) -> str:
        quoted_type = quote.get("MsgType")
        try:
            quoted_type = int(quoted_type) if quoted_type is not None else quoted_type
        except Exception:
            pass
        quoted_sender = _safe_text(quote.get("Nickname") or quote.get("sourcedisplayname")).strip()
        quoted_content = _safe_text(quote.get("Content")).strip()
        if quoted_type == 3:
            quote_xml = _safe_text(quote.get("Content"))
            md5_value = self._extract_md5_from_img_xml(quote_xml)
            resource_path = self._extract_resource_path_from_media_xml(quote_xml)
            local_path = resource_path if (resource_path and os.path.isfile(resource_path)) else ""
            if not local_path and md5_value:
                local_path = self._find_existing_file_path(md5_value=md5_value)
            parts = ["[图片]"]
            if md5_value:
                parts.append(f"md5={md5_value}")
            cdn_key = _safe_text(quote.get("cdnthumbaeskey")).strip()
            if cdn_key:
                parts.append(f"cdnthumbaeskey={cdn_key}")
            quoted_content = " ".join(parts).strip()
            media_directive = self._build_gateway_media_directive(
                gateway_path=self._map_path_for_gateway(local_path) if local_path else "",
            )
            if media_directive:
                quoted_content = f"{quoted_content}\n{media_directive}"
        elif quoted_type in {34, 43, 62}:
            quote_xml = _safe_text(quote.get("Content"))
            md5_value = self._extract_md5_from_media_xml(quote_xml)
            resource_path = self._extract_resource_path_from_media_xml(quote_xml)
            local_path = resource_path if (resource_path and os.path.isfile(resource_path)) else ""
            if not local_path and md5_value:
                local_path = self._find_existing_file_path(md5_value=md5_value)

            media_label = "语音" if quoted_type == 34 else "视频"
            parts = [f"[{media_label}]"]
            if md5_value:
                parts.append(f"md5={md5_value}")
            quoted_content = " ".join(parts).strip()
            media_directive = self._build_gateway_media_directive(
                gateway_path=self._map_path_for_gateway(local_path) if local_path else "",
            )
            if media_directive:
                quoted_content = f"{quoted_content}\n{media_directive}"
        elif quoted_type == 49:
            title = _safe_text(quote.get("title") or quote.get("Content")).strip()
            url = _safe_text(quote.get("url")).strip()
            xml_type_raw = quote.get("XmlType")
            xml_type: Optional[int]
            try:
                xml_type = int(xml_type_raw) if xml_type_raw is not None else None
            except Exception:
                xml_type = None

            if (xml_type is None or xml_type == 5) and (title or url):
                parts = ["[链接文章]"]
                if title:
                    parts.append(title)
                if url:
                    parts.append(url)
                quoted_content = " ".join(parts).strip()
            elif xml_type == 33:
                desc = _safe_text(quote.get("destination")).strip()
                weappinfo = quote.get("weappinfo") if isinstance(quote.get("weappinfo"), dict) else {}
                username = _safe_text(weappinfo.get("username")).strip()
                appid = _safe_text(weappinfo.get("appid")).strip()
                pagepath = _safe_text(weappinfo.get("pagepath") or weappinfo.get("pagePath")).strip()
                if not appid:
                    statextstr = _safe_text(quote.get("statextstr")).strip()
                    if statextstr:
                        try:
                            import base64

                            decoded = base64.b64decode(statextstr)
                            decoded_text = decoded.decode("utf-8", errors="ignore")
                            match = re.search(r"wx[0-9a-f]{16,32}", decoded_text, re.IGNORECASE)
                            if match:
                                appid = match.group(0)
                        except Exception:
                            pass

                link = url or _safe_text(quote.get("dataurl")).strip() or _safe_text(quote.get("lowurl")).strip()
                parts = ["[小程序]"]
                if title:
                    parts.append(title)
                if desc:
                    parts.append(f"desc={desc}")
                if appid:
                    parts.append(f"appid={appid}")
                if username:
                    parts.append(f"username={username}")
                if pagepath:
                    parts.append(f"pagepath={pagepath}")
                if link:
                    parts.append(f"url={link}")
                quoted_content = " ".join(parts).strip()
            elif xml_type == 6:
                md5_value = _safe_text(quote.get("md5")).strip().lower()
                appattach = quote.get("appattach") if isinstance(quote.get("appattach"), dict) else {}
                fileext = _safe_text(appattach.get("fileext") if isinstance(appattach, dict) else "").strip().lstrip(".")
                guessed_name = ""
                if md5_value and fileext:
                    guessed_name = f"{md5_value}.{fileext}"
                safe_title = os.path.basename(title) if title else ""
                local_path = self._find_existing_file_path(md5_value=md5_value, file_name=safe_title)
                parts = ["[文件]"]
                if safe_title:
                    parts.append(safe_title)
                if md5_value:
                    parts.append(f"md5={md5_value}")
                quoted_content = " ".join(parts).strip()
                media_directive = self._build_gateway_media_directive(
                    gateway_path=self._map_path_for_gateway(local_path) if local_path else "",
                )
                if media_directive:
                    quoted_content = f"{quoted_content}\n{media_directive}"
            else:
                desc = _safe_text(quote.get("destination")).strip()
                thumb = _safe_text(quote.get("thumburl")).strip()
                link = (
                    url
                    or _safe_text(quote.get("dataurl")).strip()
                    or _safe_text(quote.get("lowdataurl")).strip()
                    or _safe_text(quote.get("lowurl")).strip()
                )
                parts = ["[分享卡片]"]
                if xml_type is not None:
                    parts.append(f"xmlType={xml_type}")
                if title:
                    parts.append(title)
                if desc:
                    parts.append(f"desc={desc}")
                if link:
                    parts.append(f"url={link}")
                if thumb:
                    parts.append(f"thumb={thumb}")
                quoted_content = " ".join(parts).strip()

        lines = [prompt.strip()] if prompt.strip() else []
        if quoted_sender:
            lines.append(f"[引用消息] 来自: {quoted_sender}")
        else:
            lines.append("[引用消息]")
        lines.append(f"- 类型: {quoted_type if quoted_type is not None else '?'}")
        if quoted_content:
            lines.append(f"- 内容: {quoted_content}")
        return "\n".join(lines).strip()

    def _resolve_agent_id(self) -> str:
        configured = (self.trigger_agent_id or self.default_agent_id).strip()
        if configured:
            return configured
        if self.trigger_auto_default_agent:
            return self.gateway.default_agent_id().strip()
        return ""

    def _split_reply_chunks(self, content: str) -> list[str]:
        text = _safe_text(content)
        if not text:
            return []

        # Client869/微信侧对单条文本长度存在不透明上限，过长可能被静默截断。
        # 这里强制做一个安全上限，避免“看起来发送成功但内容不全”。
        safe_limit = 900
        limit = min(max(int(self.max_reply_chars or 0), 200), safe_limit)
        if len(text) <= limit:
            return [text]

        chunks: list[str] = []
        remaining = text
        split_chars = ("\n", "。", "！", "？", "!", "?", "；", ";", "，", ",", " ")

        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break

            window = remaining[: limit + 1]
            split_at = -1
            for marker in split_chars:
                idx = window.rfind(marker)
                if idx > split_at:
                    split_at = idx

            if split_at < int(limit * 0.5):
                split_at = limit

            chunk = remaining[:split_at].rstrip()
            if not chunk:
                chunk = remaining[:limit]
                split_at = len(chunk)
            chunks.append(chunk)
            remaining = remaining[split_at:].lstrip()

        return [item for item in chunks if item]

    def _trim_reply(self, content: str) -> str:
        chunks = self._split_reply_chunks(content)
        return chunks[0] if chunks else ""
