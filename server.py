"""
server.py
---------
gRPC server for the Alarm Clustering Engine.

Startup
-------
    python server.py [--port 50051] [--health-port 8080] [--model-dir models] [--workers 10]

Environment variables (override CLI defaults)
---------------------------------------------
    GRPC_PORT   : gRPC listen port                  (default: 50051)
    HEALTH_PORT : HTTP health-probe port             (default: 8080)
    MODEL_DIR   : directory with embeddings.npz     (default: models)
    MAX_WORKERS : gRPC thread-pool size              (default: 10)

Health endpoints (for Kubernetes probes)
-----------------------------------------
    GET /live   → 200 OK once server thread has started
    GET /ready  → 200 OK after embedder is loaded AND gRPC server is up
    GET /health → alias for /ready

Request / Response (see proto/engine.proto)
-------------------------------------------
    AnalyzeOnlineAlarmCluster(AnalyzeOnlineAlarmClusterRequest)
        → OnlineAlarmClusterReport

Status field values
-------------------
    "OK"    — clustering succeeded (even if all points are noise)
    "WARN"  — too few in-vocab alarms to cluster (< min_samples)
    "ERROR" — all alarms are OOV, or an unexpected exception occurred
"""

from __future__ import annotations

import argparse
import http.server
import logging
import os
import signal
import sys
import threading
import time
from concurrent import futures
from typing import List

import grpc

# ── Ensure proto/ is importable regardless of cwd ─────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "proto"))

import proto.engine_pb2      as pb2
import proto.engine_pb2_grpc as pb2_grpc

from embedder          import Embedder
from online_clustering import cluster_online, OnlineClusteringResult


# ── HTTP Health Server (for Kubernetes liveness / readiness probes) ────────────
class _HealthHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler serving /live and /ready (alias: /health)."""

    # Shared event set by serve() once the gRPC server is up
    ready_event: threading.Event = threading.Event()

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/live",):
            # Liveness: always 200 if the process is running
            self._respond(200, b"LIVE")
        elif self.path in ("/ready", "/health"):
            # Readiness: only 200 after gRPC server is fully started
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

    def log_message(self, fmt, *args) -> None:  # suppress access logs
        pass


def _start_health_server(port: int, ready_event: threading.Event) -> None:
    """Start a daemon HTTP health server in a background thread."""
    _HealthHandler.ready_event = ready_event
    httpd = http.server.HTTPServer(("0.0.0.0", port), _HealthHandler)
    logger.info("[Health] HTTP probe server listening on port %d", port)
    httpd.serve_forever()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("alarm_cluster_server")


# ── gRPC Servicer ──────────────────────────────────────────────────────────────
class AlarmClusteringServicer(pb2_grpc.AlarmClusteringServiceServicer):
    """
    Implements AlarmClusteringService.AnalyzeOnlineAlarmCluster.

    Steps
    -----
    1. Parse alarm records from the request.
    2. Build token = f"{managed_objects}|{probable_cause}" for each alarm.
    3. Lookup embedding via Embedder (OOV → None).
    4. Run stateless DBSCAN via online_clustering.cluster_online().
    5. Return OnlineAlarmClusterReport.
    """

    def __init__(self, model_dir: str) -> None:
        self._embedder = Embedder.get_instance(model_dir)
        logger.info(
            "[Servicer] Ready. vocab_size=%d  dim=%d",
            self._embedder.vocab_size,
            self._embedder.dim,
        )

    # ── RPC handler ────────────────────────────────────────────────────────────
    def AnalyzeOnlineAlarmCluster(
        self,
        request: pb2.AnalyzeOnlineAlarmClusterRequest,
        context: grpc.ServicerContext,
    ) -> pb2.OnlineAlarmClusterReport:

        request_id  = request.requestId
        n_alarms    = len(request.alarmRecords)
        logger.info(
            "[RPC] requestId=%s  system=%s  n_alarms=%d",
            request_id, request.system, n_alarms,
        )

        # ── Guard: empty request ───────────────────────────────────────────────
        if n_alarms == 0:
            return pb2.OnlineAlarmClusterReport(
                requestId  = request_id,
                reportType = "ONLINE_CLUSTER",
                status     = "ERROR",
                message    = "Request contains no alarm records.",
                results    = [],
            )

        # ── Step 1: tokenise & embed ───────────────────────────────────────────
        records: List[dict] = []
        for alarm in request.alarmRecords:
            token  = Embedder.make_token(alarm.managed_objects, alarm.probable_cause)
            vector = self._embedder.lookup(token)
            records.append({
                "source_id" : alarm._source_id,
                "token"     : token,
                "vector"    : vector,  # None if OOV
            })

        n_oov_check = sum(1 for r in records if r["vector"] is None)
        logger.info(
            "[RPC] requestId=%s  OOV=%d/%d",
            request_id, n_oov_check, n_alarms,
        )

        # ── Guard: all OOV ─────────────────────────────────────────────────────
        if n_oov_check == n_alarms:
            results = [
                pb2.OnlineAlarmClusterResult(
                    _source_id = r["source_id"],
                    clusterId  = -2,
                    confidence = 0.0,
                )
                for r in records
            ]
            return pb2.OnlineAlarmClusterReport(
                requestId  = request_id,
                reportType = "ONLINE_CLUSTER",
                status     = "ERROR",
                message    = (
                    f"All {n_alarms} alarm records are OOV "
                    "(tokens not found in embedding vocabulary). "
                    "Clustering cannot be performed."
                ),
                results    = results,
            )

        # ── Step 2: cluster ────────────────────────────────────────────────────
        try:
            cluster_result: OnlineClusteringResult = cluster_online(records)
        except Exception as exc:
            logger.exception("[RPC] Clustering failed for requestId=%s", request_id)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"Clustering error: {exc}")
            return pb2.OnlineAlarmClusterReport(
                requestId  = request_id,
                reportType = "ONLINE_CLUSTER",
                status     = "ERROR",
                message    = f"Internal clustering error: {exc}",
                results    = [],
            )

        # ── Step 3: determine status ───────────────────────────────────────────
        if cluster_result.n_clusters == 0 and cluster_result.n_oov < n_alarms:
            status  = "WARN"
            message = (
                f"Clustering produced 0 meaningful clusters "
                f"({cluster_result.n_noise} noise, {cluster_result.n_oov} OOV). "
                "Consider sending more alarm records or adjusting min_samples."
            )
        else:
            sil_str = (
                f"{cluster_result.silhouette:.4f}"
                if cluster_result.silhouette is not None
                else "N/A"
            )
            status  = "OK"
            message = (
                f"Clustered {n_alarms} alarms → "
                f"{cluster_result.n_clusters} clusters | "
                f"{cluster_result.n_noise} noise | "
                f"{cluster_result.n_oov} OOV | "
                f"eps={cluster_result.eps:.4f} | "
                f"silhouette={sil_str}"
            )

        logger.info("[RPC] requestId=%s  status=%s  %s", request_id, status, message)

        # ── Step 4: build proto results ────────────────────────────────────────
        # Build a lookup map from source_id → (cluster_id, confidence)
        item_map = {
            item.source_id: item for item in cluster_result.items
        }

        proto_results = []
        for alarm in request.alarmRecords:
            item = item_map.get(alarm._source_id)
            if item is not None:
                proto_results.append(pb2.OnlineAlarmClusterResult(
                    _source_id = alarm._source_id,
                    clusterId  = item.cluster_id,
                    confidence = item.confidence,
                ))
            else:
                # Fallback (should not happen)
                proto_results.append(pb2.OnlineAlarmClusterResult(
                    _source_id = alarm._source_id,
                    clusterId  = -2,
                    confidence = 0.0,
                ))

        return pb2.OnlineAlarmClusterReport(
            requestId  = request_id,
            reportType = "ONLINE_CLUSTER",
            status     = status,
            message    = message,
            results    = proto_results,
        )


# ── Server lifecycle ───────────────────────────────────────────────────────────
def serve(port: int, model_dir: str, max_workers: int, health_port: int) -> None:
    # ── Health server (starts before gRPC so /live returns 200 immediately) ──
    ready_event = threading.Event()
    health_thread = threading.Thread(
        target=_start_health_server,
        args=(health_port, ready_event),
        daemon=True,
        name="health-http",
    )
    health_thread.start()

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        options=[
            ("grpc.max_receive_message_length", 64 * 1024 * 1024),  # 64 MB
            ("grpc.max_send_message_length",    64 * 1024 * 1024),
        ],
    )

    servicer = AlarmClusteringServicer(model_dir)
    pb2_grpc.add_AlarmClusteringServiceServicer_to_server(servicer, server)

    listen_addr = f"[::]:{port}"
    server.add_insecure_port(listen_addr)
    server.start()

    # Signal readiness to the HTTP health handler
    ready_event.set()

    logger.info("=" * 60)
    logger.info("Alarm Cluster Engine — gRPC server started")
    logger.info("  Listening on  : %s", listen_addr)
    logger.info("  Health probes : http://0.0.0.0:%d/live | /ready", health_port)
    logger.info("  Model dir     : %s", model_dir)
    logger.info("  Max workers   : %d", max_workers)
    logger.info("=" * 60)

    # ── Graceful shutdown on SIGTERM / SIGINT ──────────────────────────────────
    _stop_event = [False]

    def _shutdown(signum, frame):
        logger.info("Received signal %d — initiating graceful shutdown…", signum)
        _stop_event[0] = True
        server.stop(grace=5)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    try:
        while not _stop_event[0]:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    logger.info("Server stopped.")


# ── CLI entry-point ────────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Alarm Cluster Engine — gRPC Server",
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
        help="HTTP port for Kubernetes liveness/readiness probes",
    )
    parser.add_argument(
        "--model-dir",
        default=os.environ.get("MODEL_DIR", "models"),
        help="Directory containing embeddings.npz",
    )
    parser.add_argument(
        "--workers", type=int,
        default=int(os.environ.get("MAX_WORKERS", 10)),
        help="Thread-pool size",
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
