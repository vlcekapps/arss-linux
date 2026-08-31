"""Locale-independent search normalization shared by ARSS surfaces."""

from __future__ import annotations

import re
import unicodedata


_MULTIPLE_SPACES = re.compile(r"\s+")


def normalize_search_text(value: str) -> str:
    """Apply the exact ARSS Contract search-normalization pipeline."""

    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(
        character
        for character in decomposed
        if unicodedata.category(character) not in {"Mn", "Mc", "Me"}
    ).casefold()
    words = "".join(
        character if unicodedata.category(character)[:1] in {"L", "N"} else " "
        for character in without_marks
    )
    return _MULTIPLE_SPACES.sub(" ", words).strip()


def search_text_matches(query: str, *values: str) -> bool:
    """Match a normalized phrase or every term against candidate word prefixes."""

    normalized_query = normalize_search_text(query)
    if not normalized_query:
        return True
    searchable = normalize_search_text(" ".join(values))
    if normalized_query in searchable:
        return True
    words = searchable.split()
    return all(
        any(word.startswith(term) for word in words)
        for term in normalized_query.split()
    )


__all__ = ["normalize_search_text", "search_text_matches"]
