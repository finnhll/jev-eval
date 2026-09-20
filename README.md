# jev-eval

A harness for validating Jev's three primitives - **Choice**, **Score** and **Noul** - through
OpenRouter's Decisions API (`POST /api/alpha/decisions`, model `~typesafe/jev-latest`).

Jev is not a chat model. It answers narrow, typed questions about a state and returns calibrated
probabilities: a yes/no probability (Noul), a pick from options you define (Choice), or a
position on an ordered rubric (Score). This repo asks whether those answers behave the way the
documentation says they do.

Standard library only. No install step, no dependencies.

## Results

One full run of 21 experiments is committed, raw responses included, so every claim below can be
re-derived with `python3 cli.py check` and no API key. 282 trials: 281 answered, one shape
rejected on purpose. Full write-up in
[FINDINGS.md](FINDINGS.md).

| | |
|---|---|
| Documented values | **Reproduce.** All three published tables land within 0.01 on `jev-1.13-20260917`. |
| Structural invariants | **282 / 282 clean.** Probabilities sum to 1, `choice` is its own argmax, `score` equals the probability-weighted mean of the level numbers. |
| Question independence | **Confirmed.** 13 questions batched vs 13 separate calls: answers identical to the digit. |
| Latency vs question count | **Flat.** p50 of 403 / 388 / 419 / 431 ms for 1 / 3 / 10 / 30 questions. |
| Repeat stability | **Confirmed.** 10 states x 5 repeats: value sd <= 0.0075, zero choice flips. |
| Calibration (n=20) | Brier 0.026 [0.002, 0.062], accuracy@0.5 0.95. Small sample; read as "no large bias found". |
| **Option order** | **Moves the distribution.** On an ambiguous input, reordering the `criteria` map shifted the winning probability 0.62 -> 0.48 and confidence 0.43 -> 0.22. The choice held; the number a confidence gate reads did not. |
| **Uncovered inputs** | **Confidence will not warn you.** With no `other` option, two of three out-of-taxonomy messages came back wrong at confidence 0.93+. Always include a fallback. |
| Chinese content | 0.92 agreement with the English baseline using English questions; 0.79 when the questions are also translated. Keep content in its language, write questions in English. |
| `instructions: null` | Documented as accepted, **rejected with HTTP 400**. Asserted in the suite so either side changing is visible. |

## Quickstart

```bash
export OPENROUTER_API_KEY=sk-or-v1-...

python3 cli.py plan       # what would run, and what it costs
python3 cli.py collect    # call the API, append to results/raw.jsonl
python3 cli.py check      # evaluate what was collected (offline, free, repeatable)
python3 cli.py report --out results/report.html
```

A full run is 282 calls, about 70 seconds and under a cent.

## The UI

Two ways in, same data:

```bash
python3 cli.py serve      # http://127.0.0.1:8787 — everything, including the playground
open docs/index.html      # static: the results, no API access
```

`docs/` is a dependency-free page that reads `docs/data.json`, built from the same raw
responses and the same check engine the CLI uses, so it can never disagree with
`python3 cli.py check`. It has four views:

- **Overview** — headline counts, and every expectation that did not hold
- **Case detail** — the note explaining what the experiment is for, its findings, a chart where
  one helps, and every answer with its probability distribution
- **Analysis** — the charts that answer a question on their own, pulled out of their cases
- **Raw responses** — every stored answer, filterable, with the JSON behind each one

`cli.py serve` adds what a static page cannot do: a **playground** for asking Jev your own
questions, and a per-case **re-run** button. The key is read from the environment on the server
side and never reaches the browser; the server binds to loopback only and refuses cross-origin
requests. Rebuild the static data after collecting new trials with `python3 cli.py build-ui`.

The charts follow a validated palette — three categorical hues checked for colour-vision
separation in both light and dark mode. Light-mode aqua sits below 3:1 against the surface, so
every chart that uses it also carries direct labels and a table view rather than relying on
colour alone.

## Why collection and analysis are separate

`collect` writes every response to `results/raw.jsonl` next to the exact request that produced
it. `check` reads that file and never calls the API. Thresholds get revised - that is the normal
course of this kind of work - and re-deriving them should cost nothing and produce identical
inputs each time. Re-running `collect` skips trials already stored, so an interrupted run
resumes instead of restarting.

## What a case file looks like

A case is one experiment: a question set (or several named variants of it), a list of states,
per-state expectations, and relations that must hold across states or variants.

```json
{
  "id": "s2_monotonic_ladder",
  "group": "score",
  "questions": {
    "report_actionability": {"type": "score", "instructions": "...", "criteria": ["...", "..."]}
  },
  "states": [
    {"id": "l0_broken", "state": "It's broken.", "expect": {"report_actionability": {"near": 0.0, "tol": 0.2}}}
  ],
  "relations": [
    {"type": "rank", "question": "report_actionability", "order": ["l0_broken", "l1_export"]}
  ]
}
```

Relations carry most of the weight. A single absolute threshold on one probabilistic answer is
brittle; an ordering over six states, a confidence gap between a clear and an ambiguous input,
or agreement across three phrasings of the same question is not.

Available relations: `rank`, `sum_to`, `mean_gap`, `dist_close`, `answers_match`, `variant_gap`,
`stability`, `calibration`, `threshold_sweep`, `confidence_auroc`, `variant_profile`,
`variant_agreement`, `state_agreement`, `batching_gain`, `latency_profile`, `expect_error`,
`report`.

### Choosing a Noul cutoff

A Noul returns a probability and leaves the yes/no to your code. `threshold_sweep` turns that
choice into a table rather than a guess — give it labelled states and it reports, for every
candidate cutoff, what your code would have decided and how often it would have been right:

```json
{
  "type": "threshold_sweep",
  "question": "used_python_at_work",
  "labels": {"t_pipelines": true, "f_hobby": false, "...": false}
}
```

It reports two things. A **two-way table** — accuracy, precision, recall, false yes and missed
yes at each cutoff. And a **three-way band table**, which is usually the more useful one: below
the low edge an automatic no, at or above the high edge an automatic yes, everything between to
a person. What you optimise there is accuracy on the part you decide automatically, read next to
how much traffic you hand over.

It also prints the observed values, because a cutoff placed on top of the data flips on noise.
One recorded state in this suite returns 0.82 on nine repeats and 0.80 on the tenth: a
`noul > 0.80` test would disagree with itself once in ten calls, not because the model is
unstable but because the cutoff sits where the values are.

### Two kinds of expectation

An expectation carrying `"hypothesis": true` reports as a **warning** rather than a failure when
it misses. Expectations copied from the TypeSafe docs are facts the run must reproduce;
expectations the case author guessed are predictions under test, and the model contradicting one
is a result worth reading, not a defect. Keeping the two apart is what stops the suite from
being quietly tuned until everything passes.

### Useful fields

- `variants` - several question sets over the same states; trials are the cross product
- `alias` - maps a variant's option names back to canonical ones, so a variant that renames or
  translates its options stays comparable
- `split: true` - issue one request per question instead of one request for all of them
- `sequential: true` - run this case's trials serially, for experiments that measure wall-clock
- `expect_error: true` - the API is expected to reject this shape; the rejection is the result
- `only_variants` - restrict a state to certain variants
- keys outside `type` / `instructions` / `criteria` are stripped from questions before sending,
  so case files can be annotated without the annotations reaching the model

## Layout

```
cli.py                  plan / collect / check / report / build-ui / serve
serve.py                local server: static UI plus the live API endpoints
docs/                   the web UI (also what GitHub Pages serves)
uidata.py               builds docs/data.json from the raw responses
report.py               HTML report
jev/client.py           Decisions API client, also usable standalone
jev/cases.py            case schema, validation, expansion into trials
jev/runner.py           execution, caching, resume, concurrency
eval/invariants.py      structural checks that need no ground truth
eval/metrics.py         spearman, brier, ECE, AUROC, TV distance, bootstrap
eval/checks.py          expectations and relation handlers
cases/                  the experiments, as data
scripts/                generators for cases with long option or question lists
results/raw.jsonl       every response, with its request
```

## Reproducing the committed run

`results/raw.jsonl` holds all 282 trials next to the exact request that produced each one.
`python3 cli.py check` reads that file and calls nothing, so the analysis can be re-run, the
thresholds revised, and the metrics rewritten without an API key and without spending anything.
`python3 cli.py collect` against a live key appends new trials beside the old ones.

## Layers

1. **Structural invariants** run over every response and need no ground truth: probabilities sum
   to one, `choice` is the argmax of its own distribution, `score` equals the probability-weighted
   mean of the level numbers, `legend` keys match the levels, Noul carries no confidence. A
   violation here makes every other number unsafe to read, so these run first.
2. **Capability** - reproduction of the values published in the docs, calibration against labelled
   states, monotonicity along ladders.
3. **Confidence** - whether confidence separates correct answers from incorrect ones, and whether
   it collapses where it should.
4. **Claimed properties** - self-consistency across repeats, independence between questions in one
   request, the published batching advantage, latency against question count, gateway fidelity.
