#!/usr/bin/env python3
# 待确认：真实 Favourite Artists 全量帖子的标题分布、永久路径注册表的私有存储位置。
# 歧义：首次归档时若各来源的标题不一致，以首次成功登记的标题作为冻结值；不承诺不同空白注册表独立规划结果一致。
# 后续研究：发布后的 registry 迁移、Windows 解压绝对路径长度、已存在旧版资产的媒体重用。
# 潜在优化：注册表较大时按作者分片；当前先保留标准库、可复核、确定性逻辑。
# 风险：目录名可能包含敏感标题；注册表是私有资产；不得未经授权提交到公开 Git 历史。
# 验证重点：真实快照、Unicode、DOS 保留名、大小写冲突、路径长度、冻结后标题变化、跨来源同一帖子复用。
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


def _sha_suffix(parts: Tuple[str, str, str]) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()[:12]


def _segment(service: str, author_id: str, post_id: str, title: Optional[str], suffix: str = "") -> str:
    prefix = f"[{post_id}]"
    capacity = min(MAX_POST_COMPONENT_BYTES, MAX_POST_RELATIVE_BYTES - len(f"{service}/{author_id}/".encode()))
    if len(prefix.encode()) + len(suffix.encode()) > capacity:
        raise LayoutError("relative path budget too small for complete post id")
    cleaned = _clean_text(title)
    budget = min(MAX_TITLE_BYTES, capacity - len(prefix.encode()) - len(suffix.encode()))
    cleaned = _utf8_truncate(cleaned, budget).rstrip(" ._")
    return prefix + cleaned + suffix


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

    def plan(self, service: str, author_id: str, post_id: str, title: Optional[str]) -> PostPath:
        identity = _identity(service, author_id, post_id)
        self.author(service, author_id)
        existing = self._posts.get(identity)
        if existing is not None:
            return existing  # Freeze beats a changed API title.
        for suffix in ("", "__" + _sha_suffix(identity)):
            folder = _segment(service, author_id, post_id, title, suffix)
            _check_segment(folder)
            target = PostPath(service, author_id, post_id, folder)
            key = _portable_key(target.relative)
            if key not in self._folded_paths:
                self._posts[identity] = target
                self._folded_paths[key] = identity
                return target
        raise LayoutError("portable post path collision cannot be resolved")

    def favourite_post(self, item: Mapping[str, object]) -> PostPath:
        if not isinstance(item, Mapping):
            raise LayoutError("post record must be an object")
        return self.plan(item.get("service"), item.get("user"), item.get("id"), item.get("title"))

    def artist_post(self, artist: Mapping[str, object], item: Mapping[str, object]) -> PostPath:
        if not isinstance(artist, Mapping) or not isinstance(item, Mapping):
            raise LayoutError("artist/post must be objects")
        service, author_id, _ = _identity(artist.get("service"), artist.get("id"), "dummy")
        post_service, post_author, _ = _identity(item.get("service"), item.get("user"), item.get("id"))
        if (service, author_id) != (post_service, post_author):
            raise LayoutError("post identity differs from selected favourite artist")
        self.artist(artist)
        return self.favourite_post(item)

    def to_dict(self) -> dict:
        return {"schema_version": VERSION, "layout": "service/author_id/[post_id]title",
                "posts": [{"service": x.service, "author_id": x.author_id,
                           "post_id": x.post_id, "folder": x.folder}
                          for x in sorted(self._posts.values(), key=lambda x:(x.service,x.author_id,x.post_id))],
                "authors": sorted(set(self._authors.values()))}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "FrozenLayout":
        if not isinstance(data, dict) or data.get("schema_version") != VERSION or not isinstance(data.get("posts"), list) or not isinstance(data.get("authors"), list):
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
            if not isinstance(folder, str) or not folder.startswith(f"[{post_id}]") or len(folder.encode()) > MAX_POST_COMPONENT_BYTES:
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
    for post in posts:
        registry.favourite_post(post)
    overlap = {(x.get("service"), x.get("id")) for x in artists}
    overlap_posts = sum((p["service"], p["user"]) in overlap for p in posts)
    actual = [registry.favourite_post(p) for p in posts]
    if len(actual) != len(posts):
        raise LayoutError("duplicate identities were collapsed")
    summary = {"artist_count": len(artists), "post_count": len(posts),
               "unique_paths": len({_portable_key(x.relative) for x in actual}),
               "overlap_post_count": overlap_posts,
               "title_cleaning_count": sum(_clean_text(p.get("title")) != (p.get("title") or "") for p in posts),
               "title_truncation_count": sum(
                   len(_clean_text(p.get("title")).encode("utf-8")) >
                   len(x.folder.removeprefix(f"[{x.post_id}]").encode("utf-8"))
                   for p, x in zip(posts, actual)
               ),
               "fallback_no_title": sum(x.folder == f"[{x.post_id}]" for x in actual),
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