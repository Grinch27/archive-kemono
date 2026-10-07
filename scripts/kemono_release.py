#!/usr/bin/env python3
# 待确认：云端登录与原文件可达性、作者总容量；只在GitHub Actions执行下载和发布。
# 后续研究：按失败清单补下；潜在优化：并行详情预取。
# 风险：每次完整下载限30秒，大文件可能失败；缺文件时发布明确标记的部分归档。
# 验证重点：直连、Cookie隔离、每文件最多3次、失败清单持久化、tar可解压、分卷<2GB；不算SHA256。

import gzip
import http.cookiejar
import io
import json
import lzma
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path, PurePosixPath

BASE = "https://kemono.cr"
PART_MAX = 1_900_000_000  # Also below decimal 2GB, not just GitHub's 2GiB.
TRANSIENT = {429, 500, 502, 503, 504}
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.7194.92 Safari/537.36"
FILE_ATTEMPTS, FILE_TIMEOUT = 3, 30


def user_agent():
    value = os.environ.get("USER_AGENT", DEFAULT_UA)
    if not value or "\r" in value or "\n" in value:
        raise ValueError("User-Agent为空或包含换行")
    return value


def mask(value):
    if value:
        safe = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::add-mask::" + safe, flush=True)


def media_host(url):
    p = urllib.parse.urlsplit(url)
    return (p.scheme == "https" and not p.username and not p.password
            and p.port in (None, 443)
            and bool(re.fullmatch(r"(?:n\d+\.|img\.)?kemono\.cr", p.hostname or "")))


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, media=False):
        self.media = media

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        p = urllib.parse.urlsplit(newurl)
        allowed = media_host(newurl) if self.media else p.scheme == "https" and p.netloc == "kemono.cr"
        if not allowed:
            raise RuntimeError("拒绝非官方或非HTTPS重定向")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def creator_parts(url):
    p = urllib.parse.urlsplit(url)
    match = re.fullmatch(r"/([a-z0-9_-]+)/user/([A-Za-z0-9_-]+)/?", p.path)
    if p.scheme != "https" or p.netloc != "kemono.cr" or p.query or p.fragment or not match:
        raise ValueError("作者URL必须是https://kemono.cr/{service}/user/{id}")
    return match.groups()


def canonical_path(path):
    if not isinstance(path, str):
        raise ValueError("文件路径不是字符串")
    decoded = urllib.parse.unquote(path)
    if not decoded.startswith("/") or "\\" in decoded or "\x00" in decoded or "?" in decoded or "#" in decoded:
        raise ValueError("文件路径不安全")
    if any(x in (".", "..") for x in decoded.split("/")):
        raise ValueError("拒绝目录穿越")
    if decoded.startswith("/data/"):
        decoded = decoded[5:]
    if not str(PurePosixPath(decoded)).lstrip("/"):
        raise ValueError("文件路径为空")
    return str(PurePosixPath(decoded))


def file_references(detail):
    post = detail["post"]
    resolved = {}
    for item in [*(detail.get("attachments") or []), *(detail.get("previews") or []), *(detail.get("videos") or [])]:
        if isinstance(item, dict) and item.get("path") and item.get("server"):
            resolved[canonical_path(item["path"])] = item["server"]
    refs = [post.get("file"), post.get("shared_file"), *(post.get("attachments") or []),
            *(detail.get("attachments") or []), *(detail.get("videos") or [])]
    result = {}
    for item in refs:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        path = canonical_path(item["path"])
        server = item.get("server") or resolved.get(path)
        if not server or not media_host(server):
            raise RuntimeError("原文件缺少可信下载节点")
        p = urllib.parse.urlsplit(server)
        if p.path not in ("", "/") or p.query or p.fragment:
            raise RuntimeError("下载节点不是可信基址")
        result[path] = {"path": path, "name": item.get("name"),
                        "url": server.rstrip("/") + "/data" + urllib.parse.quote(path, safe="/")}
    return list(result.values())


class Api:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), SafeRedirect(),
                                                  urllib.request.HTTPCookieProcessor(self.jar))
        self.next_request = 0.0

    def get(self, path, payload=None):
        headers = {"User-Agent": user_agent(), "Accept": "text/css", "Accept-Encoding": "identity"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = BASE
            data = json.dumps(payload).encode()
        for attempt in range(3):
            time.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + 0.5
            try:
                req = urllib.request.Request(BASE + "/api/v1" + path, data=data, headers=headers)
                with self.opener.open(req, timeout=60) as r:
                    raw = r.read(32_000_001)
                    if len(raw) > 32_000_000:
                        raise RuntimeError("JSON响应超过32MB")
                    encoding = r.headers.get("Content-Encoding", "identity").lower()
                    if raw[:2] == b"\x1f\x8b" or encoding == "gzip":
                        raw = gzip.decompress(raw)
                    elif encoding == "deflate":
                        raw = zlib.decompress(raw)
                    elif encoding not in ("", "identity"):
                        raise RuntimeError("不支持的JSON压缩编码")
                    return json.loads(raw)
            except urllib.error.HTTPError as e:
                code = e.code
                e.close()
                if code not in TRANSIENT or attempt == 2:
                    raise RuntimeError(f"API请求失败：HTTP {code}") from None
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise RuntimeError("API直连失败") from None
            time.sleep(2 ** attempt)

    def login(self):
        username = os.environ.get("KEMONO_USERNAME", "")
        password = os.environ.get("KEMONO_PASSWORD", "")
        if bool(username) != bool(password):
            raise RuntimeError("用户名和密码Secrets必须同时提供")
        if not username:
            print("未提供账号Secrets，使用公开作者API。", flush=True)
            return
        account = self.get("/authentication/login", {"username": username, "password": password})
        cookies = [c.value for c in self.jar if c.name == "session"]
        for value in cookies:
            mask(value)
        if not isinstance(account, dict) or account.get("username") != username or not cookies:
            raise RuntimeError("登录账号或session Cookie核验失败")
        print("账号登录成功；Cookie仅用于同源API，不写入归档。", flush=True)


def enumerate_posts(api, service, user, limit):
    posts, seen, offset = [], set(), 0
    while True:
        page = api.get(f"/{service}/user/{user}/posts?o={offset}")
        if not isinstance(page, list):
            raise RuntimeError("帖子分页响应不是数组")
        for post in page:
            if not isinstance(post, dict) or post.get("service") != service or post.get("user") != user:
                raise RuntimeError("分页返回了其他作者")
            key = post.get("id")
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", key) or key in seen:
                raise RuntimeError("分页帖子ID缺失、重复或不安全")
            seen.add(key)
            posts.append(post)
            if limit and len(posts) >= limit:
                return posts
        if len(page) < 50:
            return posts
        offset += 50


class DiskReader:
    def __init__(self, directory, path, size):
        self.directory, self.file, self.size, self.offset = directory, path.open("rb"), size, 0

    def read(self, length=-1):
        data = self.file.read(length)
        self.offset += len(data)
        return data

    def close(self):
        self.file.close()
        self.directory.cleanup()


class DownloadFailure(RuntimeError):
    def __init__(self, code, attempts):
        self.code, self.attempts = code, attempts
        super().__init__(f"curl exit {code}; {attempts} attempts exhausted")


def open_media(url, directory, part_bytes):
    if not media_host(url):
        raise ValueError("非官方下载地址")
    budget = shutil.disk_usage(directory).free - part_bytes - 256_000_000
    if budget <= 0:
        raise RuntimeError("runner磁盘不足，停止下载")
    staging = tempfile.TemporaryDirectory(prefix="original-", dir=directory)
    path = Path(staging.name) / "original.bin"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KEMONO_", "GH_", "GITHUB_"))}
    command = ["curl", "--disable", "--silent", "--show-error", "--fail", "--location",
               "--proto", "=https", "--proto-redir", "=https", "--noproxy", "*", "--retry", "0",
               "--connect-timeout", str(FILE_TIMEOUT), "--max-time", str(FILE_TIMEOUT),
               "--max-filesize", str(budget), "--user-agent", user_agent(),
               "--header", "Accept-Encoding: identity", "--output", str(path), url]
    try:
        for attempt in range(1, FILE_ATTEMPTS + 1):
            path.unlink(missing_ok=True)  # Fresh attempt; never mix incomplete file versions.
            print(f"curl原文件下载 {attempt}/{FILE_ATTEMPTS}，总时限{FILE_TIMEOUT}秒。", flush=True)
            try:
                result = subprocess.run(command, env=env, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, timeout=FILE_TIMEOUT)
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = 28
            if code == 0 and path.is_file():
                return DiskReader(staging, path, path.stat().st_size)
            print(f"原文件尝试{attempt}失败，curl退出码{code}。", flush=True)
        raise DownloadFailure(code, FILE_ATTEMPTS)
    except BaseException:
        staging.cleanup()
        raise


def save_failures(path, source, failures):
    data = {"source": source, "files": failures}
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)
    return data


class Release:
    def __init__(self, repo, tag):
        self.repo, self.tag, self.id = repo, tag, None
        self.title = ""

    def command(self, *args):
        for attempt in range(3):
            result = subprocess.run(["gh", *args], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
        status = re.search(r"HTTP (\d{3})", result.stderr or "")
        detail = f"HTTP {status.group(1)}" if status else f"exit {result.returncode}"
        raise RuntimeError(f"GitHub {args[0]}操作失败：{detail}；检查权限和网络")

    def create(self, title):
        # Each run has a new tag. Do not reuse or overwrite an existing Release.
        payload = {"tag_name": self.tag, "target_commitish": os.environ["GITHUB_SHA"],
                   "draft": True, "name": title, "body": "归档正在生成；草稿可能不完整。"}
        result = subprocess.run(["gh", "api", "--method", "POST", f"repos/{self.repo}/releases", "--input", "-"],
                                input=json.dumps(payload), capture_output=True, text=True)
        if result.returncode:
            status = re.search(r"HTTP (\d{3})", result.stderr or "")
            detail = f"HTTP {status.group(1)}" if status else f"exit {result.returncode}"
            raise RuntimeError(f"创建Release草稿失败：{detail}；未下载原文件")
        release = json.loads(result.stdout)
        if (type(release.get("id")) is not int or release.get("draft") is not True
                or release.get("tag_name") != self.tag):
            raise RuntimeError("创建Release草稿响应无效")
        self.id, self.title = str(release["id"]), title

    def upload(self, path):
        # Retry replacement is limited to assets of this newly created draft.
        if path.stat().st_size >= 2_000_000_000:
            raise RuntimeError("Release附件达到2GB，拒绝上传")
        self.command("release", "upload", self.tag, str(path), "--repo", self.repo, "--clobber")
        query = f'.[] | select(.name == {json.dumps(path.name)}) | {{size,state}}'
        raw = self.command("api", f"repos/{self.repo}/releases/{self.id}/assets",
                           "--paginate", "--jq", query)
        asset = json.loads(raw)
        if asset.get("state") != "uploaded" or asset.get("size") != path.stat().st_size:
            raise RuntimeError("Release附件状态或字节数不符")

    def publish(self, notes, incomplete=False):
        title = self.title + (" · INCOMPLETE" if incomplete else "")
        self.command("release", "edit", self.tag, "--repo", self.repo, "--notes", notes,
                     "--title", title, f"--prerelease={str(incomplete).lower()}", "--draft=false")


class PartWriter:
    def __init__(self, directory, stem, part_bytes, upload):
        if not 1 <= part_bytes <= PART_MAX:
            raise ValueError("分卷超过安全上限")
        self.directory, self.stem, self.part_bytes, self.upload = Path(directory), stem, part_bytes, upload
        self.file, self.path, self.size = None, None, 0
        self.assets = []

    def writable(self):
        return True

    def _start(self):
        if len(self.assets) >= 998:
            raise RuntimeError("达到998分卷上限，另保留manifest和失败清单附件名额")
        if shutil.disk_usage(self.directory).free < self.part_bytes + 128_000_000:
            raise RuntimeError("runner剩余磁盘不足一个分卷加128MB余量")
        self.path = self.directory / f"{self.stem}.part{len(self.assets) + 1:05d}"
        self.file = self.path.open("xb")
        self.size = 0

    def write(self, data):
        view = memoryview(data)
        length = len(view)
        while view:
            if self.file is None:
                self._start()
            take = min(len(view), self.part_bytes - self.size)
            self.file.write(view[:take])
            self.size += take
            view = view[take:]
            if self.size == self.part_bytes:
                self._finish_part()
        return length

    def _finish_part(self):
        self.file.close()
        self.file = None
        self.upload(self.path)
        self.assets.append({"name": self.path.name, "bytes": self.size})
        self.path.unlink()  # Only remove this run's temporary chunk after upload.
        print(f"已上传分卷{len(self.assets)}：{self.size}字节", flush=True)

    def finish(self):
        if self.file is not None:
            self._finish_part()

    def abort(self):
        if self.file is not None:
            self.file.close()
            self.file = None


def add_json(archive, name, value):
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), 0o644, 0
    archive.addfile(info, io.BytesIO(data))


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("原文件下载和Release发布仅允许在GitHub Actions运行")
    source = os.environ.get("CREATOR_URL", BASE + "/fanbox/user/56018056")
    service, user = creator_parts(source)
    try:
        limit, part_mb = int(os.environ.get("MAX_POSTS", "0")), int(os.environ.get("PART_MB", "1800"))
    except ValueError:
        raise ValueError("MAX_POSTS和PART_MB必须是整数") from None
    if limit < 0 or not 10 <= part_mb <= 1900:
        raise ValueError("MAX_POSTS须非负；PART_MB须在10–1900之间")
    compression = os.environ.get("COMPRESSION", "xz")
    if compression not in ("xz", "gzip"):
        raise ValueError("压缩格式无效")
    user_agent()
    repo = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not os.environ.get("GH_TOKEN"):
        raise RuntimeError("GitHub仓库或token配置无效")
    run, attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
    if not run.isdigit() or not attempt.isdigit():
        raise RuntimeError("运行标识无效")
    api = Api()
    api.login()
    profile = api.get(f"/{service}/user/{user}/profile")
    if not isinstance(profile, dict) or profile.get("service") != service or profile.get("id") != user:
        raise RuntimeError("作者profile标识不符")
    posts = enumerate_posts(api, service, user, limit)
    if not posts:
        raise RuntimeError("没有可归档帖子")
    if not limit and profile.get("post_count") != len(posts):
        raise RuntimeError("分页数量与profile不符，可能更新中；停止而不声明完整")
    tag = f"kemono-{service}-{user}-{run}-{attempt}"
    release = Release(repo, tag)
    release.create(f"{service}/{user} · {len(posts)} posts" + (" · sample" if limit else ""))
    print(f"Release草稿已创建：{tag}；准备{len(posts)}条帖子。", flush=True)
    with tempfile.TemporaryDirectory(prefix="kemono-release-", dir=os.environ["RUNNER_TEMP"]) as temp:
        extension = "xz" if compression == "xz" else "gz"
        stem = f"{service}-{user}.tar.{extension}"
        writer = PartWriter(temp, stem, part_mb * 1_000_000, release.upload)
        files, failures, seen = [], [], set()
        failure_path = Path(os.environ["RUNNER_TEMP"]) / "failed-files.json"
        try:
            compressor = (lzma.LZMAFile(writer, mode="w", preset=3) if compression == "xz"
                          else gzip.GzipFile(fileobj=writer, mode="wb", compresslevel=1, mtime=0))
            with compressor as compressed:
                with tarfile.open(fileobj=compressed, mode="w|") as archive:
                    add_json(archive, "metadata/profile.json", profile)
                    add_json(archive, "metadata/posts.json", posts)
                    for index, entry in enumerate(posts, 1):
                        print(f"读取帖子详情{index}/{len(posts)}。", flush=True)
                        detail = api.get(f"/{service}/user/{user}/post/{entry['id']}")
                        post = detail.get("post") if isinstance(detail, dict) else None
                        if not isinstance(post, dict) or (post.get("service"), post.get("user"), post.get("id")) != (service, user, entry["id"]):
                            raise RuntimeError("详情帖子标识不符")
                        add_json(archive, f"posts/{entry['id']}.json", detail)
                        for item in file_references(detail):
                            if item["path"] in seen:
                                continue
                            seen.add(item["path"])
                            print(f"准备原文件{len(seen)}，节点{urllib.parse.urlsplit(item['url']).hostname}。", flush=True)
                            try:
                                media = open_media(item["url"], temp, writer.part_bytes)
                            except DownloadFailure as error:
                                failures.append({**item, "post_id": entry["id"], "attempts": error.attempts,
                                                 "timeout_seconds": FILE_TIMEOUT, "exit_code": error.code})
                                save_failures(failure_path, source, failures)
                                print(f"::warning::原文件已记入失败清单；累计{len(failures)}个，继续归档。")
                                continue
                            try:
                                name = "media/" + item["path"].lstrip("/")
                                info = tarfile.TarInfo(name)
                                info.size, info.mode, info.mtime = media.size, 0o644, 0
                                archive.addfile(info, media)
                                if media.offset != media.size:
                                    raise RuntimeError("媒体读取长度不符")
                                files.append({**item, "archive_path": name, "bytes": media.size})
                            finally:
                                media.close()
                        print(f"帖子{index}/{len(posts)}；已归档{len(files)}个不同原文件。", flush=True)
                    add_json(archive, "metadata/files.json", files)
                    if failures:
                        add_json(archive, "metadata/failed-files.json", {"source": source, "files": failures})
            writer.finish()
            if failures:
                release.upload(failure_path)
            manifest = {"source": source, "post_count": len(posts), "file_count": len(files),
                        "original_bytes": sum(f["bytes"] for f in files), "limited_run": bool(limit),
                        "failed_file_count": len(failures), "media_complete": not failures,
                        "complete": not limit and not failures,
                        "format": f"Concatenate all numbered parts in order, then extract tar.{extension}",
                        "part_bytes_max": part_mb * 1_000_000, "assets": writer.assets}
            path = Path(temp) / "archive-manifest.json"
            path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            release.upload(path)
            notes = ((f"**INCOMPLETE: {len(failures)} original files failed. See failed-files.json.**\n\n" if failures else "")
                     + f"Source: {source}\n\nPosts: {len(posts)}; original files: {len(files)}; parts: {len(writer.assets)}.\n\n"
                     + ("Limited sample run.\n\n" if limit else "")
                     + "Download every numbered part. Combine before extracting (Linux/macOS):\n\n"
                     + f"```sh\ncat {stem}.part* > {stem}\ntar -xf {stem}\n```\n\n"
                     + f"Windows: `copy /b {stem}.part* {stem}`, then extract with 7-Zip.\n\n"
                     + "External embeds are retained as references. No credentials or cookies are included.")
            release.publish(notes, incomplete=bool(failures))
            print(f"::notice::Release已发布：https://github.com/{repo}/releases/tag/{tag}")
        finally:
            writer.abort()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never dump API bodies, authentication headers or raw subprocess stderr.
        if isinstance(error, (RuntimeError, ValueError)):
            print("::error::" + str(error).replace("\n", " ").replace("\r", " "), file=sys.stderr)
        else:
            print("::error::归档失败：" + type(error).__name__, file=sys.stderr)
        print("未完成归档的Release保持草稿；若失败发生在发布请求中，请核对远端状态。", file=sys.stderr)
        sys.exit(1)
