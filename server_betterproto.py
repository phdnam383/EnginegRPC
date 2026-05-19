"""
server_betterproto.py — gRPC server dùng BetterProto types.

Cấu hình qua biến môi trường:
    GRPC_PORT   (default: 50051)
    MODEL_DIR   (default: models)

K8s health probe: dùng TCP socket vào GRPC_PORT thay vì HTTP.
"""

import asyncio
import logging
import os
import sys
from typing import List

import grpc
import grpc.aio

sys.path.insert(0, os.path.dirname(__file__))

from proto_betterproto.engine import (
    AnalyzeOnlineAlarmClusterRequest,
    OnlineAlarmClusterReport,
    OnlineAlarmClusterResult,
)
from proto_betterproto.grpcio_support import add_to_server
from embedder import Embedder
from online_clustering import cluster_online, OnlineClusteringResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


class AlarmClusteringServicer:

    def __init__(self, model_dir: str) -> None:
        self._embedder = Embedder.get_instance(model_dir)
        log.info("Embedder loaded. vocab=%d dim=%d", self._embedder.vocab_size, self._embedder.dim)

    async def analyze_online_alarm_cluster(
        self,
        request: AnalyzeOnlineAlarmClusterRequest,
        context: grpc.aio.ServicerContext,
    ) -> OnlineAlarmClusterReport:

        rid = request.request_id
        alarms = request.alarm_records
        log.info("requestId=%s system=%s n=%d", rid, request.system, len(alarms))

        if not alarms:
            return OnlineAlarmClusterReport(
                request_id=rid, report_type="ONLINE_CLUSTER",
                status="ERROR", message="No alarm records.", results=[],
            )

        records: List[dict] = [
            {
                "source_id": a.source_id,
                "token":     Embedder.make_token(a.managed_objects, a.probable_cause),
                "vector":    self._embedder.lookup(
                                 Embedder.make_token(a.managed_objects, a.probable_cause)
                             ),
            }
            for a in alarms
        ]

        n_oov = sum(1 for r in records if r["vector"] is None)
        if n_oov == len(alarms):
            return OnlineAlarmClusterReport(
                request_id=rid, report_type="ONLINE_CLUSTER",
                status="ERROR",
                message=f"All {len(alarms)} alarms are OOV — cannot cluster.",
                results=[
                    OnlineAlarmClusterResult(source_id=r["source_id"], cluster_id=-2, confidence=0.0)
                    for r in records
                ],
            )

        # cluster_online блокирует CPU — запускаем в thread pool
        loop = asyncio.get_event_loop()
        try:
            result: OnlineClusteringResult = await loop.run_in_executor(None, cluster_online, records)
        except Exception as exc:
            log.exception("Clustering failed requestId=%s", rid)
            return OnlineAlarmClusterReport(
                request_id=rid, report_type="ONLINE_CLUSTER",
                status="ERROR", message=f"Clustering error: {exc}", results=[],
            )

        item_map = {item.source_id: item for item in result.items}
        sil = f"{result.silhouette:.4f}" if result.silhouette is not None else "N/A"
        status = "WARN" if result.n_clusters == 0 else "OK"
        message = (
            f"{result.n_clusters} clusters | {result.n_noise} noise | "
            f"{result.n_oov} OOV | eps={result.eps:.4f} | silhouette={sil}"
        )
        log.info("requestId=%s status=%s %s", rid, status, message)

        return OnlineAlarmClusterReport(
            request_id=rid, report_type="ONLINE_CLUSTER",
            status=status, message=message,
            results=[
                OnlineAlarmClusterResult(
                    source_id=a.source_id,
                    cluster_id=item_map[a.source_id].cluster_id if a.source_id in item_map else -2,
                    confidence=item_map[a.source_id].confidence if a.source_id in item_map else 0.0,
                )
                for a in alarms
            ],
        )


async def main() -> None:
    port = int(os.environ.get("GRPC_PORT", 50051))
    model_dir = os.environ.get("MODEL_DIR", "models")

    server = grpc.aio.server(options=[
        ("grpc.max_receive_message_length", 64 * 1024 * 1024),
        ("grpc.max_send_message_length",    64 * 1024 * 1024),
    ])

    add_to_server(AlarmClusteringServicer(model_dir), server)
    server.add_insecure_port(f"[::]:{port}")
    await server.start()

    log.info("gRPC listening on :%d  model=%s", port, model_dir)
    await server.wait_for_termination()


if __name__ == "__main__":
    asyncio.run(main())
