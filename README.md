# archive-kemono — V2 only

Both **Favourite Posts** and **Favourite Artists** use one archive layout:

```text
{service}/{author_id}/[{post_id}]{sanitized_frozen_title}/
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
titles are for readability, not identification.

## Format contract

- **Only V2 is supported for new production archives.** No V1 archive import,
  V1 recovery, V1-to-V2 converter, or V1 media layout fallback exists.
- The `layout-registry.json` release asset contains `schema_version: 2`
  and frozen post folder names. Reuse a previously **published** V2 registry
  so that the same post retains its directory even if its title changes.
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
