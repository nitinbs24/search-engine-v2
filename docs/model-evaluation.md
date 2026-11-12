# Text model evaluation (T9)

Corpus: 23 documents (18 txt, 5 pdf) and 9 images in `eval/corpus`, sources in `eval/SOURCES.md`.
Queries: 50 in `eval/queries.json` (41 text: 23 paraphrased questions, 10 keyword, 8 with a file-type or size filter; 9 image).
Metrics: recall@5 (fraction of expected objects in the top 5, objects deduplicated by key) and MRR, per kind.
Image queries score 0 on purpose: image indexing is not built yet, so they are reported separately and not hidden.

## Commands

For each row: set `text` in `workers/model-server/model_spec.json` (see below), then

    make up      # rebuilds model-server so the spec is baked in, restarts everything
    make eval    # uploads eval/corpus, waits for indexing, prints metrics, removes the corpus

All candidates have 384 dimensions, so the index was kept; the corpus is re-ingested by every `make eval`.
Checkpoint A baseline: the same `eval/run.py` against the stack as it was at commit 60ec2c6 (MiniLM, parser before T10),
run before any change.

## Results (text queries, n=41; image queries 0.000 for every row)

| Candidate | Spec | recall@5 | MRR |
|---|---|---|---|
| Checkpoint A: MiniLM, old parser | `all-MiniLM-L6-v2` | 0.951 | 0.927 |
| all-MiniLM-L6-v2 (22M) + T10 parser | `{"name":"sentence-transformers/all-MiniLM-L6-v2","dimension":384}` | 0.951 | 0.939 |
| BAAI/bge-small-en-v1.5 (33M) | `{"name":"BAAI/bge-small-en-v1.5","dimension":384,"query_prefix":"Represent this sentence for searching relevant passages: "}` | 0.951 | 0.939 |
| intfloat/e5-small-v2 (33M) | `{"name":"intfloat/e5-small-v2","dimension":384,"query_prefix":"query: ","passage_prefix":"passage: "}` | 0.951 | 0.927 |
| thenlper/gte-small (33M) | `{"name":"thenlper/gte-small","dimension":384}` | 0.951 | 0.939 |

The same two queries miss for every model ("music born in New Orleans built on syncopation", "Dockerfile container images").
The T10 parser fix (embedding the natural query) moves MiniLM MRR from 0.927 to 0.939.

## Decision

All four models are within noise of each other (one rank position on one query). The set is small and the hybrid
(BM25 + kNN, RRF) leg masks vector differences, so it cannot separate them. Per the rule "within noise: prefer the
smaller/faster model", `sentence-transformers/all-MiniLM-L6-v2` stays in `model_spec.json`.
The eval should get harder, more confusable documents before it is used to tune further.

`query_prefix` / `passage_prefix` are optional spec keys added to `TextEmbedder` so bge and e5 could be run fairly.
Image models were not evaluated (out of scope for this run).

## Note on the prefix rows

The bge and e5 rows were measured with a temporary patch that added `query_prefix` /
`passage_prefix` to the text model spec. The chosen model needs no prefixes, so that patch was
not kept. Re-measuring those two models means putting it back.

## 2026-11-12: integration run after merging T11–T14 (reranker, Redis cache, image leg)

Stack: `all-MiniLM-L6-v2` text, `clip-ViT-B-32` image, `cross-encoder/ms-marco-MiniLM-L-6-v2` reranker, CPU.
Same corpus and 50 queries as above. Unlike the Checkpoint A baseline (text 0.951 / 0.939, no reranker, no image hits),
results are now reranked, cut at `RERANK_MIN_SCORE` / `IMAGE_MIN_SCORE`, and image hits are interleaved after text hits.

| Run | text recall@5 | text MRR | image recall@5 | image MRR |
|---|---|---|---|---|
| Checkpoint A baseline (no reranker, images not indexed) | 0.951 | 0.939 | 0.000 | 0.000 |
| Merged, defaults rerank -2.0, image 0.24 | 0.537 | 0.524 | 1.000 | 1.000 |
| Merged, rerank -7.0, image 0.24 (new default), run 1 | 0.829 | 0.805 | 1.000 | 0.889 |
| Merged, rerank -7.0, image 0.24 (new default), run 2 | 0.854 | 0.829 | 1.000 | 0.889 |

Runs 1 and 2 differ only in "royal jelly hive queen" (missed in run 1, rank 1 in run 2 with the same logit -3.06);
the cause was not found.

Misses at -2.0 (text, rr=0): hive-dance bees, drug for infected wounds, movable-type press, aqueducts, molten rock,
1789 uprising, 26 mile footrace, tamper-proof ledger, packaging software, public/private key scheme, music born in
New Orleans, green pigment, RSA encryption, royal jelly hive queen, Dockerfile container images, pdf about music,
txt files about energy from the sun, pdf smaller than 6KB about tea, files larger than 13KB about bread.

Misses at -7.0 (run 2, rr=0): drug for infected wounds, molten rock, music born in New Orleans, Dockerfile container
images, txt files about energy from the sun, files larger than 13KB about bread. Not at rank 1: earth's rigid outer
shell, public/private key scheme (text); a red fruit, a group of dogs (image: a wrong text hit is placed first by the
text-first interleave).
"music born in New Orleans" and "Dockerfile container images" are the two misses from Checkpoint A: the parser turns
"music" and "images" into type filters.

### Cut-off evidence (cut-offs disabled: `RERANK_MIN_SCORE=-1000 IMAGE_MIN_SCORE=-10`, limit 21, one hit per object)

| Queries | Hits | Correct: n / min / median / max | Incorrect: n / min / median / max |
|---|---|---|---|
| text | text (rerank logit) | 40 / -11.05 / -0.83 / 9.11 | 405 / -11.49 / -11.18 / 5.24 |
| text | image (cosine) | 0 | 297 / 0.099 / 0.189 / 0.286 |
| image | text (rerank logit) | 0 | 108 / -11.36 / -11.06 / -4.08 |
| image | image (cosine) | 9 / 0.255 / 0.289 / 0.341 | 72 / -0.031 / 0.153 / 0.223 |

Rerank cut vs hits kept (text queries: correct of 40, incorrect of 405; image queries: text hits of 108):
-11: 39, 93, 40 · -10: 38, 18, 12 · -8: 35, 5, 5 · -7: 35, 2, 2 · -5: 30, 2, 1 · -4: 28, 2, 0 · -2: 22, 2, 0 · 0: 17, 2, 0.
Between -7 and -2 the same two incorrect text hits survive on text queries, while 13 more correct hits are kept;
the price is two wrong text hits on image queries, which the text-first interleave places above the right image
(image MRR 1.000 -> 0.889). With no rerank cut at all text recall@5 is 0.927 / MRR 0.880, still below the baseline:
the reranker reorders some queries worse than RRF (it scores only each file's best fused chunk, e.g. for
"molten rock bursting out of the ground" it scored volcano.txt -9.16 while another chunk of the same file scores -6.24).

Image cut-off 0.24 sits in a clean gap on image queries (correct >= 0.255, incorrect <= 0.223) and was kept.
The only image above 0.24 on a text query is 0.286 for the Himalaya question (a snowy mountain photo).
Rerank default changed from -2.0 to -7.0 in `backend/main.go`. The gap above the unrelated cluster is thin
(nearest incorrect -7.00, nearest correct -6.81) on a 41-query set, so treat -7.0 as provisional.

### Commands

    make test
    curl -XDELETE localhost:9200/search-docs   # old index lacked image_embedding; with only opensearch up
    make up
    make smoke
    make integration
    make eval                                   # defaults -2.0 / 0.24
    RERANK_MIN_SCORE=-1000 IMAGE_MIN_SCORE=-10 docker compose --env-file .env -f infrastructure/docker-compose.yml up -d --wait --no-deps backend
    python3 eval/run.py --keep                  # then scored every query at limit 21 and read the `score` field
    # edit defaultMinRerankScore to -7.0 in backend/main.go
    docker compose --env-file .env -f infrastructure/docker-compose.yml exec -T minio sh -c \
      'export MC_CONFIG_DIR=/tmp/.mc; mc alias set l http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc rm --recursive --force l/uploads/eval/'
    make test
    docker compose --env-file .env -f infrastructure/docker-compose.yml up -d --build --wait --no-deps backend
    make eval                                   # run 1
    python3 eval/run.py --keep                  # run 2, corpus removed afterwards with the mc command above

## 2026-11-12 — after the merge fixes (final Phase 2/3 numbers)

Three changes followed the integration run above, each with a unit test:

1. The reranker judges up to three chunks per file, not only the chunk that fused best; a file
   takes its best passage's score, and that passage becomes the snippet.
2. The reranker is given the query without filter phrases (`clean_query` from `ParseQuery`).
   "files larger than 13KB about bread" went from -10.5 to -6.1 for the right document.
3. Images lead the interleaved list when the best text hit scores under 0.

`make eval`, default cut-offs (-7.0 rerank, 0.24 image):

| Run | text recall@5 | text MRR | image recall@5 | image MRR |
|---|---|---|---|---|
| Checkpoint A baseline (no reranker, no cut-off, images not indexed) | 0.951 | 0.939 | 0 | 0 |
| After merge, cut-off -7.0 | 0.854 | 0.829 | 1.000 | 0.889 |
| After the three changes | 0.927 | 0.915 | 1.000 | 1.000 |

Text is one query (of 41) below the baseline. The three remaining misses:

- "the drug that saved soldiers from infected wounds": penicillin.txt ranks first but is cut.
  No chunk of the document mentions soldiers or wounds; its best chunk scores -7.36 against a
  cut-off of -7.0. The baseline kept it only because it never dropped anything.
- "music born in New Orleans built on syncopation" and "Dockerfile container images": missed in
  the baseline too. The parser reads "music" and "images" as file-type filters.

The -7.0 cut-off rests on 41 queries with a thin margin (nearest wrong hit -7.00, nearest right
hit -6.81 before these changes); treat it as provisional until the corpus has harder cases.
