# 待确认：真实API/runner行为；后续研究：云端小样本；潜在优化：更多服务样本。
# 风险：所有API、媒体及Release均模拟，不能据此宣称真实下载成功。
# 验证重点：分页、路径/域名、Range恢复、分卷重组、上传校验与失败不发布；本地不下载媒体。
import gzip
import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request

SPEC = importlib.util.spec_from_file_location("kemono_release", Path(__file__).resolve().parents[1] / "scripts/kemono_release.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


def post(number):
    return {"id": str(number), "service": "fanbox", "user": "56018056"}


class Response(io.BytesIO):
    def __init__(self, body, headers, status=200):
        super().__init__(body)
        self.headers, self.status = headers, status


class Opener:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def open(self, request, timeout):
        self.requests.append(request)
        return self.responses.pop(0)


class ArchiveTests(unittest.TestCase):
    def test_url_and_paths(self):
        self.assertEqual(app.creator_parts("https://kemono.cr/fanbox/user/56018056"), ("fanbox", "56018056"))
        self.assertEqual(app.canonical_path("/data/a/b.jpg"), "/a/b.jpg")
        for path in ["/../secret", "/a/%2e%2e/secret", "/a\\b", "/a?token=x", "/", "relative"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                app.canonical_path(path)
        self.assertFalse(app.media_host("https://n1.kemono.cr.evil.invalid/file"))
        self.assertFalse(app.media_host("http://n1.kemono.cr/file"))
        self.assertFalse(app.media_host("https://user:password@n1.kemono.cr/file"))

    def test_image_nodes_from_previews_and_deduplication(self):
        detail = {"post": {"file": {"path": "/a/main.jpg"}, "attachments": [{"path": "/a/other.jpg"}]},
                  "attachments": [], "previews": [
                      {"path": "/a/main.jpg", "server": "https://n3.kemono.cr"},
                      {"path": "/a/other.jpg", "server": "https://n2.kemono.cr"}], "videos": []}
        refs = app.file_references(detail)
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0]["url"], "https://n3.kemono.cr/data/a/main.jpg")
        detail["previews"][0]["server"] = "https://evil.invalid"
        with self.assertRaises(RuntimeError):
            app.file_references(detail)

    def test_pagination_and_limit(self):
        calls = []
        class Api:
            def get(self, path):
                calls.append(path)
                offset = int(path.rsplit("=", 1)[1])
                return [post(i) for i in range(offset, min(offset + 50, 126))]
        self.assertEqual(len(app.enumerate_posts(Api(), "fanbox", "56018056", 0)), 126)
        self.assertEqual([x.rsplit("=", 1)[1] for x in calls], ["0", "50", "100"])
        calls.clear()
        self.assertEqual(len(app.enumerate_posts(Api(), "fanbox", "56018056", 3)), 3)
        self.assertEqual(len(calls), 1)

    def test_duplicate_pagination_stops(self):
        class Api:
            def get(self, path):
                return [post(i) for i in range(50)]
        with self.assertRaises(RuntimeError):
            app.enumerate_posts(Api(), "fanbox", "56018056", 0)

    def test_range_recovery_without_cookie(self):
        first = Response(b"abc", {"Content-Length": "6", "ETag": '"v1"'})
        second = Response(b"def", {"Content-Range": "bytes 3-5/6", "ETag": '"v1"'}, 206)
        opener = Opener(first, second)
        with patch.object(app.time, "sleep"):
            media = app.MediaReader("https://n1.kemono.cr/data/a/file", opener)
            self.assertEqual(media.read(6), b"abcdef")
        self.assertEqual(opener.requests[1].get_header("Range"), "bytes=3-")
        self.assertEqual(opener.requests[1].get_header("If-range"), '"v1"')
        self.assertTrue(all(r.get_header("Cookie") is None for r in opener.requests))
        media.close()

    def test_wrong_range_or_missing_length_stops(self):
        with self.assertRaises(RuntimeError):
            app.MediaReader("https://n1.kemono.cr/data/a/file", Opener(Response(b"abc", {})))
        opener = Opener(Response(b"abc", {"Content-Length": "6"}), Response(b"abcdef", {"Content-Length": "6"}))
        with patch.object(app.time, "sleep"), self.assertRaises(RuntimeError):
            app.MediaReader("https://n1.kemono.cr/data/a/file", opener).read(6)

    def test_tar_gzip_split_reassembles_exactly(self):
        parts, sizes = [], []
        payload = bytes(range(256)) * 200
        with tempfile.TemporaryDirectory() as temp:
            def upload(path):
                parts.append(path.read_bytes())
                sizes.append(path.stat().st_size)
            writer = app.PartWriter(temp, "fixture.tar.gz", 200, upload)
            with gzip.GzipFile(fileobj=writer, mode="wb", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|") as archive:
                    app.add_json(archive, "metadata/profile.json", {"post_count": 1})
                    entry = tarfile.TarInfo("media/fixture.bin")
                    entry.size = len(payload)
                    archive.addfile(entry, io.BytesIO(payload))
            writer.finish()
            self.assertGreater(len(parts), 1)
            self.assertTrue(all(size <= 200 for size in sizes))
            self.assertEqual(list(Path(temp).iterdir()), [])
            with tarfile.open(fileobj=io.BytesIO(b"".join(parts)), mode="r:gz") as archive:
                self.assertEqual(archive.extractfile("media/fixture.bin").read(), payload)
                self.assertEqual(json.load(archive.extractfile("metadata/profile.json")), {"post_count": 1})

    def test_exact_boundary_does_not_add_empty_part(self):
        with tempfile.TemporaryDirectory() as temp:
            sizes = []
            writer = app.PartWriter(temp, "fixture.tar.gz", 10, lambda p: sizes.append(p.stat().st_size))
            writer.write(b"a" * 20)
            writer.finish()
            self.assertEqual(sizes, [10, 10])
        with self.assertRaises(ValueError):
            app.PartWriter(".", "fixture", 2_000_000_000, lambda p: None)

    def test_failed_upload_retains_chunk_and_low_disk_stops(self):
        with tempfile.TemporaryDirectory() as temp:
            def fail(path):
                raise RuntimeError("fixture upload failure")
            writer = app.PartWriter(temp, "fixture", 10, fail)
            with self.assertRaises(RuntimeError):
                writer.write(b"a" * 10)
            self.assertEqual(writer.assets, [])
            self.assertTrue(writer.path.exists())
            with patch.object(app.shutil, "disk_usage") as usage:
                usage.return_value.free = 1
                with self.assertRaises(RuntimeError):
                    app.PartWriter(temp, "low-disk", 10, fail).write(b"a")

    def test_uploaded_asset_size_must_match(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.part00001"
            path.write_bytes(b"abc")
            release = app.Release("fixture/repo", "fixture-tag")
            release.id = "123"
            with patch.object(release, "command", side_effect=["", '{"size":2,"state":"uploaded"}']):
                with self.assertRaises(RuntimeError):
                    release.upload(path)

    def test_draft_creation_uses_returned_id_without_tag_lookup(self):
        release = app.Release("fixture/repo", "fixture-tag")
        response = {"id": 123, "draft": True, "tag_name": "fixture-tag"}
        with patch.dict(os.environ, {"GITHUB_SHA": "fixture"}), patch.object(app.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response))) as command:
            release.create("fixture release")
            self.assertEqual(release.id, "123")
            self.assertEqual(command.call_count, 1)
            args, kwargs = command.call_args
            self.assertEqual(args[0][:5], ["gh", "api", "--method", "POST", "repos/fixture/repo/releases"])
            self.assertTrue(json.loads(kwargs["input"])["draft"])

    def test_aria2_failure_clears_sparse_file_before_curl(self):
        with tempfile.TemporaryDirectory() as temp:
            probe = app.MediaReader("https://n1.kemono.cr/data/a/file", Opener(Response(b"original", {"Content-Length": "8"})))
            calls = []
            def run(command, **kwargs):
                calls.append(command[0])
                self.assertNotIn("KEMONO_PASSWORD", kwargs["env"])
                self.assertNotIn("GH_TOKEN", kwargs["env"])
                if command[0] == "aria2c":
                    directory = Path(next(x[6:] for x in command if x.startswith("--dir=")))
                    target = directory / "original.bin"
                    target.write_bytes(b"\0" * 8)
                    Path(str(target) + ".aria2").write_text("fixture control")
                    return SimpleNamespace(returncode=1)
                target = Path(command[command.index("--output") + 1])
                self.assertFalse(target.exists())
                self.assertFalse(Path(str(target) + ".aria2").exists())
                target.write_bytes(b"original")
                return SimpleNamespace(returncode=0)
            with patch.dict(os.environ, {"DOWNLOAD_MODE": "aria2-curl", "KEMONO_PASSWORD": "fixture", "GH_TOKEN": "fixture"}), patch.object(app, "MediaReader", return_value=probe) as stream, patch.object(app, "run_download", side_effect=run):
                reader = app.open_media("https://n1.kemono.cr/data/a/file", temp, 10)
                self.assertEqual(reader.read(), b"original")
                reader.close()
                stream.assert_not_called()
            self.assertEqual(calls, ["aria2c", "curl"])
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_aria2_success_skips_curl(self):
        with tempfile.TemporaryDirectory() as temp:
            probe = app.MediaReader("https://n1.kemono.cr/data/a/file", Opener(Response(b"original", {"Content-Length": "8"})))
            def run(command, **kwargs):
                self.assertEqual(command[0], "aria2c")
                directory = Path(next(x[6:] for x in command if x.startswith("--dir=")))
                (directory / "original.bin").write_bytes(b"original")
                return SimpleNamespace(returncode=0)
            with patch.dict(os.environ, {"DOWNLOAD_MODE": "aria2-curl"}), patch.object(app, "MediaReader", return_value=probe) as stream, patch.object(app, "run_download", side_effect=run) as command:
                reader = app.open_media("https://n1.kemono.cr/data/a/file", temp, 10)
                self.assertEqual(reader.read(), b"original")
                reader.close()
                self.assertEqual(command.call_count, 1)
                stream.assert_not_called()

    def test_changed_media_version_stops_resume(self):
        opener = Opener(Response(b"abc", {"Content-Length": "6", "ETag": '"v1"'}),
                        Response(b"def", {"Content-Range": "bytes 3-5/6", "ETag": '"v2"'}, 206))
        with patch.object(app.time, "sleep"), self.assertRaises(RuntimeError):
            app.MediaReader("https://n1.kemono.cr/data/a/file", opener).read(6)

    def test_aria2_timeout_uses_curl_and_curl_resumes(self):
        with tempfile.TemporaryDirectory() as temp:
            probe = app.MediaReader("https://n1.kemono.cr/data/a/file", Opener(Response(b"original", {"Content-Length": "8"})))
            curl_attempts = []
            def run(command, **kwargs):
                if command[0] == "aria2c":
                    raise subprocess.TimeoutExpired("aria2c", 1)
                target = Path(command[command.index("--output") + 1])
                self.assertIn("--continue-at", command)
                curl_attempts.append(command)
                if len(curl_attempts) == 1:
                    target.write_bytes(b"ori")
                    return SimpleNamespace(returncode=1)
                self.assertEqual(target.read_bytes(), b"ori")
                target.write_bytes(b"original")
                return SimpleNamespace(returncode=0)
            with patch.dict(os.environ, {"DOWNLOAD_MODE": "aria2-curl"}), patch.object(app, "MediaReader", return_value=probe), patch.object(app, "run_download", side_effect=run), patch.object(app.time, "sleep"):
                reader = app.open_media("https://n1.kemono.cr/data/a/file", temp, 10)
                self.assertEqual(reader.read(), b"original")
                reader.close()
            self.assertEqual(len(curl_attempts), 2)

    def test_large_original_streams_without_download_tools(self):
        with tempfile.TemporaryDirectory() as temp:
            probe = app.MediaReader("https://n1.kemono.cr/data/a/file", Opener(Response(b"original", {"Content-Length": "8"})))
            with patch.dict(os.environ, {"DOWNLOAD_MODE": "aria2-curl"}), patch.object(app, "MediaReader", return_value=probe), patch.object(app.shutil, "disk_usage") as usage, patch.object(app.subprocess, "run") as run:
                usage.return_value.free = 1
                reader = app.open_media("https://n1.kemono.cr/data/a/file", temp, 10)
                self.assertIs(reader, probe)
                self.assertEqual(reader.read(), b"original")
                reader.close()
                run.assert_not_called()

    def test_download_budget_switches_to_stream(self):
        with tempfile.TemporaryDirectory() as temp:
            probe = app.MediaReader("https://n1.kemono.cr/data/a/file", Opener(Response(b"original", {"Content-Length": "8"})))
            with patch.dict(os.environ, {"DOWNLOAD_MODE": "aria2-curl"}), patch.object(app, "MediaReader", return_value=probe), patch.object(app, "run_download", return_value=SimpleNamespace(returncode=-100)):
                reader = app.open_media("https://n1.kemono.cr/data/a/file", temp, 10)
                self.assertIs(reader, probe)
                self.assertEqual(list(Path(temp).iterdir()), [])
                reader.close()

    def test_local_execution_is_blocked_before_network(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}), patch.object(app, "Api") as api:
            with self.assertRaises(RuntimeError):
                app.main()
            api.assert_not_called()

    def test_publish_only_after_complete_archive(self):
        self.exercise_main(fail=False)

    def test_gzip_selection_publishes_readable_archive(self):
        self.exercise_main(fail=False, compression="gzip")

    def test_download_failure_does_not_publish(self):
        self.exercise_main(fail=True)

    def exercise_main(self, fail, compression="xz"):
        events, assets = [], {}
        detail = {"post": {**post(1), "content": "fixture body", "file": {"path": "/a/file.bin"}},
                  "previews": [{"path": "/a/file.bin", "server": "https://n1.kemono.cr"}]}
        class Api:
            def login(self): pass
            def get(self, path):
                if path.endswith("/profile"):
                    return {"service": "fanbox", "id": "56018056", "post_count": 1}
                if "/posts?" in path: return [post(1)]
                return detail
        class Release:
            def __init__(self, repo, tag): pass
            def create(self, title): events.append("create")
            def upload(self, path):
                events.append("upload")
                assets[path.name] = path.read_bytes()
            def publish(self, notes): events.append("publish")
        with tempfile.TemporaryDirectory() as temp:
            env = {"GITHUB_ACTIONS": "true", "CREATOR_URL": "https://kemono.cr/fanbox/user/56018056",
                   "MAX_POSTS": "0", "PART_MB": "10", "GITHUB_REPOSITORY": "fixture/repo", "GH_TOKEN": "fixture",
                   "GITHUB_SHA": "fixture", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "RUNNER_TEMP": temp,
                   "COMPRESSION": compression, "DOWNLOAD_MODE": "stream"}
            original_reader = app.MediaReader
            def reader(url):
                if fail: raise RuntimeError("fixture media failure")
                return original_reader(url, Opener(Response(b"original", {"Content-Length": "8"})))
            with patch.dict(os.environ, env), patch.object(app, "Api", Api), patch.object(app, "Release", Release), patch.object(app, "MediaReader", reader):
                if fail:
                    with self.assertRaises(RuntimeError): app.main()
                    self.assertNotIn("publish", events)
                else:
                    app.main()
                    self.assertEqual(events[-1], "publish")
                    manifest = json.loads(assets["archive-manifest.json"])
                    self.assertEqual(manifest["file_count"], 1)
                    self.assertEqual(manifest["original_bytes"], 8)
                    parts = b"".join(assets[p["name"]] for p in manifest["assets"])
                    with tarfile.open(fileobj=io.BytesIO(parts), mode="r:*") as archive:
                        self.assertEqual(archive.extractfile("media/a/file.bin").read(), b"original")
                        self.assertEqual(json.load(archive.extractfile("posts/1.json"))["post"]["content"], "fixture body")


if __name__ == "__main__":
    unittest.main()
