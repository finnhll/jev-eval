"""Statistics used by the checks, implemented on the standard library only.

Sample sizes here are small by design, so anything that produces a headline
number also offers a bootstrap interval. A point estimate from 20 hand-written
cases is not evidence on its own.
"""

from __future__ import annotations

import math
import random
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def stdev(xs: Sequence[float]) -> float:
    """Population standard deviation; 0.0 for a single observation."""
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def ranks(xs: Sequence[float]) -> List[float]:
    """Ranks with ties averaged, as Spearman requires."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = shared
        i = j + 1
    return out


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Rank correlation. Returns nan when either side is constant."""
    if len(xs) != len(ys) or len(xs) < 2:
        return float("nan")
    rx, ry = ranks(xs), ranks(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else float("nan")


def inversions(xs: Sequence[float], tolerance: float = 0.0) -> List[Tuple[int, int]]:
    """Pairs (i, j), i < j, where xs[j] falls below xs[i] by more than tolerance.

    Used for monotonic ladders: the expected order is the given order.
    """
    bad = []
    for i in range(len(xs)):
        for j in range(i + 1, len(xs)):
            if xs[j] < xs[i] - tolerance:
                bad.append((i, j))
    return bad


def tv_distance(p: Mapping[str, float], q: Mapping[str, float]) -> float:
    """Total variation distance between two distributions over the same support."""
    keys = set(p) | set(q)
    return 0.5 * sum(abs(p.get(k, 0.0) - q.get(k, 0.0)) for k in keys)


def brier(pairs: Sequence[Tuple[float, bool]]) -> float:
    """Mean squared error of probabilistic predictions. Lower is better."""
    if not pairs:
        return float("nan")
    return mean([(prob - (1.0 if label else 0.0)) ** 2 for prob, label in pairs])


def reliability_bins(
    pairs: Sequence[Tuple[float, bool]], n_bins: int = 5
) -> List[Dict[str, float]]:
    """Group predictions by probability and compare each group to its outcome rate."""
    bins: List[Dict[str, float]] = []
    for b in range(n_bins):
        lo, hi = b / n_bins, (b + 1) / n_bins
        members = [
            (p, y) for p, y in pairs
            if (lo <= p < hi) or (b == n_bins - 1 and p == 1.0)
        ]
        if not members:
            bins.append({"lo": lo, "hi": hi, "n": 0,
                         "mean_prob": float("nan"), "observed": float("nan"),
                         "gap": float("nan")})
            continue
        mp = mean([p for p, _ in members])
        obs = mean([1.0 if y else 0.0 for _, y in members])
        bins.append({"lo": lo, "hi": hi, "n": len(members),
                     "mean_prob": mp, "observed": obs, "gap": obs - mp})
    return bins


def ece(pairs: Sequence[Tuple[float, bool]], n_bins: int = 5) -> float:
    """Expected calibration error: sample-weighted mean gap across bins."""
    bins = [b for b in reliability_bins(pairs, n_bins) if b["n"]]
    total = sum(b["n"] for b in bins)
    if not total:
        return float("nan")
    return sum(b["n"] * abs(b["gap"]) for b in bins) / total


def auroc(scores: Sequence[float], labels: Sequence[bool]) -> float:
    """Probability a positive outranks a negative. Ties count as half."""
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return float("nan")
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else (0.5 if p == n else 0.0)
    return wins / (len(pos) * len(neg))


def confusion(pairs: Sequence[Tuple[float, bool]], threshold: float) -> Dict[str, float]:
    """Counts and rates for one cutoff, predicting yes when value >= threshold."""
    tp = fp = tn = fn = 0
    for value, truth in pairs:
        predicted = value >= threshold
        if predicted and truth:
            tp += 1
        elif predicted and not truth:
            fp += 1
        elif not predicted and truth:
            fn += 1
        else:
            tn += 1
    n = tp + fp + tn + fn
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    f1 = (2 * precision * recall / (precision + recall)
          if precision == precision and recall == recall and (precision + recall) else float("nan"))
    return {
        "threshold": threshold, "tp": tp, "fp": fp, "tn": tn, "fn": fn, "n": n,
        "accuracy": (tp + tn) / n if n else float("nan"),
        "precision": precision, "recall": recall, "f1": f1,
    }


def band_split(pairs: Sequence[Tuple[float, bool]], lo: float, hi: float) -> Dict[str, float]:
    """Three-way routing: auto-no below lo, auto-yes at or above hi, review between.

    The number that matters when choosing a band is not overall accuracy but
    accuracy on the part you let through automatically, read next to how much
    of the traffic that leaves for a person.
    """
    auto_yes = auto_no = review = correct = 0
    for value, truth in pairs:
        if value >= hi:
            auto_yes += 1
            correct += 1 if truth else 0
        elif value <= lo:
            auto_no += 1
            correct += 0 if truth else 1
        else:
            review += 1
    decided = auto_yes + auto_no
    n = decided + review
    return {
        "lo": lo, "hi": hi, "n": n, "auto_yes": auto_yes, "auto_no": auto_no,
        "review": review,
        "coverage": decided / n if n else float("nan"),
        "review_share": review / n if n else float("nan"),
        "auto_accuracy": correct / decided if decided else float("nan"),
        "errors": decided - correct,
    }


def bootstrap_ci(
    values: Sequence,
    statistic: Callable[[Sequence], float],
    n_resamples: int = 2000,
    alpha: float = 0.05,
    seed: int = 20260920,
) -> Tuple[float, float]:
    """Percentile bootstrap interval. Wide intervals here are the honest answer."""
    if len(values) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    stats = []
    n = len(values)
    for _ in range(n_resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        try:
            stats.append(statistic(sample))
        except (ValueError, ZeroDivisionError):
            continue
    if not stats:
        return (float("nan"), float("nan"))
    stats.sort()
    lo = stats[int(alpha / 2 * len(stats))]
    hi = stats[min(len(stats) - 1, int((1 - alpha / 2) * len(stats)))]
    return (lo, hi)


def percentile(xs: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile, q in [0, 1]."""
    if not xs:
        return float("nan")
    ordered = sorted(xs)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def fmt(x: Optional[float], digits: int = 3) -> str:
    """Format a float for report tables, tolerating None and nan."""
    if x is None:
        return "-"
    if isinstance(x, float) and math.isnan(x):
        return "n/a"
    return ("%." + str(digits) + "f") % x
