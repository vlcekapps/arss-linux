# Feed normalization

RSS 2.0 and Atom are normalized to `schemas/normalized-feed.schema.json`.

- Decode XML as declared, tolerate UTF-8 with an optional byte-order mark, and
  reject external entities or network entity resolution.
- Prefer Atom `link[rel=alternate]`, then a link without `rel`, for item URLs.
- Resolve relative links against `xml:base`, then the fetched feed URL.
- RSS item identity preference is canonical link, GUID, then a deterministic
  hash of title, published value, and source position.
- Atom entry identity preference is `id`, canonical link, then the same
  deterministic fallback.
- Sort items newest-first by a successfully parsed publication instant. Place
  undated items last and preserve source order when instants are equal.
- Normalize whitespace in titles and plain descriptions, but do not invent
  publication dates or URLs.
- HTML content may be preserved as text for accessibility summaries; ARSS opens
  RSS articles only in the external browser.

The TN.cz fixture is required because its Atom markup includes explicit closing
`link` elements and entries without a content element. Both forms are valid for
ARSS consumers.
