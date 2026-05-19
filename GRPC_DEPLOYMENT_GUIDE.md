# gRPC Engine — Hướng dẫn triển khai chi tiết

Tài liệu này giải thích từng tầng của hệ thống theo thứ tự từ định nghĩa contract đến khi pod nhận được request thật.

---

## Tổng quan kiến trúc

```
┌─────────────────────────────────────────────────────────────┐
│  Caller Pod (MDAF Logic, ...)                               │
│   grpc.insecure_channel("alarm-cluster-engine:50051")       │
└──────────────────────────┬──────────────────────────────────┘
                           │  gRPC / HTTP2 / TCP
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Kubernetes Service  (ClusterIP : 50051)                    │
│   selector: app=alarm-cluster-engine                        │
└──────────────────────────┬──────────────────────────────────┘
                           │  kube-proxy round-robin
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Pod 1                       Pod 2                          │
│  server_betterproto.py       server_betterproto.py          │
│  grpc.aio  :50051            grpc.aio  :50051               │
└─────────────────────────────────────────────────────────────┘
```

Mỗi tầng bên dưới giải thích một phần cụ thể trong sơ đồ trên.

---

## Phần 1 — Định nghĩa contract: `proto/engine.proto`

**Vai trò:** Đây là ngôn ngữ chung giữa caller và server. Cả hai phía chỉ cần file `.proto` này để biết gửi/nhận gì.

```proto
syntax = "proto3";
package alarm_clustering;

service AlarmClusteringService {
    rpc AnalyzeOnlineAlarmCluster(AnalyzeOnlineAlarmClusterRequest)
        returns (OnlineAlarmClusterReport);
}
```

**Các thành phần quan trọng:**

| Thành phần | Ý nghĩa |
|---|---|
| `package alarm_clustering` | Namespace trên wire — mọi route gRPC sẽ bắt đầu bằng `/alarm_clustering.` |
| `service AlarmClusteringService` | Tên service — ghép với package thành `/alarm_clustering.AlarmClusteringService/` |
| `rpc AnalyzeOnlineAlarmCluster(...)` | Tên method — caller gọi đúng tên này |
| `message AlarmRecord` | Dữ liệu truyền qua wire ở dạng binary (protobuf encoding) |
| Field number `= 1`, `= 2` ... | **Không bao giờ đổi** — đây là định danh thật trên wire, không phải tên field |

> **Quy tắc:** Thêm field mới → dùng số tiếp theo. Xóa field → để trống số đó, không tái sử dụng. Đổi kiểu dữ liệu của field đã tồn tại → breaking change.

---

## Phần 2 — Sinh code từ proto

Có hai bộ generator được dùng trong project này.

### 2a. Generator gốc: `grpc_tools.protoc`

Sinh ra hai file trong `proto/`:

```
proto/engine_pb2.py       ← message classes (descriptor-based)
proto/engine_pb2_grpc.py  ← ServiceStub (client) + ServiceServicer (server base)
```

Lệnh sinh:
```bash
python -m grpc_tools.protoc \
    --proto_path=. \
    --python_out=proto \
    --grpc_python_out=proto \
    proto/engine.proto
```

### 2b. Generator BetterProto

Sinh ra `proto_betterproto/engine.py` với các **Python dataclass** thay vì descriptor bytes.

```bash
pip install "betterproto[compiler]>=0.3.1"

python -m grpc_tools.protoc \
    --proto_path=. \
    --python_betterproto_out=proto_betterproto \
    proto/engine.proto
```

Hoặc dùng script có sẵn:
```bash
python generate_betterproto.py
```

**Kết quả sinh ra (messages):**
```python
@dataclass
class AlarmRecord(betterproto.Message):
    source_id: str = betterproto.string_field(1)   # proto: _source_id
    managed_objects: str = betterproto.string_field(2)
    ...
```

**Lưu ý quan trọng — tên field bị đổi:**

| Proto field | Python field (BetterProto) | Lý do |
|---|---|---|
| `_source_id` | `source_id` | BetterProto bỏ underscore đầu |
| `requestId` | `request_id` | camelCase → snake_case |
| `alarmRecords` | `alarm_records` | camelCase → snake_case |
| `clusterId` | `cluster_id` | camelCase → snake_case |

> Tên field chỉ quan trọng trong Python code. Trên wire vẫn dùng **field number** nên serialize/deserialize vẫn đúng.

### 2c. Adapter grpcio (viết tay — không bị overwrite)

BetterProto sinh ServiceBase cho `grpclib`, không phải `grpcio`. File `proto_betterproto/grpcio_support.py` là adapter viết tay để bridge BetterProto types với grpcio:

```python
# proto_betterproto/grpcio_support.py
def add_to_server(servicer, server):
    handlers = {
        "AnalyzeOnlineAlarmCluster": grpc.unary_unary_rpc_method_handler(
            servicer.analyze_online_alarm_cluster,
            request_deserializer=AnalyzeOnlineAlarmClusterRequest.FromString,
            response_serializer=bytes,   # betterproto.Message.__bytes__
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "alarm_clustering.AlarmClusteringService", handlers
    )
    server.add_generic_rpc_handlers([generic_handler])
```

**Tại sao `response_serializer=bytes`?**
`bytes(message)` gọi `__bytes__` của `betterproto.Message`, trả về protobuf binary. grpcio gọi `serializer(response)` → `bytes(response)` → đúng wire format.

---

## Phần 3 — Server: `server_betterproto.py`

### 3a. Servicer class

```python
class AlarmClusteringServicer:
    def __init__(self, model_dir: str) -> None:
        self._embedder = Embedder.get_instance(model_dir)  # load model 1 lần khi khởi động

    async def analyze_online_alarm_cluster(
        self,
        request: AnalyzeOnlineAlarmClusterRequest,  # BetterProto dataclass
        context: grpc.aio.ServicerContext,
    ) -> OnlineAlarmClusterReport:
        ...
```

- Method phải là `async def` vì server dùng `grpc.aio`.
- `context` là handle để set error code nếu cần (`context.set_code(grpc.StatusCode.INTERNAL)`).
- Không cần kế thừa base class nào — grpcio chỉ cần method này tồn tại dưới đúng tên.

### 3b. Xử lý CPU-bound operation

```python
loop = asyncio.get_event_loop()
result = await loop.run_in_executor(None, cluster_online, records)
```

`cluster_online` chạy DBSCAN — thuần CPU, blocking. Nếu gọi trực tiếp trong async method, nó chiếm event loop, mọi request đến trong lúc đó bị treo. `run_in_executor` đưa nó sang thread pool, event loop vẫn tự do nhận request mới.

### 3c. Khởi động server

```python
async def main() -> None:
    server = grpc.aio.server(options=[
        ("grpc.max_receive_message_length", 64 * 1024 * 1024),  # 64 MB
        ("grpc.max_send_message_length",    64 * 1024 * 1024),
    ])

    add_to_server(AlarmClusteringServicer(model_dir), server)  # đăng ký handler
    server.add_insecure_port(f"[::]:{port}")                   # mở port
    await server.start()                                        # bắt đầu nhận connection
    await server.wait_for_termination()                         # giữ process sống
```

**Tại sao cần `max_receive_message_length`?**
Default của grpc là 4 MB. Một batch vài nghìn alarm record dễ vượt giới hạn này — server tự từ chối mà không log gì rõ ràng.

**Graceful shutdown:**
`grpc.aio` tự lắng nghe SIGTERM (tín hiệu K8s gửi khi xóa pod). Khi nhận SIGTERM, server ngừng nhận request mới, đợi request đang xử lý xong (trong `terminationGracePeriodSeconds: 30`), rồi tắt.

---

## Phần 4 — Docker image: `Dockerfile`

```dockerfile
FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt   # ← layer riêng để cache

COPY proto/             proto/
COPY proto_betterproto/ proto_betterproto/
COPY *.py               ./
COPY models/embeddings.npz models/embeddings.npz     # model baked into image

ENV GRPC_PORT=50051 \
    MODEL_DIR=/app/models

EXPOSE 50051

RUN useradd -m -u 1000 appuser && chown -R appuser /app
USER appuser                                          # không chạy root

CMD ["python", "server_betterproto.py"]
```

**Thứ tự COPY quan trọng:**
`requirements.txt` + `pip install` được copy trước source code. Docker cache layer theo thứ tự — nếu chỉ thay đổi code Python mà không thay đổi `requirements.txt`, layer `pip install` được dùng lại từ cache, build nhanh hơn nhiều.

**Build và push:**
```bash
docker build -t <YOUR_REGISTRY>/alarm-cluster-engine:1.0.0 .
docker push  <YOUR_REGISTRY>/alarm-cluster-engine:1.0.0
```

---

## Phần 5 — Kubernetes resources

### 5a. ConfigMap — biến môi trường

```yaml
# k8s/configmap.yaml
data:
  GRPC_PORT: "50051"
  MODEL_DIR: "/app/models"
```

Server đọc các biến này qua `os.environ.get(...)`. ConfigMap tách config khỏi image — đổi cổng hay đường dẫn model không cần rebuild image.

### 5b. Deployment — chạy pod

**Các trường quan trọng và lý do:**

```yaml
replicas: 2        # tối thiểu 2 để không có single point of failure
```

```yaml
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxUnavailable: 0   # không bao giờ giảm dưới 2 pod trong khi deploy
    maxSurge: 1         # tạm thời có 3 pod khi rollout
```

```yaml
terminationGracePeriodSeconds: 30  # K8s đợi 30s sau SIGTERM trước khi SIGKILL
```

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 1000       # khớp với user `appuser` trong Dockerfile
```

```yaml
resources:
  requests:
    cpu:    "250m"      # scheduler dùng để chọn node đặt pod
    memory: "256Mi"
  limits:
    cpu:    "1000m"     # bị throttle nếu vượt
    memory: "512Mi"     # bị OOMKilled nếu vượt
```

**Health probes — cách K8s biết pod có hoạt động không:**

```yaml
startupProbe:           # chạy ĐẦU TIÊN, cho phép model load trước
  tcpSocket:
    port: grpc          # kiểm tra port 50051 có mở không
  failureThreshold: 12  # thử 12 lần × 5s = 60s tối đa để khởi động
  periodSeconds: 5

livenessProbe:          # chạy SAU KHI startupProbe pass
  tcpSocket:
    port: grpc
  initialDelaySeconds: 10
  periodSeconds: 15
  failureThreshold: 3   # fail 3 lần liên tiếp → K8s restart pod

readinessProbe:         # khi fail → pod bị rút khỏi Service, không nhận traffic
  tcpSocket:
    port: grpc
  initialDelaySeconds: 5
  periodSeconds: 10
  failureThreshold: 3
```

**Thứ tự kiểm tra:**
```
Pod start → startupProbe (tối đa 60s) → pass
         → liveness + readiness bắt đầu chạy song song
         → readiness pass → pod được thêm vào Service endpoint → nhận traffic
```

**Anti-affinity** — đảm bảo 2 pod không nằm cùng node:
```yaml
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          labelSelector:
            matchLabels:
              app: alarm-cluster-engine
          topologyKey: kubernetes.io/hostname
```
Nếu node bị mất, chỉ mất 1 pod thay vì cả 2.

### 5c. Service — định tuyến traffic vào pod

```yaml
# k8s/service.yaml
spec:
  type: ClusterIP          # chỉ accessible trong cluster
  selector:
    app: alarm-cluster-engine   # route đến pod có label này
  ports:
    - name: grpc
      port: 50051           # caller dùng cổng này
      targetPort: grpc      # map vào containerPort 50051 của pod
```

Caller trong cluster kết nối bằng DNS name:
```python
channel = grpc.insecure_channel("alarm-cluster-engine:50051")
# hoặc đầy đủ hơn:
channel = grpc.insecure_channel("alarm-cluster-engine.default.svc.cluster.local:50051")
```

### 5d. PodDisruptionBudget — bảo vệ khi drain node

```yaml
# k8s/pdb.yaml
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: alarm-cluster-engine
```

Khi admin chạy `kubectl drain node` (bảo trì node), K8s không được xóa pod đến mức còn ít hơn 1 pod available. Nếu không có PDB, drain có thể xóa hết 2 pod cùng lúc → downtime.

### 5e. PersistentVolumeClaim — model hot-swap (tuỳ chọn)

```yaml
# k8s/pvc.yaml
spec:
  accessModes:
    - ReadOnlyMany    # nhiều pod cùng đọc một lúc
  resources:
    requests:
      storage: 100Mi
```

Mặc định model (`embeddings.npz`) được bake vào image. PVC cho phép cập nhật model mà không rebuild image — upload file mới vào PVC rồi `kubectl rollout restart`.

Để bật, uncomment trong `deployment.yaml`:
```yaml
volumeMounts:
  - name: model-volume
    mountPath: /app/models
    readOnly: true
volumes:
  - name: model-volume
    persistentVolumeClaim:
      claimName: alarm-cluster-model-pvc
```

---

## Phần 6 — Deploy lên cluster

```bash
# 1. Build và push image
docker build -t <YOUR_REGISTRY>/alarm-cluster-engine:1.0.0 .
docker push  <YOUR_REGISTRY>/alarm-cluster-engine:1.0.0

# 2. Cập nhật tên image trong deployment.yaml
#    Tìm dòng: image: <YOUR_REGISTRY>/alarm-cluster-engine:latest
#    Đổi thành image thật

# 3. Apply toàn bộ resources một lần
kubectl apply -k k8s/

# 4. Theo dõi rollout
kubectl rollout status deployment/alarm-cluster-engine

# 5. Kiểm tra pod
kubectl get pods -l app=alarm-cluster-engine
kubectl logs -l app=alarm-cluster-engine --follow
```

**Rollout image mới:**
```bash
# Đổi tag trong deployment.yaml rồi:
kubectl apply -k k8s/
# hoặc dùng kustomize:
kustomize edit set image alarm-cluster-engine=<YOUR_REGISTRY>/alarm-cluster-engine:<NEW_TAG>
kubectl apply -k k8s/
```

---

## Phần 7 — Caller kết nối như thế nào

Caller (pod khác trong cluster) cần cùng file `.proto` để biết contract:

```python
import grpc
import proto.engine_pb2      as pb2
import proto.engine_pb2_grpc as pb2_grpc

channel  = grpc.insecure_channel("alarm-cluster-engine:50051")
stub     = pb2_grpc.AlarmClusteringServiceStub(channel)
response = stub.AnalyzeOnlineAlarmCluster(
    pb2.AnalyzeOnlineAlarmClusterRequest(
        requestId    = "req-001",
        system       = "IMS",
        alarmRecords = [
            pb2.AlarmRecord(
                _source_id         = "src-1",
                managed_objects    = "NodeA",
                probable_cause     = "linkDown",
                perceived_severity = "MAJOR",
            )
        ],
    )
)
print(response.status, response.message)
```

Nếu caller cũng dùng BetterProto:
```python
import grpclib.client
from proto_betterproto.engine import (
    AlarmClusteringServiceStub,
    AnalyzeOnlineAlarmClusterRequest,
    AlarmRecord,
)

async def call():
    async with grpclib.client.Channel("alarm-cluster-engine", 50051) as channel:
        stub = AlarmClusteringServiceStub(channel)
        response = await stub.analyze_online_alarm_cluster(
            AnalyzeOnlineAlarmClusterRequest(
                request_id    = "req-001",
                system        = "IMS",
                alarm_records = [AlarmRecord(source_id="src-1", ...)],
            )
        )
```

---

## Tóm tắt luồng từ đầu đến cuối

```
engine.proto          →  định nghĩa contract (field number là bất biến)
       │
       ├── grpc_tools.protoc  →  proto/engine_pb2.py + engine_pb2_grpc.py
       │                         (dùng cho caller dùng grpcio)
       │
       └── betterproto        →  proto_betterproto/engine.py
                                  (dataclass messages + grpclib stub)
                                 proto_betterproto/grpcio_support.py
                                  (adapter grpcio — viết tay)
                                       │
                              server_betterproto.py
                              (AlarmClusteringServicer + asyncio.run)
                                       │
                                   Dockerfile
                              (python:3.12-slim, non-root, EXPOSE 50051)
                                       │
                              k8s/configmap.yaml   ← env vars
                              k8s/deployment.yaml  ← pod spec, probes, resources
                              k8s/service.yaml     ← ClusterIP :50051
                              k8s/pdb.yaml         ← minAvailable: 1
                              k8s/pvc.yaml         ← model hot-swap (optional)
                                       │
                              kubectl apply -k k8s/
```
