# Xây dựng gRPC Server — Alarm Cluster Engine

## Mục lục

1. [gRPC là gì?](#1-grpc-là-gì)
2. [Tổng quan kiến trúc](#2-tổng-quan-kiến-trúc)
3. [Bước 1 — Định nghĩa Contract với Protocol Buffers](#3-bước-1--định-nghĩa-contract-với-protocol-buffers)
4. [Bước 2 — Generate Python Stubs](#4-bước-2--generate-python-stubs)
5. [Bước 3 — Implement Servicer (Business Logic)](#5-bước-3--implement-servicer-business-logic)
6. [Bước 4 — Khởi động Server](#6-bước-4--khởi-động-server)
7. [Bước 5 — Implement Client](#7-bước-5--implement-client)
8. [Luồng xử lý request đầy đủ](#8-luồng-xử-lý-request-đầy-đủ)
9. [Error handling & Status codes](#9-error-handling--status-codes)
10. [Health Probes cho Kubernetes](#10-health-probes-cho-kubernetes)
11. [Chạy và kiểm thử](#11-chạy-và-kiểm-thử)

---

## 1. gRPC là gì?

**gRPC** (Google Remote Procedure Call) là framework cho phép hai service gọi hàm của nhau qua mạng như thể gọi hàm local.

So sánh với REST:

| | REST/HTTP | gRPC |
|---|---|---|
| **Protocol** | HTTP/1.1 | HTTP/2 |
| **Format** | JSON (text) | Protobuf (binary) |
| **Contract** | OpenAPI (tuỳ chọn) | `.proto` (bắt buộc) |
| **Performance** | Chậm hơn | Nhanh hơn ~5–10× |
| **Streaming** | Không native | Có (4 kiểu) |
| **Code gen** | Tuỳ tool | Built-in |

**Trong project này**: MDAF Logic gọi `AnalyzeOnlineAlarmCluster()` trên Alarm Cluster Engine như gọi hàm Python bình thường — gRPC lo toàn bộ serialization, network, và error handling.

---

## 2. Tổng quan kiến trúc

```
┌─────────────────────────────────────────────────────────────────┐
│  MDAF Logic (Client)                                            │
│                                                                 │
│   stub = AlarmClusteringServiceStub(channel)                    │
│   report = stub.AnalyzeOnlineAlarmCluster(request)  ──────────► │
└─────────────────────────────────────────────────────────────────┘
                          gRPC / HTTP2 / Protobuf
                          port 50051
┌─────────────────────────────────────────────────────────────────┐
│  Alarm Cluster Engine (Server)                                  │
│                                                                 │
│   AlarmClusteringServicer                                       │
│     └─ AnalyzeOnlineAlarmCluster(request, context)             │
│           ├─ 1. Tokenise alarms                                 │
│           ├─ 2. Lookup embedding (Embedder)                     │
│           ├─ 3. DBSCAN clustering (online_clustering)           │
│           └─ 4. Return OnlineAlarmClusterReport  ◄─────────────│
└─────────────────────────────────────────────────────────────────┘
```

**Các file liên quan:**

```
proto/engine.proto          ← Contract (nguồn sự thật duy nhất)
proto/engine_pb2.py         ← Message classes (auto-generated)
proto/engine_pb2_grpc.py    ← Stub + Servicer base (auto-generated)
server.py                   ← Server implementation
test_client.py              ← Client implementation
embedder.py                 ← Embedding lookup
online_clustering.py        ← DBSCAN logic
```

---

## 3. Bước 1 — Định nghĩa Contract với Protocol Buffers

File `proto/engine.proto` là **contract** giữa client và server. Cả hai bên đều generate code từ file này.

```protobuf
syntax = "proto3";
package alarm_clustering;

// ── 1. Khai báo Service và RPC methods ──────────────────────────
service AlarmClusteringService {
    rpc AnalyzeOnlineAlarmCluster(AnalyzeOnlineAlarmClusterRequest)
        returns (OnlineAlarmClusterReport);
}

// ── 2. Định nghĩa Request messages ──────────────────────────────
message AlarmRecord {
    string _source_id         = 1;   // field number (bắt buộc, không đổi)
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
    repeated AlarmRecord alarmRecords = 3;  // repeated = list/array
}

// ── 3. Định nghĩa Response messages ─────────────────────────────
message OnlineAlarmClusterResult {
    string _source_id = 1;
    int32  clusterId  = 2;   // -2=OOV, -1=noise, 0+=cluster
    float  confidence = 3;
}

message OnlineAlarmClusterReport {
    string requestId                          = 1;
    string reportType                         = 2;
    string status                             = 3;   // OK | WARN | ERROR
    string message                            = 4;
    repeated OnlineAlarmClusterResult results = 5;
}
```

### Quy tắc viết `.proto`

| Quy tắc | Lý do |
|---------|-------|
| Field number không bao giờ thay đổi | Protobuf dùng number để encode, không dùng tên |
| Chỉ thêm field mới, không xoá | Backward compatibility |
| `repeated` = list | Tương đương `List[T]` trong Python |
| Dùng `string` cho timestamp | Tránh timezone bugs; parse phía app |
| `syntax = "proto3"` | Tất cả field đều optional, default = zero value |

---

## 4. Bước 2 — Generate Python Stubs

Từ một file `.proto`, `grpc_tools.protoc` sinh ra **hai file Python**:

```bash
python -m grpc_tools.protoc \
    -I proto \                        # thư mục chứa .proto
    --python_out=proto \              # output cho message classes
    --grpc_python_out=proto \         # output cho stub + servicer
    proto/engine.proto
```

**Kết quả:**

```
proto/engine_pb2.py         ← Các class: AlarmRecord, AnalyzeOnlineAlarmClusterRequest, ...
proto/engine_pb2_grpc.py    ← AlarmClusteringServiceStub (client)
                               AlarmClusteringServiceServicer (server base class)
                               add_AlarmClusteringServiceServicer_to_server()
```

> **Lưu ý**: Không bao giờ sửa tay hai file generated này. Mọi thay đổi contract → sửa `.proto` → re-generate.

### Fix import trong generated stub

`grpc_tools.protoc` sinh ra `import engine_pb2` (bare import). Khi `proto/` là một Python package, cần patch:

```python
# proto/engine_pb2_grpc.py (dòng 6) — đã được patch:
try:
    from proto import engine_pb2 as engine__pb2
except ImportError:
    import engine_pb2 as engine__pb2  # fallback khi proto/ trên sys.path
```

Và thêm `proto/__init__.py` để Python nhận `proto/` là package:

```python
# proto/__init__.py
# Makes proto/ a Python package so relative imports within generated stubs work.
```

---

## 5. Bước 3 — Implement Servicer (Business Logic)

Servicer là class kế thừa từ **base class được generated**, override từng RPC method.

```python
# server.py
import proto.engine_pb2      as pb2
import proto.engine_pb2_grpc as pb2_grpc
from embedder          import Embedder
from online_clustering import cluster_online

class AlarmClusteringServicer(pb2_grpc.AlarmClusteringServiceServicer):
    """
    Implement 1 method cho mỗi rpc trong .proto
    """

    def __init__(self, model_dir: str) -> None:
        # Load embedding model một lần duy nhất (singleton)
        self._embedder = Embedder.get_instance(model_dir)

    # Tên method phải khớp chính xác với tên rpc trong .proto
    def AnalyzeOnlineAlarmCluster(
        self,
        request: pb2.AnalyzeOnlineAlarmClusterRequest,  # typed từ generated code
        context: grpc.ServicerContext,                   # dùng để set error code
    ) -> pb2.OnlineAlarmClusterReport:

        # 1. Validate
        if len(request.alarmRecords) == 0:
            return pb2.OnlineAlarmClusterReport(
                requestId  = request.requestId,
                status     = "ERROR",
                message    = "Request contains no alarm records.",
            )

        # 2. Tokenise & embed
        records = []
        for alarm in request.alarmRecords:
            token  = f"{alarm.managed_objects}|{alarm.probable_cause}"
            vector = self._embedder.lookup(token)   # None nếu OOV
            records.append({
                "source_id": alarm._source_id,
                "vector":    vector,
            })

        # 3. Cluster
        result = cluster_online(records)

        # 4. Build và trả về response message
        return pb2.OnlineAlarmClusterReport(
            requestId  = request.requestId,
            reportType = "ONLINE_CLUSTER",
            status     = "OK",
            message    = f"{result.n_clusters} clusters found",
            results    = [
                pb2.OnlineAlarmClusterResult(
                    _source_id = item.source_id,
                    clusterId  = item.cluster_id,
                    confidence = item.confidence,
                )
                for item in result.items
            ],
        )
```

### Anatomy của một Servicer method

```
def AnalyzeOnlineAlarmCluster(self, request, context):
                                     ▲            ▲
                    Protobuf message ┘            │
                    (typed, deserialized auto)    │
                                                  └─ grpc.ServicerContext
                                                     dùng để:
                                                     - context.set_code(grpc.StatusCode.INTERNAL)
                                                     - context.set_details("error message")
                                                     - context.is_active() → check nếu client cancel
```

---

## 6. Bước 4 — Khởi động Server

```python
# server.py
from concurrent import futures
import grpc

def serve(port: int, model_dir: str, max_workers: int, health_port: int):

    # 1. Tạo gRPC server với thread pool
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        options=[
            ("grpc.max_receive_message_length", 64 * 1024 * 1024),  # 64 MB
            ("grpc.max_send_message_length",    64 * 1024 * 1024),
        ],
    )

    # 2. Đăng ký servicer vào server
    servicer = AlarmClusteringServicer(model_dir)
    pb2_grpc.add_AlarmClusteringServiceServicer_to_server(servicer, server)
    #         ▲ hàm này được generate tự động từ .proto

    # 3. Bind port và start
    server.add_insecure_port(f"[::]:{port}")  # [::] = lắng nghe mọi interface
    server.start()

    # 4. Graceful shutdown khi nhận SIGTERM/SIGINT
    def _shutdown(signum, frame):
        server.stop(grace=5)   # chờ tối đa 5s cho requests đang xử lý
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    # 5. Block main thread
    while True:
        time.sleep(1)
```

### Thread model của gRPC server

```
main thread ──► server.start()   (non-blocking)
                    │
                    ├── Thread 1 ──► AnalyzeOnlineAlarmCluster (request A)
                    ├── Thread 2 ──► AnalyzeOnlineAlarmCluster (request B)
                    └── Thread N ──► ...    (max_workers = 10)
```

Mỗi RPC call được xử lý trong một thread riêng từ pool. `Embedder` dùng singleton pattern nên được share an toàn (read-only sau khi load).

---

## 7. Bước 5 — Implement Client

```python
# test_client.py
import grpc
import proto.engine_pb2      as pb2
import proto.engine_pb2_grpc as pb2_grpc

# 1. Tạo channel (kết nối tới server)
channel = grpc.insecure_channel("localhost:50051")

# 2. Tạo stub (proxy object sinh code tự động)
stub = pb2_grpc.AlarmClusteringServiceStub(channel)

# 3. Build request message
request = pb2.AnalyzeOnlineAlarmClusterRequest(
    requestId    = "req-001",
    system       = "IMS_CORE",
    alarmRecords = [
        pb2.AlarmRecord(
            _source_id         = "alarm-001",
            managed_objects    = "vdu_csdb.vnfc_csdb1",
            alarmType          = "LINK_TO_DNSGW_DOWN",
            probable_cause     = "LINK_TO_DNSGW_DOWN",
            perceived_severity = "MAJOR",
            state              = "ACTIVE",
            created_at         = "2024-01-15T10:00:00Z",
        ),
        # ... thêm alarm records
    ],
)

# 4. Gọi RPC — blocking call (giống gọi hàm local)
report = stub.AnalyzeOnlineAlarmCluster(request)

# 5. Đọc response
print(f"Status : {report.status}")
print(f"Message: {report.message}")
for r in report.results:
    print(f"  {r._source_id} → cluster={r.clusterId}  conf={r.confidence:.3f}")

# 6. Đóng channel khi xong
channel.close()
```

### Secure Channel (production)

```python
# TLS — dùng trong production thay cho insecure_channel
with open("ca.crt", "rb") as f:
    creds = grpc.ssl_channel_credentials(f.read())

channel = grpc.secure_channel("alarm-cluster-engine:50051", creds)
```

---

## 8. Luồng xử lý request đầy đủ

```
Client                              Server
  │                                   │
  │  AnalyzeOnlineAlarmClusterRequest │
  │ ─────────────────────────────────►│
  │  (Protobuf binary, HTTP/2 frame)  │
  │                                   ├─ Deserialize → pb2.AnalyzeOnlineAlarmClusterRequest
  │                                   ├─ AlarmClusteringServicer.AnalyzeOnlineAlarmCluster()
  │                                   │     ├─ Validate (empty check)
  │                                   │     ├─ Tokenise alarms
  │                                   │     │   token = managed_objects|probable_cause
  │                                   │     ├─ Embedder.lookup(token)
  │                                   │     │   ├─ In vocab  → vector (22-dim)
  │                                   │     │   └─ OOV       → None
  │                                   │     ├─ cluster_online(records)
  │                                   │     │   ├─ auto_eps() via k-NN
  │                                   │     │   ├─ DBSCAN.fit_predict()
  │                                   │     │   └─ density_confidence()
  │                                   │     └─ Build OnlineAlarmClusterReport
  │                                   ├─ Serialize → Protobuf binary
  │  OnlineAlarmClusterReport        │
  │ ◄─────────────────────────────────│
  │                                   │
```

---

## 9. Error Handling & Status Codes

### Application-level errors (dùng `status` field trong response)

Đây là pattern được dùng trong project này — trả về response hợp lệ kèm status code riêng:

| `status` | Nghĩa | Ví dụ |
|----------|-------|-------|
| `"OK"` | Thành công | 2 clusters tìm được |
| `"WARN"` | Thành công nhưng có cảnh báo | < `min_samples` alarms → 0 clusters |
| `"ERROR"` | Lỗi nghiệp vụ | Tất cả alarms đều OOV |

### gRPC-level errors (dùng `context`)

Cho các lỗi infrastructure / unexpected exceptions:

```python
def AnalyzeOnlineAlarmCluster(self, request, context):
    try:
        result = cluster_online(records)
    except Exception as exc:
        # Set gRPC status code — client sẽ nhận RpcError
        context.set_code(grpc.StatusCode.INTERNAL)
        context.set_details(f"Clustering error: {exc}")
        return pb2.OnlineAlarmClusterReport(status="ERROR", ...)
```

**Các StatusCode thường dùng:**

| Code | HTTP tương đương | Khi nào dùng |
|------|-----------------|--------------|
| `OK` | 200 | Thành công |
| `INVALID_ARGUMENT` | 400 | Request không hợp lệ |
| `NOT_FOUND` | 404 | Resource không tồn tại |
| `INTERNAL` | 500 | Lỗi server không mong đợi |
| `UNAVAILABLE` | 503 | Server chưa sẵn sàng |

### Xử lý lỗi phía Client

```python
try:
    report = stub.AnalyzeOnlineAlarmCluster(request)
except grpc.RpcError as e:
    print(f"gRPC error: {e.code()} — {e.details()}")
```

---

## 10. Health Probes cho Kubernetes

gRPC server không có HTTP natively, nên cần thêm HTTP health endpoint riêng để K8s có thể probe:

```python
# server.py
import http.server, threading

class _HealthHandler(http.server.BaseHTTPRequestHandler):
    ready_event: threading.Event = threading.Event()

    def do_GET(self):
        if self.path == "/live":
            # Liveness: process còn sống?
            self._respond(200, b"LIVE")
        elif self.path in ("/ready", "/health"):
            # Readiness: embedder đã load xong và gRPC đang up?
            code = 200 if self.ready_event.is_set() else 503
            self._respond(code, b"READY" if code == 200 else b"NOT READY")

    def _respond(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args): pass  # suppress logs

def serve(...):
    ready_event = threading.Event()

    # Start health server TRƯỚC gRPC → /live trả 200 ngay
    threading.Thread(
        target=lambda: http.server.HTTPServer(("0.0.0.0", 8080), _HealthHandler).serve_forever(),
        daemon=True,
    ).start()

    server = grpc.server(...)
    server.start()

    # Signal /ready sau khi gRPC đã start
    ready_event.set()
```

**Mapping sang K8s probes** (`k8s/deployment.yaml`):

```yaml
livenessProbe:             # Restart pod nếu process treo
  httpGet:
    path: /live
    port: 8080
  initialDelaySeconds: 10

readinessProbe:            # Không route traffic cho đến khi /ready = 200
  httpGet:
    path: /ready
    port: 8080
  initialDelaySeconds: 5

startupProbe:              # Cho thêm 60s để load model lần đầu
  httpGet:
    path: /ready
    port: 8080
  failureThreshold: 12
  periodSeconds: 5
```

---

## 11. Chạy và kiểm thử

### Chạy local

```bash
# Terminal 1 — Start server
python server.py --port 50051 --health-port 8080 --model-dir models

# Terminal 2 — Kiểm tra health
curl http://localhost:8080/live    # → LIVE
curl http://localhost:8080/ready   # → READY

# Terminal 2 — Gửi test requests (4 scenarios)
python test_client.py --scenario all

# Chạy một scenario cụ thể
python test_client.py --scenario normal
python test_client.py --scenario oov
python test_client.py --scenario small_batch
python test_client.py --scenario empty
```

### CLI options của server

```bash
python server.py --help

# Options:
#   --port          gRPC port          (default: 50051, env: GRPC_PORT)
#   --health-port   HTTP probe port    (default: 8080,  env: HEALTH_PORT)
#   --model-dir     embeddings dir     (default: models, env: MODEL_DIR)
#   --workers       thread pool size   (default: 10,    env: MAX_WORKERS)
```

### Output mong đợi khi test thành công

```
============================================================
Alarm Cluster Engine — Test Client
  Server: localhost:50051
============================================================

[Scenario: NORMAL] Sending a realistic alarm batch…
────────────────────────────────────────────────────────────
  requestId  : req-normal-001
  status     : OK
  message    : Clustered 10 alarms → 2 clusters | 2 noise | 0 OOV | ...
  results    : 10 items
  Cluster distribution:
       Noise (id= -1)  :  2 alarms
          C0 (id=  0)  :  4 alarms
          C1 (id=  1)  :  4 alarms
────────────────────────────────────────────────────────────

[Scenario: OOV] → status: ERROR  (all clusterId = -2)
[Scenario: SMALL BATCH] → status: WARN  (< min_samples)
[Scenario: EMPTY] → status: ERROR  (no records)
```

---

## Tóm tắt quy trình xây dựng

```
1. Viết engine.proto          → định nghĩa API contract
         │
         ▼
2. grpc_tools.protoc          → sinh engine_pb2.py + engine_pb2_grpc.py
         │
         ▼
3. Implement Servicer          → kế thừa AlarmClusteringServiceServicer
   (server.py)                    override AnalyzeOnlineAlarmCluster()
         │
         ▼
4. Đăng ký & start server     → grpc.server() → add_servicer → start()
         │
         ▼
5. Implement Client            → insecure_channel() → Stub → gọi RPC
   (test_client.py)
         │
         ▼
6. Thêm health probe           → HTTP /live /ready trên port 8080
   (cho Kubernetes)
```
