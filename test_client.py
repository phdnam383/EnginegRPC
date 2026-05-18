"""
test_client.py
--------------
Manual verification client for the Alarm Cluster Engine gRPC server.

Usage
-----
    # Terminal 1 — start the server
    python server.py

    # Terminal 2 — run this client
    python test_client.py [--host localhost] [--port 50051] [--scenario all]

Scenarios
---------
    normal      — batch of real-looking alarm records (mix of known tokens)
    oov         — all records have unknown tokens → expect ERROR
    small_batch — fewer than min_samples=3 records → expect WARN / noise
    all         — run all scenarios sequentially
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import grpc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "proto"))
import proto.engine_pb2      as pb2
import proto.engine_pb2_grpc as pb2_grpc


# ── Pretty-print helper ────────────────────────────────────────────────────────
def _print_report(report: pb2.OnlineAlarmClusterReport, elapsed: float) -> None:
    print(f"\n{'─' * 60}")
    print(f"  requestId  : {report.requestId}")
    print(f"  reportType : {report.reportType}")
    print(f"  status     : {report.status}")
    print(f"  message    : {report.message}")
    print(f"  elapsed    : {elapsed*1000:.1f} ms")
    print(f"  results    : {len(report.results)} items")

    # Group by clusterId for a quick summary
    cluster_counts: dict[int, int] = {}
    for r in report.results:
        cluster_counts[r.clusterId] = cluster_counts.get(r.clusterId, 0) + 1

    if cluster_counts:
        print("\n  Cluster distribution:")
        for cid in sorted(cluster_counts):
            label = {-2: "OOV", -1: "Noise"}.get(cid, f"C{cid}")
            print(f"    {label:>8} (id={cid:>3})  :  {cluster_counts[cid]} alarms")

    if report.results:
        print("\n  First 5 results:")
        for r in list(report.results)[:5]:
            print(
                f"    source_id={r._source_id:<45}  "
                f"cluster={r.clusterId:>3}  "
                f"conf={r.confidence:.3f}"
            )
    print(f"{'─' * 60}\n")


# ── Alarm record factories ─────────────────────────────────────────────────────
def _make_alarm(
    source_id:         str,
    managed_objects:   str,
    alarm_type:        str,
    probable_cause:    str,
    perceived_severity: str = "MAJOR",
    state:             str  = "ACTIVE",
    created_at:        str  = "2024-01-15T10:00:00Z",
    closed_at:         str  = "",
) -> pb2.AlarmRecord:
    return pb2.AlarmRecord(
        _source_id         = source_id,
        managed_objects    = managed_objects,
        alarmType          = alarm_type,
        probable_cause     = probable_cause,
        perceived_severity = perceived_severity,
        state              = state,
        created_at         = created_at,
        closed_at          = closed_at,
    )


# ── Scenarios ──────────────────────────────────────────────────────────────────
def scenario_normal(stub: pb2_grpc.AlarmClusteringServiceStub) -> None:
    """
    Batch of real alarm records from the training vocabulary.
    Tokens used here match the format managed_objects|probable_cause
    that was used during Skip-Gram training.
    """
    print("\n[Scenario: NORMAL] Sending a realistic alarm batch…")

    alarms = [
        _make_alarm("id-001", "vdu_csdb.vnfc_csdb1",    "LINK_TO_DNSGW_DOWN", "LINK_TO_DNSGW_DOWN"),
        _make_alarm("id-002", "vdu_sbsipc.vnfc_sbsipc1", "LINK_TO_DNSGW_DOWN", "LINK_TO_DNSGW_DOWN"),
        _make_alarm("id-003", "vdu_mtdb.vnfc_mtdb1",    "LINK_TO_DNSGW_DOWN", "LINK_TO_DNSGW_DOWN"),
        _make_alarm("id-004", "vdu_csdia.vnfc_csdia2",  "LINK_TO_LOGIC_DOWN", "LINK_TO_LOGIC_DOWN"),
        _make_alarm("id-005", "vdu_ipsmdia.vnfc_ipsmdia1","LINK_TO_DNSGW_DOWN","LINK_TO_DNSGW_DOWN"),
        _make_alarm("id-006", "vdu_csdb.vnfc_csdb1",    "LINK_TO_DNSGW_DOWN", "LINK_TO_DNSGW_DOWN"),
        _make_alarm("id-007", "vdu_sbsipc.vnfc_sbsipc1", "LINK_TO_DNSGW_DOWN", "LINK_TO_DNSGW_DOWN"),
        _make_alarm("id-008", "vdu_mtdb.vnfc_mtdb1",    "LINK_TO_DNSGW_DOWN", "LINK_TO_DNSGW_DOWN"),
        _make_alarm("id-009", "vdu_csdia.vnfc_csdia2",  "LINK_TO_LOGIC_DOWN", "LINK_TO_LOGIC_DOWN"),
        _make_alarm("id-010", "vdu_ipsmdia.vnfc_ipsmdia1","LINK_TO_DNSGW_DOWN","LINK_TO_DNSGW_DOWN"),
    ]

    req = pb2.AnalyzeOnlineAlarmClusterRequest(
        requestId    = "req-normal-001",
        system       = "IMS_CORE",
        alarmRecords = alarms,
    )

    t0     = time.perf_counter()
    report = stub.AnalyzeOnlineAlarmCluster(req)
    elapsed = time.perf_counter() - t0

    _print_report(report, elapsed)


def scenario_oov(stub: pb2_grpc.AlarmClusteringServiceStub) -> None:
    """All tokens are completely unknown — expect status=ERROR, clusterId=-2."""
    print("\n[Scenario: OOV] Sending alarms with unknown tokens…")

    alarms = [
        _make_alarm(f"oov-{i:03d}", f"unknown_node_{i}", "FAKE_ALARM_TYPE", "FAKE_CAUSE")
        for i in range(5)
    ]

    req = pb2.AnalyzeOnlineAlarmClusterRequest(
        requestId    = "req-oov-001",
        system       = "UNKNOWN_SYSTEM",
        alarmRecords = alarms,
    )

    t0     = time.perf_counter()
    report = stub.AnalyzeOnlineAlarmCluster(req)
    elapsed = time.perf_counter() - t0

    _print_report(report, elapsed)


def scenario_small_batch(stub: pb2_grpc.AlarmClusteringServiceStub) -> None:
    """Only 2 in-vocab alarms — fewer than min_samples=3 → expect WARN."""
    print("\n[Scenario: SMALL BATCH] Sending only 2 in-vocab alarms…")

    alarms = [
        _make_alarm("sm-001", "vdu_csdb.vnfc_csdb1",    "LINK_TO_DNSGW_DOWN", "LINK_TO_DNSGW_DOWN"),
        _make_alarm("sm-002", "vdu_sbsipc.vnfc_sbsipc1", "LINK_TO_DNSGW_DOWN", "LINK_TO_DNSGW_DOWN"),
    ]

    req = pb2.AnalyzeOnlineAlarmClusterRequest(
        requestId    = "req-small-001",
        system       = "IMS_CORE",
        alarmRecords = alarms,
    )

    t0     = time.perf_counter()
    report = stub.AnalyzeOnlineAlarmCluster(req)
    elapsed = time.perf_counter() - t0

    _print_report(report, elapsed)


def scenario_empty(stub: pb2_grpc.AlarmClusteringServiceStub) -> None:
    """Empty alarm list — expect status=ERROR."""
    print("\n[Scenario: EMPTY] Sending an empty alarm list…")

    req = pb2.AnalyzeOnlineAlarmClusterRequest(
        requestId    = "req-empty-001",
        system       = "IMS_CORE",
        alarmRecords = [],
    )

    t0     = time.perf_counter()
    report = stub.AnalyzeOnlineAlarmCluster(req)
    elapsed = time.perf_counter() - t0

    _print_report(report, elapsed)


# ── Main ───────────────────────────────────────────────────────────────────────
_SCENARIOS = {
    "normal":      scenario_normal,
    "oov":         scenario_oov,
    "small_batch": scenario_small_batch,
    "empty":       scenario_empty,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Alarm Cluster Engine — test client")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=int(os.environ.get("GRPC_PORT", 50051)))
    parser.add_argument(
        "--scenario",
        choices=[*_SCENARIOS, "all"],
        default="all",
        help="Which test scenario to run",
    )
    args = parser.parse_args()

    channel = grpc.insecure_channel(f"{args.host}:{args.port}")
    stub    = pb2_grpc.AlarmClusteringServiceStub(channel)

    print("=" * 60)
    print("Alarm Cluster Engine — Test Client")
    print(f"  Server: {args.host}:{args.port}")
    print("=" * 60)

    to_run = list(_SCENARIOS.values()) if args.scenario == "all" else [_SCENARIOS[args.scenario]]
    for fn in to_run:
        try:
            fn(stub)
        except grpc.RpcError as exc:
            print(f"[gRPC ERROR] {exc.code()}: {exc.details()}")

    channel.close()


if __name__ == "__main__":
    main()
