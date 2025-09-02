"""
WebSocket streaming client (Python) using the `websockets` library.

This module is a production-ready, single-file implementation intended to
replace a typical C++ WebSocket client used for live data streaming.

Key features:
- Async client built on `websockets` for efficient I/O
- Clean start/stop lifecycle, graceful shutdown
- Auto-reconnect with exponential backoff + jitter
- Heartbeats via built-in ping/pong handling
- Outbound send queue with backpressure
- Pluggable callbacks: on_message, on_connect, on_disconnect, on_error, on_retry
- Structured error handling approximating common C++ patterns
- TLS/headers/subprotocols support
- Typed, documented, and easy to test

Usage (minimal):

    import asyncio
    from websocket_stream_client import WebSocketStreamClient, WSConfig

    async def main():
        async def on_message(msg, *, is_binary: bool):
            print("RX:", msg if not is_binary else f"<{len(msg)} bytes>")

        client = WebSocketStreamClient(
            WSConfig(url="wss://echo.websocket.events"),
            on_message=on_message,
        )
        await client.start()
        await client.send_text("hello")
        await asyncio.sleep(3)
        await client.stop()

    asyncio.run(main())

Notes:
- Install deps: `pip install websockets>=12.0`
- Map typical C++ error handling to Python exceptions/closures—see bottom.
"""
from __future__ import annotations

import asyncio
import dataclasses
import enum
import logging
import random
import ssl as ssl_module
from typing import Any, Awaitable, Callable, Dict, Optional, Sequence, Union

import websockets
from websockets.exceptions import (
    ConnectionClosed,  # base
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidHandshake,
    InvalidStatusCode,
    NegotiationError,
    WebSocketException,
)
from websockets.client import WebSocketClientProtocol

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

@dataclasses.dataclass
class WSConfig:
    url: str
    headers: Optional[Dict[str, str]] = None
    subprotocols: Optional[Sequence[str]] = None
    ssl: Optional[ssl_module.SSLContext] = None

    # Timeouts & heartbeat (mirrors common C++ knobs)
    connect_timeout: float = 10.0  # seconds
    close_timeout: float = 10.0
    ping_interval: Optional[float] = 20.0  # None disables pings
    ping_timeout: Optional[float] = 20.0

    # Reconnect/backoff
    reconnect: bool = True
    initial_backoff: float = 0.5
    max_backoff: float = 30.0
    backoff_multiplier: float = 2.0
    max_retries: Optional[int] = None  # None = unlimited

    # Messaging
    max_size: Optional[int] = 16 * 1024 * 1024  # 16 MiB; None = unlimited
    send_queue_maxsize: int = 1000
    send_queue_put_timeout: float = 5.0

    # Logging
    log_level: int = logging.INFO


# ----------------------------------------------------------------------------
# Error codes and mapping (to mirror typical C++ error handling semantics)
# ----------------------------------------------------------------------------

class WSErrorSeverity(enum.Enum):
    TRANSIENT = "transient"  # safe to retry
    FATAL = "fatal"          # do not retry automatically


@dataclasses.dataclass
class WSError:
    message: str
    exception: Optional[BaseException] = None
    severity: WSErrorSeverity = WSErrorSeverity.TRANSIENT
    close_code: Optional[int] = None  # RFC6455 close code if available


# ----------------------------------------------------------------------------
# Client implementation
# ----------------------------------------------------------------------------

OnMessage = Callable[[Union[str, bytes]], Awaitable[None]]
OnMessageDetailed = Callable[[Union[str, bytes]], Awaitable[None]]  # kept for bwd compat


class WebSocketStreamClient:
    """High-level streaming client with reconnection and backpressure."""

    def __init__(
        self,
        config: WSConfig,
        on_message: Optional[Callable[[Union[str, bytes],], Awaitable[None]]] = None,
        *,
        on_connect: Optional[Callable[[WebSocketClientProtocol], Awaitable[None]]] = None,
        on_disconnect: Optional[Callable[[Optional[WSError]], Awaitable[None]]] = None,
        on_error: Optional[Callable[[WSError], Awaitable[None]]] = None,
        on_retry: Optional[Callable[[int, float, WSError], Awaitable[None]]] = None,
    ) -> None:
        self.cfg = config
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.on_error = on_error
        self.on_retry = on_retry

        self._ws: Optional[WebSocketClientProtocol] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._sender_task: Optional[asyncio.Task] = None
        self._send_queue: asyncio.Queue[Union[str, bytes]] = asyncio.Queue(
            maxsize=self.cfg.send_queue_maxsize
        )
        self._stop_event = asyncio.Event()
        self._running = False

        self._retries = 0
        self._backoff = self.cfg.initial_backoff

        logging.basicConfig(level=self.cfg.log_level)
        self.log = logging.getLogger(self.__class__.__name__)

    # --------------------------- Lifecycle ---------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        await self._cancel_io_tasks()
        await self._close_ws()

    async def _run_loop(self) -> None:
        try:
            while self._running and not self._stop_event.is_set():
                err: Optional[WSError] = None
                try:
                    await self._connect()
                    if self.on_connect:
                        await self.on_connect(self._ws)  # type: ignore[arg-type]
                    self._retries = 0
                    self._backoff = self.cfg.initial_backoff

                    # Start I/O tasks
                    self._reader_task = asyncio.create_task(self._reader())
                    self._sender_task = asyncio.create_task(self._sender())

                    # Wait until reader exits (disconnect or error)
                    await asyncio.wait(
                        [self._reader_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                except (asyncio.TimeoutError, InvalidHandshake, InvalidStatusCode, NegotiationError, WebSocketException) as ex:
                    err = self._classify_exception(ex)
                    await self._emit_error(err)
                except Exception as ex:  # unexpected
                    err = WSError(message=str(ex), exception=ex, severity=WSErrorSeverity.TRANSIENT)
                    await self._emit_error(err)
                finally:
                    # Clean up current connection & tasks
                    await self._cancel_io_tasks()
                    await self._close_ws()
                    if self.on_disconnect:
                        await self.on_disconnect(err)

                if not self._running or not self.cfg.reconnect:
                    break

                # Decide on retry
                if err and err.severity == WSErrorSeverity.FATAL:
                    self.log.error("Fatal error — not retrying: %s", err.message)
                    break

                # Exponential backoff with jitter
                delay = self._backoff * (1.0 + random.random() * 0.5)
                if self.on_retry and err:
                    await self.on_retry(self._retries, delay, err)
                await asyncio.wait([self._stop_event.wait()], timeout=delay)
                if self.cfg.max_retries is not None and self._retries >= self.cfg.max_retries:
                    self.log.error("Max retries reached — giving up")
                    break
                self._retries += 1
                self._backoff = min(self.cfg.max_backoff, self._backoff * self.cfg.backoff_multiplier)
        finally:
            await self._cancel_io_tasks()
            await self._close_ws()

    # ---------------------------- Connect ----------------------------------

    async def _connect(self) -> None:
        self.log.info("Connecting to %s", self.cfg.url)
        self._ws = await asyncio.wait_for(
            websockets.connect(
                self.cfg.url,
                extra_headers=self.cfg.headers,
                subprotocols=list(self.cfg.subprotocols) if self.cfg.subprotocols else None,
                ssl=self.cfg.ssl,
                ping_interval=self.cfg.ping_interval,
                ping_timeout=self.cfg.ping_timeout,
                max_size=self.cfg.max_size,
                close_timeout=self.cfg.close_timeout,
                compression=None,  # leave None to let websockets negotiate defaults
                open_timeout=self.cfg.connect_timeout,
            ),
            timeout=self.cfg.connect_timeout,
        )
        self.log.info("Connected")

    # ----------------------------- Reader ----------------------------------

    async def _reader(self) -> None:
        assert self._ws is not None
        ws = self._ws
        try:
            async for msg in ws:
                if self.on_message:
                    try:
                        await self.on_message(msg)
                    except Exception as cb_ex:
                        self.log.exception("on_message callback raised: %s", cb_ex)
                # Successful traffic resets backoff a bit
                self._retries = 0
                self._backoff = max(self.cfg.initial_backoff, self._backoff / 2.0)
        except ConnectionClosedOK as ex:
            # Graceful remote close
            raise ex
        except ConnectionClosedError as ex:
            # Abnormal close (e.g., network hiccup)
            raise ex
        except WebSocketException as ex:
            raise ex
        except Exception as ex:
            raise ex

    # ----------------------------- Sender ----------------------------------

    async def _sender(self) -> None:
        assert self._ws is not None
        ws = self._ws
        try:
            while True:
                item = await self._send_queue.get()
                if item is None:  # sentinel for shutdown
                    break
                try:
                    if isinstance(item, (bytes, bytearray, memoryview)):
                        await ws.send(item)
                    else:
                        await ws.send(str(item))
                except Exception as ex:
                    # Put the item back if send failed; allows retry after reconnect
                    try:
                        self._send_queue.put_nowait(item)
                    except asyncio.QueueFull:
                        self.log.warning("Send queue full; dropping unsent message after error")
                    raise ex
        finally:
            # Drain sentinel if pending
            pass

    # ------------------------------ API ------------------------------------

    async def send_text(self, data: str) -> None:
        await self._queue_send(data)

    async def send_bytes(self, data: Union[bytes, bytearray, memoryview]) -> None:
        await self._queue_send(bytes(data))

    async def send(self, data: Union[str, bytes, bytearray, memoryview]) -> None:
        if isinstance(data, (bytes, bytearray, memoryview)):
            await self.send_bytes(data)
        else:
            await self.send_text(str(data))

    async def _queue_send(self, data: Union[str, bytes]) -> None:
        try:
            await asyncio.wait_for(
                self._send_queue.put(data),
                timeout=self.cfg.send_queue_put_timeout,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                "Timed out enqueuing outbound message (backpressure). Increase send_queue_maxsize or put timeout."
            )

    # --------------------------- Utilities ---------------------------------

    async def _cancel_io_tasks(self) -> None:
        for task in (self._reader_task, self._sender_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        self._reader_task = None
        self._sender_task = None

    async def _close_ws(self) -> None:
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.close(code=1000, reason="client shutdown"), timeout=self.cfg.close_timeout)
            except Exception:
                pass
            finally:
                self._ws = None

    async def _emit_error(self, err: WSError) -> None:
        self._log_error(err)
        if self.on_error:
            try:
                await self.on_error(err)
            except Exception:
                self.log.exception("on_error callback raised")

    def _log_error(self, err: WSError) -> None:
        level = logging.ERROR if err.severity == WSErrorSeverity.FATAL else logging.WARNING
        suffix = f" (close_code={err.close_code})" if err.close_code is not None else ""
        self.log.log(level, "%s%s", err.message, suffix)

    def _classify_exception(self, ex: BaseException) -> WSError:
        # Map websockets exceptions to a retry/fatal policy.
        if isinstance(ex, InvalidStatusCode):
            code = ex.status_code
            if code in (401, 403):
                return WSError(
                    message=f"Authentication/authorization failed: HTTP {code}",
                    exception=ex,
                    severity=WSErrorSeverity.FATAL,
                )
            elif 400 <= code < 500:
                return WSError(
                    message=f"Client error during handshake: HTTP {code}",
                    exception=ex,
                    severity=WSErrorSeverity.FATAL,
                )
            else:
                return WSError(
                    message=f"Server error during handshake: HTTP {code}",
                    exception=ex,
                    severity=WSErrorSeverity.TRANSIENT,
                )
        if isinstance(ex, InvalidHandshake):
            return WSError(
                message="Invalid WebSocket handshake (protocol error)",
                exception=ex,
                severity=WSErrorSeverity.FATAL,
            )
        if isinstance(ex, NegotiationError):
            return WSError(
                message="WebSocket negotiation error (subprotocols/extensions)",
                exception=ex,
                severity=WSErrorSeverity.FATAL,
            )
        if isinstance(ex, ConnectionClosedOK):
            return WSError(
                message="Connection closed normally by remote peer",
                exception=ex,
                severity=WSErrorSeverity.TRANSIENT,
                close_code=ex.code,
            )
        if isinstance(ex, ConnectionClosedError):
            # Abnormal close -> retry
            return WSError(
                message=f"Connection closed abnormally by remote peer (code={ex.code})",
                exception=ex,
                severity=WSErrorSeverity.TRANSIENT,
                close_code=ex.code,
            )
        if isinstance(ex, ConnectionClosed):
            return WSError(
                message=f"Connection closed (code={ex.code})",
                exception=ex,
                severity=WSErrorSeverity.TRANSIENT,
                close_code=getattr(ex, "code", None),
            )
        if isinstance(ex, asyncio.TimeoutError):
            return WSError(
                message="Operation timed out",
                exception=ex,
                severity=WSErrorSeverity.TRANSIENT,
            )
        if isinstance(ex, WebSocketException):
            return WSError(
                message=f"WebSocket error: {ex.__class__.__name__}",
                exception=ex,
                severity=WSErrorSeverity.TRANSIENT,
            )
        return WSError(message=str(ex), exception=ex, severity=WSErrorSeverity.TRANSIENT)


# ----------------------------------------------------------------------------
# Helper: create a secure SSL context (if needed)
# ----------------------------------------------------------------------------

def make_default_ssl_context() -> ssl_module.SSLContext:
    ctx = ssl_module.create_default_context(purpose=ssl_module.Purpose.SERVER_AUTH)
    # Customize as needed, e.g., client certs or pinned CAs.
    return ctx


# ----------------------------------------------------------------------------
# Example runnable (python websocket_stream_client.py)
# ----------------------------------------------------------------------------

async def _example() -> None:
    async def on_message(msg):
        if isinstance(msg, (bytes, bytearray)):
            print(f"<binary {len(msg)} bytes>")
        else:
            print("RX:", msg)

    async def on_connect(_ws):
        print("connected; sending a test message…")

    async def on_disconnect(err: Optional[WSError]):
        print("disconnected", "with error:" if err else "gracefully.", err.message if err else "")

    async def on_error(err: WSError):
        print("error:", err.message)

    async def on_retry(n: int, delay: float, err: WSError):
        print(f"retry {n} in {delay:.2f}s due to: {err.message}")

    client = WebSocketStreamClient(
        WSConfig(url="wss://echo.websocket.events"),
        on_message=on_message,
        on_connect=on_connect,
        on_disconnect=on_disconnect,
        on_error=on_error,
        on_retry=on_retry,
    )
    await client.start()
    await asyncio.sleep(1)
    await client.send_text("hello from Python client")
    await asyncio.sleep(3)
    await client.stop()


if __name__ == "__main__":
    asyncio.run(_example())

# ----------------------------------------------------------------------------
# C++ → Python error-handling mapping (reference)
# ----------------------------------------------------------------------------
# C++ (typical)                          Python websockets equivalent        Policy
# --------------------------------------------------------------------------------------
# std::errc::connection_refused          OSError during connect              TRANSIENT (retry)
# std::errc::timed_out                   asyncio.TimeoutError                TRANSIENT (retry)
# handshake: HTTP 401/403                InvalidStatusCode(401/403)          FATAL (auth/config)
# handshake: HTTP 4xx other              InvalidStatusCode(4xx)              FATAL (client bug)
# handshake: HTTP 5xx                    InvalidStatusCode(5xx)              TRANSIENT (retry)
# protocol violation                      InvalidHandshake/NegotiationError   FATAL
# normal close (1000)                    ConnectionClosedOK(code=1000)       TRANSIENT (no log noise)
# abnormal close (1006/1011 etc.)        ConnectionClosedError               TRANSIENT (retry)
# --------------------------------------------------------------------------------------
