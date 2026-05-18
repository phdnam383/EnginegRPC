"""
embedder.py
-----------
Singleton embedding loader.

Loads `models/embeddings.npz` once at startup and exposes
fast lookup methods for the gRPC server.

Token format: "{managed_objects}|{probable_cause}"
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from sklearn.preprocessing import normalize

logger = logging.getLogger(__name__)

_MODEL_DIR = os.environ.get("MODEL_DIR", "models")


class Embedder:
    """
    Singleton wrapper around the pre-trained Skip-Gram embeddings.

    Usage
    -----
    emb = Embedder.get_instance()           # load once
    vec = emb.lookup("vdu_csdb|LINK_DOWN")  # np.ndarray | None
    """

    _instance: Optional["Embedder"] = None

    def __init__(self, model_dir: str = _MODEL_DIR) -> None:
        npz_path = Path(model_dir) / "embeddings.npz"
        if not npz_path.exists():
            raise FileNotFoundError(
                f"[Embedder] embeddings.npz not found at '{npz_path}'. "
                "Run word2vec_skipgram.py first to train and save embeddings."
            )

        data = np.load(npz_path, allow_pickle=True)
        matrix: np.ndarray = data["matrix"].astype(np.float32)  # (V, D)
        tokens: List[str]  = data["tokens"].tolist()

        # L2-normalize once — cosine sim becomes dot product later
        self._matrix_norm: np.ndarray      = normalize(matrix, norm="l2")
        self._token2idx:   Dict[str, int]  = {t: i for i, t in enumerate(tokens)}
        self._vocab_size:  int             = len(tokens)
        self._dim:         int             = matrix.shape[1]

        logger.info(
            "[Embedder] Loaded %d tokens, dim=%d from '%s'",
            self._vocab_size, self._dim, npz_path,
        )

    # ── Singleton factory ──────────────────────────────────────────────────────
    @classmethod
    def get_instance(cls, model_dir: str = _MODEL_DIR) -> "Embedder":
        if cls._instance is None:
            cls._instance = cls(model_dir)
        return cls._instance

    # ── Public API ─────────────────────────────────────────────────────────────
    @property
    def dim(self) -> int:
        return self._dim

    @property
    def vocab_size(self) -> int:
        return self._vocab_size

    def is_in_vocab(self, token: str) -> bool:
        return token in self._token2idx

    def lookup(self, token: str) -> Optional[np.ndarray]:
        """
        Return the L2-normalised embedding vector for *token*.
        Returns None if token is OOV (out-of-vocabulary).
        """
        idx = self._token2idx.get(token)
        if idx is None:
            return None
        return self._matrix_norm[idx]

    def lookup_batch(
        self,
        tokens: List[str],
    ) -> Dict[str, Optional[np.ndarray]]:
        """
        Batch lookup. Returns a dict {token: vector | None}.
        OOV tokens map to None.
        """
        return {t: self.lookup(t) for t in tokens}

    @staticmethod
    def make_token(managed_objects: str, probable_cause: str) -> str:
        """Build the canonical token used during training."""
        return f"{managed_objects}|{probable_cause}"
