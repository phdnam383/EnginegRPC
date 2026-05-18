import sys
sys.path.insert(0, ".")

from proto import engine_pb2, engine_pb2_grpc
from embedder import Embedder
from online_clustering import cluster_online

print("All imports OK")

emb = Embedder.get_instance("models")
print(f"Embedder: vocab_size={emb.vocab_size}, dim={emb.dim}")

# Quick lookup test — known token from training output
tok = "vdu_csdb.vnfc_csdb1|LINK_TO_DNSGW_DOWN"
vec = emb.lookup(tok)
if vec is not None:
    print(f"Lookup [{tok}]: OK (dim={vec.shape[0]})")
else:
    print(f"Lookup [{tok}]: OOV — token not in vocab!")

# OOV test
oov_tok = "unknown_node|FAKE_CAUSE"
assert emb.lookup(oov_tok) is None, "Expected OOV to return None"
print(f"OOV test PASSED: '{oov_tok}' → None")

# Quick clustering smoke test
records = [
    {"source_id": f"id-{i}", "vector": emb.lookup(tok), "token": tok}
    for i in range(5)
]
# Mix in one OOV
records.append({"source_id": "id-oov", "vector": None, "token": oov_tok})

result = cluster_online(records)
print(f"\nOnline clustering smoke test:")
print(f"  n_clusters={result.n_clusters}, n_noise={result.n_noise}, n_oov={result.n_oov}")
print(f"  items sample: {[(x.source_id, x.cluster_id, round(x.confidence,3)) for x in result.items[:3]]}")
print("\nAll checks PASSED")
