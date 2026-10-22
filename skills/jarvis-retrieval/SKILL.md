---
name: jarvis-retrieval
description: Search the user's personal indexed corpus (local files, Drive, Notion) via the Jarvis MCP tools, and show the provenance behind the answer. Use for any question about their own documents, notes, files or previously saved content - including "find the actual document", "what did I write about X", and "what's the last/latest Y".
---

# Jarvis retrieval

Answer from the user's indexed corpus using the `jarvis` MCP tools, then show
the receipt.

## Pick the tool by question shape

| Question shape | Tool |
|---|---|
| "what do I know about X", topical / fuzzy | `search` |
| "find the actual document", a named title | `get_document` |
| "the last/latest X" | `query_db` to rank by date, then `get_document` |
| exact, structural, counting, "every X", earliest/latest by date | `query_db` |
| "what topics exist", browsing the tree | `get_hierarchy`, then `browse` |
| "more like this one" from a known chunk or topic | `related` |

## `search` alone is not enough for specific-document asks

`search` is chunked and semantic, so it surfaces *mentions* of a document
far more readily than the document itself - a task-list entry saying
"write the investor update" outranks the update. Whenever the ask names or
implies one particular document, also run `query_db` against
`document_sources.title` / `documents.normalized_markdown`, and prefer the
full document text over the chunk when they disagree.

```sql
SELECT ds.title, ds.source_modified_at, ds.connector, ds.source_url
FROM document_sources ds
JOIN documents d ON d.content_hash = ds.content_hash
WHERE ds.tombstoned = 0 AND ds.title LIKE '%<subject>%'
ORDER BY ds.source_modified_at DESC;
```

`chunk_context` joins document metadata and the topic breadcrumb in one
view - use it instead of re-deriving those joins.

## "Latest" means `source_modified_at`

`documents.first_seen_at` and `last_ingested_at` are Jarvis's own
ingestion timestamps. Everything ingested in one run shares the same
value, so ordering by them returns an arbitrary row - and it will look
like a plausible answer. The content date is
`document_sources.source_modified_at`.

```sql
SELECT ds.title, ds.source_modified_at, ds.source_url
FROM document_sources ds
JOIN documents d ON d.content_hash = ds.content_hash
WHERE ds.tombstoned = 0 AND lower(ds.title) LIKE '%investor update%'
ORDER BY ds.source_modified_at DESC
LIMIT 5;
```

## Read documents with `get_document`, never `query_db`

`query_db` truncates every cell, so selecting `normalized_markdown`
returns a silently cut-off document. Once you know which document you
want, call `get_document` with its `document_id` or `title` to get the
full text. If you catch yourself reporting that the database truncated
something, you used the wrong tool.

## Search efficiently

- One well-formed query beats several near-duplicates; each extra call
  adds long-tail noise to the receipt without adding evidence.
- Use `filters` (`topic`, `source`, `date_range`, `file_type`) rather than
  searching broadly and discarding results afterwards.
- Raise `limit` only when genuinely paging for more; the default of 10 is
  usually already past the useful hits.
- Don't re-derive the schema each session. It is stable; `query_db`'s own
  description carries the reference.

## Always finish with the receipt

After writing the answer, call `show_provenance` as the **last** action of
the turn. It takes no arguments and resets each time.

- Call it after the prose, never before - it is meant to be read after the
  answer, and calling it last is the only thing that puts it there.
- **Write nothing after it.** Do not summarise, reformat, quote or
  comment on its output, and do not add a closing remark. The receipt is
  the end of the turn; prose after it is exactly what this tool exists to
  replace.
- A `provenance_pending` field on a `search` / `query_db` result means the
  receipt is still owed for this turn.
- If no Jarvis tool was used, don't call it.

## Report what the corpus actually says

- If the corpus doesn't answer the question, say so instead of assembling
  something plausible from loosely related chunks.
- Distinguish "Jarvis has no document on this" from "the documents
  disagree" - the receipt makes both visible, so the prose should match it.
- When the best hits are weak, say the evidence is thin rather than
  presenting it with unearned confidence.
