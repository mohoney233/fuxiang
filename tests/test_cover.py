# -*- coding: utf-8 -*-
"""地图封面（COVER）层的离线回归 —— 不接触游戏进程。

锁住的是会静默写坏东西或误上送的那部分：
  * 槽位越界 / 路径不在 doc_dir 下 / 文件不存在 —— 都必须拒，不能默默写；
  * dry_run 必须**在任何写入或上送之前**返回（否则"预演"会真改到封面）；
  * FilePickerProxy.get_file_md5 是类级函数（签名只有 (path)）：按实例调会 TypeError，
    本模块踩过一次（2026-09-30），这里用假类把这条形状钉住；
  * 上传回执只认 sharing_type == UGC_MAP_COVER_PHOTO，别的照片不能混进来；
  * apply 前 URL 形态校验（空串/非 http 不许写进地图记录）；
  * 文档里写的 op 数量等于 OPS 里真实的 cover_* 数量（同 test_doc_counts 的动机）。
"""
import ast
import os
import re
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))

import fuxiang_cover as FC        # noqa: E402

DOC = os.path.join(ROOT, "COVER.md")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 假对象：只造到 op 真正摸到的那一层
# ---------------------------------------------------------------------------
GK = types.SimpleNamespace(g_ui_mgr=None, g_avatar=None)


class _RoomBase:
    def __init__(self, doc_dir):
        self.scene_photo_path = []
        self.scene_photo_md5 = []
        self.ugc_room_cover = None
        self.scene_photo_upload_flag = False
        self.ugc_key = "abc123"
        self.calls = []
        self.doc_dir = doc_dir

    def create_unit_with_info(self):        # __globals__ 里带 GK，_gk() 就是这么取的
        return None

    def set_photo_path(self, p, force_index=None):
        self.calls.append(("set_photo_path", p, force_index))
        self._put(self.scene_photo_path, p, force_index)

    def set_photo_md5(self, m, force_index=None):
        self.calls.append(("set_photo_md5", m, force_index))
        self._put(self.scene_photo_md5, m, force_index)

    @staticmethod
    def _put(lst, val, idx):
        if idx is None:
            lst.append(val)
            return
        while len(lst) <= idx:
            lst.append(None)
        lst[idx] = val


# _room() 是按 type(room).__name__ == "WorldBattleUGCEdit" 判"在不在编辑器"的，
# 所以假房间的类型名必须真叫这个（class 体里写 __name__ 不算数，得用 type() 造）。
FakeRoom = type("WorldBattleUGCEdit", (_RoomBase,), {})


class FakeAvatar:
    def __init__(self):
        self.unique_id = "42"
        self.uploads = []

    def get_map_verify_id(self, uuid):
        return "%s_1" % uuid

    def upload_img_to_filepicker(self, sharing_type, img_name, furniture_id=None,
                                 expire_ts=None, hall_keeper=False, md5="",
                                 other_info=None, show_tips=True):
        self.uploads.append((sharing_type, img_name, md5, other_info))

    def after_upload_photo(self, info, sharing_type, share_url, img_name, save_hall=False):
        return "orig"

    def set_ugc_map_cover_photo(self, uuid, photo_lst):
        self.rpc = (uuid, list(photo_lst))


class FakeEdit:
    def __init__(self, room):
        self._room = room

    def get_room(self):
        return self._room

    def smart_reload(self, name):
        return sys.modules[name] if name in sys.modules else __import__(name)

    @staticmethod
    def _safe(v):
        return v


class _Recorder:
    """假 FilePickerProxy：get_file_md5 是类上的普通函数（只收 path）。

    游戏里就是这样：类上取是 function、实例上取会绑 self ⇒ 按实例调必 TypeError。
    """

    def get_file_md5(path):
        return "md5of" + os.path.basename(path)

    def get_file_size_limit(self, sharing_type, other_info=None):
        return [0, 12374016]


def _install_fake_filepicker():
    pkg = types.ModuleType("common_cs")
    mod = types.ModuleType("common_cs.FilePickerProxy")
    mod.FilePickerProxy = _Recorder
    si = types.ModuleType("common_cs.ServerInfo")

    class _SD:
        def get_center_host_num(self):
            return 2001

    si.ServerData = _SD
    pkg.FilePickerProxy = mod
    pkg.ServerInfo = si
    saved = {k: sys.modules.get(k) for k in
             ("common_cs", "common_cs.FilePickerProxy", "common_cs.ServerInfo")}
    sys.modules["common_cs"] = pkg
    sys.modules["common_cs.FilePickerProxy"] = mod
    sys.modules["common_cs.ServerInfo"] = si
    return saved


def _restore(saved):
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def make_env():
    doc = tempfile.mkdtemp(prefix="fxcover_doc_")
    live = os.path.join(doc, "res", "ui", "live")
    os.makedirs(live, exist_ok=True)
    img = os.path.join(live, "art.jpg")
    with open(img, "wb") as f:
        f.write(b"\xff\xd8fakejpeg")
    room = FakeRoom(doc)
    av = FakeAvatar()
    GK.g_avatar = av
    GK.g_ui_mgr = None
    real_doc = FC._doc_dir
    FC._doc_dir = lambda: doc.replace("\\", "/")
    return {"doc": doc, "img": img, "rel": "res/ui/live/art.jpg",
            "room": room, "edit": FakeEdit(room), "av": av, "real_doc": real_doc}


def teardown_env(env):
    FC._doc_dir = env["real_doc"]


class TestSetSlotGuards(unittest.TestCase):
    def setUp(self):
        self.env = make_env()
        self.saved = _install_fake_filepicker()

    def tearDown(self):
        _restore(self.saved)
        teardown_env(self.env)

    def test_index_out_of_range_rejected(self):
        for bad in (4, -1, 99):
            r = FC.op_cover_set_slot(self.env["edit"],
                                    {"path": self.env["rel"], "index": bad})
            self.assertFalse(r["ok"], "index=%s 竟然放行" % bad)
            self.assertIn("index", r["error"])
        self.assertEqual(self.env["room"].calls, [], "越界时不该碰 room")

    def test_path_outside_doc_dir_rejected(self):
        r = FC.op_cover_set_slot(self.env["edit"], {"path": "C:/Windows/win.ini"})
        self.assertFalse(r["ok"])
        self.assertIn("doc_dir", r["error"])

    def test_missing_file_rejected(self):
        r = FC.op_cover_set_slot(self.env["edit"], {"path": "res/ui/live/nope.jpg"})
        self.assertFalse(r["ok"])
        self.assertIn("不存在", r["error"])

    def test_dry_run_writes_nothing(self):
        r = FC.op_cover_set_slot(self.env["edit"],
                                {"path": self.env["rel"], "index": 0, "dry_run": True})
        self.assertTrue(r["ok"] and r["dry_run"])
        self.assertEqual(r["md5"], "md5ofart.jpg")
        self.assertEqual(self.env["room"].calls, [], "dry_run 把东西真写了")
        self.assertEqual(self.env["room"].scene_photo_path, [])

    def test_md5_is_called_class_level(self):
        """get_file_md5 只收 (path)：按实例调会 TypeError（2026-09-30 实机踩过）。"""
        self.assertEqual(_Recorder.get_file_md5.__code__.co_argcount, 1)
        with self.assertRaises(TypeError):
            _Recorder().get_file_md5("x.jpg")          # 实例调 = (self, path)
        r = FC.op_cover_set_slot(self.env["edit"],
                                {"path": self.env["rel"], "index": 0, "dry_run": True})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["md5"], "md5ofart.jpg")

    def test_real_write_goes_through_native_setters(self):
        r = FC.op_cover_set_slot(self.env["edit"], {"path": self.env["rel"], "index": 2})
        self.assertTrue(r["ok"], r)
        room = self.env["room"]
        self.assertEqual([c[0] for c in room.calls], ["set_photo_path", "set_photo_md5"])
        self.assertEqual(room.scene_photo_path[2], self.env["rel"])
        self.assertEqual(room.scene_photo_md5[2], "md5ofart.jpg")
        self.assertEqual(r["readback"]["index"], 2)


class TestUploadAndApply(unittest.TestCase):
    def setUp(self):
        self.env = make_env()
        self.saved = _install_fake_filepicker()
        room = self.env["room"]
        room.scene_photo_path = [self.env["rel"]]
        room.scene_photo_md5 = ["md5ofart.jpg"]

    def tearDown(self):
        _restore(self.saved)
        teardown_env(self.env)

    def test_upload_dry_run_does_not_send(self):
        r = FC.op_cover_upload(self.env["edit"], {"index": 0, "dry_run": True})
        self.assertTrue(r["ok"] and r["dry_run"])
        self.assertEqual(r["sharing_type"], FC.COVER_SHARING_TYPE)
        self.assertEqual(self.env["av"].uploads, [], "dry_run 竟然发起了上传")
        self.assertFalse(getattr(self.env["av"], "_fx_cover_hooked", False),
                         "dry_run 不该装回执钩子")

    def test_upload_empty_slot_rejected(self):
        r = FC.op_cover_upload(self.env["edit"], {"index": 3})
        self.assertFalse(r["ok"])
        self.assertIn("空", r["error"])

    def test_capture_filters_by_sharing_type(self):
        av = self.env["av"]
        FC._install_capture(self.env["edit"], av)
        av.after_upload_photo({"other_info": {"ugc_key": "abc123", "cover_photo_idx": 0}},
                             "UGC_MAP_COVER_PHOTO", "http://cdn/x1", "res/ui/live/art.jpg")
        av.after_upload_photo({"other_info": {}}, "PHOTO_FRAME", "http://cdn/x2", "y.jpg")
        rows, hooked = FC._cover_replies(self.env["edit"], 0.0)
        self.assertTrue(hooked)
        self.assertEqual(len(rows), 1, "别的分享类型的回执混进封面了")
        self.assertEqual(rows[0]["share_url"], "http://cdn/x1")
        # 原生调用仍要转发出去（钩子只观察）
        self.assertEqual(av.after_upload_photo({}, "X", "u", "n"), "orig")

    def test_apply_rejects_non_http_urls(self):
        r = FC.op_cover_apply(self.env["edit"], {"urls": ["ftp://x/1.jpg"]})
        self.assertFalse(r["ok"])
        self.assertFalse(hasattr(self.env["av"], "rpc"), "校验没过就上了 RPC")

    def test_apply_dry_run_does_not_call_rpc(self):
        r = FC.op_cover_apply(self.env["edit"],
                              {"urls": ["http://cdn/1.jpg"], "dry_run": True})
        self.assertTrue(r["ok"] and r["dry_run"])
        self.assertFalse(hasattr(self.env["av"], "rpc"))

    def test_apply_uses_uploaded_urls_in_slot_order(self):
        av = self.env["av"]
        FC._install_capture(self.env["edit"], av)
        av.after_upload_photo({"other_info": {"ugc_key": "abc123", "cover_photo_idx": 1}},
                             "UGC_MAP_COVER_PHOTO", "http://cdn/second", "b.jpg")
        av.after_upload_photo({"other_info": {"ugc_key": "abc123", "cover_photo_idx": 0}},
                             "UGC_MAP_COVER_PHOTO", "http://cdn/first", "a.jpg")
        r = FC.op_cover_apply(self.env["edit"], {})
        self.assertTrue(r["ok"], r)
        self.assertEqual(av.rpc, ("abc123", ["http://cdn/first", "http://cdn/second"]))


class TestCoverDocCounts(unittest.TestCase):
    def _registered(self):
        path = os.path.join(ROOT, "bridge", "fuxiang_edit.py")
        tree = ast.parse(_read(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "OPS":
                        return sorted(k.value for k in node.value.keys
                                      if isinstance(k, ast.Constant)
                                      and str(k.value).startswith("cover_"))
        self.fail("fuxiang_edit.py 里找不到 OPS 字典")

    def test_registered_ops_match_doc(self):
        keys = self._registered()
        self.assertEqual(len(keys), len(set(keys)), "OPS 里 cover_ 键重复")
        doc = _read(DOC)
        got = [int(m.group(1)) for m in re.finditer(r"(\d+) 个 op", doc)]
        self.assertTrue(got, "COVER.md 里没写「N 个 op」，测试模式已过期")
        for v in got:
            self.assertEqual(v, len(keys), "COVER.md 写 %d 个 op，OPS 实际 %d 个" % (v, len(keys)))

    def test_doc_lists_every_registered_op(self):
        for k in self._registered():
            self.assertIn("`%s`" % k, _read(DOC), "%s 注册了但 COVER.md 没写" % k)


if __name__ == "__main__":
    unittest.main(verbosity=2)
