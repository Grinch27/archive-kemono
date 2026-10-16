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
  media/              # {post_id}_p{order}.{original_extension}
    987654_p01.jpg
    987654_p02.mp4
    987654_p03.png
  previews/           # Replacement preview reuses the SAME page-order p index
    987654_p02.jpg
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
- `status.json` records each media item, its `page_position`,
  `page_media_total`, original reference, file association and failures.
  Numbering is **per post, not across the collection**. Images, videos and
  other media share one sequence based on the page's available ordering:
  primary file, visible HTML media in DOM order, then detached attachments
  in their API order. Distinct types are never independently renumbered.
  For 3 media items, use `p01` to `p03`; for 25, `p001` to `p025`.
  The width is the decimal digits in that post's total media item count,
  **plus one leading-zero position**. Repeated references reuse the
  same number, and missing originals retain their slots, with failure
  recorded in `status.json`. The original filenames/paths live in JSON,
  not in on-disk names. A preview uses the same p number and its *actual*
  image extension, even when the original was a video.
  Detached API attachment arrays cannot always recover the precise
  rendered page placement; their original order is preserved without
  asserting unsupported DOM positions. External embeds remain references.
  Files from different posts have separate archive paths, even if they
  originated from the same URL. A preview is never counted as an original.
- The existing streamed, size-checked Release upload, 1,800 MB default part
  size, API safeguards, and original/preview one-attempt three-second timeouts
  remain unchanged.
- The compressed Favourite Posts release is always **one tar.xz stream**.
  When the upload fits in exactly one part, the final asset is named
  `favorites.tar.xz` and can be extracted directly. When the stream requires
  multiple parts, the final assets are named
  `favorites.tar.xz.part00001`, `favorites.tar.xz.part00002`, etc.; download
  all parts and concatenate them in numeric order before extracting.
  The streaming uploader cannot know the part count in advance, so the
  single uploaded part is renamed on GitHub **after complete xz verification**
  by updating the asset name in place (same asset ID, size and digest).
  `archive-manifest.json` and the Release description use the final name.
  Release metadata (manifest, summary, registry) remains separate from
  the tar stream, never in V1-style root directories.

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
