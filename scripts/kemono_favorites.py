#!/usr/bin/env python3
# 待确认：实时收藏与历史快照一致性、媒体节点、总容量及云端耗时。
# 后续研究：失败原文件补下；潜在优化：依据实测调整每批25帖及并行数。
# 风险：3秒下载期限可能丢失大附件；预览不是原图；公开发布第三方内容。
# 验证重点：私有字段隔离、逐跳重定向校验、历史集合覆盖、单tar.xz重组、上传确认。
"""Cloud-only favorites staging and a single, verified xz -9e release."""

import argparse
import contextlib
import gzip
import hashlib
from html.parser import HTMLParser
import io
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.parse

import kemono_release as base

PART_BYTES = 1_800_000_000
BATCH_SIZE = 25
PRIVATE_KEYS = {"faved_seq", "relation_id", "cookie", "cookies", "session",
                "password", "token", "access_token", "refresh_token", "authorization"}
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k.lower() not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def key(post):
    result = tuple(post.get(k) for k in ("service", "user", "id"))
    if any(not isinstance(v, str) or not ID_PATTERN.fullmatch(v) for v in result):
        raise ValueError("invalid post identity")
    return result


def validate_inventory(posts, baseline):
    if not isinstance(posts, list) or not posts:
        raise ValueError("favorites API did not return a nonempty array")
    identities, fingerprints = set(), {}
    for post in posts:
        identity = key(post)
        if identity in identities:
            raise ValueError("duplicate favorite post")
        identities.add(identity)
        fingerprints[digest(identity)] = digest(clean(post))
    missing = set(baseline["posts"]) - fingerprints.keys()
    changed = sum(fingerprints.get(k) != v for k, v in baseline["posts"].items()
                  if k in fingerprints)
    if missing or changed:
        fields = []
        for name, expected in baseline.get("field_hashes", {}).items():
            actual = {digest(key(p)): [name in p, clean(p.get(name))] for p in posts
                      if digest(key(p)) in baseline["posts"]}
            if digest(actual) != expected:
                fields.append(name)
        raise RuntimeError(f"historical baseline mismatch: missing={len(missing)}, changed={changed}, fields={','.join(fields)}; private snapshot transfer required")
    return [clean(p) for p in posts]


def gh(*args, binary=False):
    result = subprocess.run(["gh", *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError("GitHub operation failed; credentials and response omitted")
    return result.stdout if binary else json.loads(result.stdout or b"null")


def repo():
    value = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("invalid GitHub repository")
    return value


def assets(release_id):
    pages = gh("api", f"repos/{repo()}/releases/{release_id}/assets?per_page=100",
               "--paginate", "--slurp")
    return [asset for page in pages for asset in page]


def upload(tag, release_id, path):
    path = Path(path)
    # Never clobber an existing asset: interrupted attempts retain their evidence.
    existing = [a for a in assets(release_id) if a["name"] == path.name]
    if existing:
        raise RuntimeError("asset already exists; use a new run attempt")
    gh("release", "upload", tag, str(path), "--repo", repo(), binary=True)
    matches = [a for a in assets(release_id) if a["name"] == path.name]
    if len(matches) != 1 or matches[0]["state"] != "uploaded" or matches[0]["size"] != path.stat().st_size:
        raise RuntimeError("uploaded asset size/state mismatch")
    remote_digest = matches[0].get("digest")
    if remote_digest:
        with path.open("rb") as source:
            local_digest = hashlib.file_digest(source, "sha256").hexdigest()
        if remote_digest != "sha256:" + local_digest:
            raise RuntimeError("uploaded asset digest mismatch")
    return {k: matches[0][k] for k in ("id", "name", "size", "state")}


def save_json(path, value):
    Path(path).write_bytes(encoded(value))


def download_json_asset(release_id, name):
    matches = [a for a in assets(release_id) if a["name"] == name]
    if len(matches) != 1 or matches[0]["state"] != "uploaded":
        raise RuntimeError("missing staging metadata")
    if matches[0]["size"] > 32_000_000:
        raise RuntimeError("staging metadata exceeds 32MB")
    return gh("api", f"repos/{repo()}/releases/assets/{matches[0]['id']}",
              "-H", "Accept: application/octet-stream")


def create_draft(tag, title):
    value = gh("api", f"repos/{repo()}/releases", "--method", "POST",
               "-f", f"tag_name={tag}", "-f", f"target_commitish={os.environ['GITHUB_SHA']}",
               "-F", "draft=true", "-f", f"name={title}")
    if not value.get("draft") or not isinstance(value.get("id"), int):
        raise RuntimeError("GitHub did not create a draft")
    return value["id"]


def emit(**values):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for name, value in values.items():
            output.write(f"{name}={value}\n")


def inventory():
    if not os.environ.get("KEMONO_USERNAME") or not os.environ.get("KEMONO_PASSWORD"):
        raise RuntimeError("both existing Kemono account Secrets are required")
    api = base.Api()
    api.login()
    raw = api.get("/account/favorites?type=post")
    baseline = json.loads(Path(".github/favorites-baseline.json").read_text())
    posts = validate_inventory(raw, baseline)
    # Repeat once to reject a moving or unstable target list.
    again = validate_inventory(api.get("/account/favorites?type=post"), baseline)
    if {digest(key(p)): digest(p) for p in posts} != {digest(key(p)): digest(p) for p in again}:
        raise RuntimeError("favorites changed during inventory; retry with a stable target list")
    stamp = f"{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    tag = "kemono-favorites-staging-" + stamp
    release_id = create_draft(tag, "Private staging for favorites " + stamp)
    path = Path(os.environ["RUNNER_TEMP"]) / "targets.json"
    save_json(path, posts)
    upload(tag, release_id, path)
    count = (len(posts) + BATCH_SIZE - 1) // BATCH_SIZE
    if count > 256:
        raise RuntimeError("target list exceeds the GitHub matrix limit; adjust batch size before running")
    print(f"Frozen targets: {len(posts)}; historical baseline: {len(baseline['posts'])}; batches: {count}")
    emit(staging_id=release_id, staging_tag=tag, matrix=json.dumps(list(range(count))),
         target_count=len(posts), batch_count=count)


class HtmlMedia(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in ("img", "source", "video", "audio"):
            return
        for name in ("src", "data-src", "poster"):
            if attrs.get(name):
                self.urls.append(attrs[name])
        for name in ("srcset", "data-srcset"):
            value = attrs.get(name, "")
            if value.startswith("data:"):
                self.urls.append(value)
            else:
                self.urls.extend(x.strip().split()[0] for x in value.split(",") if x.strip())


def references(snapshot, detail):
    post = detail.get("post", {}) if isinstance(detail, dict) else {}
    resolved, thumbnails = {}, set()
    nodes = [*(detail.get("attachments") or []), *(detail.get("previews") or []),
             *(detail.get("videos") or [])]
    for item in nodes:
        if isinstance(item, dict) and item.get("path"):
            try:
                path = base.canonical_path(item["path"])
            except ValueError:
                continue
            if item.get("server"):
                server = item["server"]
                parsed = urllib.parse.urlsplit(server)
                if base.media_host(server) and parsed.path in ("", "/") and not parsed.query and not parsed.fragment:
                    resolved[path] = server.rstrip("/")
            if item.get("type") == "thumbnail":
                thumbnails.add(path)
    refs = []
    for source in (snapshot, post):
        for item in [source.get("file"), source.get("shared_file"), *(source.get("attachments") or [])]:
            if isinstance(item, dict) and item.get("path"):
                refs.append({"raw": item["path"], "field": "attachment", "name": item.get("name")})
        parser = HtmlMedia()
        parser.feed(source.get("content") or "")
        refs.extend({"raw": url, "field": "html"} for url in parser.urls)
    for item in nodes:
        if isinstance(item, dict) and item.get("path") and item.get("type") != "thumbnail":
            refs.append({"raw": item["path"], "field": "detail"})
    for ref in refs:
        raw = ref["raw"]
        try:
            parsed = urllib.parse.urlsplit(raw)
            if parsed.scheme or parsed.netloc:
                url = urllib.parse.urljoin(base.BASE, raw)
                if not base.media_host(url):
                    ref.update(state="unsupported_source", url=raw)
                    continue
                path = base.canonical_path(parsed.path)
                if parsed.query or parsed.fragment:
                    raise ValueError("URL query or fragment requires review")
                if parsed.path.startswith("/thumbnail/"):
                    ref["url"], ref["preview_url"] = None, url
                else:
                    ref["url"] = url
            else:
                path = base.canonical_path(raw)
                quoted = urllib.parse.quote(path, safe="/")
                ref["url"] = resolved[path] + "/data" + quoted if path in resolved else None
            ref["path"] = path
            if path in thumbnails:
                ref["preview_url"] = "https://img.kemono.cr/thumbnail/data" + urllib.parse.quote(path, safe="/")
            ref["state"] = "pending"
        except (ValueError, TypeError):
            ref["state"] = "unsafe_reference"
    return refs


def image_header(head):
    return (head.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM"))
            or head[:4] == b"RIFF" and head[8:12] == b"WEBP"
            or head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis", b"heic", b"heix")
            or head.startswith((b"II*\x00", b"MM\x00*")))


@contextlib.contextmanager
def download(url, directory, image_only=False):
    # Redirects share one deadline and are validated before the next request.
    deadline = time.monotonic() + base.FILE_TIMEOUT
    with tempfile.TemporaryDirectory(prefix="media-", dir=directory) as staging:
        path, header = Path(staging) / "media.bin", Path(staging) / "headers"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GH_", "GITHUB_", "KEMONO_"))}
        for _ in range(6):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise base.DownloadFailure(28, 1)
            if not base.media_host(url):
                raise base.DownloadFailure(67, 1)
            budget = shutil.disk_usage(directory).free - PART_BYTES - 256_000_000
            if budget <= 0:
                raise RuntimeError("insufficient runner disk")
            command = ["curl", "--disable", "--silent", "--show-error", "--fail",
                       "--proto", "=https", "--noproxy", "*", "--retry", "0",
                       "--connect-timeout", str(remaining), "--max-time", str(remaining),
                       "--max-filesize", str(budget), "--user-agent", base.user_agent(),
                       "--header", "Accept-Encoding: identity", "--dump-header", str(header),
                       "--write-out", "%{http_code}", "--output", str(path), url]
            try:
                result = subprocess.run(command, env=env, capture_output=True, timeout=remaining)
            except subprocess.TimeoutExpired:
                raise base.DownloadFailure(28, 1) from None
            if result.returncode:
                raise base.DownloadFailure(result.returncode, 1)
            code = result.stdout.decode("ascii", errors="replace")
            headers = header.read_text(encoding="iso-8859-1")
            if code in ("301", "302", "303", "307", "308"):
                locations = re.findall(r"(?im)^location:\s*(.+?)\s*$", headers)
                if len(locations) != 1:
                    raise base.DownloadFailure(47, 1)
                url = urllib.parse.urljoin(url, locations[0])
                continue
            if code != "200" or not path.is_file() or path.stat().st_size == 0:
                raise base.DownloadFailure(22, 1)
            with path.open("rb") as source:
                head = source.read(512)
            bad_html = head.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<head", b"<body"))
            expected_image = image_only or urllib.parse.urlsplit(url).path.lower().endswith(
                (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".tif", ".tiff", ".heic"))
            if bad_html or re.search(r"(?im)^content-type:\s*text/html", headers) or expected_image and not image_header(head):
                raise base.DownloadFailure(65, 1)
            yield path
            return
        raise base.DownloadFailure(47, 1)


def add_bytes(archive, name, value):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(value), 0o644, 0
    archive.addfile(info, io.BytesIO(value))


def safe_member(member):
    path = PurePosixPath(member.name)
    if not member.isfile() or path.is_absolute() or ".." in path.parts or "\\" in member.name or any(ord(c) < 32 for c in member.name):
        raise ValueError("unsafe staging archive member")


def write_part_archive(directory, stem, tag, release_id, produce, extreme=False):
    uploaded = []
    def ready(path):
        uploaded.append(upload(tag, release_id, path))
    writer = base.PartWriter(directory, stem, PART_BYTES, ready)
    try:
        compressed = (lzma.LZMAFile(writer, "w", preset=9 | lzma.PRESET_EXTREME,
                                    check=lzma.CHECK_CRC64) if extreme else
                      gzip.GzipFile(fileobj=writer, mode="wb", compresslevel=1, mtime=0))
        with compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as archive:
                result = produce(archive)
        writer.finish()
        return uploaded, result
    finally:
        writer.abort()


def batch(index, staging_id, staging_tag):
    posts = download_json_asset(staging_id, "targets.json")
    selected = posts[index * BATCH_SIZE:(index + 1) * BATCH_SIZE]
    if not selected:
        raise ValueError("empty batch")
    directory = Path(os.environ["RUNNER_TEMP"]) / f"favorites-batch-{index:05d}"
    directory.mkdir(exist_ok=False)
    api = base.Api()  # Public post API; do not copy account cookies into batch jobs.
    counts = {"posts_processed": 0, "metadata_saved": 0, "original_saved": 0,
              "preview_saved": 0, "missing_originals": 0, "detail_failed": 0}
    def produce(archive):
        saved = {}
        for position, snapshot in enumerate(selected, 1):
            service, user, post_id = key(snapshot)
            prefix = f"posts/{service}/{user}/{post_id}"
            base.add_json(archive, prefix + "/snapshot.json", clean(snapshot))
            content = snapshot.get("content")
            if isinstance(content, str):
                add_bytes(archive, prefix + "/content.html", content.encode("utf-8"))
            detail, detail_error = {}, None
            try:
                detail = api.get(f"/{service}/user/{user}/post/{post_id}")
                if not isinstance(detail, dict) or not isinstance(detail.get("post"), dict) or key(detail["post"]) != key(snapshot):
                    raise ValueError("post detail identity mismatch")
                detail = clean(detail)
                base.add_json(archive, prefix + "/detail.json", detail)
            except (RuntimeError, ValueError):
                detail = {}
                detail_error = "detail_unavailable"
                counts["detail_failed"] += 1
            records = references(snapshot, detail)
            for record in records:
                if record["state"] != "pending":
                    counts["missing_originals"] += 1
                    continue
                identity = record.get("url") or "unresolved:" + record["path"]
                if identity in saved:
                    record.update(saved[identity])
                    continue
                result = {"state": "download_failed", "reason": "missing_media_node" if not record.get("url") else "download_failed"}
                for kind, url in (("original", record.get("url")), ("preview", record.get("preview_url"))):
                    if not url:
                        continue
                    try:
                        with download(url, directory, image_only=kind == "preview") as path:
                            name = ("media/" if kind == "original" else "previews/") + hashlib.sha256(identity.encode()).hexdigest()
                            info = tarfile.TarInfo(name)
                            info.size, info.mode, info.mtime = path.stat().st_size, 0o644, 0
                            with path.open("rb") as source:
                                archive.addfile(info, source)
                            result.update(state=kind + "_saved", archive_path=name, bytes=info.size)
                            if kind == "original":
                                result.pop("reason", None)
                            counts[kind + "_saved"] += 1
                            break
                    except base.DownloadFailure as error:
                        result[kind + "_failure"] = {"exit_code": error.code, "attempts": 1,
                                                     "timeout_seconds": base.FILE_TIMEOUT}
                if result["state"] != "original_saved":
                    counts["missing_originals"] += 1
                record.update(result)
                saved[identity] = result
            base.add_json(archive, prefix + "/status.json", {
                "identity": key(snapshot), "detail_error": detail_error,
                "source_content_empty": not bool(content), "media": records,
                "original_media_complete": detail_error is None and all(r["state"] == "original_saved" for r in records)})
            counts["posts_processed"] += 1
            counts["metadata_saved"] += 1
            print(f"Batch {index}: post {position}/{len(selected)} processed", flush=True)
        base.add_json(archive, f"batches/{index:05d}/summary.json", counts)
        return counts
    parts, _ = write_part_archive(directory, f"batch-{index:05d}.tar.gz", staging_tag, staging_id, produce)
    path = directory / f"batch-{index:05d}.json"
    save_json(path, {"index": index, "parts": parts, "counts": counts})
    upload(staging_tag, staging_id, path)


class PartReader:
    """Sequential GitHub asset reader with bounded memory and checked subprocesses."""
    def __init__(self, parts):
        self.parts, self.position, self.process, self.size = parts, 0, None, 0

    def read(self, length=-1):
        if length < 0:
            raise ValueError("unbounded reads are forbidden")
        result = bytearray()
        while len(result) < length:
            if self.process is None:
                if self.position == len(self.parts):
                    break
                part = self.parts[self.position]
                self.process = subprocess.Popen(["gh", "api", f"repos/{repo()}/releases/assets/{part['id']}",
                                                 "-H", "Accept: application/octet-stream"],
                                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self.size = 0
            chunk = self.process.stdout.read(length - len(result))
            if chunk:
                self.size += len(chunk)
                result.extend(chunk)
            else:
                self.process.stdout.close()
                code = self.process.wait()
                self.process = None
                if code or self.size != self.parts[self.position]["size"]:
                    raise RuntimeError("staging asset download failed or truncated")
                self.position += 1
        return bytes(result)

    def close(self):
        if self.process is not None:
            self.process.terminate()
            self.process.stdout.close()
            self.process.wait()
            self.process = None


def copy_member(archive, member, source, seen):
    safe_member(member)
    fingerprint = hashlib.sha256()
    class HashReader:
        def read(self, size):
            data = source.read(size)
            fingerprint.update(data)
            return data
    reader = HashReader()
    if member.name in seen:
        left = member.size
        while left:
            data = reader.read(min(1_048_576, left))
            if not data:
                raise RuntimeError("truncated tar member")
            left -= len(data)
        if seen[member.name] != (member.size, fingerprint.hexdigest()):
            raise RuntimeError("conflicting duplicate tar member")
    else:
        archive.addfile(member, reader)
        seen[member.name] = (member.size, fingerprint.hexdigest())


def verify_archive(parts, expected):
    reader, found = PartReader(parts), {}
    try:
        with lzma.LZMAFile(reader, "rb") as compressed:
            with tarfile.open(fileobj=compressed, mode="r|") as archive:
                for member in archive:
                    safe_member(member)
                    if member.name in found:
                        raise RuntimeError("duplicate final archive member")
                    with archive.extractfile(member) as source:
                        fingerprint = hashlib.file_digest(source, "sha256").hexdigest()
                    found[member.name] = (member.size, fingerprint)
            while compressed.read(1_048_576):
                pass  # Force validation of the xz footer and CRC64, including later parts.
        if found != expected or reader.position != len(parts):
            raise RuntimeError("final archive verification mismatch")
    finally:
        reader.close()


def assemble(staging_id, staging_tag):
    posts = download_json_asset(staging_id, "targets.json")
    batch_count = (len(posts) + BATCH_SIZE - 1) // BATCH_SIZE
    manifests = [download_json_asset(staging_id, f"batch-{i:05d}.json") for i in range(batch_count)]
    remote = {a["id"]: a for a in assets(staging_id)}
    for index, manifest in enumerate(manifests):
        if manifest["index"] != index:
            raise RuntimeError("batch index mismatch")
        if manifest["counts"]["posts_processed"] != len(posts[index * BATCH_SIZE:(index + 1) * BATCH_SIZE]):
            raise RuntimeError("batch post count mismatch")
        for part in manifest["parts"]:
            actual = remote.get(part["id"], {})
            if actual.get("state") != "uploaded" or any(actual.get(k) != part[k] for k in ("name", "size")):
                raise RuntimeError("staging part mismatch")
    directory = Path(os.environ["RUNNER_TEMP"]) / "favorites-final"
    directory.mkdir(exist_ok=False)
    tag = staging_tag.replace("-staging-", "-archive-")
    release_id = create_draft(tag, "Favourite Posts · assembling")
    seen = {}
    totals = {name: sum(m["counts"][name] for m in manifests) for name in manifests[0]["counts"]}
    summary = {"target_union_total": len(posts), **totals, "posts_pending": 0,
               "complete": totals["missing_originals"] == 0 and totals["detail_failed"] == 0,
               "compression": "xz -9e; one tar stream; CRC64", "part_bytes_max": PART_BYTES,
               "preview_is_original": False}
    def produce(archive):
        for manifest in manifests:
            reader = PartReader(manifest["parts"])
            try:
                with gzip.GzipFile(fileobj=reader, mode="rb") as compressed:
                    with tarfile.open(fileobj=compressed, mode="r|") as source_tar:
                        for member in source_tar:
                            safe_member(member)
                            with source_tar.extractfile(member) as source:
                                copy_member(archive, member, source, seen)
                    while compressed.read(1_048_576):
                        pass
                if reader.position != len(manifest["parts"]):
                    raise RuntimeError("unconsumed staging archive parts")
            finally:
                reader.close()
            print(f"Merged batch {manifest['index'] + 1}/{batch_count}", flush=True)
        for post in posts:
            prefix = "posts/" + "/".join(key(post))
            required = [prefix + "/snapshot.json", prefix + "/status.json"]
            if isinstance(post.get("content"), str):
                required.append(prefix + "/content.html")
            if any(name not in seen for name in required):
                raise RuntimeError("final archive is missing a target post or original text")
        data = encoded(summary)
        add_bytes(archive, "archive-summary.json", data)
        seen["archive-summary.json"] = (len(data), hashlib.sha256(data).hexdigest())
        return summary
    parts, _ = write_part_archive(directory, "favorites.tar.xz", tag, release_id, produce, extreme=True)
    print("Re-downloading final parts in cloud to verify every member and xz CRC64", flush=True)
    verify_archive(parts, seen)
    path = directory / "archive-manifest.json"
    save_json(path, {**summary, "assets": parts, "verified": True, "member_count": len(seen)})
    upload(tag, release_id, path)
    incomplete = not summary["complete"]
    notes = (f"{'INCOMPLETE: missing originals or post details. ' if incomplete else ''}"
             f"Processed {len(posts)} favourite posts. Metadata saved: {totals['metadata_saved']}. "
             f"Original downloads: {totals['original_saved']}; previews: {totals['preview_saved']}.\n\n"
             "Download every favorites.tar.xz.partNNNNN, concatenate in numeric order, then extract tar.xz. "
             "Linux: `cat favorites.tar.xz.part* > favorites.tar.xz && tar -xf favorites.tar.xz`. "
             "Windows cmd: `copy /b favorites.tar.xz.part* favorites.tar.xz`.\n\n"
             "Each post has snapshot.json, original content.html when present, and status.json. "
             "status.json records all media references and failures. External embeds remain references. "
             "Media and previews use stable filenames; their post associations are in status.json. "
             "Every final archive member and the complete xz stream were verified in GitHub Actions. "
             "Account credentials, cookies and favourite sequence fields are excluded.")
    gh("api", f"repos/{repo()}/releases/{release_id}", "--method", "PATCH",
       "-F", "draft=false", "-F", f"prerelease={str(incomplete).lower()}",
       "-f", f"name=Favourite Posts · {len(posts)} posts" + (" · INCOMPLETE" if incomplete else ""),
       "-f", f"body={notes}")
    print(f"RELEASE_URL=https://github.com/{repo()}/releases/tag/{tag}", flush=True)
    emit(release_url=f"https://github.com/{repo()}/releases/tag/{tag}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("inventory", "batch", "assemble"))
    parser.add_argument("--index", type=int)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("real network/downloads are allowed only in GitHub Actions")
    if args.mode == "inventory":
        inventory()
    else:
        staging_id = int(os.environ["STAGING_ID"])
        staging_tag = os.environ["STAGING_TAG"]
        if args.mode == "batch":
            if args.index is None or args.index < 0:
                raise ValueError("invalid batch index")
            batch(args.index, staging_id, staging_tag)
        else:
            assemble(staging_id, staging_tag)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Print only controlled errors; never API bodies, command stderr or source content.
        message = str(error) if isinstance(error, (RuntimeError, ValueError)) else type(error).__name__
        print("::error::" + message.replace("\n", " ").replace("\r", " "), flush=True)
        raise SystemExit(1) from None
