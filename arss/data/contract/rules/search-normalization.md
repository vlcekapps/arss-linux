# Search normalization

Directory and station search use the same deterministic matching rule:

1. Unicode-normalize to NFKD.
2. Remove combining marks.
3. Case-fold.
4. Replace punctuation with spaces and collapse whitespace.
5. Split the query into terms.
6. Every query term must prefix-match at least one candidate word; a full
   normalized substring also matches.

For example, `srdce nevi` matches `Srdce nevidomého dopraváka`. Implementations
must not require whole-word equality or exact diacritics.
