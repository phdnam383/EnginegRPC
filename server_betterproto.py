"""
server_betterproto.py
---------------------
Async gRPC server for the Alarm Clustering Engine — uses BetterProto stubs
instead of grpc_tools.protoc-generated code.

Differences vs server.py (grpc_tools.protoc)
---------------------------------------------
  Generator   : BetterProto  (python generate_betterproto.py)
  Stubs       : proto_betterproto/engine.py  (@dataclass messages)
  gRPC layer  : grpc.aio  (asyncio-native, replaces grpc + ThreadPoolExecutor)
  Field names : snake_case  (request_id, alarm_records, source_id, cluster_id …)
  Proto "_source_id" → Python "source_id"  (betterproto strips leading underscore)

Startup
-------
    python server_betterproto.py [--port 50051] [--health-port 8080]
                                  [--model-dir models] [--workers 10]

Environment variables (override CLI defaults)
---------------------------------------------
    GRPC_PORT   : gRPC listen port          (default: 50051)
    HEALTH_PORT : HTTP health-probe port    (default: 8080)
    MODEL_DIR   : directory with embeddings.npz  (default: models)
    MAX_WORKERS : asyncio thread-executor size   (default: 10)

Health endpoints
----------------
    GET /live   → 200 after event-loop starts
    GET /ready  → 200 after gRPC server is fully started
    GET /health → alias for /ready
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import logging
import os
import signal
import sys
import threading
from typing import List

import grpc
import grpc.aio

# ── BetterProto-generated stubs ────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from proto_betterproto.engine import (
    AlarmClusteringServiceBase,
    AnalyzeOnlineAlarmClusterRequest,
    OnlineAlarmClusterReport,
    OnlineAlarmClusterResult,
)

from embedder import Embedder
from online_clustering import cluster_online, OnlineClusteringResult


# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("alarm_cluster_server_betterproto")


# ── HTTP Health Server ─────────────────────────────────────────────────────────
class _HealthHandler(http.server.BaseHTTPRequestHandler):
    ready_event: threading.Event = threading.Event()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/live":
            self._respond(200, b"LIVE")
        elif self.path in ("/ready", "/health"):
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
        pass


def _start_health_server(port: int, ready_event: threading.Event) -> None:
    _HealthHandler.ready_event = ready_event
    httpd = http.server.HTTPServer(("0.0.0.0", port), _HealthHandler)
    logger.info("[Health] HTTP probe server listening on port %d", port)
    httpd.serve_forever()


# ── gRPC Servicer (BetterProto) ────────────────────────────────────────────────
class AlarmClusteringServicer(AlarmClusteringServiceBase):
    """
    Async implementation of AlarmClusteringService using BetterProto stubs.

    BetterProto differences from server.py
    ---------------------------------------
    • All RPC methods are coroutines (async def).
    • Message fields are snake_case attributes on @dataclass objects.
    • alarm._source_id  → alarm.source_id
    • request.requestId → request.request_id
    • request.alarmRecords → request.alarm_records
    • result.clusterId  → result.cluster_id
    """

    def __init__(self, model_dir: str) -> None:
        self._embedder = Embedder.get_instance(model_dir)
        logger.info(
            "[Servicer] Ready (BetterProto). vocab_size=%d  dim=%d",
            self._embedder.vocab_size,
            self._embedder.dim,
        )

    async def analyze_online_alarm_cluster(
        self,
        request: AnalyzeOnlineAlarmClusterRequest,
    ) -> OnlineAlarmClusterReport:

        request_id = request.request_id
        n_alarms = len(request.alarm_records)
        logger.info(
            "[RPC] requestId=%s  system=%s  n_alarms=%d",
            request_id, request.system, n_alarms,
        )

        # ── Guard: empty request ───────────────────────────────────────────────
        if n_alarms == 0:
            return OnlineAlarmClusterReport(
                request_id=request_id,
                report_type="ONLINE_CLUSTER",
                status="ERROR",
                message="Request contains no alarm records.",
                results=[],
            )

        # ── Step 1: tokenise & embed ───────────────────────────────────────────
        # BetterProto field names: alarm.source_id, alarm.managed_objects,
        #   alarm.probable_cause, alarm.alarm_type, etc.
        records: List[dict] = []
        for alarm in request.alarm_records:
            token = Embedder.make_token(alarm.managed_objects, alarm.probable_cause)
            vector = self._embedder.lookup(token)
            records.append({
                "source_id": alarm.source_id,
                "token": token,
                "vector": vector,
            })

        n_oov = sum(1 for r in records if r["vector"] is None)
        logger.info("[RPC] requestId=%s  OOV=%d/%d", request_id, n_oov, n_alarms)

        # ── Guard: all OOV ─────────────────────────────────────────────────────
        if n_oov == n_alarms:
            results = [
                OnlineAlarmClusterResult(
                    source_id=r["source_id"],
                    cluster_id=-2,
                    confidence=0.0,
                )
                for r in records
            ]
            return OnlineAlarmClusterReport(
                request_id=request_id,
                report_type="ONLINE_CLUSTER",
                status="ERROR",
                message=(
                    f"All {n_alarms} alarm records are OOV "
                    "(tokens not found in embedding vocabulary). "
                    "Clustering cannot be performed."
                ),
                results=results,
            )

        # ── Step 2: cluster (run sync in thread to avoid blocking event loop) ──
        loop = asyncio.get_event_loop()
        try:
            cluster_result: OnlineClusteringResult = await loop.run_in_executor(
                None, cluster_online, records
            )
        except Exception as exc:
            logger.exception("[RPC] Clustering failed for requestId=%s", request_id)
            return OnlineAlarmClusterReport(
                request_id=request_id,
                report_type="ONLINE_CLUSTER",
                status="ERROR",
                message=f"Internal clustering error: {exc}",
                results=[],
            )

        # ── Step 3: determine status ───────────────────────────────────────────
        if cluster_result.n_clusters == 0 and cluster_result.n_oov < n_alarms:
            status = "WARN"
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
            status = "OK"
            message = (
                f"Clustered {n_alarms} alarms → "
                f"{cluster_result.n_clusters} clusters | "
                f"{cluster_result.n_noise} noise | "
                f"{cluster_result.n_oov} OOV | "
                f"eps={cluster_result.eps:.4f} | "
                f"silhouette={sil_str}"
            )

        logger.info("[RPC] requestId=%s  status=%s  %s", request_id, status, message)

        # ── Step 4: build BetterProto result objects ───────────────────────────
        item_map = {item.source_id: item for item in cluster_result.items}

        proto_results = []
        for alarm in request.alarm_records:
            item = item_map.get(alarm.source_id)
            proto_results.append(
                OnlineAlarmClusterResult(
                    source_id=alarm.source_id,
                    cluster_id=item.cluster_id if item else -2,
                    confidence=item.confidence if item else 0.0,
                )
            )

        return OnlineAlarmClusterReport(
            request_id=request_id,
            report_type="ONLINE_CLUSTER",
            status=status,
            message=message,
            results=proto_results,
        )


# ── Server lifecycle ───────────────────────────────────────────────────────────
async def _serve_async(
    port: int,
    model_dir: str,
    max_workers: int,
    ready_event: threading.Event,
) -> None:
    servicer = AlarmClusteringServicer(model_dir)

    server = grpc.aio.server(
        options=[
            ("grpc.max_receive_message_length", 64 * 1024 * 1024),
            ("grpc.max_send_message_length",    64 * 1024 * 1024),
        ],
    )

    # Register handlers from BetterProto's __mapping__()
    handlers = servicer.__mapping__()
    generic_handler = grpc.method_handlers_generic_handler(
        "alarm_clustering.AlarmClusteringService", handlers
    )
    server.add_generic_rpc_handlers([generic_handler])

    listen_addr = f"[::]:{port}"
    server.add_insecure_port(listen_addr)
    await server.start()

    ready_event.set()

    logger.info("=" * 60)
    logger.info("Alarm Cluster Engine (BetterProto) — gRPC server started")
    logger.info("  Listening on  : %s", listen_addr)
    logger.info("  Model dir     : %s", model_dir)
    logger.info("  Stubs         : proto_betterproto/engine.py (BetterProto)")
    logger.info("=" * 60)

    # Graceful shutdown on SIGTERM / SIGINT
    loop = asyncio.get_event_loop()

    def _on_signal():
        logger.info("Shutdown signal received — stopping server…")
        asyncio.ensure_future(server.stop(grace=5))

    loop.add_signal_handler(signal.SIGTERM, _on_signal)
    loop.add_signal_handler(signal.SIGINT,  _on_signal)

    await server.wait_for_termination()
    logger.info("Server stopped.")


def serve(port: int, model_dir: str, max_workers: int, health_port: int) -> None:
    # Start HTTP health server (daemon thread, no asyncio dependency)
    ready_event = threading.Event()
    health_thread = threading.Thread(
        target=_start_health_server,
        args=(health_port, ready_event),
        daemon=True,
        name="health-http",
    )
    health_thread.start()

    asyncio.run(_serve_async(port, model_dir, max_workers, ready_event))


# ── CLI entry-point ────────────────────────────────────────────────────────────
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
        help="Thread-executor size for blocking ops (clustering)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    serve(
        port=args.port,
        model_dir=args.model_dir,
        max_workers=args.workers,
        health_port=args.health_port,
    )
