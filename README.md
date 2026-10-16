# archive-kemono — V2 only

Both **Favourite Posts** and **Favourite Artists** use one archive layout:

```text
{service}/{author_id}/{post_id}_p{zero_padded_sequence}/
  post.json
  snapshot.json       # When a favourite-post snapshot is available
  listing.json        # Optional
  detail.json         # When the post-detail API succeeds
  content.html        # When content is a string
  status.json
  media/              # Downloaded original files, local to this post
  previews/           # Preview replacements, never counted as originals
```

An artist archive may also contain `{service}/{author_id}/_author.json`,
`_posts.json`, `_files.json`, and `_failed-files.json`. All tar member
paths use POSIX separators. The identity is `(service, author_id, post_id)`;
the p-number is the first registered post's sequence (width = digits(total) + 1). Titles remain only in JSON.

## Format contract

- **Only V2 is supported for new production archives.** No V1 archive import,
  V1 recovery, V1-to-V2 converter, or V1 media layout fallback exists.
- The `layout-registry.json` release asset contains `schema_version: 2`,
  the layout identifier, and frozen post folder names. For 1,289 posts use
  five digits: `12345_p00001` to `67890_p01289` (example post IDs).
  For 25 posts use three digits: `12345_p001`. The initial sequence and
  padding are frozen across subsequent runs and both favourite modes. Adding
  posts does not rename previously archived folders. The post title remains
  intact in the JSON/HTML content, not the folder name.
  Only published registries with the **new** layout identifier are reusable.
  Previous title-based V2 registries are incompatible and are not imported.
- A Favourite Posts staging batch manifest must contain `schema_version: 2`.
  Assembly rejects missing/older versions and rejects tar entries that are
  not inside the approved V2 post directories. The final
  `archive-manifest.json` also has `schema_version: 2`.
- `status.json` records a post's original and preview file associations.
  Files from different posts have separate archive paths, even if they
  originated from the same URL. A preview is never counted as an original.
- The existing streamed, size-checked Release upload, 1,800 MB default part
  size, API safeguards, and original/preview one-attempt three-second timeouts
  remain unchanged.
- The compressed release is a **single tar.xz stream split across part files**;
  concatenate the ordered parts before extracting. Release metadata
  (manifest, summary, registry) is stored as separate release assets rather
  than V1-style root directories inside the tar.

## Operations and privacy

- Favourite Posts: `.github/workflows/kemono-favorites.yml`. It runs a
  matrix of 25-post batches, `max-parallel: 10`. A push only triggers the full
  media run when `.github/favorites-request.json` changes; changing V2
  source files alone does not start a new full archive.
- Favourite Artists: `.github/workflows/kemono-release.yml`, manually
  triggered with a creator URL. Both workflows share one concurrency group
  for coordinating the frozen registry.
- Inspect: `.github/workflows/kemono-favorites-inspect.yml`. Pushes run
  offline regression tests only. The explicit `workflow_dispatch` input
  `release_id` is required to inspect a **V2 staging Release**.
- Real media downloads are restricted to GitHub Actions. Use
  `python3 -m unittest discover -s tests -q -b` for offline regression.
- Private account favourites, credentials, cookies, and favourites sequence
  values must not appear in published archive metadata. Check permission to
  redistribute third-party material before public publication.

**Historical V1 Releases are left untouched as legacy backups.** Their files
are not part of the maintained production format, and a successful V2 run
does not retroactively convert them. Do not delete historical assets without
a separate, explicit instruction.
