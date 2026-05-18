from typing import List, Set, Tuple
from alarm_storm_detection import load_data, detect_alarm_storms, storm_summary


# ── Token helper ───────────────────────────────────────────────────────────────
def make_token(managed_objects: str, probable_cause: str) -> str:
    return f"{managed_objects}|{probable_cause}"


def build_sequences(
    storms: list,
) -> Tuple[List[List[str]], Set[str]]:
    sequences: List[List[str]] = []
    vocab: Set[str] = set()

    for storm in storms:
        seq = []
        for alarm in storm:
            token = make_token(
                alarm["managed_objects"],
                alarm["probable_cause"],
            )
            seq.append(token)
            vocab.add(token)
        sequences.append(seq)

    return sequences, vocab


def sequences_stats(sequences: List[List[str]]) -> None:
    lengths = [len(s) for s in sequences]
    import numpy as np
    print(f"  Tổng sequences : {len(sequences):,}")
    print(f"  Độ dài trung bình  : {np.mean(lengths):.1f}")
    print(f"  Độ dài min/max : {min(lengths)} / {max(lengths)}")
    total_tokens = sum(lengths)
    print(f"  Tổng tokens (có lặp): {total_tokens:,}")


# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from alarm_storm_detection import load_data, detect_alarm_storms

    print("=" * 60)
    print("SEQUENCE BUILDER")
    print("=" * 60)

    df = load_data()
    storms = detect_alarm_storms(df)
    sequences, vocab = build_sequences(storms)

    print(f"\nNumber of storms → sequences: {len(sequences)}")
    print(f"Vocabulary size (unique tokens): {len(vocab)}")

    print("\nSequence statistics:")
    sequences_stats(sequences)

    print("\n--- First 3 sequences ---")
    for i, seq in enumerate(sequences[:3]):
        print(f"  Storm {i}: {seq[:8]}{'...' if len(seq) > 8 else ''}")