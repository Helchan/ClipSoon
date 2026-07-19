"""Case-insensitive ordered matching and deterministic relevance scoring.

The contract is intentionally small and visible here:

* Empty query: newest ``updated_at`` first.
* Non-empty query: every normalized query character must occur in order.
* Exact field equality always wins.
* Other scores combine query/content length coverage and match continuity.
* Recency is only a tie-breaker when relevance scores are equal.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from clipsoon.core import ClipItem, ClipKind

EXACT_SCORE = 1_000_000.0
LENGTH_WEIGHT = 5_000.0
CONTINUITY_WEIGHT = 3_500.0
COMPACTNESS_WEIGHT = 1_500.0


@dataclass(frozen=True, slots=True)
class SearchResult:
    item: ClipItem
    score: float


@dataclass(frozen=True, slots=True)
class _IndexedClip:
    item: ClipItem
    fields: tuple[str, ...]


class SearchEngine:
    """Normalized in-memory index; typing never reads SQLite or image files."""

    def __init__(self, items: Sequence[ClipItem] = ()) -> None:
        self.replace(items)

    def replace(self, items: Sequence[ClipItem]) -> None:
        self._records = tuple(_index_item(item) for item in items)

    def rank(
        self,
        query: str,
        *,
        now: float,  # retained in the API for injectable-clock callers
        kind: ClipKind | None = None,
        limit: int | None = None,
    ) -> list[SearchResult]:
        del now
        query = normalize(query)
        records = (record for record in self._records if kind is None or record.item.kind is kind)
        if query:
            ranked = []
            for record in records:
                score = _score_record(record, query)
                if score is not None:
                    ranked.append(SearchResult(record.item, score))
        else:
            ranked = [SearchResult(record.item, record.item.updated_at) for record in records]
        ranked.sort(
            key=lambda result: (
                result.score,
                result.item.updated_at,
                result.item.created_at,
                result.item.id,
            ),
            reverse=True,
        )
        return ranked if limit is None else ranked[:limit]


def normalize(value: str) -> str:
    """NFKC + casefold gives Unicode-aware, case-insensitive matching."""
    return unicodedata.normalize("NFKC", value).casefold()


def rank_items(
    items: list[ClipItem],
    query: str,
    *,
    now: float,
    kind: ClipKind | None = None,
    limit: int | None = None,
) -> list[SearchResult]:
    return SearchEngine(items).rank(query, now=now, kind=kind, limit=limit)


def score_text(query: str, content: str) -> float | None:
    """Return relevance or ``None`` when query is not an ordered subsequence."""
    query, content = normalize(query), normalize(content)
    if not query or not content:
        return None
    if query == content:
        return EXACT_SCORE
    alignment = _best_alignment(query, content)
    if alignment is None:
        return None
    adjacent_pairs, span = alignment
    length_ratio = len(query) / len(content)
    continuity = adjacent_pairs / (len(query) - 1) if len(query) > 1 else 1.0
    compactness = len(query) / span
    return (
        length_ratio * LENGTH_WEIGHT
        + continuity * CONTINUITY_WEIGHT
        + compactness * COMPACTNESS_WEIGHT
    )


def _index_item(item: ClipItem) -> _IndexedClip:
    if item.kind is ClipKind.TEXT:
        primary = normalize(item.text)
    elif item.kind is ClipKind.FILES:
        primary = normalize(" ".join(item.files))
    else:
        primary = normalize(f"图片 image {item.width}x{item.height}")
    source = normalize(item.source_app)
    return _IndexedClip(item, tuple(field for field in (primary, source) if field))


def _score_record(record: _IndexedClip, query: str) -> float | None:
    scores = (score_text(query, field) for field in record.fields)
    return max((score for score in scores if score is not None), default=None)


def _best_alignment(query: str, content: str) -> tuple[int, int] | None:
    """Find the exact highest-scoring ordered alignment.

    Contiguous substrings and impossible queries take fast C-level ``find``
    paths. The remaining cases use a sparse dynamic program. For each partial
    query and adjacency count, only the latest start is retained because it
    strictly dominates an earlier start with the same state.
    """
    if query in content:
        return max(0, len(query) - 1), len(query)
    if len(query) > len(content):
        return None
    position = 0
    for character in query:
        position = content.find(character, position)
        if position < 0:
            return None
        position += 1

    query_positions: dict[str, list[int]] = {}
    for index, character in enumerate(query):
        query_positions.setdefault(character, []).append(index)

    size = len(query)
    ending_previous: dict[int, dict[int, int]] = {}
    ended_before_previous: list[dict[int, int]] = [{} for _ in range(size)]
    best: tuple[float, int, int] | None = None
    for content_index, character in enumerate(content):
        ending_here: dict[int, dict[int, int]] = {}
        for query_index in query_positions.get(character, ()):
            current: dict[int, int] = {}
            if query_index == 0:
                current[0] = content_index
            else:
                for adjacent, start in ending_previous.get(query_index - 1, {}).items():
                    current[adjacent + 1] = max(current.get(adjacent + 1, -1), start)
                for adjacent, start in ended_before_previous[query_index - 1].items():
                    current[adjacent] = max(current.get(adjacent, -1), start)
            if current:
                ending_here[query_index] = current

        for adjacent, start in ending_here.get(size - 1, {}).items():
            span = content_index - start + 1
            continuity = adjacent / max(1, size - 1)
            compactness = size / span
            quality = continuity * CONTINUITY_WEIGHT + compactness * COMPACTNESS_WEIGHT
            candidate = (quality, adjacent, span)
            if best is None or candidate[0] > best[0] or (candidate[0] == best[0] and span < best[2]):
                best = candidate

        for query_index, states in ending_previous.items():
            prior = ended_before_previous[query_index]
            for adjacent, start in states.items():
                prior[adjacent] = max(prior.get(adjacent, -1), start)
        ending_previous = ending_here

    return None if best is None else (best[1], best[2])
