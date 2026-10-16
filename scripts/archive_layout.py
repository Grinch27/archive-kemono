#!/usr/bin/env python3
# 待确认：Favourite Posts与Artists任务排序是否影响首次分配的序号、永久注册表的私有存储位置。
# 歧义：按首次任务总数确定p字段宽度；后续集合增长不重命名；不同空白注册表无法协调序号。
# 后续研究：V2 冻结注册表完整性核验、Windows 解压路径长度与长期备份策略。
# 潜在优化：注册表较大时按作者分片；当前先保留标准库、可复核、确定性逻辑。
# 风险：编号与任务顺序可能透露账户收藏信息；注册表不得未经授权提交到公开 Git 历史。
# 验证重点：真实快照、按任务总数补零、DOS保留名、大小写冲突、跨来源同一帖子复用及序号冻结。
"""Shared, offline V2 path planner for favourite posts and favourite artists.

This module DOES NOT download content, change a repository or write a registry by
itself. Paths are POSIX tar member paths; they are not direct filesystem writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Mapping, Optional, Tuple

VERSION = 2
LAYOUT_NAME = "service/author_id/post_id_p{padded_sequence}"
MAX_TITLE_BYTES = 112
MAX_POST_COMPONENT_BYTES = 180
MAX_POST_RELATIVE_BYTES = 205
MAX_MEMBER_RELATIVE_BYTES = 240
MAX_SNAPSHOT_BYTES = 32_000_000
ID = re.compile(r"[A-Za-z0-9_-]+\Z", re.ASCII)
SERVICE = re.compile(r"[a-z0-9_-]+\Z", re.ASCII)
INVALID_WINDOWS = frozenset('<>:"/\\|?*')
RESERVED = re.compile(r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$")


class LayoutError(ValueError):
    """A layout cannot be safely or unambiguously represented."""


def _utf8_truncate(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    result = []
    length = 0
    for char in value:
        size = len(char.encode("utf-8"))
        if length + size > limit:
            break
        length += size
        result.append(char)
    # Avoid ending on an unattached combining character after codepoint truncation.
    while result and unicodedata.combining(result[-1]):
        result.pop()
    return "".join(result)


def _clean_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise LayoutError("title must be a string or null")
    value = unicodedata.normalize("NFC", value)
    chars = []
    for char in value:
        kind = unicodedata.category(char)
        if char in INVALID_WINDOWS or kind.startswith("C"):
            chars.append("_")
        elif char.isspace():
            chars.append(" ")
        else:
            chars.append(char)
    cleaned = "".join(chars)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned.strip(" ._")


def _identity(service: str, author_id: str, post_id: str) -> Tuple[str, str, str]:
    if not isinstance(service, str) or not SERVICE.fullmatch(service):
        raise LayoutError("invalid lowercase service")
    for label, item in (("author_id", author_id), ("post_id", post_id)):
        if not isinstance(item, str) or not ID.fullmatch(item):
            raise LayoutError(f"invalid {label}")
    if len(service.encode()) > 48 or len(author_id.encode()) > 96 or len(post_id.encode()) > 96:
        raise LayoutError("identity component is too long")
    if RESERVED.fullmatch(service) or RESERVED.fullmatch(author_id):
        raise LayoutError("identity would create a Windows reserved directory name")
    return service, author_id, post_id


def _portable_key(path: str) -> str:
    return unicodedata.normalize("NFD", path).casefold()


def serial_width(post_count: int) -> int:
    """One extra leading-zero position beyond the decimal width of total posts."""
    if isinstance(post_count, bool) or not isinstance(post_count, int) or post_count < 1:
        raise LayoutError("post total must be a positive integer")
    return len(str(post_count)) + 1


def _segment(service: str, author_id: str, post_id: str,
             position: int, post_count: int) -> str:
    if isinstance(position, bool) or not isinstance(position, int) or position < 1:
        raise LayoutError("post sequence must be a positive integer")
    width = max(serial_width(post_count), len(str(position)) + 1)
    value = f"{post_id}_p{position:0{width}d}"
    budget = min(MAX_POST_COMPONENT_BYTES, MAX_POST_RELATIVE_BYTES - len(f"{service}/{author_id}/".encode()))
    if len(value.encode("utf-8")) > budget:
        raise LayoutError("post ID and sequence exceed path budget")
    return value


def _check_segment(segment: str) -> None:
    if (not segment or segment in (".", "..") or segment[-1] in " ."
            or any(x in INVALID_WINDOWS for x in segment)
            or any(unicodedata.category(x).startswith("C") for x in segment)
            or unicodedata.normalize("NFC", segment) != segment
            or "/" in segment):
        raise LayoutError("unsafe or noncanonical path component")
    if RESERVED.fullmatch(segment):
        raise LayoutError("reserved DOS device name")


@dataclass(frozen=True)
class PostPath:
    service: str
    author_id: str
    post_id: str
    folder: str

    @property
    def relative(self) -> str:
        return f"{self.service}/{self.author_id}/{self.folder}"

    def member(self, name: str) -> str:
        if name not in {"post.json", "snapshot.json", "listing.json", "detail.json", "content.html", "status.json", "offline.html"}:
            raise LayoutError("unrecognized post member name")
        path = f"{self.relative}/{name}"
        if len(path.encode("utf-8")) > MAX_MEMBER_RELATIVE_BYTES:
            raise LayoutError("post member exceeds portable path budget")
        return path

    def media_member(self, identity_path: str, *, preview: bool = False) -> str:
        """Deterministic post-local media path; identity_path must be a trusted path, not a URL."""
        if not isinstance(identity_path, str) or not identity_path.startswith("/"):
            raise LayoutError("media identity must be an absolute URL path")
        if "\\" in identity_path or "?" in identity_path or "#" in identity_path or "\x00" in identity_path:
            raise LayoutError("unsafe media path")
        from urllib.parse import unquote
        decoded = unquote(identity_path)
        if (not decoded.startswith("/") or "\\" in decoded or "?" in decoded or "#" in decoded
                or any(unicodedata.category(ch).startswith("C") for ch in decoded)
                or any(p in (".", "..") for p in decoded.split("/"))):
            raise LayoutError("unsafe or traversing media path")
        stem_name = PurePosixPath(decoded).name
        if not stem_name or stem_name in (".", ".."):
            raise LayoutError("missing media basename")
        ext = PurePosixPath(stem_name).suffix
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", ext):
            ext = ""
        short_stem = stem_name[:-len(ext)] if ext else stem_name
        short_stem = _clean_text(short_stem) or "file"
        subfolder = "previews" if preview else "media"
        sig = hashlib.sha256(decoded.encode("utf-8")).hexdigest()[:16]
        suffix = f"__{sig}{ext.lower()}"
        available = MAX_MEMBER_RELATIVE_BYTES - len(f"{self.relative}/{subfolder}/".encode()) - len(suffix.encode())
        if available < 1:
            raise LayoutError("no portable path space for media name")
        file_name = _utf8_truncate(short_stem, min(68, available)).rstrip(" ._") + suffix
        if len(file_name.encode()) > 180:
            raise LayoutError("media filename exceeds portable component budget")
        return f"{self.relative}/{subfolder}/{file_name}"


class FrozenLayout:
    """In-memory, append-only path registry. Persist it privately between runs."""

    def __init__(self) -> None:
        self._posts: Dict[Tuple[str, str, str], PostPath] = {}
        self._folded_paths: Dict[str, Tuple[str, str, str]] = {}
        self._authors: Dict[str, str] = {}

    def author(self, service: str, author_id: str) -> str:
        service, author_id, _ = _identity(service, author_id, "dummy")
        base = f"{service}/{author_id}"
        fold = _portable_key(base)
        other = self._authors.get(fold)
        if other is not None and other != base:
            raise LayoutError("case-insensitive author directory collision")
        self._authors[fold] = base
        return base

    def artist(self, record: Mapping[str, object]) -> str:
        """Register an artist without inventing non-favourite post targets."""
        if not isinstance(record, Mapping):
            raise LayoutError("artist record must be an object")
        return self.author(record.get("service"), record.get("id"))

    def plan(self, service: str, author_id: str, post_id: str,
             title: Optional[str] = None, *, position: Optional[int] = None,
             total_posts: Optional[int] = None) -> PostPath:
        identity = _identity(service, author_id, post_id)
        self.author(service, author_id)
        existing = self._posts.get(identity)
        if existing is not None:
            return existing  # First assigned post sequence is frozen across both modes.
        # The title stays in JSON and does not influence directory names.
        if position is None:
            position = len(self._posts) + 1
        if total_posts is None:
            total_posts = max(position, len(self._posts) + 1)
        serial_width(total_posts)
        if isinstance(position, bool) or not isinstance(position, int) or not 1 <= position <= total_posts:
            raise LayoutError("sequence must belong to its initial post total")
        # Case-insensitive post-ID collisions may require skipping a sequence.
        for candidate in range(position, position + len(self._posts) + 2):
            folder = _segment(service, author_id, post_id, candidate, total_posts)
            _check_segment(folder)
            target = PostPath(service, author_id, post_id, folder)
            key = _portable_key(target.relative)
            if key not in self._folded_paths:
                self._posts[identity] = target
                self._folded_paths[key] = identity
                return target
        raise LayoutError("portable post path collision cannot be resolved")

    def favourite_post(self, item: Mapping[str, object], *, position: Optional[int] = None,
                       total_posts: Optional[int] = None) -> PostPath:
        if not isinstance(item, Mapping):
            raise LayoutError("post record must be an object")
        return self.plan(item.get("service"), item.get("user"), item.get("id"),
                         item.get("title"), position=position, total_posts=total_posts)

    def artist_post(self, artist: Mapping[str, object], item: Mapping[str, object],
                    *, position: Optional[int] = None, total_posts: Optional[int] = None) -> PostPath:
        if not isinstance(artist, Mapping) or not isinstance(item, Mapping):
            raise LayoutError("artist/post must be objects")
        service, author_id, _ = _identity(artist.get("service"), artist.get("id"), "dummy")
        post_service, post_author, _ = _identity(item.get("service"), item.get("user"), item.get("id"))
        if (service, author_id) != (post_service, post_author):
            raise LayoutError("post identity differs from selected favourite artist")
        self.artist(artist)
        return self.favourite_post(item, position=position, total_posts=total_posts)

    def to_dict(self) -> dict:
        return {"schema_version": VERSION, "layout": LAYOUT_NAME,
                "posts": [{"service": x.service, "author_id": x.author_id,
                           "post_id": x.post_id, "folder": x.folder}
                          for x in sorted(self._posts.values(), key=lambda x:(x.service,x.author_id,x.post_id))],
                "authors": sorted(set(self._authors.values()))}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "FrozenLayout":
        if (not isinstance(data, dict) or data.get("schema_version") != VERSION
                or data.get("layout") != LAYOUT_NAME
                or not isinstance(data.get("posts"), list) or not isinstance(data.get("authors"), list)):
            raise LayoutError("invalid V2 registry")
        registry = cls()
        for author in data["authors"]:
            if not isinstance(author, str) or len(author.split("/")) != 2:
                raise LayoutError("invalid registered author")
            service, user = author.split("/")
            registry.author(service, user)
        for row in data["posts"]:
            if not isinstance(row, dict):
                raise LayoutError("invalid registered post")
            service, user, post_id = _identity(row.get("service"), row.get("author_id"), row.get("post_id"))
            folder = row.get("folder")
            if (not isinstance(folder, str) or not re.fullmatch(re.escape(post_id) + r"_p0[0-9]+", folder)
                    or int(folder.rsplit("_p", 1)[1]) < 1
                    or len(folder.encode()) > MAX_POST_COMPONENT_BYTES):
                raise LayoutError("invalid frozen post folder")
            _check_segment(folder)
            obj = PostPath(service, user, post_id, folder)
            if len(obj.relative.encode()) > MAX_POST_RELATIVE_BYTES:
                raise LayoutError("overlong frozen post path")
            identity = (service, user, post_id)
            if identity in registry._posts:
                raise LayoutError("duplicate frozen post identity")
            registry.author(service, user)
            portable = _portable_key(obj.relative)
            if portable in registry._folded_paths:
                raise LayoutError("frozen post path collision")
            registry._posts[identity] = obj
            registry._folded_paths[portable] = identity
        return registry


def inspect_zip(path: Path) -> dict:
    """Read only the two JSON members. Return aggregate counts; no private records."""
    with zipfile.ZipFile(path, "r") as z:
        expected = {"favorite_posts.json", "favorite_artists.json", "README.md"}
        if set(z.namelist()) != expected or len(z.namelist()) != len(expected):
            raise LayoutError("unexpected ZIP entries")
        if any(z.getinfo(name).file_size > MAX_SNAPSHOT_BYTES for name in expected):
            raise LayoutError("snapshot exceeds read budget")
        if z.testzip() is not None:
            raise LayoutError("ZIP CRC failure")
        posts = json.loads(z.read("favorite_posts.json"))
        artists = json.loads(z.read("favorite_artists.json"))
    if not isinstance(posts, list) or not isinstance(artists, list):
        raise LayoutError("JSON root must be a list")
    registry = FrozenLayout()
    for artist in artists:
        registry.artist(artist)
    for position, post in enumerate(posts, 1):
        registry.favourite_post(post, position=position, total_posts=len(posts))
    overlap = {(x.get("service"), x.get("id")) for x in artists}
    overlap_posts = sum((p["service"], p["user"]) in overlap for p in posts)
    actual = [registry.favourite_post(p) for p in posts]
    if len(actual) != len(posts):
        raise LayoutError("duplicate identities were collapsed")
    summary = {"artist_count": len(artists), "post_count": len(posts),
               "unique_paths": len({_portable_key(x.relative) for x in actual}),
               "overlap_post_count": overlap_posts,
               "post_sequence_width": serial_width(len(posts)) if posts else 0,
               "post_sequence_first": actual[0].folder if actual else None,
               "post_sequence_last": actual[-1].folder if actual else None,
               "max_post_folder_bytes": max((len(x.folder.encode()) for x in actual), default=0),
               "max_post_relative_bytes": max((len(x.relative.encode()) for x in actual), default=0),
               "media_path_smoke_test": all(len(x.media_member("/data/example/image.jpeg").encode()) <= MAX_MEMBER_RELATIVE_BYTES for x in actual)}
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only V2 folder layout dry run")
    parser.add_argument("--snapshot-zip", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect_zip(args.snapshot_zip), ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()