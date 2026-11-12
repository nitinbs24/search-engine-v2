"""Picks the sentence of a passage that best answers the query. No models here: the scorer is passed in."""
import re

# A sentence ends at . ! ? followed by whitespace and a capital letter or digit, so decimals (3.5) and
# lowercase continuations ("e.g. the") do not split.
# shortcut: "Dr. Smith" and "U.S. Army" still split; add an abbreviation list if it shows in real results.
_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def split_sentences(text: str) -> list[str]:
    return [s for s in _BOUNDARY.split(text.strip()) if s]


def best_sentences(score, query: str, passages: list[str], passage_scores: list[float], top: int) -> list[str]:
    """One entry per passage: its best sentence, or "" for passages outside the `top` highest-scoring ones.
    All candidate sentences go to `score(query, sentences)` in a single call."""
    out = [""] * len(passages)
    candidates = []  # (passage index, sentence)
    for i in sorted(range(len(passages)), key=lambda i: -passage_scores[i])[:top]:
        sentences = split_sentences(passages[i])
        if len(sentences) == 1:  # nothing to choose between: no scoring needed
            out[i] = sentences[0]
        else:
            candidates += [(i, s) for s in sentences]
    best = {}
    for (i, s), sc in zip(candidates, score(query, [s for _, s in candidates]) if candidates else []):
        if i not in best or sc > best[i][0]:
            best[i] = (sc, s)
    for i, (_, s) in best.items():
        out[i] = s
    return out
