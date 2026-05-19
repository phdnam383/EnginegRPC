# Hướng Dẫn Xây Dựng gRPC Server với BetterProto (Python)

> Hướng dẫn này được viết dựa trên **project AlarmClusterEngine** thực tế.  
> So sánh song song giữa hai cách tiếp cận: `grpc_tools.protoc` (classic) và `betterproto` (modern).

---

## Mục lục

1. [Tổng quan kiến trúc](#1-tổng-quan-kiến-trúc)
2. [Cài đặt dependencies](#2-cài-đặt-dependencies)
3. [Bước 1 — Định nghĩa hợp đồng (.proto)](#3-bước-1--định-nghĩa-hợp-đồng-proto)
4. [Bước 2 — Sinh Python stubs](#4-bước-2--sinh-python-stubs)
5. [Bước 3 — Triển khai Servicer](#5-bước-3--triển-khai-servicer)
6. [Bước 4 — Khởi động Server](#6-bước-4--khởi-động-server)
7. [Bước 5 — HTTP Health Probe](#7-bước-5--http-health-probe)
8. [Bước 6 — Graceful Shutdown](#8-bước-6--graceful-shutdown)
9. [Bước 7 — CLI Entry-Point](#9-bước-7--cli-entry-point)
10. [Sơ đồ luồng hoàn chỉnh](#10-sơ-đồ-luồng-hoàn-chỉnh)
11. [Bảng so sánh classic vs betterproto](#11-bảng-so-sánh-classic-vs-betterproto)
12. [Các lỗi thường gặp](#12-các-lỗi-thường-gặp)

---

## 1. Tổng quan kiến trúc

```
┌─────────────────────────────────────────────────────────┐
│                  AlarmClusterEngine                     │
│                                                         │
│  proto/engine.proto                                     │
│        │                                                │
│        ▼ (generate_betterproto.py)                      │
│  proto_betterproto/engine.py   ◄── BetterProto stubs   │
│        │                                                │
│        ▼                                                │
│  server_betterproto.py                                  │
│  ├── _HealthHandler     (HTTP /live /ready /health)     │
│  ├── AlarmClusteringServicer  (gRPC handler)            │
│  └── serve()            (asyncio event loop)            │
│                                                         │
│  PORT 50051  ◄── gRPC (grpc.aio)                        │
│  PORT 8080   ◄── HTTP health probes                     │
└─────────────────────────────────────────────────────────┘
```

### Hai cách sinh stubs

| Cách | Tool | Output | Kiểu message | RPC style |
|------|------|--------|--------------|-----------|
| Classic | `grpc_tools.protoc` | `engine_pb2.py` + `engine_pb2_grpc.py` | raw descriptor | sync |
| **BetterProto** | `betterproto[compiler]` | `proto_betterproto/engine.py` | `@dataclass` | **async** |

---

## 2. Cài đặt dependencies

### `requirements.txt`

```txt
grpcio>=1.62.0
grpcio-tools>=1.62.0
betterproto[compiler]>=0.3.1
```

- **`grpcio`** — runtime gRPC cho Python (client/server transport layer)
- **`grpcio-tools`** — cung cấp `grpc_tools.protoc`, compiler cho `.proto` → Python
- **`betterproto[compiler]`** — plugin cho protoc, sinh `@dataclass` thay vì raw descriptor; phần `[compiler]` bao gồm `grpclib` và plugin

```bash
pip install -r requirements.txt
```

---

## 3. Bước 1 — Định nghĩa hợp đồng (.proto)

**File:** `proto/engine.proto`

```proto
syntax = "proto3";

package alarm_clustering;

// ── Service ─────────────────────────────────────────────────────────────────
service AlarmClusteringService {
    rpc AnalyzeOnlineAlarmCluster(AnalyzeOnlineAlarmClusterRequest)
        returns (OnlineAlarmClusterReport);
}

// ── Request ──────────────────────────────────────────────────────────────────
message AlarmRecord {
    string _source_id         = 1;
    string managed_objects    = 2;
    string alarmType          = 3;
    string probable_cause     = 4;
    string perceived_severity = 5;
    string state              = 6;
    string created_at         = 7;
    string closed_at          = 8;
}

message AnalyzeOnlineAlarmClusterRequest {
    string requestId                  = 1;
    string system                     = 2;
    repeated AlarmRecord alarmRecords = 3;
}

// ── Response ─────────────────────────────────────────────────────────────────
message OnlineAlarmClusterResult {
    string _source_id = 1;
    int32  clusterId  = 2;
    float  confidence = 3;
}

message OnlineAlarmClusterReport {
    string requestId                          = 1;
    string reportType                         = 2;
    string status                             = 3;
    string message                            = 4;
    repeated OnlineAlarmClusterResult results = 5;
}
```

### Giải thích các thành phần trong `.proto`

| Thành phần | Vai trò |
|-----------|---------|
| `syntax = "proto3"` | Phiên bản Protocol Buffers |
| `package alarm_clustering` | Namespace — ảnh hưởng tên service đăng ký: `"alarm_clustering.AlarmClusteringService"` |
| `service` | Khai báo gRPC service và các RPC method |
| `rpc ... returns (...)` | Một RPC method: unary request → unary response |
| `message` | Định nghĩa kiểu dữ liệu (tương đương class/struct) |
| `repeated` | Mảng (list) các phần tử |
| `int32`, `float`, `string` | Scalar types của Protobuf |

> **Lưu ý BetterProto:** Field `_source_id` trong `.proto` có dấu gạch dưới ở đầu.  
> BetterProto sẽ **tự động strip** dấu gạch dưới đó → trong Python là `source_id`.

---

## 4. Bước 2 — Sinh Python stubs

### Script tự động: `generate_betterproto.py`

```python
import subprocess, sys
from pathlib import Path

ROOT      = Path(__file__).parent
PROTO_DIR = ROOT / "proto"
OUT_DIR   = ROOT / "proto_betterproto"

def generate() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "grpc_tools.protoc",
        f"--proto_path={ROOT}",                    # ← root để import đúng package
        f"--python_betterproto_out={OUT_DIR}",      # ← plugin betterproto
        str(PROTO_DIR / "engine.proto"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("STDERR:", result.stderr)
        sys.exit(result.returncode)
    print("Done →", OUT_DIR)

if __name__ == "__main__":
    generate()
```

```bash
python generate_betterproto.py
```

### Output sinh ra: `proto_betterproto/engine.py`

BetterProto sinh ra:
- **`@dataclass` message classes** — `AlarmRecord`, `AnalyzeOnlineAlarmClusterRequest`, `OnlineAlarmClusterResult`, `OnlineAlarmClusterReport`
- **`AlarmClusteringServiceBase`** — abstract base class, override các method này để implement logic
- **`AlarmClusteringServiceStub`** — dùng ở phía **client** để gọi RPC

### Ký hiệu tên field sau khi sinh (Quan trọng!)

| Proto field | BetterProto Python attribute |
|-------------|------------------------------|
| `_source_id` | `source_id` *(strip leading `_`)* |
| `requestId` | `request_id` *(camelCase → snake_case)* |
| `alarmRecords` | `alarm_records` |
| `clusterId` | `cluster_id` |
| `reportType` | `report_type` |

---

## 5. Bước 3 — Triển khai Servicer

**File:** `server_betterproto.py`

### Import stubs

```python
from proto_betterproto.engine import (
    AlarmClusteringServiceBase,           # ← base class để kế thừa
    AnalyzeOnlineAlarmClusterRequest,     # ← type hint cho request
    OnlineAlarmClusterReport,             # ← type hint cho response
    OnlineAlarmClusterResult,
)
```

### Kế thừa `ServiceBase` và implement RPC method

```python
class AlarmClusteringServicer(AlarmClusteringServiceBase):
    """
    - Kế thừa AlarmClusteringServiceBase (do BetterProto sinh ra).
    - Override từng RPC method được khai báo trong .proto.
    - Các method phải là 'async def' (BetterProto dùng asyncio).
    - KHÔNG cần tham số 'context' như trong grpc_tools classic.
    """

    def __init__(self, model_dir: str) -> None:
        # Khởi tạo dependencies (embedder, model, DB connection, ...)
        self._embedder = Embedder.get_instance(model_dir)

    async def analyze_online_alarm_cluster(
        self,
        request: AnalyzeOnlineAlarmClusterRequest,
    ) -> OnlineAlarmClusterReport:
        # ── Đọc dữ liệu từ request (snake_case) ──────────────────────
        request_id = request.request_id          # requestId trong proto
        alarms     = request.alarm_records       # alarmRecords trong proto

        # ── Xử lý logic ───────────────────────────────────────────────
        if not alarms:
            return OnlineAlarmClusterReport(
                request_id  = request_id,
                report_type = "ONLINE_CLUSTER",
                status      = "ERROR",
                message     = "Request contains no alarm records.",
                results     = [],
            )

        # Chạy blocking I/O trong thread pool để không block event loop
        loop = asyncio.get_event_loop()
        cluster_result = await loop.run_in_executor(None, cluster_online, records)

        # ── Trả về response ───────────────────────────────────────────
        return OnlineAlarmClusterReport(
            request_id  = request_id,
            report_type = "ONLINE_CLUSTER",
            status      = "OK",
            message     = f"Clustered {len(alarms)} alarms",
            results     = [...],
        )
```

### Các nguyên tắc quan trọng khi implement Servicer

| Quy tắc | Lý do |
|---------|-------|
| Method phải là `async def` | BetterProto dùng `grpc.aio` (asyncio) |
| Không có `context` parameter | BetterProto ẩn đi, dùng exception thay thế |
| Field name là **snake_case** | BetterProto tự chuyển từ camelCase |
| Tên method là **snake_case** | `AnalyzeOnlineAlarmCluster` → `analyze_online_alarm_cluster` |
| Dùng `await loop.run_in_executor()` | Chạy CPU-bound / blocking code trong thread pool |

---

## 6. Bước 4 — Khởi động Server

### `grpc.aio.server` — async native server

```python
import grpc
import grpc.aio
import asyncio

async def _serve_async(
    port: int,
    model_dir: str,
    ready_event: threading.Event,
) -> None:

    # 1. Tạo servicer instance
    servicer = AlarmClusteringServicer(model_dir)

    # 2. Tạo gRPC server với options
    server = grpc.aio.server(
        options=[
            ("grpc.max_receive_message_length", 64 * 1024 * 1024),  # 64 MB
            ("grpc.max_send_message_length",    64 * 1024 * 1024),
        ],
    )

    # 3. Đăng ký handlers (BetterProto cách)
    #    servicer.__mapping__() trả về dict {method_name: handler}
    handlers = servicer.__mapping__()
    generic_handler = grpc.method_service_handler(
        "alarm_clustering.AlarmClusteringService", handlers
    )
    server.add_generic_rpc_handlers([generic_handler])

    # 4. Bind port và start
    server.add_insecure_port(f"[::]:{port}")
    await server.start()

    # 5. Signal readiness
    ready_event.set()

    # 6. Chờ đến khi shutdown
    await server.wait_for_termination()
```

### Đăng ký handler: classic vs betterproto

**Classic (`grpc_tools.protoc`):**
```python
# server.py — dùng hàm được sinh tự động
pb2_grpc.add_AlarmClusteringServiceServicer_to_server(servicer, server)
```

**BetterProto:**
```python
# server_betterproto.py — dùng __mapping__() generic
handlers = servicer.__mapping__()
generic_handler = grpc.method_service_handler(
    "alarm_clustering.AlarmClusteringService",  # ← phải khớp với package.ServiceName
    handlers
)
server.add_generic_rpc_handlers([generic_handler])
```

> **Tên service string:** Luôn là `"{package}.{ServiceName}"` — khớp với khai báo trong `.proto`.  
> Project này: `"alarm_clustering.AlarmClusteringService"`

### Các gRPC server options phổ biến

```python
options = [
    ("grpc.max_receive_message_length", 64 * 1024 * 1024),  # 64 MB
    ("grpc.max_send_message_length",    64 * 1024 * 1024),
    ("grpc.keepalive_time_ms",          10_000),             # 10s
    ("grpc.keepalive_timeout_ms",        5_000),             # 5s
    ("grpc.keepalive_permit_without_calls", True),
    ("grpc.http2.min_recv_ping_interval_without_data_ms", 5_000),
]
```

---

## 7. Bước 5 — HTTP Health Probe

Cần thiết cho **Kubernetes liveness/readiness probes**.  
Chạy trên một thread riêng để độc lập với asyncio event loop của gRPC.

```python
import http.server
import threading

class _HealthHandler(http.server.BaseHTTPRequestHandler):
    # Shared event — được set khi gRPC server sẵn sàng
    ready_event: threading.Event = threading.Event()

    def do_GET(self) -> None:
        if self.path == "/live":
            # Liveness: process đang chạy → luôn 200
            self._respond(200, b"LIVE")

        elif self.path in ("/ready", "/health"):
            # Readiness: chỉ 200 khi gRPC server đã start xong
            if self.ready_event.is_set():
                self._respond(200, b"READY")
            else:
                self._respond(503, b"NOT READY")
        else:
            self._respond(404, b"NOT FOUND")

    def _respond(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args) -> None:
        pass  # Tắt access log cho gọn


def _start_health_server(port: int, ready_event: threading.Event) -> None:
    _HealthHandler.ready_event = ready_event
    httpd = http.server.HTTPServer(("0.0.0.0", port), _HealthHandler)
    httpd.serve_forever()   # blocking — phải chạy trong thread riêng


def serve(port, model_dir, max_workers, health_port):
    ready_event = threading.Event()

    # Start HTTP health server trong daemon thread
    health_thread = threading.Thread(
        target=_start_health_server,
        args=(health_port, ready_event),
        daemon=True,         # ← tự kết thúc khi main thread dừng
        name="health-http",
    )
    health_thread.start()

    # Start gRPC server (set ready_event bên trong khi server đã start)
    asyncio.run(_serve_async(port, model_dir, ready_event))
```

### Luồng probe sequence

```
Process start
     │
     ▼
health_thread.start()     → /live  = 200 (ngay lập tức)
     │
     ▼
gRPC server.start()
     │
     ▼
ready_event.set()         → /ready = 200 (sau khi gRPC sẵn sàng)
```

---

## 8. Bước 6 — Graceful Shutdown

### Với `grpc.aio` (asyncio) — dùng `add_signal_handler`

```python
async def _serve_async(...):
    ...
    await server.start()

    loop = asyncio.get_event_loop()

    def _on_signal():
        logger.info("Shutdown signal — stopping...")
        asyncio.ensure_future(server.stop(grace=5))  # 5s grace period

    loop.add_signal_handler(signal.SIGTERM, _on_signal)
    loop.add_signal_handler(signal.SIGINT,  _on_signal)

    await server.wait_for_termination()
```

### Với `grpc.server` sync (classic) — dùng `signal.signal`

```python
def serve(...):
    ...
    server.start()
    _stop_event = [False]

    def _shutdown(signum, frame):
        _stop_event[0] = True
        server.stop(grace=5)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    while not _stop_event[0]:
        time.sleep(1)
```

> **Tại sao `grace=5`?**  
> Cho phép các RPC đang xử lý có 5 giây để hoàn thành trước khi server đóng cứng.  
> Rất quan trọng trong môi trường Kubernetes (rolling update, pod eviction).

---

## 9. Bước 7 — CLI Entry-Point

```python
import argparse
import os

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Alarm Cluster Engine — gRPC Server (BetterProto)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("GRPC_PORT", 50051)),
        help="gRPC listen port",
    )
    parser.add_argument(
        "--health-port", type=int,
        default=int(os.environ.get("HEALTH_PORT", 8080)),
        help="HTTP health probe port",
    )
    parser.add_argument(
        "--model-dir",
        default=os.environ.get("MODEL_DIR", "models"),
        help="Directory containing model files",
    )
    parser.add_argument(
        "--workers", type=int,
        default=int(os.environ.get("MAX_WORKERS", 10)),
        help="Thread-executor size for blocking ops",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    serve(
        port        = args.port,
        model_dir   = args.model_dir,
        max_workers = args.workers,
        health_port = args.health_port,
    )
```

### Chạy server

```bash
# Mặc định
python server_betterproto.py

# Tùy chỉnh port
python server_betterproto.py --port 50052 --health-port 8081

# Qua environment variable
GRPC_PORT=50052 MODEL_DIR=/data/models python server_betterproto.py
```

---

## 10. Sơ đồ luồng hoàn chỉnh

```
engine.proto
     │
     │  python generate_betterproto.py
     ▼
proto_betterproto/
└── engine.py
    ├── AlarmRecord               (@dataclass)
    ├── AnalyzeOnlineAlarmClusterRequest  (@dataclass)
    ├── OnlineAlarmClusterResult  (@dataclass)
    ├── OnlineAlarmClusterReport  (@dataclass)
    ├── AlarmClusteringServiceBase    ← server kế thừa
    └── AlarmClusteringServiceStub    ← client dùng
         │
         ▼
server_betterproto.py
│
├── _HealthHandler (http.server)
│   ├── GET /live   → 200 LIVE
│   ├── GET /ready  → 200 READY | 503 NOT READY
│   └── GET /health → alias /ready
│
├── AlarmClusteringServicer(AlarmClusteringServiceBase)
│   └── async analyze_online_alarm_cluster(request) → response
│       ├── parse request.alarm_records
│       ├── embed tokens
│       └── await loop.run_in_executor(cluster_online)
│
└── serve()
    ├── threading.Thread(_start_health_server)   ← port 8080
    └── asyncio.run(_serve_async())              ← port 50051
        ├── grpc.aio.server(options=[...])
        ├── servicer.__mapping__() → register handlers
        ├── server.add_insecure_port("[::]:{port}")
        ├── await server.start()
        ├── ready_event.set()
        ├── loop.add_signal_handler(SIGTERM/SIGINT)
        └── await server.wait_for_termination()
```

---

## 11. Bảng so sánh classic vs betterproto

| Tiêu chí | `grpc_tools.protoc` (classic) | `betterproto` (modern) |
|----------|-------------------------------|------------------------|
| **Sinh file** | `engine_pb2.py` + `engine_pb2_grpc.py` | `proto_betterproto/engine.py` |
| **Message type** | Raw descriptor class | `@dataclass` |
| **Field access** | `request.requestId` (camelCase) | `request.request_id` (snake_case) |
| **Field `_source_id`** | `alarm._source_id` | `alarm.source_id` *(strip `_`)* |
| **Servicer base** | `AlarmClusteringServiceServicer` | `AlarmClusteringServiceBase` |
| **RPC method** | `def AnalyzeOnlineAlarmCluster(self, request, context)` | `async def analyze_online_alarm_cluster(self, request)` |
| **Register handler** | `pb2_grpc.add_...Servicer_to_server(s, server)` | `servicer.__mapping__()` |
| **gRPC runtime** | `grpc.server(ThreadPoolExecutor(...))` | `grpc.aio.server(...)` |
| **Shutdown** | `signal.signal()` + polling loop | `loop.add_signal_handler()` + `asyncio.ensure_future()` |
| **IDE support** | Kém (no type hints) | Tốt (typed dataclass) |
| **Async native** | Không | **Có** |
| **Context object** | Có (`grpc.ServicerContext`) | Không cần (exception-based) |

---

## 12. Các lỗi thường gặp

### ❌ `ModuleNotFoundError: No module named 'proto_betterproto'`

**Nguyên nhân:** Chưa chạy `generate_betterproto.py`.

```bash
python generate_betterproto.py
```

---

### ❌ `AttributeError: 'AlarmRecord' object has no attribute '_source_id'`

**Nguyên nhân:** BetterProto strip leading underscore.

```python
# ❌ Sai
alarm._source_id

# ✅ Đúng
alarm.source_id
```

---

### ❌ `AttributeError: 'AnalyzeOnlineAlarmClusterRequest' has no attribute 'requestId'`

**Nguyên nhân:** BetterProto chuyển camelCase → snake_case.

```python
# ❌ Sai
request.requestId
request.alarmRecords

# ✅ Đúng
request.request_id
request.alarm_records
```

---

### ❌ `RuntimeError: This event loop is already running`

**Nguyên nhân:** Gọi `asyncio.run()` trong môi trường đã có event loop (Jupyter, FastAPI...).

```python
# ✅ Dùng trong môi trường đã có event loop
import nest_asyncio
nest_asyncio.apply()
asyncio.run(main())
```

---

### ❌ `grpc._channel._InactiveRpcError: StatusCode.UNAVAILABLE`

**Nguyên nhân:** Server chưa start khi client kết nối, hoặc sai port.

```python
# Kiểm tra health trước khi gọi
import urllib.request
resp = urllib.request.urlopen("http://localhost:8080/ready")
assert resp.status == 200
```

---

### ❌ `STDERR: --python_betterproto_out: protoc-gen-python_betterproto: Plugin not found`

**Nguyên nhân:** `betterproto[compiler]` chưa được cài đúng, hoặc dùng `protoc` hệ thống thay vì `grpc_tools.protoc`.

```bash
# ✅ Luôn dùng grpc_tools.protoc qua Python
python -m grpc_tools.protoc --python_betterproto_out=... proto/engine.proto

# ❌ Không dùng protoc hệ thống
protoc --python_betterproto_out=... proto/engine.proto
```

---

### ❌ Server đăng ký sai tên service

**Nguyên nhân:** String tên service không khớp với `package` trong `.proto`.

```python
# proto: package alarm_clustering; service AlarmClusteringService { ... }

# ✅ Đúng
generic_handler = grpc.method_service_handler(
    "alarm_clustering.AlarmClusteringService",  # package.ServiceName
    handlers
)

# ❌ Sai
generic_handler = grpc.method_service_handler(
    "AlarmClusteringService",  # thiếu package prefix
    handlers
)
```

---

*Tài liệu này phản ánh codebase tại `f:\AI\Clustering\AlarmClusterEngine` — cập nhật lần cuối: 2026-05-19.*
