"""
generate_betterproto.py
-----------------------
Regenerates proto_betterproto/engine.py from proto/engine.proto using BetterProto.

Usage:
    python generate_betterproto.py

Requirements:
    pip install "betterproto[compiler]>=0.3.1"

What this does vs grpc_tools.protoc
------------------------------------
  grpc_tools.protoc  → engine_pb2.py + engine_pb2_grpc.py
                        (raw descriptor bytes, classic protobuf API)
  BetterProto        → proto_betterproto/engine.py
                        (Python @dataclass messages, async ServiceStub/ServiceBase)
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PROTO_DIR = ROOT / "proto"
OUT_DIR = ROOT / "proto_betterproto"


def _ensure_betterproto() -> None:
    try:
        import betterproto  # noqa: F401
        import grpc_tools  # noqa: F401
    except ImportError:
        print("Installing betterproto[compiler] …")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "betterproto[compiler]>=0.3.1"],
        )


def generate() -> None:
    _ensure_betterproto()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "grpc_tools.protoc",
        f"--proto_path={ROOT}",
        f"--python_betterproto_out={OUT_DIR}",
        str(PROTO_DIR / "engine.proto"),
    ]

    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("STDERR:", result.stderr)
        sys.exit(result.returncode)

    print("Done. Generated files in", OUT_DIR)
    for f in sorted(OUT_DIR.rglob("*.py")):
        print(" ", f.relative_to(ROOT))


if __name__ == "__main__":
    generate()
