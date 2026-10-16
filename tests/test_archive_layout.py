# 待确认：全部 Favourite Artist 枚举帖子的未知标题、不同 service 的实际媒体文件名。
# 歧义：独立空白注册表无法协调互相冲突的标题；同一持久化注册表可以冻结规范名称。
# 后续研究：完整多卷重组、新旧状态索引迁移和最终云端产物路径核验。
# 潜在优化：真实大量作者和帖子对注册表加载及哈希冲突的压力测试。
# 风险：不得向公共输出复制真实收藏正文或标题；仅输出聚合测试计数。
# 验证重点：身份唯一、跨模式共享、恢复、Unicode、Windows 安全、媒体归属、未知数据失败、TAR 校验。

import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile
import sys

# Runs both beside the isolated prototype and when installed as tests/ under the repository.
HERE = Path(__file__).resolve().parent
SRC = HERE if (HERE / "archive_layout.py").is_file() else HERE.parent / "scripts"
sys.path.insert(0, str(SRC))
import archive_layout as layout

ZIP_PATH = os.environ.get("KEMONO_SNAPSHOT_ZIP")


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.registry = layout.FrozenLayout()

    def test_three_level_paths_and_no_title(self):
        p = self.registry.plan("fanbox", "123", "999", " hello / world ")
        self.assertEqual(p.relative, "fanbox/123/[999]hello _ world")
        self.assertEqual(self.registry.plan("fanbox", "123", "1000", None).relative, "fanbox/123/[1000]")
        self.assertEqual(p.member("content.html"), p.relative + "/content.html")

    def test_first_title_is_frozen_through_reload_and_artist_mode(self):
        first = self.registry.favourite_post({"service":"patreon", "user":"406", "id":"987", "title":"Original"})
        data = self.registry.to_dict()
        loaded = layout.FrozenLayout.from_dict(json.loads(json.dumps(data)))
        later = loaded.artist_post({"service":"patreon", "id":"406"},
                                   {"service":"patreon", "user":"406", "id":"987", "title":"Renamed and changed"})
        self.assertEqual(first, later)
        self.assertEqual(first.relative, "patreon/406/[987]Original")

    def test_windows_case_collisions_of_post_ids(self):
        a = self.registry.plan("fanbox", "user", "Ab", "A")
        b = self.registry.plan("fanbox", "user", "ab", "a")
        self.assertNotEqual(layout._portable_key(a.relative),layout._portable_key(b.relative))
        self.assertTrue(b.folder.startswith("[ab]"))
        self.assertIn("__", b.folder)
        loaded = layout.FrozenLayout.from_dict(self.registry.to_dict())
        self.assertEqual(loaded.plan("fanbox", "user", "ab", "later title"),b)

    def test_authors_with_case_collisions_stop(self):
        self.registry.author("fanbox", "Ab")
        with self.assertRaises(layout.LayoutError):
            self.registry.author("fanbox", "ab")

    def test_unicode_and_windows_filename_safety(self):
        x = self.registry.plan("fanbox","A1","B2", '  e\u0301 / < > : " \\ | ? * \u202etest.  ')
        self.assertIn("é",x.folder)
        self.assertNotIn("/",x.folder)
        self.assertTrue(x.folder.endswith("test"))
        self.assertEqual(layout._clean_text("\u200b"),"")
        with self.assertRaises(layout.LayoutError):
            self.registry.plan("fanbox","CON","1","bad")
        with self.assertRaises(layout.LayoutError):
            self.registry.plan("Fanbox","123","1","bad")

    def test_long_unicode_title_does_not_split_utf8(self):
        p = self.registry.plan("fantia", "A", "999", "图" * 200)
        self.assertLessEqual(len(p.folder.encode("utf-8")),layout.MAX_POST_COMPONENT_BYTES)
        self.assertLessEqual(len(p.relative.encode("utf-8")),layout.MAX_POST_RELATIVE_BYTES)
        self.assertEqual(p.folder, p.folder.encode("utf-8").decode("utf-8"))

    def test_very_long_identifier_is_rejected(self):
        with self.assertRaises(layout.LayoutError):
            self.registry.plan("fanbox", "A"*100, "1", "x")
        with self.assertRaises(layout.LayoutError):
            self.registry.plan("fanbox", "a", "A"*100, "x")

    def test_media_within_post_is_stable_across_modes(self):
        p = self.registry.plan("patreon", "12", "13", "Hello")
        q = self.registry.artist_post({"service":"patreon","id":"12"},
            {"service":"patreon","user":"12","id":"13","title":"Changed"})
        original = p.media_member("/data/a/b/image.PNG")
        self.assertEqual(original,q.media_member("/data/a/b/image.PNG"))
        self.assertTrue(original.startswith(p.relative+"/media/"))
        self.assertTrue(p.media_member("/data/a/b/image.PNG",preview=True).startswith(p.relative+"/previews/"))
        self.assertNotEqual(original,p.media_member("/data/a/b/image.PNG",preview=True))
        with self.assertRaises(layout.LayoutError):
            p.media_member("/data/../passwd")
        with self.assertRaises(layout.LayoutError):
            p.media_member("/data/with?token")
        with self.assertRaises(layout.LayoutError):
            p.media_member("/data/evil%5cname.jpg")

    def test_malformed_registry_and_duplicate_entry_fail(self):
        p=self.registry.plan("fanbox","123","456","Test")
        malformed = self.registry.to_dict()
        malformed["posts"][0]["folder"]="[456]../BAD"
        with self.assertRaises(layout.LayoutError):
            layout.FrozenLayout.from_dict(malformed)
        r=self.registry.to_dict()
        r["posts"].append(dict(r["posts"][0]))
        with self.assertRaises(layout.LayoutError):
            layout.FrozenLayout.from_dict(r)
        self.assertTrue(p.relative)

    def test_mismatched_artist_post_does_not_mutate_registry(self):
        with self.assertRaises(layout.LayoutError):
            self.registry.artist_post({"service":"fanbox","id":"1"},
                {"service":"fanbox","user":"2","id":"3","title":"wrong"})
        self.assertEqual(self.registry.to_dict()["posts"],[])
        self.assertEqual(self.registry.to_dict()["authors"],[])

    def test_artists_do_not_create_unrequested_posts(self):
        self.registry.artist({"service":"fanbox","id":"222","name":"Some Artist"})
        self.assertEqual(self.registry.to_dict()["posts"],[])

    def test_synthetic_tar_roundtrip(self):
        p = self.registry.plan("fanbox","222","12","A title")
        names=[p.member("post.json"),p.member("content.html"),p.member("status.json"),p.media_member("/data/f/file.jpg")]
        with tempfile.TemporaryFile() as f:
            with tarfile.open(fileobj=f,mode="w:gz") as tar:
                for name in names:
                    payload=(name+" test").encode()
                    info=tarfile.TarInfo(name)
                    info.size=len(payload)
                    tar.addfile(info,io.BytesIO(payload))
            f.seek(0)
            with tarfile.open(fileobj=f,mode="r:gz") as tar:
                self.assertEqual(tar.getnames(),names)
                for item in tar:
                    self.assertEqual(tar.extractfile(item).read(),(item.name+" test").encode())

    @unittest.skipUnless(ZIP_PATH and Path(ZIP_PATH).is_file(),"real ZIP fixture not supplied")
    def test_real_snapshot_and_cross_mode_intersection(self):
        with zipfile.ZipFile(ZIP_PATH,"r") as z:
            posts=json.loads(z.read("favorite_posts.json"))
            artists=json.loads(z.read("favorite_artists.json"))
        by_key={(x["service"],x["id"]):x for x in artists}
        reg=layout.FrozenLayout()
        for artist in artists:
            reg.artist(artist)
        paths=[reg.favourite_post(p) for p in posts]
        self.assertEqual(len(paths),len(posts))
        self.assertEqual(len({layout._portable_key(x.relative) for x in paths}),len(posts))
        reg2=layout.FrozenLayout.from_dict(reg.to_dict())
        intersection=0
        for p in posts:
            artist=by_key.get((p["service"],p["user"]))
            if artist:
                intersection+=1
                different=dict(p,title="different title after archiving")
                self.assertEqual(reg2.artist_post(artist,different),reg.favourite_post(p))
        self.assertGreater(intersection,0)
        self.assertEqual(layout.inspect_zip(Path(ZIP_PATH))["post_count"],len(posts))


if __name__ == "__main__":
    unittest.main()