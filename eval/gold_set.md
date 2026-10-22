# Retrieval accuracy gold set (J-14)

29 queries over the 17-document corpus in `eval/corpus/` (extracted
verbatim from `tests/test_pipeline.py`'s `corpus`, `mixed_types`,
`mixed`, `team_a`/`team_b`, and `topical_corpus` fixtures). Background
and results: `docs/J14_ACCURACY_EVAL.md`. Harness: `eval/run_eval.py`.

`wide_corpus` is excluded from the corpus and this gold set - it's four
sentences repeated with "Note {i}" appended, built for clustering-count
purposes, not distinct content worth querying.

Collision-watch pairs (expected legitimate hard-case failures, not bugs
if they miss): `espresso.md`/`coffee_a.md`/`coffee_b.md` (#6, #19),
`bio_a.md`/`bio_b.md` (#26, #27, #29), the onboarding trio
`readme.md`/`handbook.md`/`changelog.txt` (#7, #10-12).

| # | Query | Correct source | Type | Note |
|---|---|---|---|---|
| 1 | What is the Parthenon a temple to? | greece.md | Easy | shares "Parthenon," "temple" |
| 2 | When was the ancient temple on the hill above Athens finished? | greece.md | Hard | avoids "Parthenon," "Acropolis," "completed" |
| 3 | How long is a blue whale? | whales.txt | Easy | shares "blue whale" |
| 4 | What's the biggest creature that has ever lived? | whales.txt | Hard | avoids "whale" entirely |
| 5 | How much coffee is used for a double shot of espresso? | espresso.md | Easy | shares "double shot," "espresso," "coffee" |
| 6 | What's a typical dose of grounds for a strong short coffee drink? | espresso.md | Hard | collision risk: BM25 may favor coffee_a.md (2x "coffee"); relies on vector+RRF. Legitimate hard case, not a broken query |
| 7 | Where should a new hire start? | readme.md | Hard | anchors on "start," which handbook.md/changelog.txt don't contain |
| 8 | Which team is Priya Nair on? | contacts.csv | Easy | shares "Priya Nair" |
| 9 | What was completed in release 2.4? | release.html | Easy | shares "release," "2.4," "completed" |
| 10 | Where can I find the onboarding checklist and company policies? | handbook.md | Easy | shares "onboarding checklist," "policy" |
| 11 | Where did the onboarding checklist move to? | changelog.txt | Easy | shares "onboarding checklist," "moved" |
| 12 | What happened to the getting-started checklist? | changelog.txt | Hard | avoids "handbook" (was misdirecting), "onboarding," "moved," "wiki" |
| 13 | What does an aardvark eat, and when? | aardvark.md | Easy | shares "aardvark" (query doesn't share "termites"/"night" verbatim) |
| 14 | Which nocturnal animal digs for insects in mounds? | aardvark.md | Hard | avoids "aardvark," "termites," "night," "forages" |
| 15 | What is the coldest continent? | antarctica.md | Easy | near-verbatim match, softest "easy" in the set |
| 16 | Who leads a troop of gorillas? | gorilla.md | Easy | shares "gorillas," "troop," "led" |
| 17 | What's the social structure of the largest great apes? | gorilla.md | Hard | avoids "gorilla," "troop," "silverback" |
| 18 | What sets the flavour balance of espresso? | coffee_a.md | Easy | shares "espresso," "flavour balance" |
| 19 | Why do two espressos from the same beans taste different? | coffee_a.md | Hard | targets extraction yield -> flavour balance directly; collision risk vs coffee_b.md (and espresso.md) |
| 20 | How does roasting coffee beans darker change the taste? | coffee_b.md | Easy | shares "roasting," "darker" ("bitter" is the answer, not shared in the query) |
| 21 | Why does longer time over heat make beans taste harsher? | coffee_b.md | Hard | avoids "roast," "bitter," "coffee," "Maillard" |
| 22 | How does SQLite's write-ahead logging help readers and writers? | sqlite_a.md | Easy | shares "SQLite," "write-ahead logging" |
| 23 | How can one process update a database file while others keep reading, without stalling? | sqlite_a.md | Hard | avoids "SQLite," "WAL," "write-ahead" |
| 24 | What does a SQLite B-tree index let the query planner avoid? | sqlite_b.md | Easy | shares "SQLite," "B-tree index," "planner" |
| 25 | What data structure speeds up lookups so the database skips checking every row? | sqlite_b.md | Hard | avoids "SQLite," "index," "table scan" |
| 26 | What does photosynthesis produce from carbon dioxide and water? | bio_a.md | Easy | shares "photosynthesis," "carbon dioxide," "water"; collision risk vs bio_b.md - both lead with "Photosynthesis" |
| 27 | How do plants turn sunlight into food and release a breathable gas? | bio_a.md | Hard | "food"->glucose, "breathable gas"->oxygen distinguish from bio_b; collision risk, weak embedder may grab bio_b |
| 28 | What do the light reactions in chlorophyll drive? | bio_b.md | Easy | shares "light reactions," "chlorophyll" |
| 29 | How does the green pigment in leaves turn captured light into a chemical fuel the cell can use? | bio_b.md | Hard | avoids "chlorophyll," "light reactions," "ATP"; collision risk vs bio_a.md |

Split: 16 easy / 13 hard, across all 17 documents, none skipped.
