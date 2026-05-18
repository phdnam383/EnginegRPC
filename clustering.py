import json
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Literal
from pathlib import Path

from sklearn.cluster import DBSCAN
from sklearn.preprocessing import normalize
from sklearn.metrics import silhouette_score
from sklearn.neighbors import NearestNeighbors


# ── Config ─────────────────────────────────────────────────────────────────────
OUTPUT_DIR   = Path("output")
MIN_SAMPLES  = 3   # DBSCAN min_samples
K_NEIGHBORS  = 4   # k cho k-NN distance plot (thường = min_samples)


# ── Result container ───────────────────────────────────────────────────────────
@dataclass
class ClusterResult:
    labels:      np.ndarray          # (-1 = noise)
    eps:         float
    metric:      str
    n_clusters:  int
    n_noise:     int
    silhouette:  float
    token_df:    pd.DataFrame        # columns: id, cluster, confidence
    vectors_norm: np.ndarray         # L2-normalized embedding matrix


# ── Auto eps ───────────────────────────────────────────────────────────────────
def auto_eps(
    vectors_norm: np.ndarray,
    metric:       str,
    k:            int = K_NEIGHBORS,
) -> float:
    """
    Tìm eps tự động bằng k-NN distance sorted (knee point).
    Dùng thư viện `kneed` nếu có, fallback về percentile 90 nếu không.
    """
    nbrs = NearestNeighbors(n_neighbors=k, metric=metric).fit(vectors_norm)
    dists, _ = nbrs.kneighbors(vectors_norm)
    knn_dist = np.sort(dists[:, -1])  # khoảng cách đến neighbor thứ k

    try:
        from kneed import KneeLocator
        knee = KneeLocator(
            range(len(knn_dist)), knn_dist,
            curve="convex", direction="increasing"
        )
        eps = knn_dist[knee.knee] if knee.knee is not None else float(np.percentile(knn_dist, 90))
    except ImportError:
        eps = float(np.percentile(knn_dist, 90))

    return eps, knn_dist


# ── Main function ──────────────────────────────────────────────────────────────
def run_dbscan(
    token2vec:    Dict[str, np.ndarray],
    metric:       Literal["cosine", "euclidean"] = "cosine",
    eps:          Optional[float] = None,
    min_samples:  int = MIN_SAMPLES,
    random_state: Optional[int] = None,
) -> ClusterResult:
    """
    Chạy DBSCAN trên embedding vectors.

    Parameters
    ----------
    token2vec    : Dict[str, np.ndarray] – output của train_skipgram()
    metric       : 'cosine' | 'euclidean'
    eps          : float | None – nếu None sẽ tự động tìm bằng knee
    min_samples  : int – DBSCAN min_samples
    random_state : int | None – seed cho numpy và silhouette_score

    Returns
    -------
    ClusterResult
    """
    if random_state is not None:
        np.random.seed(random_state)
    tokens  = list(token2vec.keys())
    vectors = np.array([token2vec[t] for t in tokens], dtype=np.float32)

    # L2-normalize (đảm bảo cosine = 1 - dot product)
    vectors_norm = normalize(vectors, norm="l2")

    # Auto eps
    auto, knn_dist = auto_eps(vectors_norm, metric)
    if eps is None:
        eps = auto
    print(f"[DBSCAN] metric={metric} | eps={eps:.4f} (auto={auto:.4f}) | min_samples={min_samples}")

    # DBSCAN
    db     = DBSCAN(eps=eps, min_samples=min_samples, metric=metric)
    labels = db.fit_predict(vectors_norm)

    # Stats
    unique_labels = set(labels)
    n_clusters    = len(unique_labels - {-1})
    n_noise       = int((labels == -1).sum())

    # Silhouette (chỉ tính trên non-noise points, cần >= 2 cụm)
    mask = labels != -1
    if n_clusters >= 2 and mask.sum() > n_clusters:
        sil = silhouette_score(vectors_norm[mask], labels[mask], metric=metric,
                               random_state=random_state)
    else:
        sil = float("nan")

    print(f"[DBSCAN] Kết quả: {n_clusters} cụm | {n_noise} noise ({n_noise/len(labels)*100:.1f}%)")
    if not np.isnan(sil):
        print(f"[DBSCAN] Silhouette Score: {sil:.4f}")

    # Build cluster result DataFrame
    # confidence = 1.0 (placeholder – no formula defined yet)
    token_df = pd.DataFrame({
        "id"         : tokens,
        "cluster"    : labels,
        "confidence" : 1.0,
    })

    OUTPUT_DIR.mkdir(exist_ok=True)
    token_df.to_csv(OUTPUT_DIR / "cluster_labels.csv", index=False)
    print(f"[DBSCAN] Saved cluster_labels.csv ({len(token_df):,} rows)")

    return ClusterResult(
        labels       = labels,
        eps          = eps,
        metric       = metric,
        n_clusters   = n_clusters,
        n_noise      = n_noise,
        silhouette   = sil,
        token_df     = token_df,
        vectors_norm = vectors_norm,
    )


def cluster_profile(result: ClusterResult) -> pd.DataFrame:
    """Summary statistics per cluster, parsed from token format vnfc_id|alarm_type|probable_cause."""
    df = result.token_df.copy()

    # Parse token fields for profiling
    parts        = df["id"].str.split("|", expand=True)
    df["vnfc_id"]        = parts[0]
    df["alarm_type"]     = parts[1]
    df["probable_cause"] = parts[2]

    rows = []
    for cid in sorted(df["cluster"].unique()):
        sub = df[df["cluster"] == cid]
        top_alarm = sub["alarm_type"].value_counts().index[0] if len(sub) else "—"
        top_cause = sub["probable_cause"].value_counts().index[0] if len(sub) else "—"
        rows.append({
            "cluster"        : cid,
            "label"          : "noise" if cid == -1 else f"C{cid}",
            "n_tokens"       : len(sub),
            "n_vnfc"         : sub["vnfc_id"].nunique(),
            "n_alarm_types"  : sub["alarm_type"].nunique(),
            "top_alarm_type" : top_alarm,
            "top_cause"      : top_cause,
        })
    return pd.DataFrame(rows)


# ── Main ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    from alarm_storm_detection import load_data, detect_alarm_storms
    from word2vec_skipgram import load_embeddings

    parser = argparse.ArgumentParser(description="DBSCAN Clustering on pre-trained Word2Vec embeddings")
    parser.add_argument("--metric", choices=["cosine", "euclidean"], default="cosine")
    parser.add_argument("--eps",   type=float, default=None)
    parser.add_argument("--model-dir", default="models",
                        help="Directory containing embeddings.npz (default: models)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed để đảm bảo reproducibility")
    args = parser.parse_args()

    print("=" * 60)
    print("DBSCAN CLUSTERING")
    print("=" * 60)

    # ── 1. Load data ──────────────────────────────────────────────────
    df = load_data()
    print(f"Loaded {len(df):,} alarm records.")

    # ── 2. Load pre-trained embeddings ────────────────────────────────
    print(f"\nLoading embeddings from '{args.model_dir}/'...")
    token2vec, token2idx = load_embeddings(args.model_dir)
    print(f"Loaded {len(token2vec):,} token vectors.")

    # ── 3. Run DBSCAN ───────────────────────────────────────────────
    print()
    result  = run_dbscan(token2vec, metric=args.metric, eps=args.eps, random_state=args.seed)
    profile = cluster_profile(result)

    print("\n--- Cluster Profile ---")
    print(profile.to_string(index=False))

    # ── 4. Join cluster labels back to full alarm records ────────────────
    # Each alarm in df maps to one token via (vnfc_id, alarm_type, probable_cause)
    df["_token"] = (
        df["vnfc_id"] + "|" +
        df["alarm_type"] + "|" +
        df["probable_cause"]
    )
    token_map = result.token_df.set_index("id")[["cluster", "confidence"]]
    df = df.join(token_map, on="_token").drop(columns=["_token"])

    # Alarms whose token was unseen by the model get cluster = -2 / confidence = 0
    df["cluster"]    = df["cluster"].fillna(-2).astype(int)
    df["confidence"] = df["confidence"].fillna(0.0)

    # ── 5. Save to JSON ───────────────────────────────────────────────
    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / "cluster_result.json"

    output = {
        "meta": {
            "n_alarms"   : len(df),
            "n_clusters" : result.n_clusters,
            "n_noise"    : result.n_noise,
            "eps"        : round(result.eps, 6),
            "metric"     : result.metric,
            "silhouette" : None if np.isnan(result.silhouette) else round(float(result.silhouette), 6),
        },
        "cluster_profile": profile.to_dict(orient="records"),
        "alarms": df[["id", "vnfc_id", "alarm_type", "probable_cause",
                       "perceived_severity", "state",
                       "raised_time", "cleared_time",
                       "cluster", "confidence"]].to_dict(orient="records"),
    }

    class _NumpyEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, (np.integer,)):
                return int(obj)
            if isinstance(obj, (np.floating,)):
                return float(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            return super().default(obj)

    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False, cls=_NumpyEncoder), encoding="utf-8")
    print(f"\nSaved cluster_result.json → {out_path}  ({len(df):,} alarms)")