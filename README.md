# WebSocket Stream Client (Python)

A production-ready **WebSocket streaming client** built on top of the [`websockets`](https://websockets.readthedocs.io/) library.  
This project is designed to replace a typical C++ WebSocket client used for **live data streaming**.

---

## ✨ Features

- ✅ **Async I/O** using Python `asyncio`  
- ✅ **Auto-reconnect** with exponential backoff + jitter  
- ✅ **Heartbeat support** via ping/pong handling  
- ✅ **Backpressure** with outbound send queue  
- ✅ **Pluggable callbacks**: `on_message`, `on_connect`, `on_disconnect`, `on_error`, `on_retry`  
- ✅ **TLS/headers/subprotocols** supported  
- ✅ **Error handling** mapped to common C++ patterns  
- ✅ **Typed, documented, and ready for production**  

---

## 📂 Project Structure

```
WebSocket/
├── websocket_stream_client.py   # Main WebSocket client implementation
└── README.md                    # This file
```

---

## 🚀 Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/your-username/websocket-stream-client.git
   cd websocket-stream-client
   ```

2. Install dependencies (Python 3.8+ required):
   ```bash
   pip install websockets>=12.0
   ```

---

## 🛠 Usage Example

```python
import asyncio
from websocket_stream_client import WebSocketStreamClient, WSConfig

async def on_message(msg, *, is_binary: bool = isinstance(msg, (bytes, bytearray))):
    print("RX:", msg if not is_binary else f"<{len(msg)} bytes>")

async def main():
    client = WebSocketStreamClient(
        WSConfig(url="wss://echo.websocket.events"),
        on_message=on_message,
    )
    await client.start()
    await client.send_text("hello world")
    await asyncio.sleep(3)
    await client.stop()

asyncio.run(main())
```

---

## ⚙️ Configuration Options

The `WSConfig` dataclass exposes several tunable parameters:

| Parameter            | Default     | Description |
|----------------------|-------------|-------------|
| `url`               | **required** | WebSocket endpoint |
| `headers`           | `None`      | Extra HTTP headers |
| `subprotocols`      | `None`      | WebSocket subprotocols |
| `ssl`               | `None`      | Custom SSL context |
| `connect_timeout`   | `10.0`      | Connection timeout (s) |
| `ping_interval`     | `20.0`      | Ping heartbeat interval (s) |
| `ping_timeout`      | `20.0`      | Timeout waiting for pong (s) |
| `reconnect`         | `True`      | Auto-reconnect enabled |
| `initial_backoff`   | `0.5`       | Initial retry backoff (s) |
| `max_backoff`       | `30.0`      | Max retry delay (s) |
| `send_queue_maxsize`| `1000`      | Max queued outbound messages |

---

## 🛡 Error Handling

Errors are normalized into a `WSError` object containing:

- `message` – Human-readable description  
- `exception` – Original exception  
- `severity` – `TRANSIENT` (retryable) or `FATAL` (do not retry)  
- `close_code` – RFC6455 close code if available  

This allows Python exceptions to mirror typical **C++ error handling semantics**.

---

## 🔄 C++ → Python Error Mapping

| C++ Error (typical)                 | Python Equivalent                      | Policy      |
|-------------------------------------|----------------------------------------|-------------|
| `std::errc::connection_refused`     | `OSError` during connect               | Retry       |
| `std::errc::timed_out`              | `asyncio.TimeoutError`                 | Retry       |
| Handshake: HTTP 401/403             | `InvalidStatusCode(401/403)`           | Fatal       |
| Handshake: HTTP 4xx (other)         | `InvalidStatusCode(4xx)`               | Fatal       |
| Handshake: HTTP 5xx                 | `InvalidStatusCode(5xx)`               | Retry       |
| Protocol violation                  | `InvalidHandshake` / `NegotiationError`| Fatal       |
| Normal close (1000)                 | `ConnectionClosedOK(code=1000)`        | Retry       |
| Abnormal close (1006/1011, etc.)    | `ConnectionClosedError`                | Retry       |

---

## 🧪 Running the Example

You can run the included example with:

```bash
python websocket_stream_client.py
```

Expected output:
```
connected; sending a test message…
RX: hello from Python client
disconnected gracefully.
```

---

## 📜 License

This project is licensed under the **BSD-3-Clause License**.

---

## 🙌 Contributing

Pull requests and issues are welcome! Please open an issue if you encounter a bug or would like to request a feature.



## 🔌 How It Works (Simple Diagram)

```
+------------------+          WebSocket           +------------------+
|  Your Python App | <-------------------------> |  WebSocket Server|
|                  |   (always open connection)  |  (e.g. market    |
|  - send messages |                              |   data feed,     |
|  - receive data  |                              |   chat server)   |
+------------------+                              +------------------+

- Your app connects once and stays connected.
- The server can push data to you anytime (no need to ask repeatedly).
- You can send messages back instantly.
```

This project is a Python WebSocket client.

A WebSocket is like a special internet pipe between your program and a server.
Unlike normal HTTP requests (which are one-way and short-lived), a WebSocket stays open all the time so that:

The server can keep sending you live updates (streaming data).

You can send data back instantly without reconnecting every time.

What this project does:

Connects to a WebSocket server (e.g., a stock price feed, IoT device hub, chat server, etc.).

Keeps the connection alive automatically (reconnects if it drops, sends heartbeats so it doesn’t time out).

Receives messages in real time (your code gets them instantly via a callback function).

Sends messages back (text or binary) using a queue, so you don’t lose data even if you send faster than the network.

Handles errors safely (decides when to retry or when it’s a fatal error, similar to robust C++ clients).

Example in real life:

If you were building a trading app, this client could connect to a stock market data feed and stream live prices.

If you were making a chat app, it could keep the chat open so you receive messages instantly.

If you had IoT sensors, they could send data continuously to your server via this client.