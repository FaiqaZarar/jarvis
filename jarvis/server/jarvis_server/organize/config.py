"""Tunable constants for the organize engine (J-08).

Kept in one place, not scattered inline, because spec §6 is explicit that
these are "prototype-tuned, not guessed" - they are expected to be
adjusted after seeing the engine run against the real corpus (see the
J-08 design doc's benchmark: if >~40-50% of chunks land in `Unfiled` on
real data, lower `HDBSCAN_MIN_SAMPLES` / `HDBSCAN_MIN_CLUSTER_SIZE`
before proceeding). Starting values are from the design doc's §1 table,
approved pre-build.
"""

# ---------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------

# UMAP is stochastic. A pinned seed is NOT a nice-to-have here: an
# unpinned UMAP would produce a slightly different low-dim embedding on
# every run, HDBSCAN would then draw slightly different cluster
# boundaries, and J-09's reconciliation would read those boundary
# wobbles as real split / merge / reparent events - manufacturing
# hierarchy churn out of pure RNG noise. The determinism test in
# tests/test_clustering.py guards this.
RANDOM_SEED = 20260828

# ---------------------------------------------------------------------
# UMAP dimensionality reduction (design doc §1)
# ---------------------------------------------------------------------

UMAP_N_COMPONENTS = 5      # reduce for clustering only; full embeddings stay in the vector index
UMAP_N_NEIGHBORS = 15      # start; raise as the corpus grows toward broader themes
UMAP_MIN_DIST = 0.0        # pack points tightly for density clustering (not the viz default)
UMAP_METRIC = "cosine"     # matches embedding similarity

# ---------------------------------------------------------------------
# HDBSCAN base clustering (design doc §1)
# ---------------------------------------------------------------------

# Range in the design doc is 3-5 / 1-3; starting at the low end because
# the MVP corpus is small and the cost of being too coarse (everything
# in one blob) is worse here than being slightly too fine.
HDBSCAN_MIN_CLUSTER_SIZE = 3
HDBSCAN_MIN_SAMPLES = 1              # kept below min_cluster_size so a small corpus isn't dumped into noise
HDBSCAN_CLUSTER_SELECTION_METHOD = "leaf"   # more, smaller, homogeneous clusters -> finer tree

# ---------------------------------------------------------------------
# k-means fallback (design doc "what I did not stop on": built, not
# separately tuned until real data actually triggers it)
# ---------------------------------------------------------------------

# Triggered only when HDBSCAN returns a degenerate result (everything
# noise, or fewer than this many real clusters).
KMEANS_FALLBACK_MIN_CLUSTERS = 2

# k when the fallback fires: a plain heuristic, deliberately un-tuned.
KMEANS_K_DIVISOR = 4       # k ~= n_chunks / this
KMEANS_K_MIN = 2
KMEANS_K_MAX = 12

# ---------------------------------------------------------------------
# Placement (root-to-leaf descent, deepest-confident node) - spec §6
# ---------------------------------------------------------------------

# Cosine similarity a chunk must have with a node's centroid to be
# placed *at* that node (vs. stopping shallower / going to Unfiled).
# Deliberately conservative: the design doc says favour Unfiled over a
# false-confident deep placement. One value for all levels for now;
# per-level tuning is a later refinement.
PLACEMENT_SIMILARITY_THRESHOLD = 0.45

# NOTE: the tree-shape guard-rails (depth cap ~4-5 levels, minimum branch
# fan-out) are spec-FIXED, not tunable - they are hardcoded directly in
# clustering.py (MAX_TREE_DEPTH / MIN_BRANCH_FAN_OUT there), deliberately
# not here, so this file stays "things we expect to tune".

# ---------------------------------------------------------------------
# LLM labelling (design doc §3)
# ---------------------------------------------------------------------

# How many representative member chunks (nearest to the cluster centroid)
# to show the LLM when titling a cluster - bounded so the prompt doesn't
# grow with cluster size.
LABEL_SAMPLE_SIZE = 5

# Chars of each sampled chunk to include in the labelling prompt.
LABEL_SAMPLE_CHARS = 400

# ---------------------------------------------------------------------
# Reserved Unfiled topic - spec §3/§6 (first-class, always exists,
# fully searchable, never matched/renamed by reconciliation)
# ---------------------------------------------------------------------

# Fixed, valid UUID (the "0f11ed" head is just a mnemonic for "unfiled").
# Stable across every run so reconciliation (J-09) can special-case it by
# id and never treat it as a born/tombstoned/renamed node.
UNFILED_TOPIC_ID = "0f11ed00-0000-4000-8000-000000000000"
UNFILED_TOPIC_NAME = "Unfiled"
UNFILED_TOPIC_DESCRIPTION = "Outlier chunks not confidently placed under any topic. Fully searchable."

# ---------------------------------------------------------------------
# Synthetic hierarchy root (J-09). The organize engine can emit a forest
# (several top-level clusters that never rolled up further); the spec
# requires a strict single-parent *tree*, and the staged-build validator
# requires every non-reserved topic to have a parent. Reconciliation
# parents every organize root - and Unfiled - under this one fixed,
# reserved node so there is always exactly one true root. Excluded from
# matching by its id, exactly like Unfiled: never born/renamed/tombstoned.
ROOT_TOPIC_ID = "40000000-0000-4000-8000-000000000000"
ROOT_TOPIC_NAME = "Knowledge Base"
ROOT_TOPIC_DESCRIPTION = "The root of the topic hierarchy. Every topic descends from here."

# ---------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------

# The design doc calls for tracking the Unfiled rate every run. Log
# a WARNING (not just INFO) once it crosses this, as the "lower the
# HDBSCAN params before proceeding" signal.
UNFILED_PCT_WARN_THRESHOLD = 40.0


# The knobs that actually change how clustering behaves, in one list, so
# the debug UI's playground can offer exactly these and a trace can record
# what they were set to. Deliberately excludes the reconcile constants
# (they run after clustering) and the reserved-topic identifiers.
TUNABLE_NAMES = (
    "RANDOM_SEED",
    "UMAP_N_COMPONENTS",
    "UMAP_N_NEIGHBORS",
    "UMAP_MIN_DIST",
    "UMAP_METRIC",
    "HDBSCAN_MIN_CLUSTER_SIZE",
    "HDBSCAN_MIN_SAMPLES",
    "HDBSCAN_CLUSTER_SELECTION_METHOD",
    "KMEANS_FALLBACK_MIN_CLUSTERS",
    "KMEANS_K_DIVISOR",
    "KMEANS_K_MIN",
    "KMEANS_K_MAX",
    "PLACEMENT_SIMILARITY_THRESHOLD",
    "LABEL_SAMPLE_SIZE",
    "LABEL_SAMPLE_CHARS",
)


def snapshot() -> dict:
    """Current value of every tunable in TUNABLE_NAMES."""
    return {name: globals()[name] for name in TUNABLE_NAMES}

# ---------------------------------------------------------------------
# Reconciliation - matching new clusters to existing Topics (J-09).
# spec §6: "prototype-tuned, not guessed". These starting values are
# from the J-09 design doc (docs/J09_DESIGN.md §2) and are expected to be
# retuned against the real corpus plus a real second re-sync - a unit
# test can't validate them, only real before/after data can.
# ---------------------------------------------------------------------

# Blended match score = w * centroid_cosine + (1 - w) * membership_jaccard.
# Centroid is favoured slightly: it is robust to a handful of chunks
# moving between syncs, where Jaccard swings more. Jaccard is the
# tie-breaker and the split/merge cardinality signal.
RECONCILE_CENTROID_WEIGHT = 0.6

# A new<->old pair is a candidate match only if its blended score is at
# or above this. One global value to start (spec writes tau singular);
# revisit per-level only if deep nodes systematically fail to match.
RECONCILE_MATCH_TAU = 0.5

# Re-label discipline (spec §6): a carried-forward node is only re-labelled
# if it "materially changed". Below - the two drift thresholds that count
# as material for a 1:1 matched node (born / split / merged always count).
RELABEL_JACCARD_DROP = 0.5      # member set Jaccard vs the node's own prior self below this
RELABEL_CENTROID_DRIFT = 0.15   # (1 - cosine) between new and prior centroid above this
