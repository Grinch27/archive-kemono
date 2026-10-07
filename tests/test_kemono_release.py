# 待确认：真实API/runner行为；后续研究：云端小样本；潜在优化：更多服务样本。
# 风险：所有API、媒体及Release均模拟，不能据此宣称真实下载成功。
# 验证重点：1次/10秒与预览回退、失败去重和持久化、部分归档可解压、分卷重组、上传失败不发布；本地不下载媒体。
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

SPEC = importlib.util.spec_from_file_location("kemono_release", Path(__file__).resolve().parents[1] / "scripts/kemono_release.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


def post(number):
    return {"id": str(number), "service": "fanbox", "user": "56018056"}


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


    def test_default_user_agent_matches_workflow(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn("Chrome/138.0.7194.92", app.user_agent())
            workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/kemono-release.yml").read_text()
            self.assertIn(f'default: "{app.DEFAULT_UA}"', workflow)
            self.assertIn(f"inputs.user_agent || '{app.DEFAULT_UA}'", workflow)

    def test_file_attempts_timeout_cleanup_and_secret_isolation(self):
        with tempfile.TemporaryDirectory() as temp:
            def download(command, **kwargs):
                self.assertEqual(command[0], "curl")
                self.assertEqual(kwargs["timeout"], 10)
                self.assertEqual(command[command.index("--max-time") + 1], "10")
                self.assertEqual(command[command.index("--retry") + 1], "0")
                self.assertEqual(command[command.index("--noproxy") + 1], "*")
                self.assertNotIn("KEMONO_PASSWORD", kwargs["env"])
                self.assertNotIn("GH_TOKEN", kwargs["env"])
                self.assertNotIn("--cookie", command)
                target = Path(command[command.index("--output") + 1])
                self.assertFalse(target.exists())
                target.write_bytes(b"partial")
                raise subprocess.TimeoutExpired(command, 10)
            with patch.dict(os.environ, {"KEMONO_PASSWORD":"fixture", "GH_TOKEN":"fixture"}), patch.object(app.subprocess, "run", side_effect=download) as run:
                with self.assertRaises(app.DownloadFailure) as failure:
                    app.open_media("https://n1.kemono.cr/data/a/file", temp, 10)
                self.assertEqual(run.call_count, 1)
                self.assertEqual((failure.exception.code, failure.exception.attempts), (28, 1))
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_file_success_needs_one_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            attempts = []
            def download(command, **kwargs):
                target = Path(command[command.index("--output") + 1])
                self.assertFalse(target.exists())
                attempts.append(1)
                target.write_bytes(b"original")
                return SimpleNamespace(returncode=0)
            with patch.object(app.subprocess, "run", side_effect=download):
                reader = app.open_media("https://n1.kemono.cr/data/a/file", temp, 10)
                self.assertEqual(reader.read(), b"original")
                self.assertEqual(reader.offset, reader.size)
                reader.close()
            self.assertEqual(len(attempts), 1)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_disk_budget_prevents_download(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(app.shutil, "disk_usage") as usage, patch.object(app.subprocess, "run") as run:
            usage.return_value.free = 1
            with self.assertRaises(RuntimeError):
                app.open_media("https://n1.kemono.cr/data/a/file", temp, 10)
            run.assert_not_called()

    def test_partial_release_is_explicitly_marked(self):
        release = app.Release("fixture/repo", "fixture-tag")
        release.title = "fixture"
        with patch.object(release, "command") as command:
            release.publish("missing files", incomplete=True)
            args = command.call_args.args
            self.assertIn("--prerelease=true", args)
            self.assertIn("fixture · INCOMPLETE", args)

    def test_local_execution_is_blocked_before_network(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}), patch.object(app, "Api") as api:
            with self.assertRaises(RuntimeError):
                app.main()
            api.assert_not_called()

    def test_complete_archive_is_readable(self):
        self.exercise_main(fail=False)

    def test_gzip_selection_publishes_readable_archive(self):
        self.exercise_main(fail=False, compression="gzip")

    def test_failures_are_deduplicated_and_later_files_are_archived(self):
        self.exercise_main(fail=True)

    def test_failure_list_survives_archive_upload_error(self):
        self.exercise_main(fail=True, upload_error=True)

    def test_preview_fallback_is_archived_without_claiming_original_success(self):
        self.exercise_main(fail=True, fallback=True)

    def test_failed_preview_does_not_stop_later_files(self):
        self.exercise_main(fail=True, fallback=False)

    def test_missing_node_still_tries_preview(self):
        self.exercise_main(fail=True, fallback=True, missing_node=True)

    def test_original_success_does_not_download_preview(self):
        self.exercise_main(fail=False, fallback=True)

    def test_preview_rejects_html_and_accepts_image_header(self):
        with tempfile.TemporaryDirectory() as temp:
            for payload, valid in [(b"<html>bad gateway</html>", False), (b"\xff\xd8\xfffixture", True)]:
                def download(command, **kwargs):
                    Path(command[command.index("--output") + 1]).write_bytes(payload)
                    return SimpleNamespace(returncode=0)
                with patch.object(app.subprocess, "run", side_effect=download) as run:
                    if valid:
                        reader = app.open_media("https://img.kemono.cr/thumbnail/data/a/file.jpg", temp, 10, image_only=True)
                        self.assertEqual(reader.read(), payload)
                        reader.close()
                    else:
                        with self.assertRaises(app.DownloadFailure) as error:
                            app.open_media("https://img.kemono.cr/thumbnail/data/a/file.jpg", temp, 10, image_only=True)
                        self.assertEqual(error.exception.code, 65)
                    self.assertEqual(run.call_count, 1)
                self.assertEqual(list(Path(temp).iterdir()), [])

    def exercise_main(self, fail, compression="xz", upload_error=False, fallback=None, missing_node=False):
        events, assets, calls, published = [], {}, [], []
        class Api:
            def login(self): pass
            def get(self, path):
                if path.endswith("/profile"):
                    return {"service": "fanbox", "id": "56018056", "post_count": 2}
                if "/posts?" in path: return [post(1), post(2)]
                number = int(path.rsplit("/", 1)[1])
                return {"post": {**post(number), "content": "fixture body", "file": {"path": "/a/file.bin"},
                                 "attachments": [{"path":"/a/later.bin", "server":"https://n1.kemono.cr"}] if number == 2 else []},
                        "previews": [{"path":"/a/file.bin", "server":None if missing_node else "https://n1.kemono.cr",
                                      "type":"thumbnail" if fallback is not None else "other"}]}
        class Release:
            def __init__(self, repo, tag): pass
            def create(self, title): events.append("create")
            def upload(self, path):
                if upload_error: raise RuntimeError("fixture upload failure")
                events.append("upload")
                assets[path.name] = path.read_bytes()
            def publish(self, notes, incomplete=False):
                events.append("publish")
                published.append((notes, incomplete))
        class Reader(io.BytesIO):
            def __init__(self):
                super().__init__(b"original")
                self.size, self.offset = 8, 0
            def read(self, n=-1):
                value = super().read(n)
                self.offset += len(value)
                return value
        def reader(url, directory, part_bytes, image_only=False):
            calls.append(url)
            if fail and url.endswith("/file.bin") and (not image_only or fallback is False):
                raise app.DownloadFailure(28, 1)
            return Reader()
        with tempfile.TemporaryDirectory() as temp:
            env = {"GITHUB_ACTIONS":"true", "CREATOR_URL":"https://kemono.cr/fanbox/user/56018056",
                   "MAX_POSTS":"0", "PART_MB":"10", "GITHUB_REPOSITORY":"fixture/repo", "GH_TOKEN":"fixture",
                   "GITHUB_SHA":"fixture", "GITHUB_RUN_ID":"123", "GITHUB_RUN_ATTEMPT":"1", "RUNNER_TEMP":temp,
                   "COMPRESSION":compression}
            with patch.dict(os.environ, env), patch.object(app, "Api", Api), patch.object(app, "Release", Release), patch.object(app, "open_media", reader):
                if upload_error:
                    with self.assertRaises(RuntimeError): app.main()
                    self.assertNotIn("publish", events)
                    self.assertEqual(len(json.loads((Path(temp)/"failed-files.json").read_text())["files"]), 1)
                    return
                app.main()
            self.assertEqual(len(calls), 2 + int(fail and fallback is not None) - int(missing_node))
            self.assertEqual(sum("/thumbnail/data/" in url for url in calls), int(fail and fallback is not None))
            self.assertEqual(events[-1], "publish")
            self.assertEqual(published[0][1], fail)
            manifest = json.loads(assets["archive-manifest.json"])
            self.assertEqual(manifest["file_count"], 1 + int(not fail or fallback is True))
            self.assertEqual(manifest["original_bytes"], 8 if fail else 16)
            self.assertEqual(manifest["failed_file_count"], int(fail))
            self.assertEqual(manifest["complete"], not fail)
            self.assertEqual(manifest["media_complete"], not fail)
            parts = b"".join(assets[p["name"]] for p in manifest["assets"])
            with tarfile.open(fileobj=io.BytesIO(parts), mode="r:*") as archive:
                self.assertEqual(archive.extractfile("media/a/later.bin").read(), b"original")
                self.assertEqual(json.load(archive.extractfile("posts/2.json"))["post"]["content"], "fixture body")
                if fail:
                    failures = json.loads(assets["failed-files.json"])
                    self.assertEqual(json.load(archive.extractfile("metadata/failed-files.json")), failures)
                    item = failures["files"][0]
                    self.assertEqual((item["post_id"], item["attempts"], item["timeout_seconds"], item["exit_code"]), ("1", 0 if missing_node else 1, 10, 0 if missing_node else 28))
                    self.assertNotIn("media/a/file.bin", archive.getnames())
                    self.assertEqual(manifest["preview_file_count"], int(fallback is True))
                    self.assertEqual(manifest["preview_bytes"], 8 if fallback is True else 0)
                    if fallback is True:
                        self.assertEqual(archive.extractfile("previews/a/file.bin").read(), b"original")
                        self.assertEqual(item["preview_archive_path"], "previews/a/file.bin")
                        records = json.load(archive.extractfile("metadata/files.json"))
                        self.assertEqual(records[0]["kind"], "preview")
                    elif fallback is False:
                        self.assertEqual(item["preview_failure"]["exit_code"], 28)
                    self.assertIn("INCOMPLETE", published[0][0])
                else:
                    self.assertEqual(archive.extractfile("media/a/file.bin").read(), b"original")


if __name__ == "__main__":
    unittest.main()
