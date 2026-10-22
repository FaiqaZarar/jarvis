# J-14 — Retrieval accuracy evaluation: findings

**Status:** first eval run complete (2026-09-08). Closes the
"accuracy evaluation" strand of J-14. Two follow-ups fall out of it:
one bug filed (#82), one tuning decision for the maintainers (below).

**Ticket:** J-14. Spec source of truth: `mvp-spec.md` §7.1 (hybrid
BM25 + vector search, RRF fusion).

**Harness:** `scripts/run_eval.py` — reproducible, one command from the
repo root. Uses the same internal ranking functions `_search_impl` calls
(`_candidate_chunk_ids` / `_fts5_ranked_ids` / `_vector_ranked_ids` /
`_rrf_score`) so it measures the real pipeline, and logs BM25-only,
vector-only, and fused sub-ranks per query, not just the fused top-1.

---

## 1. Setup

- **Corpus:** 17 documents, the union of every distinct file across
  `tests/test_pipeline.py`'s fixtures (`corpus`, `mixed_types`, `mixed`,
  `team_a`/`team_b`, `topical_corpus`). `wide_corpus` excluded — it's four
  sentences repeated for clustering-count purposes, not distinct content.
- **Gold set:** 29 queries, one relevant document each, hand-written to
  cover all 17 docs. 16 "easy" (share surface vocabulary with the target),
  13 "hard" (deliberately avoid the target's distinctive words, forcing
  the vector side to carry the retrieval).
- **Embeddings:** real `OpenRouterVoyageEmbedder` (`voyage-4`, 1024-dim)
  against a fresh ingest. The `StubEmbedder` used in unit tests is a
  character-sum hash with no semantic content — every hard query would
  score meaninglessly under it.
- **Note on the hybrid:** `_sanitize_fts5_query` drops stopwords
  (including question words: how / why / what / …) and sub-3-char terms,
  then ORs the survivors. Several hard queries are full sentences whose
  content words don't appear in the target at all, so on those the BM25
  side contributes little or nothing and retrieval is effectively
  vector-only. That's expected, not a defect — worth stating plainly
  rather than hiding.

---

## 2. Results

| Metric | Value | |
|---|---|---|
| **Precision@1 (overall)** | **0.793** | 23 / 29 |
| Precision@1 (easy) | 1.000 | 16 / 16 |
| Precision@1 (hard) | 0.538 | 7 / 13 |
| **Recall@3** | **1.000** | correct doc is always in the top 3 |
| Recall@5 | 1.000 | |
| MRR | 0.897 | |

**The easy set is perfect. Every one of the 6 misses is the correct
document sitting at fused rank 2** — nothing is actually lost, and
Recall@3 is 100%. That framing matters for §4: this is a "how should
fusion weight a thin keyword match" question, not "the retriever can't
find the document."

### The 6 misses

| # | Query | Correct (bm25/vec/fused) | Returned (bm25/vec) | Cause |
|---|---|---|---|---|
| 4 | biggest creature that has ever lived | whales.txt (–/1/2) | gorilla.md (1/3) | Porter stem: "lived" → "live", present in `gorilla.md` ("Gorillas **live** in troops"). whales.txt says "existed". |
| 14 | nocturnal animal digs for insects in mounds | aardvark.md (–/1/2) | whales.txt (1/4) | spurious stemmed 1-term BM25 hit on whales.txt |
| 17 | social structure of the largest great apes | gorilla.md (–/1/2) | whales.txt (1/2) | whales.txt keyword-matches "largest"; gorilla.md has zero keyword overlap |
| 6 | typical dose of grounds for a strong short coffee drink | espresso.md (2/1/2) | coffee_a.md (1/2) | **exact RRF tie** — see #82 |
| 19 | why do two espressos from the same beans taste different | coffee_a.md (3/1/2) | coffee_b.md (1/2) | `coffee_b.md` owns "beans" + "taste" on BM25; near-tie on fused. Known collision zone; the "same beans" framing is genuinely ambiguous between roast (`coffee_b`) and extraction (`coffee_a`). |
| 29 | green pigment in leaves turn captured light into a chemical fuel | bio_b.md (2/2/2) | bio_a.md (1/1) | **only miss where the vector side also picked wrong.** Genuine `bio_a`/`bio_b` semantic collision — the hardest query in the set, arguably a fair miss. |

Reworked queries from gold-set review all behaved as predicted: #7
("Where should a new hire start?") and #12 ("What happened to the
getting-started checklist?") both hit; #19 misses to its collision
partner rather than to a random doc.

---

## 3. Finding 1 — bug: RRF tiebreak is nondeterministic (filed as #82)

`_rrf_score` is symmetric in its two inputs, so `(bm25 2, vec 1)` and
`(bm25 1, vec 2)` produce identical fused scores. `_search_impl` then
breaks the tie via `set`-iteration order over chunk-id strings —
non-deterministic across processes (`PYTHONHASHSEED`). **Two identical
queries against an unchanged index can return different top-1 results.**

Query #6 is a live instance: `espresso.md` and `coffee_a.md` score
bit-for-bit equal; which wins flips with the hash seed.

This is a determinism bug in the same class as the UMAP/HDBSCAN
nondeterminism fixed in #54 — not a ranking-quality question. Fix is a
deterministic secondary sort key (prefer lower vector rank, then
`chunk_id`). Tracked in **#82**, to be fixed independently of any tuning
decision here.

---

## 4. Finding 2 — tuning question for the maintainers: does a thin keyword match deserve equal fusion weight?

**Not a bug — a design tradeoff, so this is a maintainer decision, not something
to quietly tune.**

4 of the 6 misses (#4, #14, #17, and arguably #6) share one shape: a
**single** BM25 term — frequently an accidental one introduced by Porter
stemming ("lived" → "live") — matches an unrelated document, and RRF's
"appears in both ranked lists" credit lifts it over a document the vector
side correctly ranked #1. RRF with `k = 60` weights the two lists
equally; a lone spurious keyword hit currently counts as much as a strong
semantic match.

**Lead with this:** Recall@3 is 100%. The correct document is never lost,
only demoted to rank 2. So every fix option below is cheap and low-risk,
and none of them touches the vector side:

| Option | Effect | Cost |
|---|---|---|
| Raise RRF `k` (e.g. 60 → ~90–100) | flattens the rank-1-vs-rank-2 gap on each list, so a single list's #1 matters less relative to appearing in both | one constant |
| Min-BM25-score floor before a keyword hit contributes to fusion | drops accidental 1-term matches entirely; a doc must clear a relevance bar to count on the keyword side | one threshold + a BM25 score read (currently only rank is used) |
| Light re-rank of the fused top-3 | exact cosine (or a cross-encoder later) reorders just the head | more compute per query, but only on 3 candidates |
| Require ≥2 surviving query terms to match before BM25 contributes | kills the single-accidental-word case specifically | small change in `_fts5_ranked_ids` |

**Recommended starting point (to bring to the maintainers, not to apply
unilaterally):** raise `k` toward ~90–100 **or** add a min-BM25-score
floor. Both are one-liners. **Do not propose a specific number yet** —
the current gold set is 29 queries over 17 tiny documents; a `k` or
threshold tuned to it will overfit. Stress-test any candidate value
against a larger query set (target ~60–80 queries, and ideally a corpus
a few times bigger) before locking a default. `scripts/run_eval.py` is
built to re-run cheaply for exactly this (corpus embeddings are
cache-checked; only the query embeddings re-run).

---

## 5. Status

- Accuracy-evaluation strand of J-14: **done.** Baseline captured,
  harness committed, findings recorded.
- **#82** filed — determinism bug, fix independently.
- RRF weighting: **decision pending with the maintainers**, with a bigger-query-set
  stress test as the prerequisite for proposing a number.
- Not in scope here: the tuning pass itself, end-to-end test coverage
  beyond retrieval, docs refresh — the rest of J-14 (#14).
