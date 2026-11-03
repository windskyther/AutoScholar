"""Binary relevance metrics. Duplicate IDs consume ranks but earn no second gain."""

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RankingScore:
    recall_at_k: dict[int, float]
    hit_rate_at_k: dict[int, float]
    mrr: float
    ndcg_at_k: dict[int, float]


@dataclass(frozen=True, slots=True)
class RankingAggregate(RankingScore):
    count: int


def normalize_ks(ks: Sequence[int]) -> tuple[int, ...]:
    if not ks or any(type(k) is not int or not 1 <= k <= 50 for k in ks):
        raise ValueError("Benchmark K values must be positive integers at most 50")
    result = tuple(sorted(set(ks)))
    if len(result) > 10:
        raise ValueError("Select at most 10 distinct K values")
    return result


def validate_ids(ids: Sequence[str]) -> None:
    if len(ids) > 100 or any(
        not isinstance(value, str)
        or not value
        or len(value) > 200
        or value != value.strip()
        or any(ord(char) < 32 for char in value)
        for value in ids
    ):
        raise ValueError("Relevance IDs must be nonempty bounded strings")


def score_ranking(
    ranked_ids: Sequence[str],
    relevant_ids: Sequence[str],
    *,
    ks: Sequence[int] = (5, 10),
) -> RankingScore:
    cutoffs = normalize_ks(ks)
    validate_ids(relevant_ids)
    if not relevant_ids:
        raise ValueError("Ranking metrics require relevant IDs")
    ranked = list(ranked_ids[: cutoffs[-1]])
    validate_ids(ranked)
    relevant = set(relevant_ids)
    seen: set[str] = set()
    gains = []
    for identifier in ranked:
        gains.append(identifier in relevant and identifier not in seen)
        seen.add(identifier)
    first = next((rank for rank, gain in enumerate(gains, 1) if gain), None)
    recalls, hits, ndcgs = {}, {}, {}
    for k in cutoffs:
        found = len(set(ranked[:k]) & relevant)
        recalls[k] = found / len(relevant)
        hits[k] = float(found > 0)
        dcg = sum(1 / math.log2(rank + 1) for rank, gain in enumerate(gains[:k], 1) if gain)
        ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(k, len(relevant)) + 1))
        ndcgs[k] = dcg / ideal
    return RankingScore(recalls, hits, 1 / first if first is not None else 0.0, ndcgs)


def aggregate_rankings(scores: Sequence[RankingScore]) -> RankingAggregate | None:
    if not scores:
        return None
    ks = tuple(scores[0].recall_at_k)
    if any(tuple(score.recall_at_k) != ks for score in scores):
        raise ValueError("Cannot aggregate different K values")
    count = len(scores)
    return RankingAggregate(
        recall_at_k={k: sum(score.recall_at_k[k] for score in scores) / count for k in ks},
        hit_rate_at_k={k: sum(score.hit_rate_at_k[k] for score in scores) / count for k in ks},
        mrr=sum(score.mrr for score in scores) / count,
        ndcg_at_k={k: sum(score.ndcg_at_k[k] for score in scores) / count for k in ks},
        count=count,
    )
