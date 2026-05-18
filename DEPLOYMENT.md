# Alarm Cluster Engine — Deployment Guide

## Architecture

```
MDAF Logic  ──gRPC──►  alarm-cluster-engine (ClusterIP:50051)
                              │
                              ├── embedder.py      (models/embeddings.npz)
                              ├── online_clustering.py  (DBSCAN)
                              └── HTTP :8080  /live /ready  ◄── K8s probes
```

## Prerequisites

| Tool | Version |
|------|---------|
| Docker | ≥ 24 |
| kubectl | ≥ 1.27 |
| Kubernetes cluster | ≥ 1.27 |
| Container registry | Any (GCR, ECR, GHCR…) |

---

## Step 1 — Build & Push the Docker Image

```bash
# Set your registry
export REGISTRY=gcr.io/YOUR_PROJECT   # or your own registry

# Build (includes models/embeddings.npz ~215 KB)
docker build -t $REGISTRY/alarm-cluster-engine:latest .

# Push
docker push $REGISTRY/alarm-cluster-engine:latest
```

---

## Step 2 — Configure the Image Reference

Edit `k8s/deployment.yaml` and replace the placeholder:

```yaml
# Before
image: <YOUR_REGISTRY>/alarm-cluster-engine:latest

# After
image: gcr.io/YOUR_PROJECT/alarm-cluster-engine:latest
```

Or use the Kustomize image override (recommended for CI/CD):

```bash
cd k8s
kustomize edit set image alarm-cluster-engine=gcr.io/YOUR_PROJECT/alarm-cluster-engine:1.0.0
```

---

## Step 3 — Deploy to Kubernetes

```bash
# Apply all resources (ConfigMap, PVC, Deployment, Service, PDB)
kubectl apply -k k8s/

# Watch rollout
kubectl rollout status deployment/alarm-cluster-engine

# Verify pods are running
kubectl get pods -l app=alarm-cluster-engine
```

Expected output:
```
NAME                                     READY   STATUS    RESTARTS
alarm-cluster-engine-7d4b9f8c6-abcde    1/1     Running   0
alarm-cluster-engine-7d4b9f8c6-fghij    1/1     Running   0
```

---

## Step 4 — Verify Health Probes

```bash
# Port-forward to test locally
kubectl port-forward svc/alarm-cluster-engine 8080:8080

# In another terminal
curl http://localhost:8080/live    # → LIVE
curl http://localhost:8080/ready   # → READY
```

---

## Step 5 — Test the gRPC Service

```bash
# Port-forward gRPC port
kubectl port-forward svc/alarm-cluster-engine 50051:50051

# Run test client
python test_client.py --host localhost --port 50051
```

---

## K8s Resources Summary

| File | Resource | Purpose |
|------|----------|---------|
| `configmap.yaml` | ConfigMap | Runtime env vars (ports, model dir, workers) |
| `pvc.yaml` | PersistentVolumeClaim | Optional hot-swap model storage (100 Mi) |
| `deployment.yaml` | Deployment | 2 replicas, rolling update, health probes |
| `service.yaml` | Service (ClusterIP) | Internal DNS: `alarm-cluster-engine:50051` |
| `pdb.yaml` | PodDisruptionBudget | Keep ≥ 1 pod during node drain / upgrades |
| `kustomization.yaml` | Kustomize | One-command deploy / delete |

---

## Health Probe Endpoints (port 8080)

| Path | Probe Type | Returns |
|------|-----------|---------|
| `GET /live` | Liveness | `200 LIVE` always (process alive) |
| `GET /ready` | Readiness | `200 READY` after embedder loaded + gRPC up; `503` otherwise |
| `GET /health` | Alias for /ready | same as /ready |

---

## Hot-Swap Model Without Rebuilding Image

When embeddings are retrained:

```bash
# 1. Copy new embeddings to the PVC via a temporary pod
kubectl run model-uploader --image=busybox --restart=Never \
  --overrides='{"spec":{"volumes":[{"name":"m","persistentVolumeClaim":{"claimName":"alarm-cluster-model-pvc"}}],"containers":[{"name":"c","image":"busybox","command":["sh","-c","sleep 3600"],"volumeMounts":[{"name":"m","mountPath":"/models"}]}]}}'

kubectl cp models/embeddings.npz model-uploader:/models/embeddings.npz
kubectl delete pod model-uploader

# 2. Uncomment the volumeMounts section in k8s/deployment.yaml

# 3. Rolling restart (pods reload the new model from PVC)
kubectl rollout restart deployment/alarm-cluster-engine
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GRPC_PORT` | `50051` | gRPC listen port |
| `HEALTH_PORT` | `8080` | HTTP health probe port |
| `MODEL_DIR` | `/app/models` | Path to `embeddings.npz` |
| `MAX_WORKERS` | `10` | gRPC thread pool size |

---

## Teardown

```bash
kubectl delete -k k8s/
```
