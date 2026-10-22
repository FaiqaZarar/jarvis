"""Jarvis retrieval-accuracy eval harness (J-14).

    python scripts/run_eval.py          # needs .venv active + EMBEDDING_API_KEY

What it does:
  1. Builds a fresh temp folder with the 17-document merged gold-set corpus
     (exact text from tests/test_pipeline.py's fixtures).
  2. Ingests it into a brand-new SQLite DB via run_filesystem_ingest, using
     the REAL OpenRouterVoyageEmbedder (organize_hierarchy=False - retrieval
     eval only, no topic graph needed).
  3. For each of the 29 gold-set queries, calls the same internal ranking
     functions _search_impl uses (_candidate_chunk_ids / _fts5_ranked_ids /
     _vector_ranked_ids / _rrf_score) directly, so BM25-only, vector-only,
     and fused sub-ranks are all visible per query - not just the fused top-1.
  4. Maps chunk_id -> document title, prints per-query diagnostics, and rolls
     up Precision@1, Recall@3, Recall@5, MRR.

Findings from the first run: docs/J14_ACCURACY_EVAL.md.

Determinism: RRF can produce exact score ties (see issue #82); run with
PYTHONHASHSEED set for a reproducible tiebreak until that's fixed.

Cost: ~30 embedding API calls on the first run against a given DB file
(1 batch of 17 corpus chunks + 29 query embeddings). Corpus embeddings are
cache-checked; re-running against the same DB only re-embeds the queries.
"""

import sys
import tempfile
from pathlib import Path

# `jarvis` is not pip-installed and this script lives in scripts/, so the
# repo root is not on sys.path when run as `python scripts/run_eval.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------
# 1. The merged gold-set corpus - exact text from tests/test_pipeline.py
# ---------------------------------------------------------------------

CORPUS_FILES = {
    "greece.md": (
        "# Athens\n\nThe Parthenon sits on the Acropolis of Athens, "
        "completed around 438 BC as a temple to Athena.\n"
    ),
    "whales.txt": (
        "The blue whale is the largest animal known to have existed, "
        "reaching about 30 metres in length.\n"
    ),
    "espresso.md": (
        "# Espresso\n\nA double shot of espresso is pulled from roughly "
        "18 grams of finely ground coffee.\n"
    ),
    "readme.md": "# Onboarding\n\nStart with the runbook.\n",
    "contacts.csv": "name,team\nPriya Nair,platform\nMarco Rossi,billing\n",
    "release.html": "<h1>Release 2.4</h1><p>The <b>quokka</b> migration completed.</p>",
    "handbook.md": "# Handbook\n\nonboarding checklist and policy links\n",
    "changelog.txt": "onboarding checklist moved to the wiki\n",
    "aardvark.md": "# Aardvark\n\nThe aardvark forages for termites at night.\n",
    "antarctica.md": "# Antarctica\n\nAntarctica is the coldest continent.\n",
    "gorilla.md": "# Gorilla\n\nGorillas live in troops led by a silverback.\n",
    "coffee_a.md": (
        "# Espresso\n\nEspresso forces hot water through ground coffee under pressure; "
        "coffee extraction yield sets the flavour balance.\n"
    ),
    "coffee_b.md": (
        "# Roasting\n\nRoasting coffee beans drives Maillard reactions; a darker coffee "
        "roast tastes more bitter than a light coffee roast.\n"
    ),
    "sqlite_a.md": (
        "# SQLite WAL\n\nSQLite write-ahead logging lets one SQLite writer and many "
        "SQLite readers proceed without blocking.\n"
    ),
    "sqlite_b.md": (
        "# SQLite Indexes\n\nA SQLite B-tree index lets the SQLite planner skip a full "
        "table scan at the cost of extra SQLite write work.\n"
    ),
    "bio_a.md": (
        "# Photosynthesis\n\nPhotosynthesis turns carbon dioxide and water into glucose "
        "using light; photosynthesis releases oxygen.\n"
    ),
    "bio_b.md": (
        "# Light Reactions\n\nThe photosynthesis light reactions in chlorophyll drive "
        "electron transport; photosynthesis stores energy as ATP.\n"
    ),
}

# ---------------------------------------------------------------------
# 2. Gold-set queries: (query, correct_doc, type)
# ---------------------------------------------------------------------

GOLD_SET = [
    ("What is the Parthenon a temple to?", "greece.md", "easy"),
    ("When was the ancient temple on the hill above Athens finished?", "greece.md", "hard"),
    ("How long is a blue whale?", "whales.txt", "easy"),
    ("What's the biggest creature that has ever lived?", "whales.txt", "hard"),
    ("How much coffee is used for a double shot of espresso?", "espresso.md", "easy"),
    ("What's a typical dose of grounds for a strong short coffee drink?", "espresso.md", "hard"),
    ("Where should a new hire start?", "readme.md", "hard"),
    ("Which team is Priya Nair on?", "contacts.csv", "easy"),
    ("What was completed in release 2.4?", "release.html", "easy"),
    ("Where can I find the onboarding checklist and company policies?", "handbook.md", "easy"),
    ("Where did the onboarding checklist move to?", "changelog.txt", "easy"),
    ("What happened to the getting-started checklist?", "changelog.txt", "hard"),
    ("What does an aardvark eat, and when?", "aardvark.md", "easy"),
    ("Which nocturnal animal digs for insects in mounds?", "aardvark.md", "hard"),
    ("What is the coldest continent?", "antarctica.md", "easy"),
    ("Who leads a troop of gorillas?", "gorilla.md", "easy"),
    ("What's the social structure of the largest great apes?", "gorilla.md", "hard"),
    ("What sets the flavour balance of espresso?", "coffee_a.md", "easy"),
    ("Why do two espressos from the same beans taste different?", "coffee_a.md", "hard"),
    ("How does roasting coffee beans darker change the taste?", "coffee_b.md", "easy"),
    ("Why does longer time over heat make beans taste harsher?", "coffee_b.md", "hard"),
    ("How does SQLite's write-ahead logging help readers and writers?", "sqlite_a.md", "easy"),
    ("How can one process update a database file while others keep reading, without stalling?", "sqlite_a.md", "hard"),
    ("What does a SQLite B-tree index let the query planner avoid?", "sqlite_b.md", "easy"),
    ("What data structure speeds up lookups so the database skips checking every row?", "sqlite_b.md", "hard"),
    ("What does photosynthesis produce from carbon dioxide and water?", "bio_a.md", "easy"),
    ("How do plants turn sunlight into food and release a breathable gas?", "bio_a.md", "hard"),
    ("What do the light reactions in chlorophyll drive?", "bio_b.md", "easy"),
    ("How does the green pigment in leaves turn captured light into a chemical fuel the cell can use?", "bio_b.md", "hard"),
]

assert len(GOLD_SET) == 29, f"expected 29 queries, got {len(GOLD_SET)}"


def build_corpus_dir(tmp_root: Path) -> Path:
    corpus_dir = tmp_root / "eval_corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    for fname, content in CORPUS_FILES.items():
        (corpus_dir / fname).write_text(content, encoding="utf-8")
    return corpus_dir


def main():
    try:
        from jarvis_server.internal.db import db
        from jarvis_server.internal.embed.embed import OpenRouterVoyageEmbedder
        from jarvis.migrate import run_migrations
        from jarvis_server.pipeline import run_filesystem_ingest
    except ImportError as e:
        print(f"FAILED to import jarvis modules: {e}")
        print("Run with the repo's .venv active.")
        sys.exit(1)

    try:
        from jarvis_server.mcp_tools.search import (
            RANKING_POOL_SIZE,
            _candidate_chunk_ids,
            _fts5_ranked_ids,
            _rrf_score,
            _vector_ranked_ids,
        )
    except ImportError as e:
        print(f"FAILED to import internal ranking functions: {e}")
        print("Name/signature drift in jarvis_server.mcp_tools.search since this")
        print("harness was written. Available names:")
        import jarvis_server.mcp_tools.search as search_mod
        for name in dir(search_mod):
            if not name.startswith("__"):
                print(f"  {name}")
        sys.exit(1)

    tmp_root = Path(tempfile.mkdtemp(prefix="jarvis_eval_"))
    corpus_dir = build_corpus_dir(tmp_root)
    db_path = tmp_root / "eval.db"

    print(f"Corpus dir: {corpus_dir}")
    print(f"DB path:    {db_path}")
    print(f"Files:      {len(CORPUS_FILES)}")
    print()

    run_migrations(db_path)
    conn = db.get_connection(db_path)
    embedder = OpenRouterVoyageEmbedder()

    print("Ingesting corpus with real embeddings (one-time cost)...")
    result = run_filesystem_ingest(
        conn, str(corpus_dir), embedder=embedder, organize_hierarchy=False
    )
    print(f"  pulled={result['sync']['pulled']} errors={result['sync']['errors']} "
          f"chunks_created={result['chunks_created']} "
          f"newly_embedded={result['embed']['newly_embedded']} "
          f"api_calls={result['embed']['api_calls_made']}")
    if result["sync"]["errors"] > 0:
        print("WARNING: sync errors during ingest - check output above before trusting results.")
    print()

    chunk_to_title = {}
    for row in conn.execute(
        "SELECT c.chunk_id, ds.title FROM chunks c "
        "JOIN document_sources ds ON c.content_hash = ds.content_hash "
        "WHERE ds.tombstoned = 0"
    ):
        chunk_to_title[row["chunk_id"]] = row["title"]

    n_docs_found = len(set(chunk_to_title.values()))
    if n_docs_found != len(CORPUS_FILES):
        print(f"WARNING: expected {len(CORPUS_FILES)} distinct doc titles, found {n_docs_found}.")
        print("Titles found:", sorted(set(chunk_to_title.values())))
        print()

    def doc_rank_map(ranked: dict) -> dict:
        """chunk_id->rank dict -> doc_title->best(lowest) rank dict."""
        out = {}
        for chunk_id, rank in ranked.items():
            title = chunk_to_title.get(chunk_id)
            if title is None:
                continue
            if title not in out or rank < out[title]:
                out[title] = rank
        return out

    rows = []
    for query, correct_doc, qtype in GOLD_SET:
        cand = _candidate_chunk_ids(conn, None)
        fts = _fts5_ranked_ids(conn, query, cand, RANKING_POOL_SIZE)
        qvec = embedder.embed([query])[0]
        vec = _vector_ranked_ids(conn, qvec, cand, RANKING_POOL_SIZE)

        all_ids = set(fts.keys()) | set(vec.keys())
        fused_scores = {cid: _rrf_score(fts.get(cid), vec.get(cid)) for cid in all_ids}
        fused_ranked = sorted(fused_scores.items(), key=lambda p: -p[1])
        fused_rank_by_id = {cid: i + 1 for i, (cid, _s) in enumerate(fused_ranked)}

        bm25_by_doc = doc_rank_map(fts)
        vec_by_doc = doc_rank_map(vec)
        fused_by_doc = doc_rank_map(fused_rank_by_id)

        top_doc = None
        top_doc_subranks = None
        if fused_ranked:
            top_doc = chunk_to_title.get(fused_ranked[0][0], "?")
            top_doc_subranks = (bm25_by_doc.get(top_doc), vec_by_doc.get(top_doc))

        fused_rank = fused_by_doc.get(correct_doc)
        rows.append({
            "query": query, "correct_doc": correct_doc, "type": qtype,
            "bm25_rank": bm25_by_doc.get(correct_doc),
            "vector_rank": vec_by_doc.get(correct_doc),
            "fused_rank": fused_rank,
            "top_fused_doc": top_doc,
            "top_fused_subranks": top_doc_subranks,
            "hit_at_1": fused_rank == 1,
            "hit_at_3": fused_rank is not None and fused_rank <= 3,
            "hit_at_5": fused_rank is not None and fused_rank <= 5,
        })

    print(f"{'#':<3} {'type':<5} {'bm25':<5} {'vec':<5} {'fused':<6} {'@1':<3} {'top_fused_doc':<15} query")
    print("-" * 110)
    for i, r in enumerate(rows, start=1):
        print(f"{i:<3} {r['type']:<5} "
              f"{str(r['bm25_rank']):<5} {str(r['vector_rank']):<5} {str(r['fused_rank']):<6} "
              f"{'Y' if r['hit_at_1'] else 'N':<3} "
              f"{(r['top_fused_doc'] or '?')[:15]:<15} {r['query'][:60]}")

    n = len(rows)
    p1 = sum(r["hit_at_1"] for r in rows) / n
    r3 = sum(r["hit_at_3"] for r in rows) / n
    r5 = sum(r["hit_at_5"] for r in rows) / n
    mrr = sum((1.0 / r["fused_rank"]) if r["fused_rank"] else 0.0 for r in rows) / n
    easy = [r for r in rows if r["type"] == "easy"]
    hard = [r for r in rows if r["type"] == "hard"]

    print()
    print("=" * 50)
    print("ROLLUP METRICS")
    print("=" * 50)
    print(f"Precision@1 (overall): {p1:.3f}  ({sum(r['hit_at_1'] for r in rows)}/{n})")
    print(f"  Precision@1 (easy):  {sum(r['hit_at_1'] for r in easy) / len(easy):.3f}  ({sum(r['hit_at_1'] for r in easy)}/{len(easy)})")
    print(f"  Precision@1 (hard):  {sum(r['hit_at_1'] for r in hard) / len(hard):.3f}  ({sum(r['hit_at_1'] for r in hard)}/{len(hard)})")
    print(f"Recall@3:  {r3:.3f}")
    print(f"Recall@5:  {r5:.3f}")
    print(f"MRR:       {mrr:.3f}")
    print()

    misses = [r for r in rows if not r["hit_at_1"]]
    if misses:
        print(f"MISSES ({len(misses)} of {n}), for collision-watch diagnosis:")
        for r in misses:
            ws = r["top_fused_subranks"] or (None, None)
            print(f'  [{r["type"]}] "{r["query"][:60]}"')
            print(f"      expected {r['correct_doc']:<15} bm25={r['bm25_rank']} vec={r['vector_rank']} fused={r['fused_rank']}")
            print(f"      got      {str(r['top_fused_doc']):<15} bm25={ws[0]} vec={ws[1]} fused=1")

    conn.close()
    print()
    print(f"(Corpus + DB left at {tmp_root} for inspection.)")


if __name__ == "__main__":
    main()
