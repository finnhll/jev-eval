# Findings

One full run: 282 responses, 21 experiments, `typesafe/jev-1.13-20260917` via OpenRouter,
2026-09-20. Every number below is reproducible from `results/raw.jsonl` with
`python3 cli.py check`, without an API key.

**PASS 159 · FAIL 1 · WARN 7 · INFO 222**

A **FAIL** is an expectation taken from the TypeSafe documentation that did not reproduce.
A **WARN** is an expectation this suite's author predicted and the model contradicted — a
result, not a defect. All seven warnings are recorded in the `note` field of the case that
raised them.

---

## The documented numbers reproduce

The docs publish recorded `jev-1.13.0` answers in three tables. All three reproduce on
`jev-1.13-20260917`:

| Case | Published | Observed | Max deviation |
|---|---|---|---|
| N1 `is_human_escalation`, 6 states | 0.02 / 0.07 / 0.26 / 0.40 / 0.84 / 0.99 | identical | 0.00 |
| S1 `bug_severity`, 5 states | 0.0 / 1.0 / 1.11 / 1.43 / 2.0 | 0.00 / 1.00 / 1.12 / 1.43 / 2.00 | 0.01 |
| X Python experience, 4 states | noul 0.03 / 0.14 / 0.81 / 0.92 | identical | 0.00 |

Confidence values reproduce too (S1 `safari_crash` 0.35 published, 0.32 observed; C1
`three_way` 0.42 published, 0.48 observed).

## Structural invariants: 282 / 282 clean

Checked on every response, no ground truth needed:

- probabilities sum to 1
- `choice` is the argmax of its own distribution
- `score` equals Σ(level number × probability), the definition the docs give
- `legend` keys match the level indices; answer ids match the question ids
- Noul carries no `confidence`; Choice and Score always do

The score identity holds to within rounding. Probabilities come back at two decimals and are
multiplied by the level number before summing, so the tolerance has to widen with the level
count — `0.01 + 0.005 × (levels − 1)`. A fixed 0.02 tolerance produces false alarms on a
ten-level rubric.

---

## The one failure: option order moves the distribution

**C2.** The same question, the same state, three orderings of the `criteria` map. Nothing about
the judgment changed.

The two unambiguous states are perfectly order-invariant (TV distance 0.000). The ambiguous one
is not:

| criteria order | choice | returns | billing | shipping | confidence |
|---|---|---|---|---|---|
| returns, shipping, billing | returns | 0.62 | 0.33 | 0.05 | **0.43** |
| billing, shipping, returns | returns | 0.48 | 0.46 | 0.06 | **0.22** |
| shipping, billing, returns | returns | 0.58 | 0.36 | 0.06 | **0.37** |

Total variation distance 0.14 between the first two. The **selected answer is stable**; the
number a confidence gate reads is not.

Code thresholding near 0.3 would route the same ticket differently depending only on the order
the options were written in. Where a Choice is genuinely split, treat its confidence as
approximate, or fix one canonical ordering and never change it.

## The more consequential result: confidence does not detect an uncovered input

**C4.** Three messages belonging to none of the three listed departments, asked with and without
an `other` option:

| input | no fallback | with fallback |
|---|---|---|
| "Do you sponsor work visas?" | billing, conf 0.58 | other, conf 1.00 |
| "Your footer says 'Copywright'" | billing, conf **0.96** | other, conf 1.00 |
| "We'd like to discuss a partnership" | billing, conf **0.93** | other, conf 1.00 |

Two of three arrived wrong and confident. The prediction that confidence would collapse when
nothing fits was wrong, and the practical consequence is firm: **every Choice needs a fallback
option.** Confidence will not warn you that your taxonomy has a hole.

---

## Claimed properties

| Claim | Source | Result |
|---|---|---|
| Questions evaluated independently | docs | **Confirmed.** 13 questions batched vs 13 separate calls: answers identical to the digit, both states. |
| Batching is 11.5× cheaper | docs | **Partly.** Observed 8.0–8.2×. Cheaper, less than claimed; the ratio depends on state size. |
| Batching is 9.6× faster | docs | **Exceeded.** Observed 18.8–24.3× wall-clock, serial on both arms. |
| Adding questions barely changes latency | docs | **Confirmed.** p50 for 1 / 3 / 10 / 30 questions: 403 / 388 / 419 / 431 ms. |
| Stable across repeated evaluations | docs | **Confirmed.** 10 states × 5 repeats: value sd ≤ 0.0075, zero choice flips. |
| A Score measures one dimension | docs | **Confirmed.** Bundling detail + politeness + urgency into the levels dropped mean confidence 0.92 → 0.72. A detailed, plain report fell from 2.00 at confidence 1.00 to 1.25 at 0.61. |
| Numeric level labels degrade a Score | docs | **Confirmed, with a limit.** Mid-scale: confidence 1.00 → 0.36. At an unmistakable top-of-scale report: 1.00 → 0.97. Bare numbers cost almost nothing where the state is extreme, and nearly everything where it is not. |
| `instructions` accepts `null` | docs | **Contradicted.** HTTP 400, validator expected string, record or array. Every other documented shape — null criteria, object criteria, array instructions, nested subtree criteria, object score levels — passes through the OpenRouter gateway intact. |

---

## Per-primitive

### Noul

- **Negation is symmetric.** `p("contains personal data") + p("is free of personal data")`
  summed to 1.00 ± 0.01 on every state. Phrasing direction does not shift the answer, so a
  codebase does not need a phrasing audit — though the docs' advice to phrase positively still
  holds for the humans reading the code.
- **Compound questions behave as predicted.** A 2×2 of angry × refund-seeking. The compound
  "angry and asking for a refund" reads 0.99 / 0.13 / 0.15 / 0.02 against atomic parts of
  0.98+0.99 / 0.98+0.05 / 0.07+0.99 / low+low. It tracks something like a conjunction, but the
  two mixed corners collapse two different situations onto near-identical numbers. Decompose.
- **Calibration**, 20 labelled resume snippets: Brier **0.026** [0.002, 0.062], accuracy@0.5
  **0.95** [0.85, 1.00], ECE 0.094. The single miss was the case that is genuinely arguable —
  "taught an introductory Python class at a community college", returned 0.45. Twenty samples
  cannot establish calibration; this shows no large systematic bias, nothing more.

### Choice

- **Option names vs descriptions.** Replacing meaningful names with `opt_a/opt_b/opt_c` while
  keeping the descriptions changed nothing (agreement 1.00, mean confidence −0.01): the
  descriptions carry the signal. Dropping the descriptions and keeping the names cost more
  (agreement 0.67, confidence −0.08). Self-explanatory names work, but not as well as the
  docs' `tone` example might suggest.
- **100 options cost nothing but tokens.** 3 / 20 / 100 options with the correct answer always
  present: accuracy held, confidence stayed 1.00, latency flat (360 / 362 / 382 ms p50). Input
  tokens rose 362 → 638 → 1892. Give the full list.

### Score

- **Monotonic ladder.** Six reports written as a strictly increasing ladder before any were
  sent: Spearman 0.986, zero inversions. The top two rungs both saturate at 4.00, so the scale
  stops resolving once a report is thorough.
- **Level count.** The same eight states under 2, 5 and 10 levels:

  | levels | Spearman | mean confidence | input tokens |
  |---|---|---|---|
  | 2 | 0.55 | 0.99 | 308 |
  | 5 | **0.976** | 0.85 | 361 |
  | 10 | **0.976** | 0.82 | 407 |

  Two levels saturate — six of eight states land at 0.99 or 1.00 — so the high confidence is an
  artifact of having nowhere to put anything. Ten levels rank no better than five and report
  less confidence. **Five is the sweet spot here**; the docs' advice to add only levels you can
  describe distinctly is doing real work.

### Path references

Targeting works, and more importantly it fails loudly rather than silently:

| question | value |
|---|---|
| `` `ticket.messages[0].text` `` requests a refund (customer's message) | 0.99 |
| `` `ticket.messages[1].text` `` requests a refund (support's reply) | 0.02 |
| `` `ticket.messages[9].text` `` requests a refund (out of range) | 0.02 |
| `` `order.nonexistent_field` `` shows a duplicate payment | 0.12 |

An out-of-range index returns 0.02, not the 0.99 it would return if the model quietly fell back
to reading the whole state. A schema change that breaks a path degrades to a low answer rather
than a plausible wrong one. Cross-field reasoning tracks mutations correctly: flipping
`refund_policy` to "no refunds" moved `policy_supports_refund` from 0.98 to 0.02.

---

## Chinese-language content

**L.** Eight tickets written twice, under three conditions:

| condition | agreement with English baseline | mean \|Δ value\| | mean Δ confidence |
|---|---|---|---|
| Chinese content, English questions | **0.92** | 0.072 | +0.023 |
| Chinese content, Chinese questions | **0.79** | 0.099 | −0.055 |

The docs say non-English input has lower accuracy without quantifying it. Chinese content costs
about 8 points of agreement. Translating the *questions* into Chinese costs another 13 — the
opposite of the intuitive move.

**Keep the content in its own language; write `instructions` and `criteria` in English.**

Caveat: these eight tickets are generic customer-support prose written for this suite. Real
product language — jargon, in-house shorthand, clipped phrasing — would likely move both
numbers. Re-run `cases/cross/l1_language_parity.json` with your own tickets before acting on it.

---

## Reading these numbers

The robust results here are the ones resting on relations rather than point values: ordering
along a ladder, a confidence gap between clear and ambiguous inputs, agreement across three
phrasings, identity between a batched and a split call. Those hold on small samples because
they compare like with like.

The fragile ones are the absolute statistics — calibration above all. Twenty hand-written
states produce bins holding one or two samples, and bootstrap intervals wide enough to contain
most hypotheses. They are reported with intervals attached for exactly that reason, and should
be read as "no large bias found", not "well calibrated".
