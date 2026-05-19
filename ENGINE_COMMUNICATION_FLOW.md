# Luồng Giao Tiếp Engine ↔ server_betterproto.py

Tài liệu kỹ thuật chi tiết về toàn bộ luồng dữ liệu từ khi client gửi request cho đến khi server trả về kết quả phân cụm alarm.

---

## 1. Kiến Trúc Tổng Quan

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLIENT (Engine)                         │
│  test_client.py  /  bất kỳ gRPC client nào                     │
│                                                                 │
│  proto/engine_pb2_grpc.AlarmClusteringServiceStub               │
│  channel = grpc.insecure_channel("host:50051")                  │
└────────────────────────────┬────────────────────────────────────┘
                             │  HTTP/2  (gRPC wire protocol)
                             │  Binary Protobuf payload
                             │  port :50051 (default)
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                    server_betterproto.py                        │
│                                                                 │
│  grpc.aio.Server  (asyncio event loop)                          │
│  ├── add_to_server()  → đăng ký handler thủ công               │
│  └── AlarmClusteringServicer                                    │
│       ├── Embedder (singleton, load 1 lần lúc khởi động)       │
│       └── analyze_online_alarm_cluster()                        │
│            └── online_clustering.cluster_online()  (thread)    │
└─────────────────────────────────────────────────────────────────┘
```

**Công nghệ sử dụng:**

| Thành phần | Thư viện |
|---|---|
| gRPC transport | `grpcio` + `grpc.aio` (asyncio) |
| Protobuf types | `betterproto` (dataclass, không dùng `grpcio`-gen stub) |
| Embedding model | NumPy + `embeddings.npz` (Skip-Gram pre-trained) |
| Clustering | scikit-learn `DBSCAN` + `NearestNeighbors` |

---

## 2. Định Nghĩa Proto — Nguồn Sự Thật

File: [proto/engine.proto](proto/engine.proto)

```protobuf
package alarm_clustering;

service AlarmClusteringService {
    rpc AnalyzeOnlineAlarmCluster(AnalyzeOnlineAlarmClusterRequest)
        returns (OnlineAlarmClusterReport);
}

message AlarmRecord {
    string _source_id         = 1;   // ID duy nhất của alarm
    string managed_objects    = 2;   // VD: "vdu_csdb.vnfc_csdb1"
    string alarmType          = 3;
    string probable_cause     = 4;   // VD: "LINK_TO_DNSGW_DOWN"
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

message OnlineAlarmClusterResult {
    string _source_id = 1;
    int32  clusterId  = 2;  // -2=OOV, -1=Noise, 0+=cluster
    float  confidence = 3;  // [0.0, 1.0]
}

message OnlineAlarmClusterReport {
    string requestId                          = 1;
    string reportType                         = 2;  // "ONLINE_CLUSTER"
    string status                             = 3;  // "OK" | "WARN" | "ERROR"
    string message                            = 4;  // human-readable summary
    repeated OnlineAlarmClusterResult results = 5;
}
```

**BetterProto field mapping** — tên field trong Python khác proto gốc:

| Proto field | Python attribute |
|---|---|
| `_source_id` | `source_id` (leading `_` bị bỏ) |
| `requestId` | `request_id` |
| `alarmRecords` | `alarm_records` |
| `alarmType` | `alarm_type` |
| `clusterId` | `cluster_id` |
| `reportType` | `report_type` |

---

## 3. Khởi Động Server

File: [server_betterproto.py:134-152](server_betterproto.py#L134-L152)

```
asyncio.run(main())
         │
         ▼
main()
 ├── đọc env: GRPC_PORT (default 50051), MODEL_DIR (default "models")
 ├── grpc.aio.server(options=[
 │       max_receive_message_length = 64 MB,
 │       max_send_message_length    = 64 MB,
 │   ])
 ├── AlarmClusteringServicer(model_dir)
 │    └── Embedder.get_instance(model_dir)
 │         └── load models/embeddings.npz → ma trận (V×D) float32
 │              L2-normalize toàn bộ ma trận 1 lần duy nhất
 │
 ├── add_to_server(servicer, server)
 │    └── đăng ký handler thủ công (không dùng betterproto server-side)
 │
 ├── server.add_insecure_port("[::]:<port>")
 └── server.start() → server.wait_for_termination()
```

**Tại sao đăng ký handler thủ công?**

BetterProto chỉ sinh code client (`AlarmClusteringServiceStub`) dùng `grpclib`, không sinh server-side cho `grpcio`. File [server_betterproto.py:37-49](server_betterproto.py#L37-L49) tự đăng ký bằng `grpc.unary_unary_rpc_method_handler`:

```python
handlers = {
    "AnalyzeOnlineAlarmCluster": grpc.unary_unary_rpc_method_handler(
        servicer.analyze_online_alarm_cluster,
        request_deserializer  = AnalyzeOnlineAlarmClusterRequest.FromString,
        response_serializer   = bytes,   # betterproto tự serialize → bytes
    ),
}
server.add_generic_rpc_handlers([
    grpc.method_handlers_generic_handler(
        "alarm_clustering.AlarmClusteringService", handlers
    )
])
```

- `request_deserializer`: dùng `betterproto.Message.FromString` để parse binary proto → Python dataclass.
- `response_serializer`: trả về `bytes` thô vì `OnlineAlarmClusterReport.__bytes__()` của betterproto tự encode.

---

## 4. Luồng Xử Lý Request Chi Tiết

### 4.1 Sequence Diagram

```
Client                  gRPC Server               Servicer              Embedder         OnlineClustering
  │                         │                         │                     │                    │
  │── AnalyzeOnlineAlarm ──►│                         │                     │                    │
  │   (binary proto/HTTP2)  │                         │                     │                    │
  │                         │── deserialize ─────────►│                     │                    │
  │                         │   FromString()          │                     │                    │
  │                         │                         │                     │                    │
  │                         │                    validate alarms            │                    │
  │                         │                    (empty check)              │                    │
  │                         │                         │                     │                    │
  │                         │                         │── make_token() ────►│                    │
  │                         │                         │   per alarm         │                    │
  │                         │                         │◄── token str ───────│                    │
  │                         │                         │                     │                    │
  │                         │                         │── lookup(token) ───►│                    │
  │                         │                         │   per alarm         │                    │
  │                         │                         │◄── np.ndarray|None ─│                    │
  │                         │                         │                     │                    │
  │                         │                    check all-OOV              │                    │
  │                         │                         │                     │                    │
  │                         │                         │── run_in_executor ──────────────────────►│
  │                         │                         │   (thread pool)     │    cluster_online() │
  │                         │                         │◄── OnlineClusteringResult ───────────────│
  │                         │                         │                     │                    │
  │                         │                    build Report               │                    │
  │                         │                         │                     │                    │
  │                         │◄── serialize ───────────│                     │                    │
  │                         │    __bytes__()          │                     │                    │
  │◄── OnlineAlarmCluster ──│                         │                     │                    │
  │    Report (binary)      │                         │                     │                    │
```

### 4.2 Bước 1 — Client gửi Request

Client tạo `AnalyzeOnlineAlarmClusterRequest` (protobuf) và gọi RPC:

```python
# test_client.py — dùng grpcio-generated stub (engine_pb2_grpc)
stub = pb2_grpc.AlarmClusteringServiceStub(channel)
report = stub.AnalyzeOnlineAlarmCluster(req)
```

Payload được serialize thành binary protobuf, gửi qua HTTP/2 đến server.

### 4.3 Bước 2 — Deserialize → Python Dataclass

gRPC gọi `AnalyzeOnlineAlarmClusterRequest.FromString(raw_bytes)` trả về Python dataclass:

```python
@dataclass
class AnalyzeOnlineAlarmClusterRequest(betterproto.Message):
    request_id:   str             # field 1
    system:       str             # field 2
    alarm_records: List[AlarmRecord]  # field 3
```

### 4.4 Bước 3 — Validate Đầu Vào

File: [server_betterproto.py:70-74](server_betterproto.py#L70-L74)

```python
if not alarms:
    return OnlineAlarmClusterReport(
        status="ERROR", message="No alarm records.", results=[],
    )
```

Nếu list alarm rỗng → trả về ngay với `status="ERROR"`, không chạy embedding hay clustering.

### 4.5 Bước 4 — Tokenization + Embedding Lookup

File: [server_betterproto.py:76-85](server_betterproto.py#L76-L85)

Với mỗi alarm, server:

1. **Tạo token** — ghép `managed_objects` và `probable_cause` bằng `|`:
   ```
   token = f"{managed_objects}|{probable_cause}"
   # VD: "vdu_csdb.vnfc_csdb1|LINK_TO_DNSGW_DOWN"
   ```

2. **Lookup vector** — tra bảng embedding (đã L2-normalize):
   ```python
   vector = embedder.lookup(token)  # np.ndarray (dim,) hoặc None nếu OOV
   ```

Kết quả là danh sách records:
```python
records = [
    {"source_id": "id-001", "token": "vdu_csdb.vnfc_csdb1|LINK_TO_DNSGW_DOWN", "vector": np.array([...])},
    {"source_id": "id-002", "token": "unknown|FAKE", "vector": None},  # OOV
    ...
]
```

### 4.6 Bước 5 — Kiểm Tra All-OOV

File: [server_betterproto.py:87-97](server_betterproto.py#L87-L97)

```python
n_oov = sum(1 for r in records if r["vector"] is None)
if n_oov == len(alarms):
    # Tất cả alarm đều không có trong vocab → không thể cluster
    return OnlineAlarmClusterReport(
        status="ERROR",
        message=f"All {len(alarms)} alarms are OOV — cannot cluster.",
        results=[OnlineAlarmClusterResult(source_id=..., cluster_id=-2, confidence=0.0) for ...],
    )
```

### 4.7 Bước 6 — Clustering (Thread Pool)

File: [server_betterproto.py:99-107](server_betterproto.py#L99-L107)

```python
result = await loop.run_in_executor(None, cluster_online, records)
```

Vì `cluster_online` dùng scikit-learn (CPU-bound, không async-friendly), nó được chạy trong **thread pool executor** để không block asyncio event loop của gRPC server.

**Bên trong `cluster_online()`** — [online_clustering.py](online_clustering.py):

```
records
  │
  ├─ Tách OOV (vector=None) → cluster_id=-2, confidence=0.0
  │
  ├─ Kiểm tra số in-vocab < min_samples (3)
  │   └─ Nếu đúng → tất cả in-vocab nhận cluster_id=-1 (noise), return sớm
  │
  ├─ Stack vectors → ma trận (N×D) float32
  │   └─ Re-normalize L2 (để đảm bảo cosine metric)
  │
  ├─ Auto-eps: NearestNeighbors(k=4) → kNN distance → knee point
  │   └─ Nếu thiếu thư viện `kneed` → dùng percentile 90th
  │
  ├─ DBSCAN(eps=auto_eps, min_samples=3, metric="cosine")
  │   └─ labels: -1=noise, 0,1,2,...=cluster
  │
  ├─ Silhouette score (nếu n_clusters ≥ 2)
  │
  └─ Density confidence per point:
      confidence = n_neighbours(point, radius=eps) / max_n_neighbours(cluster)
      Noise → 0.0
```

**Ngữ nghĩa cluster_id:**

| cluster_id | Ý nghĩa |
|---|---|
| `-2` | OOV — token không có trong embedding vocab |
| `-1` | Noise — điểm nhiễu theo DBSCAN |
| `0, 1, 2, ...` | Thuộc cụm cluster số N |

### 4.8 Bước 7 — Xây Dựng Response

File: [server_betterproto.py:109-129](server_betterproto.py#L109-L129)

```python
item_map = {item.source_id: item for item in result.items}

status  = "WARN" if result.n_clusters == 0 else "OK"
message = (
    f"{result.n_clusters} clusters | {result.n_noise} noise | "
    f"{result.n_oov} OOV | eps={result.eps:.4f} | silhouette={sil}"
)

return OnlineAlarmClusterReport(
    request_id  = rid,
    report_type = "ONLINE_CLUSTER",
    status      = status,
    message     = message,
    results     = [
        OnlineAlarmClusterResult(
            source_id  = a.source_id,
            cluster_id = item_map[a.source_id].cluster_id,  # -2 nếu không tìm thấy
            confidence = item_map[a.source_id].confidence,
        )
        for a in alarms
    ],
)
```

**Thứ tự kết quả** giữ nguyên theo thứ tự `alarms` gốc trong request (không sắp xếp lại theo cluster).

---

## 5. Các Đường Lỗi và Cảnh Báo

```
Request đến
     │
     ├──[alarm_records rỗng]──────────────► ERROR: "No alarm records."
     │
     ├──[tất cả token OOV]────────────────► ERROR: "All N alarms are OOV"
     │                                             results: tất cả cluster_id=-2
     │
     ├──[in-vocab < 3 (min_samples)]──────► status=WARN hoặc OK tuỳ cluster count
     │   cluster_online trả n_clusters=0       message: "0 clusters | ..."
     │   → status = "WARN"
     │
     ├──[cluster_online raise Exception]──► ERROR: "Clustering error: <exc>"
     │                                             results: []
     │
     └──[n_clusters ≥ 1]──────────────────► OK: "N clusters | ..."
```

---

## 6. Embedder Singleton — Chi Tiết Kỹ Thuật

File: [embedder.py](embedder.py)

```
Lần đầu: AlarmClusteringServicer.__init__()
    └── Embedder.get_instance(model_dir)
         └── Embedder.__init__()
              ├── load models/embeddings.npz
              │    ├── matrix: np.ndarray (V × D) float32
              │    └── tokens: List[str]
              ├── normalize(matrix, norm="l2")  ← L2-normalize 1 lần duy nhất
              └── _token2idx: Dict[str, int]    ← O(1) lookup

Các lần sau: trả về _instance đã tồn tại (không load lại)
```

**Token format chuẩn** (phải khớp với format dùng lúc train Word2Vec):

```
"{managed_objects}|{probable_cause}"
```

Ví dụ: `"vdu_csdb.vnfc_csdb1|LINK_TO_DNSGW_DOWN"`

**OOV detection:**

```python
def lookup(self, token: str) -> Optional[np.ndarray]:
    idx = self._token2idx.get(token)  # None nếu không có
    if idx is None:
        return None
    return self._matrix_norm[idx]     # vector đã L2-normalize
```

---

## 7. Cấu Hình Runtime

| Biến môi trường | Default | Mô tả |
|---|---|---|
| `GRPC_PORT` | `50051` | Port server lắng nghe |
| `MODEL_DIR` | `"models"` | Thư mục chứa `embeddings.npz` |

**gRPC server options:**

| Option | Giá trị |
|---|---|
| `max_receive_message_length` | 64 MB |
| `max_send_message_length` | 64 MB |

---

## 8. Sơ Đồ Luồng Dữ Liệu Tổng Hợp

```
Client Request
┌──────────────────────────────────────┐
│ AnalyzeOnlineAlarmClusterRequest     │
│   request_id: "req-001"              │
│   system:     "IMS_CORE"             │
│   alarm_records: [                   │
│     {source_id, managed_objects,     │
│      probable_cause, ...} × N        │
│   ]                                  │
└──────────────────┬───────────────────┘
                   │ binary protobuf / HTTP2
                   ▼
         gRPC Deserialization
         FromString() → dataclass
                   │
                   ▼
            Guard: alarms empty? ──YES──► ERROR response
                   │NO
                   ▼
         Per-alarm Tokenization
         make_token(managed_objects, probable_cause)
         → "vdu_csdb.vnfc_csdb1|LINK_TO_DNSGW_DOWN"
                   │
                   ▼
         Embedder.lookup(token)
         ┌──────────────────────┐
         │  embeddings.npz      │
         │  matrix: (V × D)     │
         │  token2idx: dict     │
         └──────────────────────┘
         → np.ndarray(D,) L2-norm  │  None (OOV)
                   │
                   ▼
         Guard: ALL OOV? ──YES──► ERROR response (cluster_id=-2 all)
                   │NO
                   ▼
         run_in_executor (thread pool)
         ┌──────────────────────────────────────────┐
         │ cluster_online(records)                   │
         │  1. Tách OOV (cluster_id=-2)             │
         │  2. Guard: in-vocab < 3? → noise all     │
         │  3. Stack vectors (N×D), re-normalize    │
         │  4. Auto-eps: kNN k=4 → knee/P90         │
         │  5. DBSCAN(eps, min_samples=3, cosine)   │
         │  6. Silhouette (nếu ≥2 clusters)         │
         │  7. Density confidence per point         │
         └──────────────────────────────────────────┘
                   │
                   ▼ OnlineClusteringResult
         Build OnlineAlarmClusterReport
         status = "OK" | "WARN" | "ERROR"
         results: [OnlineAlarmClusterResult × N]
           ├── source_id
           ├── cluster_id: -2 | -1 | 0 | 1 | ...
           └── confidence: [0.0, 1.0]
                   │
                   │ __bytes__() → binary protobuf
                   ▼
              Client Response
```

---

## 9. Khác Biệt Giữa server.py và server_betterproto.py

| Khía cạnh | server.py | server_betterproto.py |
|---|---|---|
| Proto types | `engine_pb2` (grpcio-generated) | `proto_betterproto.engine` (betterproto dataclass) |
| Server registration | `add_AlarmClusteringServicer_to_server()` (generated) | `add_to_server()` thủ công |
| Request type | `pb2.AnalyzeOnlineAlarmClusterRequest` | `betterproto` dataclass |
| Response serialize | grpcio tự động | `bytes` thô, betterproto encode |
| Python style | class-based protobuf | `@dataclass` + type hints |

---

## 10. Chạy Và Kiểm Thử

```bash
# Khởi động server
GRPC_PORT=50051 MODEL_DIR=models python server_betterproto.py

# Chạy test client (dùng grpcio-gen stub)
python test_client.py --host localhost --port 50051 --scenario normal
python test_client.py --scenario oov
python test_client.py --scenario small_batch
python test_client.py --scenario all

# Smoke test
python _smoke_test.py
```

**Kết quả mong đợi theo scenario:**

| Scenario | status | cluster_id phổ biến |
|---|---|---|
| normal (10 alarms, in-vocab) | `OK` | 0, 1, ... |
| oov (tất cả unknown token) | `ERROR` | -2 |
| small_batch (< 3 in-vocab) | `WARN` | -1 (noise) |
| empty | `ERROR` | (không có results) |
