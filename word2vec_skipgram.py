import random
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, random_split
from collections import Counter, defaultdict
from typing import List, Dict, Tuple, Optional
from pathlib import Path
import time
import matplotlib.pyplot as plt


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


# ── Helpers ────────────────────────────────────────────────────────────────────
def auto_vector_size(vocab_size: int) -> int:
    size = max(16, min(128, int(vocab_size ** 0.5)))
    return size


def build_vocab(sequences: List[List[str]]) -> Tuple[Dict[str, int], Dict[int, str]]:
    all_tokens = [tok for seq in sequences for tok in seq]
    unique     = sorted(set(all_tokens))
    token2idx  = {tok: i for i, tok in enumerate(unique)}
    idx2token  = {i: tok for tok, i in token2idx.items()}
    return token2idx, idx2token


# ── Dataset ────────────────────────────────────────────────────────────────────
class SkipGramDataset(Dataset):
    def __init__(
        self,
        sequences:   List[List[str]],
        token2idx:   Dict[str, int],
        window_size: int = 5,
        n_negatives: int = 5,
    ):
        self.token2idx   = token2idx
        self.vocab_size  = len(token2idx)
        self.n_negatives = n_negatives

        # ── Negative sampling distribution: freq^0.75 ──────────────────────────
        counter = Counter(tok for seq in sequences for tok in seq)
        freqs   = np.array([counter.get(idx2tok, 0)
                            for idx2tok in sorted(token2idx, key=token2idx.get)])
        freqs   = freqs ** 0.75
        self.neg_dist = (freqs / freqs.sum()).astype(np.float32)

        # ── Build positive pairs ───────────────────────────────────────────────
        pairs: List[Tuple[int, int]] = []
        for seq in sequences:
            idxs = [token2idx[t] for t in seq]
            n    = len(idxs)
            for i in range(n):
                left  = max(0, i - window_size)
                right = min(n, i + window_size + 1)
                for j in range(left, right):
                    if i != j:
                        pairs.append((idxs[i], idxs[j]))

        self.pairs = torch.tensor(pairs, dtype=torch.long)

        # Precompute positive context sets per center for negative rejection
        self.positive_contexts: Dict[int, set] = defaultdict(set)
        for c, ctx in pairs:
            self.positive_contexts[c].add(ctx)

        print(f"  [Dataset] {len(self.pairs):,} cặp (center, context) | vocab={self.vocab_size} | dim={auto_vector_size(self.vocab_size)}")

    def __len__(self) -> int:
        return len(self.pairs)

    def __getitem__(self, idx: int):
        center, context = self.pairs[idx]
        excluded = self.positive_contexts[center.item()]

        negatives: List[int] = []
        while len(negatives) < self.n_negatives:
            candidates = np.random.choice(
                self.vocab_size,
                size=max(self.n_negatives * 3, 16),
                p=self.neg_dist,
            )
            for c in candidates:
                if int(c) not in excluded and int(c) not in negatives:
                    negatives.append(int(c))
                    if len(negatives) == self.n_negatives:
                        break

        return center, context, torch.tensor(negatives, dtype=torch.long)


# ── Model ──────────────────────────────────────────────────────────────────────
class SkipGramModel(nn.Module):
    def __init__(self, vocab_size: int, vector_size: int):
        super().__init__()
        self.in_embeddings  = nn.Embedding(vocab_size, vector_size)
        self.out_embeddings = nn.Embedding(vocab_size, vector_size)

        # Init weights
        nn.init.uniform_(self.in_embeddings.weight,  -0.5 / vector_size, 0.5 / vector_size)
        nn.init.zeros_(self.out_embeddings.weight)

    def forward(
        self,
        center:    torch.Tensor,   # (B,)
        context:   torch.Tensor,   # (B,)
        negatives: torch.Tensor,   # (B, n_neg)
    ) -> torch.Tensor:
        """
        Negative Sampling Loss:
            L = -log σ(v_c · v_o)  - Σ log σ(-v_c · v_neg)
        """
        v_c   = self.in_embeddings(center)          # (B, D)
        v_o   = self.out_embeddings(context)         # (B, D)
        v_neg = self.out_embeddings(negatives)        # (B, n_neg, D)

        # Positive loss
        pos_score = (v_c * v_o).sum(dim=1)           # (B,)
        pos_loss  = -torch.log(torch.sigmoid(pos_score) + 1e-7)  # (B,)

        # Negative loss
        neg_score = torch.bmm(v_neg, v_c.unsqueeze(2)).squeeze(2)  # (B, n_neg)
        neg_loss  = -torch.log(torch.sigmoid(-neg_score) + 1e-7).sum(dim=1)  # (B,)

        return (pos_loss + neg_loss).mean()

    @torch.no_grad()
    def get_embeddings(self) -> np.ndarray:
        return self.in_embeddings.weight.detach().cpu().numpy()


# ── Training ───────────────────────────────────────────────────────────────────
def train_skipgram(
    sequences:   List[List[str]],
    window_size: int = 5,
    n_negatives: int = 5,
    batch_size:  int = 1024,
    epochs:      int = 50,
    lr:          float = 0.001,
    val_ratio:   float = 0.1,   # Fraction of pairs used for validation
    patience:    int = 5,       # The number of consecutive epochs did not improve before stopping
    min_delta:   float = 1e-4,  # Minimum threshold to be considered as "improved"
    seed:        int = 42,
    device:      Optional[str] = None,
) -> Tuple[Dict[str, np.ndarray], "SkipGramModel", Dict[str, int], Dict[str, List[float]]]:
    set_seed(seed)
    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    device = torch.device(device)
    print(f"\n[SkipGram] Device: {device}")

    # Vocabulary
    token2idx, idx2token = build_vocab(sequences)
    vocab_size  = len(token2idx)
    vector_size = auto_vector_size(vocab_size)
    print(f"[SkipGram] vocab_size={vocab_size} | vector_size={vector_size} (= sqrt({vocab_size})≈{vocab_size**0.5:.1f})")

    # Dataset & Loader
    dataset  = SkipGramDataset(sequences, token2idx, window_size, n_negatives)
    n_val    = max(1, int(len(dataset) * val_ratio))
    n_train  = len(dataset) - n_val
    train_ds, val_ds = random_split(dataset, [n_train, n_val],
                                    generator=torch.Generator().manual_seed(seed))
    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True,  num_workers=0)
    val_loader   = DataLoader(val_ds,   batch_size=batch_size, shuffle=False, num_workers=0)
    print(f"  [Split] train={n_train:,} pairs | val={n_val:,} pairs ({val_ratio*100:.0f}%)")

    # Model & Optimizer
    model     = SkipGramModel(vocab_size, vector_size).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)

    # Training loop
    print(f"\n[SkipGram] Bắt đầu training {epochs} epochs (patience={patience}, min_delta={min_delta})...")
    best_loss  = float("inf")
    no_improve = 0
    best_state = None
    history: Dict[str, List[float]] = {"train": [], "val": []}

    for epoch in range(1, epochs + 1):
        t0         = time.time()
        total_loss = 0.0
        model.train()
        for center, context, negatives in train_loader:
            center    = center.to(device)
            context   = context.to(device)
            negatives = negatives.to(device)

            optimizer.zero_grad()
            loss = model(center, context, negatives)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        train_loss = total_loss / len(train_loader)

        # Validation loss
        model.eval()
        val_total = 0.
        with torch.no_grad():
            for center, context, negatives in val_loader:
                center    = center.to(device)
                context   = context.to(device)
                negatives = negatives.to(device)
                val_total += model(center, context, negatives).item()
        val_loss = val_total / len(val_loader)

        history["train"].append(train_loss)
        history["val"].append(val_loss)

        elapsed = time.time() - t0
        if epoch == 1 or epoch % 10 == 0 or epoch == epochs:
            print(f"  Epoch {epoch:>3}/{epochs} | Train: {train_loss:.4f} | Val: {val_loss:.4f} | {elapsed:.1f}s")

        if val_loss < best_loss - min_delta:
            best_loss  = val_loss
            no_improve = 0
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
        else:
            no_improve += 1
            if no_improve >= patience:
                print(f"  [EarlyStopping] Dừng tại epoch {epoch} | best_val_loss={best_loss:.4f}")
                break

    # Restore best weights
    if best_state is not None:
        model.load_state_dict(best_state)

    # Extract embeddings
    model.eval()
    emb_matrix = model.get_embeddings()  # (vocab_size, vector_size)
    token2vec  = {tok: emb_matrix[idx] for tok, idx in token2idx.items()}

    print(f"\n[SkipGram] Done! Embedding shape: {emb_matrix.shape}")
    return token2vec, model, token2idx, history


# ── Plot ───────────────────────────────────────────────────────────────────────
def plot_loss(
    history:   Dict[str, List[float]],
    save_path: Optional[str] = None,
) -> None:
    """Vẽ đường train_loss và val_loss theo epoch."""
    epochs = range(1, len(history["train"]) + 1)
    plt.figure(figsize=(8, 5))
    plt.plot(epochs, history["train"], label="Train Loss", linewidth=2)
    if history.get("val"):
        plt.plot(epochs, history["val"], label="Val Loss", linewidth=2, linestyle="--")
    plt.xlabel("Epoch")
    plt.ylabel("Loss")
    plt.title("SkipGram – Training & Validation Loss")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    if save_path:
        plt.savefig(save_path, dpi=150)
        print(f"  [Plot] Saved → {save_path}")
    else:
        plt.show()
    plt.close()


# ── Save / Load ────────────────────────────────────────────────────────────────
def save_embeddings(
    token2vec: Dict[str, np.ndarray],
    token2idx: Dict[str, int],
    out_dir:   str = "models",
) -> None:
    """
    Lưu embedding ra 2 file trong thư mục `out_dir`:
      - embeddings.npz  : ma trận numpy + vocab (load nhanh trong pipeline)
      - vectors.tsv     : embedding values  }
      - metadata.tsv    : token labels       }  dùng cho TF Projector
    """
    save_path = Path(out_dir)
    save_path.mkdir(parents=True, exist_ok=True)

    tokens = [tok for tok, _ in sorted(token2idx.items(), key=lambda x: x[1])]
    matrix = np.array([token2vec[t] for t in tokens], dtype=np.float32)

    # ── 1. NumPy archive (.npz) ───────────────────────────────────────────────
    npz_path = save_path / "embeddings.npz"
    np.savez(npz_path, matrix=matrix, tokens=np.array(tokens))
    print(f"  [Save] embeddings.npz  → {npz_path}  shape={matrix.shape}")

    # ── 2. TSV pair (TF Embedding Projector) ─────────────────────────────────
    vec_path  = save_path / "vectors.tsv"
    meta_path = save_path / "metadata.tsv"

    np.savetxt(vec_path, matrix, delimiter="\t", fmt="%.6f")
    meta_path.write_text("\n".join(tokens), encoding="utf-8")
    print(f"  [Save] vectors.tsv     → {vec_path}")
    print(f"  [Save] metadata.tsv    → {meta_path}")


def load_embeddings(out_dir: str = "models") -> Tuple[Dict[str, np.ndarray], Dict[str, int]]:
    """Load toàn bộ embedding từ file .npz → (token2vec, token2idx)."""
    npz_path = Path(out_dir) / "embeddings.npz"
    data      = np.load(npz_path, allow_pickle=True)
    matrix    = data["matrix"]            # (vocab_size, vector_size)
    tokens    = data["tokens"].tolist()   # List[str]
    token2idx = {tok: i for i, tok in enumerate(tokens)}
    token2vec = {tok: matrix[i] for i, tok in enumerate(tokens)}
    print(f"  [Load] {npz_path}  shape={matrix.shape}")
    return token2vec, token2idx


def get_token_embedding(
    query:   "str | List[str]",
    out_dir: str = "models",
) -> "np.ndarray | Dict[str, Optional[np.ndarray]]":
    """
    Tra cứu embedding vector theo token mà không cần load toàn bộ dict.

    Parameters
    ----------
    query   : str hoặc List[str] – token (hoặc danh sách token) cần tra cứu.
              Token có dạng 'vnfc_id|alarm_type|probable_cause'.
    out_dir : str – thư mục chứa embeddings.npz

    Returns
    -------
    - Nếu query là str  → np.ndarray shape (D,), hoặc None nếu OOV.
    - Nếu query là list → Dict[str, np.ndarray | None]  (None cho token OOV).
    """
    npz_path = Path(out_dir) / "embeddings.npz"
    data     = np.load(npz_path, allow_pickle=True)
    matrix   = data["matrix"]                    # (vocab_size, D)
    tokens   = data["tokens"].tolist()           # List[str]
    token2idx: Dict[str, int] = {t: i for i, t in enumerate(tokens)}

    single = isinstance(query, str)
    keys   = [query] if single else list(query)

    result: Dict[str, Optional[np.ndarray]] = {}
    for tok in keys:
        idx = token2idx.get(tok)
        result[tok] = matrix[idx] if idx is not None else None

    if single:
        vec = result[query]
        if vec is None:
            print(f"  [OOV] token not found in vocab: '{query}'")
        return vec
    return result


# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from alarm_storm_detection import load_data, detect_alarm_storms
    from sequence_builder import build_sequences

    print("=" * 60)
    print("WORD2VEC SKIPGRAM (PyTorch)")
    print("=" * 60)

    df = load_data()
    storms = detect_alarm_storms(df)
    seqs, _ = build_sequences(storms)

    token2vec, model, token2idx, history = train_skipgram(seqs, epochs=20)

    # Save embeddings
    print("\nSaving embeddings...")
    save_embeddings(token2vec, token2idx, out_dir="models")

    # Plot loss
    plot_loss(history, save_path="models/loss_curve.png")

    # Top-5 most similar tokens to the first token
    from sklearn.metrics.pairwise import cosine_similarity

    tokens = list(token2idx.keys())
    vectors = np.array([token2vec[t] for t in tokens])

    query = tokens[0]
    q_vec = token2vec[query].reshape(1, -1)
    sims = cosine_similarity(q_vec, vectors)[0]
    top5 = np.argsort(sims)[::-1][:6]

    print(f"\nTop-5 most similar tokens to '{query}':")
    for i in top5[1:]:
        print(f"  {tokens[i]:40s}  sim={sims[i]:.4f}")