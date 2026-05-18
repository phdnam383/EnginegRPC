"""
online_clustering.py
--------------------
Stateless DBSCAN clustering for the gRPC online serving path.

Unlike clustering.py (which writes CSV / plots), this module is
purely functional: it takes a list of (id, vector) pairs and returns
cluster assignments — no file I/O, no side-effects.

OOV policy
----------
Records whose token was not found in the embedding vocab arrive
here with `vector = None`.  They are excluded from the DBSCAN run
and receive cluster_id = -2, confidence = 0.0 in the final output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Tuple

import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.metrics import silhouette_score
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import normalize

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
MIN_SAMPLES  = 3   # DBSCAN min_samples (same as offline pipeline)
K_NEIGHBORS  = 4   # k for k-NN distance (= min_samples by convention)


# ── Result types ───────────────────────────────────────────────────────────────
@dataclass
class AlarmClusterItem:
    source_id:  str
    cluster_id: int    # -2 = OOV, -1 = noise, 0+ = cluster
    confidence: float  # 0.0 for OOV / noise; density-based score otherwise


@dataclass
class OnlineClusteringResult:
    items:      List[AlarmClusterItem]
    n_clusters: int
    n_noise:    int
    n_oov:      int
    eps:        float
    metric:     str
    silhouette: Optional[float]   # None when < 2 clusters


# ── Helpers ────────────────────────────────────────────────────────────────────
def _auto_eps(
    vectors_norm: np.ndarray,
    metric: str,
    k: int = K_NEIGHBORS,
) -> float:
    """
    Compute eps automatically via the k-NN distance knee point.
    Falls back to the 90th-percentile if `kneed` is not available.
    """
    nbrs = NearestNeighbors(n_neighbors=k, metric=metric).fit(vectors_norm)
    dists, _ = nbrs.kneighbors(vectors_norm)
    knn_dist = np.sort(dists[:, -1])

    try:
        from kneed import KneeLocator
        knee = KneeLocator(
            range(len(knn_dist)), knn_dist,
            curve="convex", direction="increasing",
        )
        eps = (
            float(knn_dist[knee.knee])
            if knee.knee is not None
            else float(np.percentile(knn_dist, 90))
        )
    except ImportError:
        eps = float(np.percentile(knn_dist, 90))

    return eps


def _density_confidence(
    labels: np.ndarray,
    vectors_norm: np.ndarray,
    eps: float,
    metric: str,
) -> np.ndarray:
    """
    Compute a per-point confidence score in [0, 1] based on how
    many neighbours a point has relative to the densest core point
    in its cluster.

    confidence = n_neighbours(point) / max_n_neighbours(cluster)
    Noise points (label == -1) get 0.0.
    """
    n = len(labels)
    confidences = np.zeros(n, dtype=np.float32)

    nbrs = NearestNeighbors(radius=eps, metric=metric).fit(vectors_norm)
    neighbour_counts = np.array(
        [len(indices) - 1 for indices in nbrs.radius_neighbors(vectors_norm)[1]]
    )  # -1 to exclude self

    unique_clusters = set(labels) - {-1}
    for cid in unique_clusters:
        mask = labels == cid
        max_count = neighbour_counts[mask].max()
        if max_count > 0:
            confidences[mask] = neighbour_counts[mask] / max_count

    return confidences


# ── Main entry-point ───────────────────────────────────────────────────────────
def cluster_online(
    records: List[Dict],
    metric: Literal["cosine", "euclidean"] = "cosine",
    eps: Optional[float] = None,
    min_samples: int = MIN_SAMPLES,
    random_state: Optional[int] = None,
) -> OnlineClusteringResult:
    """
    Run online DBSCAN clustering on a batch of alarm records.

    Parameters
    ----------
    records : list of dict, each must have:
        - "source_id"  : str
        - "vector"     : np.ndarray | None  (None → OOV)
    metric  : 'cosine' | 'euclidean'
    eps     : float | None  (auto-computed if None)
    min_samples : DBSCAN min_samples
    random_state : seed for reproducibility

    Returns
    -------
    OnlineClusteringResult
    """
    if random_state is not None:
        np.random.seed(random_state)

    # ── 1. Separate OOV from in-vocab records ─────────────────────────────────
    in_vocab: List[Dict] = []
    oov:      List[Dict] = []
    for r in records:
        (in_vocab if r["vector"] is not None else oov).append(r)

    n_oov = len(oov)
    logger.info(
        "[OnlineCluster] total=%d  in-vocab=%d  OOV=%d",
        len(records), len(in_vocab), n_oov,
    )

    # Pre-build result list: OOV items get cluster_id=-2
    items: List[AlarmClusterItem] = [
        AlarmClusterItem(source_id=r["source_id"], cluster_id=-2, confidence=0.0)
        for r in oov
    ]

    # ── 2. Not enough in-vocab points to cluster ───────────────────────────────
    if len(in_vocab) < min_samples:
        logger.warning(
            "[OnlineCluster] Only %d in-vocab points (< min_samples=%d). "
            "All assigned to noise (cluster_id=-1).",
            len(in_vocab), min_samples,
        )
        items += [
            AlarmClusterItem(source_id=r["source_id"], cluster_id=-1, confidence=0.0)
            for r in in_vocab
        ]
        return OnlineClusteringResult(
            items=items,
            n_clusters=0,
            n_noise=len(in_vocab),
            n_oov=n_oov,
            eps=0.0,
            metric=metric,
            silhouette=None,
        )

    # ── 3. Stack vectors (already L2-normalized from Embedder) ────────────────
    vectors_norm = np.array(
        [r["vector"] for r in in_vocab], dtype=np.float32
    )
    # Re-normalize in case vectors come from a different source
    vectors_norm = normalize(vectors_norm, norm="l2")

    # ── 4. Auto eps ───────────────────────────────────────────────────────────
    auto_eps_val = _auto_eps(vectors_norm, metric)
    used_eps = eps if eps is not None else auto_eps_val
    logger.info(
        "[OnlineCluster] metric=%s | eps=%.4f (auto=%.4f) | min_samples=%d",
        metric, used_eps, auto_eps_val, min_samples,
    )

    # ── 5. DBSCAN ─────────────────────────────────────────────────────────────
    db = DBSCAN(eps=used_eps, min_samples=min_samples, metric=metric)
    labels = db.fit_predict(vectors_norm)

    unique_labels = set(labels)
    n_clusters    = len(unique_labels - {-1})
    n_noise       = int((labels == -1).sum())

    # ── 6. Silhouette ─────────────────────────────────────────────────────────
    mask = labels != -1
    silhouette: Optional[float] = None
    if n_clusters >= 2 and mask.sum() > n_clusters:
        try:
            silhouette = float(
                silhouette_score(
                    vectors_norm[mask], labels[mask],
                    metric=metric, random_state=random_state,
                )
            )
        except Exception:
            pass

    logger.info(
        "[OnlineCluster] Result: %d clusters | %d noise | silhouette=%s",
        n_clusters, n_noise,
        f"{silhouette:.4f}" if silhouette is not None else "N/A",
    )

    # ── 7. Confidence scores ──────────────────────────────────────────────────
    confidences = _density_confidence(labels, vectors_norm, used_eps, metric)

    # ── 8. Build result items for in-vocab records ────────────────────────────
    for i, r in enumerate(in_vocab):
        items.append(AlarmClusterItem(
            source_id  = r["source_id"],
            cluster_id = int(labels[i]),
            confidence = float(confidences[i]),
        ))

    return OnlineClusteringResult(
        items      = items,
        n_clusters = n_clusters,
        n_noise    = n_noise,
        n_oov      = n_oov,
        eps        = float(used_eps),
        metric     = metric,
        silhouette = silhouette,
    )
