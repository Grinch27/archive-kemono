# 待确认：云端API与GitHub权限；后续研究：真实媒体格式和容量。
# 潜在优化：更多服务样本；风险：合成测试不证明媒体实际可达。
# 验证重点：历史集合、隐私、HTML引用、逐跳校验、单xz分卷、跨批次合并和失败不发布。
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import kemono_favorites as app


def post(number):
    return {"service": "fanbox", "user": "fixture", "id": str(number),
            "title": "合成标题", "content": "合成正文", "faved_seq": number,
            "attachments": [{"path": "/a/original.jpg"}], "file": {}}


class FavoritesTests(unittest.TestCase):
    def test_restore_newest_published_v2_registry_across_modes(self):
        frozen = app.layout.FrozenLayout()
        first = frozen.plan("fantia", "a1", "p1", "First title")
        releases = [
            {"tag_name": "kemono-favorites-staging-22-1", "draft": True,
             "id": 2, "assets": [{"name": "layout-registry.json"}]},
            {"tag_name": "kemono-fantia-a1-12-1", "draft": False,
             "id": 12, "assets": [{"name": "layout-registry.json"}]},
        ]
        with patch.object(app, "gh", return_value=releases), patch.object(
                app, "download_json_asset", return_value=frozen.to_dict()) as downloaded:
            restored = app.load_latest_layout()
        self.assertEqual(restored.plan("fantia", "a1", "p1", "Changed title"), first)
        downloaded.assert_called_once_with(12, "layout-registry.json")

    def test_baseline_accepts_additions_but_rejects_missing_changed_and_duplicate(self):
        source = post(1)
        baseline = {"posts": {app.digest(app.key(source)): app.digest(app.clean(source))}}
        self.assertEqual(len(app.validate_inventory([source, post(2)], baseline)), 2)
        changed = {**source, "content": "changed"}
        for targets in ([post(2)], [changed], [source, source]):
            with self.subTest(targets=len(targets)), self.assertRaises((RuntimeError, ValueError)):
                app.validate_inventory(targets, baseline)

    def test_private_keys_are_removed_recursively_without_modifying_source(self):
        source = {**post(1), "nested": [{"Token": "fixture", "keep": None}]}
        result = app.clean(source)
        self.assertNotIn("faved_seq", result)
        self.assertEqual(result["nested"], [{"keep": None}])
        self.assertIn("faved_seq", source)

    def test_api_additional_fields_preserve_exact_historical_snapshot(self):
        source = post(1)
        baseline = {"posts": {app.digest(app.key(source)): app.digest(app.clean(source))},
                    "field_hashes": {k: "unused" for k in app.clean(source)}}
        expanded = {**source, "api_only_field": "new version metadata"}
        self.assertEqual(app.validate_inventory([expanded], baseline), [app.clean(source)])
        expanded["content"] = "changed historical text"
        with self.assertRaises(RuntimeError):
            app.validate_inventory([expanded], baseline)

    def test_html_media_and_trusted_nodes(self):
        source = post(1)
        source["content"] = ('<img src="https://downloads.fanbox.cc/example.jpg">'
                             '<img data-src="/a/original.jpg" srcset="/a/b.jpg 1x, /a/c.jpg 2x">'
                             '<video poster="/a/poster.jpg"><source src="/a/video.mp4"></video>'
                             '<a href="https://example.invalid/ordinary">link</a>')
        detail = {"post": {}, "previews": [{"path": "/a/original.jpg", "type": "thumbnail", "server": "https://n1.kemono.cr"}]}
        refs = app.references(source, detail)
        self.assertEqual(refs[0]["url"], "https://n1.kemono.cr/data/a/original.jpg")
        self.assertIn("preview_url", refs[0])
        self.assertTrue(any(r["state"] == "unsupported_source" for r in refs))
        self.assertTrue(any(r["raw"] == "/a/video.mp4" for r in refs))
        self.assertFalse(any("ordinary" in r["raw"] for r in refs))

    def test_unsafe_paths_and_preview_urls_are_not_originals(self):
        source = {**post(1), "attachments": [{"path": "/../secret"}],
                  "content": '<img src="https://img.kemono.cr/thumbnail/data/a/image.jpg">'}
        refs = app.references(source, {})
        self.assertEqual(refs[0]["state"], "unsafe_reference")
        self.assertIsNone(refs[1]["url"])
        self.assertIn("preview_url", refs[1])

    def test_redirect_is_validated_before_second_request_and_tokens_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            def curl(args, **kwargs):
                self.assertNotIn("--location", args)
                self.assertNotIn("GH_TOKEN", kwargs["env"])
                self.assertNotIn("KEMONO_PASSWORD", kwargs["env"])
                header = Path(args[args.index("--dump-header") + 1])
                header.write_text("HTTP/1.1 302 Found\r\nLocation: https://evil.invalid/a.jpg\r\n\r\n")
                return SimpleNamespace(returncode=0, stdout=b"302")
            with patch.dict(os.environ, {"GH_TOKEN": "fixture", "KEMONO_PASSWORD": "fixture"}), patch.object(app.subprocess, "run", side_effect=curl) as run:
                with self.assertRaises(app.base.DownloadFailure):
                    with app.download("https://n1.kemono.cr/a.jpg", directory):
                        self.fail("unsafe redirect accepted")
                self.assertEqual(run.call_count, 1)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_original_rejects_html_and_empty_body(self):
        for body in (b"<html>error</html>", b""):
            with self.subTest(size=len(body)), tempfile.TemporaryDirectory() as directory:
                def curl(args, **kwargs):
                    Path(args[args.index("--dump-header") + 1]).write_text("HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n")
                    Path(args[args.index("--output") + 1]).write_bytes(body)
                    return SimpleNamespace(returncode=0, stdout=b"200")
                with patch.object(app.subprocess, "run", side_effect=curl), self.assertRaises(app.base.DownloadFailure):
                    with app.download("https://n1.kemono.cr/a.jpg", directory):
                        self.fail("bad original accepted")

    def test_tar_dedup_conflict_and_paths(self):
        seen = {}
        with io.BytesIO() as result:
            with tarfile.open(fileobj=result, mode="w|") as archive:
                item = tarfile.TarInfo("media/fixture")
                item.size = 3
                app.copy_member(archive, item, io.BytesIO(b"abc"), seen)
                app.copy_member(archive, item, io.BytesIO(b"abc"), seen)
                with self.assertRaises(RuntimeError):
                    app.copy_member(archive, item, io.BytesIO(b"def"), seen)
            result.seek(0)
            with tarfile.open(fileobj=result) as archive:
                self.assertEqual(archive.getnames(), ["media/fixture"])
        for name in ("../x", "/abs", "a\\b", "a\nx"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                app.safe_member(tarfile.TarInfo(name))

    def test_no_network_from_local_main(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}), patch.object(sys, "argv", ["app", "inventory"]), patch.object(app, "inventory") as inventory:
            with self.assertRaises(RuntimeError):
                app.main()
            inventory.assert_not_called()

    def test_paginated_assets_support_older_packaged_gh(self):
        rows = b'{"id":1,"name":"first"}\n{"id":2,"name":"second"}\n'
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "fixture/repo"}), patch.object(app, "gh", return_value=rows) as gh:
            self.assertEqual([a["id"] for a in app.assets(123)], [1, 2])
            self.assertNotIn("--slurp", gh.call_args.args)

    def test_cli_errors_show_only_safe_status(self):
        result = SimpleNamespace(returncode=1, stderr=b'private-value unknown flag: --slurp', stdout=b'')
        with patch.object(app.subprocess, "run", return_value=result), self.assertRaises(RuntimeError) as error:
            app.gh("api", "fixture")
        self.assertIn("unknown flag: --slurp", str(error.exception))
        self.assertNotIn("private-value", str(error.exception))

    def test_upload_failure_retains_part(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "PART_BYTES", 30), patch.object(app, "upload", side_effect=RuntimeError("synthetic upload failure")):
            with self.assertRaises(RuntimeError):
                app.write_part_archive(directory, "batch.tar.gz", "tag", 1,
                                       lambda archive: app.add_bytes(archive, "data", b"abc"))
            self.assertTrue(list(Path(directory).glob("*.part*")))

    def test_full_synthetic_pipeline_two_batches_single_extreme_xz(self):
        targets = [app.clean(post(i)) for i in range(3)]
        store, payloads, next_id, publications = {}, {}, [10], []
        def upload(tag, release_id, path):
            next_id[0] += 1
            data = Path(path).read_bytes()
            item = {"id": next_id[0], "name": Path(path).name, "size": len(data), "state": "uploaded"}
            store.setdefault(release_id, []).append(item)
            payloads[item["id"]] = data
            return item
        def read_json(release_id, name):
            if name == "targets.json":
                return targets
            if name == "layout-registry.json":
                registry = app.layout.FrozenLayout()
                for target in targets:
                    registry.favourite_post(target)
                return registry.to_dict()
            return json.loads(next(payloads[a["id"]] for a in store[release_id] if a["name"] == name))
        class Api:
            def get(self, path):
                number = int(path.rsplit("/", 1)[1])
                return {"post": targets[number], "previews": [{"path": "/a/original.jpg", "server": "https://n1.kemono.cr"}]}
        class Reader(io.BytesIO):
            def __init__(self, parts):
                self.parts, self.position = parts, 0
                super().__init__(b"".join(payloads[p["id"]] for p in parts))
            def read(self, size=-1):
                value = super().read(size)
                if self.tell() == len(self.getbuffer()):
                    self.position = len(self.parts)
                return value
        @contextlib.contextmanager
        def download(url, directory, image_only=False):
            path = Path(directory) / "fixture-media"
            path.write_bytes(b"\xff\xd8\xfffixture")
            yield path
            path.unlink()
        def github(*args, **kwargs):
            publications.append(args)
            return {}
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            for target, value in (("BATCH_SIZE", 2), ("PART_BYTES", 200), ("upload", upload),
                                  ("download_json_asset", read_json), ("PartReader", Reader),
                                  ("assets", lambda release_id: store.get(release_id, [])),
                                  ("create_draft", lambda *args: 2), ("gh", github), ("download", download)):
                stack.enter_context(patch.object(app, target, value))
            stack.enter_context(patch.object(app.base, "Api", Api))
            stack.enter_context(patch.dict(os.environ, {"RUNNER_TEMP": temp, "GITHUB_OUTPUT": str(Path(temp) / "outputs"), "GITHUB_REPOSITORY": "fixture/repo"}))
            app.batch(0, 1, "stage")
            app.batch(1, 1, "stage")
            app.assemble(1, "kemono-favorites-staging-fixture")
            manifest = read_json(2, "archive-manifest.json")
            self.assertTrue(manifest["verified"])
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["target_union_total"], 3)
            self.assertGreaterEqual(len(manifest["assets"]), 3)
            self.assertTrue(all(0 < p["size"] <= 200 for p in manifest["assets"]))
            compressed = b"".join(payloads[p["id"]] for p in manifest["assets"])
            with tarfile.open(fileobj=io.BytesIO(compressed), mode="r:xz") as archive:
                self.assertEqual(sum("/media/" in n for n in archive.getnames()), 3)
                self.assertEqual(sum(n.endswith("/snapshot.json") for n in archive.getnames()), 3)
                self.assertEqual(archive.extractfile("fanbox/fixture/[0]合成标题/content.html").read().decode(), "合成正文")
                self.assertNotIn(b"faved_seq", archive.extractfile("fanbox/fixture/[0]合成标题/snapshot.json").read())
            self.assertEqual(len(publications), 1)
            self.assertIn("draft=false", publications[0])


if __name__ == "__main__":
    unittest.main()
