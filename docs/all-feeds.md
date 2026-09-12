# All feeds: Linux 1.8.0

Both subscription lists prepend a virtual All row at two subscriptions. The
row is presentation only: it has no URL, is never stored, exported, deleted,
renamed, made default, or passed to background monitoring. Filtering matches
the localized All label, but opening it loads the complete subscription set.

`aggregate.py` is GTK-independent. Each item retains the original subscription
and article/episode; identical publisher entry IDs from different feed URLs
remain separate. All rows expose title, source, date (or localized unknown date)
in that order, regardless of the single-feed date preference. Podcasts retain
their original enclosure and source metadata; RSS uses the external browser.

The All window's native labelled dropdown saves `rss_all_sort` and
`podcast_all_sort` separately. Defaults are `date`; invalid persisted values
fall back defensively. Date ordering is newest first, missing timestamps last.
Source ordering uses case-insensitive Czech/English alphabetic keys, including
Czech č/ř/š/ž and ch after h, followed by source URL (distinct feeds), then date.
Ties use title and source-scoped entry identity. No process-global locale change
is made. Sorting is local, preserves selected item identity and leaves keyboard
focus on the dropdown. Back restores the All subscription row.

Each source is fetched through the existing bounded, secure feed client on an
application worker. Loads are sequential, avoiding concurrent use of its HTTP
session. Closing the view stops subsequent fetches and ignores late results;
the already-running request retains the client's existing network timeout.
Successful source results remain visible after individual failures. The status
names failed sources and Retry reloads all sources. Manual notification
checkpoints advance only for actual successful source URLs, never for All.

## Verification and release gates

- Offline domain tests cover the two-source threshold, date and source ordering,
  Czech/English collation, missing dates, deterministic ties, source-scoped IDs,
  original playback objects, partial/full failures, cancellation and settings.
- Storage tests cover independent durable settings, invalid-value migration and
  rejection without overwriting prior data.
- Native GUI smoke checks All's absent edit menu, labels, unchanged stores,
  original external-browser/player callbacks, sorting and partial failure.
- Large-text smoke includes RSS and podcast All windows and their sort controls.
- Manual Orca gate on Fedora: from both subscription lists activate All using
  Enter; confirm title/source/date order, arrow navigation and Back focus. Tab to
  sorting, choose Source, check that focus remains there and rows regroup. Check
  a failed feed, Retry, Czech/English and 200% text. This is a required manual
  check, not an assertion that Orca was tested on the Windows development host.

The Windows development host has no GTK/PyGObject, Fedora or WSL runtime. Native
GTK, AT-SPI and RPM release gates must run in the Fedora 45 CI environment or
on the physical Fedora system; Windows-only checks do not replace those gates.
