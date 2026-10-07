# 待确认：真实API/runner行为；后续研究：云端小样本；潜在优化：更多服务样本。
# 风险：所有API、媒体及Release均模拟，不能据此宣称真实下载成功。
# 验证重点：1次/3秒与预览回退、失败去重和持久化、部分归档可解压、分卷重组、上传失败不发布；本地不下载媒体。
import gzip
import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path
import tarfile
import tempfile
import textwrap
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

    def test_handoff_requires_matching_workflow_acknowledgment(self):
        for response, valid in [("fixture.part00001\n", True), ("", False), ("wrong\n", False)]:
            with patch.object(app.sys, "stdin", io.StringIO(response)), patch.object(app.sys, "stdout", io.StringIO()) as output:
                if valid:
                    app.handoff(Path("fixture.part00001"))
                else:
                    with self.assertRaises(RuntimeError): app.handoff(Path("fixture.part00001"))
                self.assertEqual(output.getvalue(), "ARCHIVE_PART=fixture.part00001\n")

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
                self.assertEqual(kwargs["timeout"], 3)
                self.assertEqual(command[command.index("--max-time") + 1], "3")
                self.assertEqual(command[command.index("--retry") + 1], "0")
                self.assertEqual(command[command.index("--noproxy") + 1], "*")
                self.assertNotIn("KEMONO_PASSWORD", kwargs["env"])
                self.assertNotIn("GH_TOKEN", kwargs["env"])
                self.assertNotIn("--cookie", command)
                target = Path(command[command.index("--output") + 1])
                self.assertFalse(target.exists())
                target.write_bytes(b"partial")
                raise subprocess.TimeoutExpired(command, 3)
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

    def test_local_execution_is_blocked_before_network(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}), patch.object(app, "Api") as api:
            with self.assertRaises(RuntimeError):
                app.main()
            api.assert_not_called()

    def test_complete_archive_is_readable(self):
        self.exercise_main(fail=False)

    def test_gzip_selection_generates_readable_archive(self):
        self.exercise_main(fail=False, compression="gzip")

    def test_failures_are_deduplicated_and_later_files_are_archived(self):
        self.exercise_main(fail=True)

    def test_failure_list_survives_workflow_handoff_error(self):
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

    def test_workflow_release_streaming_and_token_isolation(self):
        self.exercise_workflow()

    def test_workflow_upload_failure_stops_before_publish(self):
        self.exercise_workflow(mode="upload_failure")

    def test_workflow_size_mismatch_stops_before_publish(self):
        self.exercise_workflow(mode="size_mismatch")

    def test_workflow_processing_failure_stops_before_publish(self):
        self.exercise_workflow(mode="producer_failure")

    def exercise_workflow(self, mode="success"):
        workflow = (Path(__file__).resolve().parents[1]/".github/workflows/kemono-release.yml").read_text()
        def run_block(name):
            step = workflow.split("      - name: " + name + "\n", 1)[1].split("\n      - name:", 1)[0]
            return textwrap.dedent(step.split("        run: |\n", 1)[1])
        script = run_block("Create Release draft") + '\nset -a\nsource "$GITHUB_ENV"\nset +a\n'
        script += run_block("Process archive and upload ready parts") + "\n"
        script += run_block("Upload metadata and publish Release")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/"scripts").mkdir()
            (root/"bin").mkdir()
            producer = r'''
import os, sys
from pathlib import Path
assert "GH_TOKEN" not in os.environ and "GITHUB_TOKEN" not in os.environ
if os.environ["FIXTURE_MODE"] == "producer_failure": sys.exit(37)
folder = Path(os.environ["ARCHIVE_DIR"])
folder.mkdir()
for number in (1, 2):
    assert not list(folder.glob("*.part*"))
    path = folder / f"patreon-4068015.tar.xz.part{number:05d}"
    path.write_bytes(b"fixture")
    print("ARCHIVE_PART=" + path.name, flush=True)
    assert sys.stdin.readline().strip() == path.name
    path.unlink()
(folder/"archive-manifest.json").write_text("{}")
(folder/"release-notes.md").write_text("fixture notes")
(Path(os.environ["RUNNER_TEMP"])/"failed-files.json").write_text("{}")
'''
            (root/"scripts/kemono_release.py").write_text(textwrap.dedent(producer))
            gh = root/"bin/gh"
            gh.write_text("#!" + app.sys.executable + "\n" + textwrap.dedent(r'''
import json, os, re, sys
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ["RUNNER_TEMP"])
with (root/"gh.log").open("a") as log: log.write(json.dumps(args) + "\n")
if "--method" in args:
    print("321")
elif args[:2] == ["release", "upload"]:
    if os.environ["FIXTURE_MODE"] == "upload_failure": sys.exit(9)
    state_path = root/"state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    for value in args[3:]:
        if value == "--clobber": continue
        path = Path(value)
        state[path.name] = path.stat().st_size
    state_path.write_text(json.dumps(state))
elif args[0] == "api":
    name = re.search(r'\.name == "([^"]+)"', args[-1]).group(1)
    size = json.loads((root/"state.json").read_text())[name]
    if os.environ["FIXTURE_MODE"] == "size_mismatch": size += 1
    print(f"uploaded:{size}")
'''))
            gh.chmod(0o700)
            env = {**os.environ, "PATH":str(root/"bin") + ":" + os.environ["PATH"],
                   "RUNNER_TEMP":temp, "ARCHIVE_DIR":str(root/"kemono-archive"), "GH_TOKEN":"fixture", "GITHUB_TOKEN":"fixture",
                   "GH_REPO":"fixture/repo", "CREATOR_URL":"https://kemono.cr/patreon/user/4068015",
                   "GITHUB_RUN_ID":"123", "GITHUB_RUN_ATTEMPT":"1", "GITHUB_SHA":"fixture",
                   "GITHUB_ENV":str(root/"env"), "GITHUB_OUTPUT":str(root/"outputs"),
                   "RELEASE_TITLE":"fixture · INCOMPLETE", "INCOMPLETE":"true", "FIXTURE_MODE":mode}
            result = subprocess.run(["bash", "-c", script], cwd=root, env=env, capture_output=True, text=True, timeout=10)
            calls = [json.loads(line) for line in (root/"gh.log").read_text().splitlines()]
            edits = [call for call in calls if call[:2] == ["release", "edit"]]
            if mode == "success":
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(edits), 1)
                self.assertIn("--prerelease=true", edits[0])
                self.assertIn("--draft=false", edits[0])
                self.assertEqual(len(json.loads((root/"state.json").read_text())), 4)
            else:
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(edits, [])
                if mode != "producer_failure":
                    self.assertTrue(list((root/"kemono-archive").glob("*.part*")))

    def exercise_main(self, fail, compression="xz", upload_error=False, fallback=None, missing_node=False):
        assets, calls = {}, []
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
        def ready(path):
            if upload_error: raise RuntimeError("fixture handoff failure")
            assets[path.name] = path.read_bytes()
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
                   "MAX_POSTS":"0", "PART_MB":"10", "RUNNER_TEMP":temp, "GITHUB_OUTPUT":str(Path(temp)/"outputs"),
                   "COMPRESSION":compression}
            with patch.dict(os.environ, env), patch.object(app, "Api", Api), patch.object(app, "handoff", ready), patch.object(app, "open_media", reader):
                if upload_error:
                    with self.assertRaises(RuntimeError): app.main()
                    self.assertFalse((Path(temp)/"kemono-archive/archive-manifest.json").exists())
                    self.assertEqual(len(json.loads((Path(temp)/"failed-files.json").read_text())["files"]), 1)
                    return
                app.main()
            self.assertEqual(len(calls), 2 + int(fail and fallback is not None) - int(missing_node))
            self.assertEqual(sum("/thumbnail/data/" in url for url in calls), int(fail and fallback is not None))
            result_dir = Path(temp)/"kemono-archive"
            manifest = json.loads((result_dir/"archive-manifest.json").read_text())
            notes = (result_dir/"release-notes.md").read_text()
            self.assertIn(f"incomplete={str(fail).lower()}", (Path(temp)/"outputs").read_text())
            self.assertFalse(hasattr(app, "Release"))
            self.assertFalse(list(result_dir.glob("*.part*")))
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
                    failures = json.loads((Path(temp)/"failed-files.json").read_text())
                    self.assertEqual(json.load(archive.extractfile("metadata/failed-files.json")), failures)
                    item = failures["files"][0]
                    self.assertEqual((item["post_id"], item["attempts"], item["timeout_seconds"], item["exit_code"]), ("1", 0 if missing_node else 1, 3, 0 if missing_node else 28))
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
                    self.assertIn("INCOMPLETE", notes)
                else:
                    self.assertEqual(archive.extractfile("media/a/file.bin").read(), b"original")


if __name__ == "__main__":
    unittest.main()
