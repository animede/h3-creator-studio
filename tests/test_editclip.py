import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from studio import editclip as E

SOURCES = {
    "a": {"kind": "video", "length_s": 10.0, "width": 1280, "height": 720},
    "b": {"kind": "video", "length_s": 8.0, "width": 640, "height": 360},
    "p": {"kind": "image", "length_s": None, "width": 100, "height": 100},
    "s": {"kind": "audio", "length_s": 30.0, "width": None, "height": None},
}
PATHS = {"a": "/m/a.mp4", "b": {"path": "/m/b.mp4", "has_audio": False}, "p": "/m/p.png", "s": "/m/s.wav"}


def clip(oid="a", a=0.0, b=4.0):
    return {"id": oid, "kind": "video", "in": a, "out": b}


def state(clips=None, **kw) -> dict:
    """検証済みの状態(a 4 秒・b 4 秒・画像 3 秒。plain 11 秒)。"""
    raw = {"clips": clips if clips is not None else [clip("a", 0, 4), clip("b", 0, 4), {"id": "p", "kind": "image", "duration": 3}], **kw}
    return E.validate_state(raw, SOURCES)


def xfade(after, d, kind="crossfade"):
    return {"after": after, "type": kind, "duration": d}


def graph(st, **kw) -> str:
    return E.build_graph(st, PATHS, **kw)[0]


class ValidateTest(unittest.TestCase):
    def bad(self, message, raw=None, **kw):
        with self.assertRaises(ValueError) as cm:
            E.validate_state(raw if raw is not None else {"clips": [clip()], **kw}, SOURCES)
        self.assertIn(message, str(cm.exception))

    def test_normalizes(self):
        st = state(transitions=[xfade(0, 1), {"after": 1, "type": "cut", "duration": 0}], effects=[{"type": "mono"}, {"type": "saturation", "value": "1.5", "start": 1, "end": 2}])
        self.assertEqual(st["clips"][2], {"id": "p", "kind": "image", "in": 0.0, "out": 3.0, "duration": 3.0})
        self.assertEqual(st["clips"][0], {"id": "a", "kind": "video", "in": 0.0, "out": 4.0, "duration": 4.0})
        self.assertEqual(st["transitions"], [xfade(0, 1.0)])               # cut は省く
        self.assertEqual(st["effects"][0], {"type": "mono", "value": None, "start": None, "end": None})
        looks = E.validate_state({"clips": [clip()], "effects": [{"type": k, "start": 1, "end": 2} for k in E.LOOKS]}, SOURCES)
        self.assertEqual([e["value"] for e in looks["effects"]], [None] * len(E.LOOKS))        # 見た目の効果は値を持たない
        graph = E.build_graph(looks, PATHS)[0]
        for k, flt in E.LOOKS.items():
            sep = ":" if "=" in flt else "="                                                       # オプションの無い negate は「negate=enable=…」
            self.assertIn(f"{flt}{sep}enable='between(t,1,2)'", graph, k)                          # 固定のフィルタに区間の enable
        self.assertIn(",negate=enable='between(t,1,2)',", graph)
        self.assertNotIn("negate:enable", graph)
        rng = E.validate_state({"clips": [clip()], "effects": [{"type": "fade_from_black", "start": 0, "end": 1}, {"type": "black", "start": 1, "end": 2},
                                                               {"type": "fade_to_white", "start": 2, "end": 3}, {"type": "zoom_in", "start": 0, "end": 4},
                                                               {"type": "zoom_out", "value": 1.5, "start": 1, "end": 3}]}, SOURCES)
        self.assertEqual([e["value"] for e in rng["effects"]], [None, None, None, 1.3, 1.5])            # ズームだけ値(既定 1.3)
        g = E.build_graph(rng, PATHS)[0]
        self.assertIn("fade=t=in:st=0:d=1:enable='between(t,0,1)'", g)
        self.assertIn("drawbox=c=black:t=fill:enable='between(t,1,2)'", g)
        self.assertIn("fade=t=out:st=2:d=1:c=white:enable='between(t,2,3)'", g)
        self.assertIn("scale=w='trunc(iw*(if(between(t,0,4),1+(1.3-1)*clip((t-0)/4,0,1),1))/2)*2':h='trunc(ih*(if(between(t,0,4),1+(1.3-1)*clip((t-0)/4,0,1),1))/2)*2'"
                      ":eval=frame,crop=", g)
        self.assertIn("if(between(t,1,3),1.5-(1.5-1)*clip((t-1)/2,0,1),1)", g)

        self.assertEqual(st["effects"][1]["value"], 1.5)
        self.assertEqual(st["audio"], {"mode": "original", "song_id": None, "song_db": -14.0})
        self.assertEqual((st["width"], st["height"], st["fps"]), (1280, 720, 24))   # 先頭の動画から
        self.assertEqual((st["fade_in"], st["fade_out"]), (0.0, 0.0))

    def test_source_at_maps_export_time_to_the_clip_and_its_source_seconds(self):
        st = state(clips=[clip("a", 2, 6), clip("b", 1, 5)], transitions=[xfade(0, 1)])     # export: a 0〜4、b 3〜7(1 秒重なる)
        self.assertEqual(E.source_at(st, 1.0), {"index": 0, "clip": st["clips"][0], "source_s": 3.0})
        self.assertEqual(E.source_at(st, 3.5)["index"], 1)                                     # 重なりは後ろのクリップ
        self.assertEqual(E.source_at(st, 3.5)["source_s"], 1.5)
        self.assertEqual(E.source_at(st, 99)["source_s"], 5.0)                                 # 末尾に丸める
        self.assertIsNone(E.source_at({**E.empty_state()}, 1))
        pans = E.validate_state({"clips": [clip()], "effects": [{"type": k, "start": 0, "end": 2} for k in E.PANS]}, SOURCES)
        self.assertEqual([e["value"] for e in pans["effects"]], [1.2, 1.2, 1.2, 1.2, 1.3, 1.3])
        g = E.build_graph(pans, PATHS)[0]
        W, H = pans["width"], pans["height"]
        self.assertIn(f"if(between(t,0,2),1.2,1)", g)                                                       # パンは一定の拡大
        self.assertIn(f"crop={W}:{H}:x='(iw-{W})*clip((t-0)/2,0,1)':y='(ih-{H})/2'", g)                    # 右へ
        self.assertIn(f"crop={W}:{H}:x='(iw-{W})*(1-clip((t-0)/2,0,1))':y='(ih-{H})/2'", g)                # 左へ
        self.assertIn(f"crop={W}:{H}:x='(iw-{W})/2':y='(ih-{H})*clip((t-0)/2,0,1)'", g)                    # 下へ
        self.assertIn(f"crop={W}:{H}:x='(iw-{W})/2':y='(ih-{H})*(1-clip((t-0)/2,0,1))'", g)                # 上へ
        self.assertIn(f"if(between(t,0,2),1+(1.3-1)*clip((t-0)/2,0,1),1)", g)                               # ズームしながら

    def test_shortest_piece_rule_is_the_same_for_split_delete_and_validation(self):
        # 分割・削除は MIN_CLIP_S - EPS(0.099 秒)までの切れ端を残す。検証も、動画・画像とも同じ許容で受ける(画像だけ 0.1 ちょうどを求めて断っていた)
        img = E.validate_state({"clips": [{"id": "p", "duration": 3}]}, SOURCES)
        for piece in (E.split_at(img, 0.099), E.delete_range(img, 0.099, 3.0)):
            self.assertAlmostEqual(piece["clips"][0]["duration"], 0.099)
            self.assertEqual(E.validate_state(piece, SOURCES)["clips"][0]["duration"], 0.099)
        vid = E.validate_state({"clips": [clip("a", 0, 4)]}, SOURCES)
        self.assertEqual(E.validate_state(E.split_at(vid, 0.099), SOURCES)["clips"][0]["duration"], 0.099)
        for raw in ({"id": "p", "duration": 0.098}, {"id": "a", "in": 0, "out": 0.098}):
            with self.assertRaises(ValueError, msg=raw):
                E.validate_state({"clips": [raw]}, SOURCES)

    def test_zoom_and_pan_crop_to_the_rendered_size(self):
        # プレビューは短辺 480 に縮めて作るので、ズーム・パンの crop もその寸法で切る(元の寸法で切ると、854×480 から 1024×576 は切り出せず失敗していた)
        st = E.validate_state({"clips": [{"id": "p", "duration": 3}], "effects": [{"type": "zoom_in", "start": 0, "end": 2},
                                                                                {"type": "pan_right", "start": 1, "end": 3}]}, SOURCES)
        self.assertEqual((st["width"], st["height"]), (1024, 576))
        preview, export = graph(st, preview=True), graph(st)
        self.assertIn("crop=854:480:(iw-854)/2:(ih-480)/2", preview)
        self.assertIn("crop=854:480:x='(iw-854)*clip((t-1)/2,0,1)':y='(ih-480)/2'", preview)
        self.assertNotIn("crop=1024", preview)
        self.assertIn("crop=1024:576:(iw-1024)/2:(ih-576)/2", export)
        self.assertIn("crop=1024:576:x='(iw-1024)*clip((t-1)/2,0,1)':y='(ih-576)/2'", export)

    def test_empty_and_image_only(self):
        self.assertEqual(E.validate_state({}, SOURCES), E.empty_state())
        st = E.validate_state({"clips": [{"id": "p"}]}, SOURCES)             # 画像の秒数の既定は 3、寸法は 1024×576 / 24
        self.assertEqual((st["clips"][0]["duration"], st["width"], st["height"], st["fps"]), (3.0, 1024, 576, 24))
        st = E.validate_state({"clips": [{"id": "p"}, clip("b", 0, 2)], "width": 99, "height": 99}, SOURCES)   # 渡された寸法は使わず、最初の動画から
        self.assertEqual((st["width"], st["height"]), (640, 360))

    def test_odd_dimensions_are_made_even(self):
        src = {"v": {"kind": "video", "length_s": 5, "width": 641, "height": 361, "fps": 30}}
        st = E.validate_state({"clips": [{"id": "v", "in": 0, "out": 1}]}, src)
        self.assertEqual((st["width"], st["height"], st["fps"]), (640, 360, 30))

    def test_clip_errors(self):
        self.bad("クリップ 1の生成物が見つかりません", {"clips": [{"id": "zzz", "in": 0, "out": 1}]})
        self.bad("クリップ 1には動画か画像を使ってください", {"clips": [{"id": "s", "in": 0, "out": 1}]})
        self.bad("クリップ 2の開始は 0 秒以上", {"clips": [clip(), clip("a", -1, 2)]})
        self.bad("クリップ 1の終了は開始より後", {"clips": [clip("a", 3, 3)]})
        self.bad("クリップ 1の終了が元の動画の長さ(10.00 秒)を超えています", {"clips": [clip("a", 0, 10.5)]})
        self.bad("クリップ 1は短すぎます", {"clips": [clip("a", 1, 1.05)]})
        self.bad("クリップ 1の開始は数字で指定", {"clips": [{"id": "a", "in": "x", "out": 2}]})
        self.bad("クリップ 1(画像)の秒数は", {"clips": [{"id": "p", "duration": 0}]})
        self.bad("clips は配列", {"clips": "x"})
        self.bad("state は辞書", "x")
        st = E.validate_state({"clips": [clip("a", 0, 10.02)]}, SOURCES)       # 長さのわずかな超過(丸め)は、長さに収める
        self.assertEqual(st["clips"][0]["out"], 10.0)

    def test_transition_errors(self):
        two = [clip("a", 0, 4), clip("b", 0, 2)]
        self.bad("位置が正しくありません", {"clips": two, "transitions": [xfade(1, 1)]})
        self.bad("位置が正しくありません", {"clips": [clip()], "transitions": [xfade(0, 1)]})
        self.bad("重複", {"clips": two, "transitions": [xfade(0, 1), xfade(0, 1)]})
        self.bad("cut / crossfade / fadeblack", {"clips": two, "transitions": [{"after": 0, "type": "wipe", "duration": 1}]})
        self.bad("0.5〜2 秒", {"clips": two, "transitions": [xfade(0, 0.2)]})
        self.bad("0.5〜2 秒", {"clips": two, "transitions": [xfade(0, 2.5)]})
        self.bad("半分(1.00 秒)を超えています", {"clips": two, "transitions": [xfade(0, 1.5)]})      # 短いほうの 2 秒の半分
        E.validate_state({"clips": two, "transitions": [xfade(0, 1.0)]}, SOURCES)                       # ちょうど半分は通る

    def test_effect_errors(self):
        self.bad("効果 1の種類", {"clips": [clip()], "effects": [{"type": "glow"}]})
        self.bad("効果 1(zoom_in)は区間", {"clips": [clip()], "effects": [{"type": "zoom_in"}]})
        self.bad("効果 1(black)は区間", {"clips": [clip()], "effects": [{"type": "black", "start": 1}]})
        self.bad("効果 1の値(3)は 1.05〜2", {"clips": [clip()], "effects": [{"type": "zoom_out", "value": 3, "start": 0, "end": 2}]})
        self.bad("効果 1の値(3.5)は 0〜3", {"clips": [clip()], "effects": [{"type": "saturation", "value": 3.5}]})
        self.bad("効果 1の値(-0.1)は 0〜3", {"clips": [clip()], "effects": [{"type": "contrast", "value": -0.1}]})
        self.bad("効果 1の値(1.5)は -1〜1", {"clips": [clip()], "effects": [{"type": "brightness", "value": 1.5}]})
        self.bad("効果 1の終了は開始より後", {"clips": [clip()], "effects": [{"type": "mono", "start": 2, "end": 1}]})
        self.bad("効果 1の開始は 0 秒以上", {"clips": [clip()], "effects": [{"type": "mono", "start": -1, "end": 1}]})
        self.bad("効果 1の区間がプロジェクトの長さ(4.00 秒)の外", {"clips": [clip()], "effects": [{"type": "mono", "start": 1, "end": 5}]})
        self.bad("効果 1の区間がプロジェクトの長さ(4.00 秒)の外", {"clips": [clip()], "effects": [{"type": "mono", "start": 4, "end": None}]})
        ok = E.validate_state({"clips": [clip()], "effects": [{"type": "brightness", "value": -1}, {"type": "mono", "start": None, "end": 2}]}, SOURCES)
        self.assertEqual([e["end"] for e in ok["effects"]], [None, 2.0])

    def test_audio_and_fade_errors(self):
        self.bad("original / music / mix / none", {"clips": [clip()], "audio": {"mode": "loud"}})
        self.bad("曲を選んでください", {"clips": [clip()], "audio": {"mode": "music"}})
        self.bad("曲が見つかりません", {"clips": [clip()], "audio": {"mode": "mix", "song_id": "nope"}})
        self.bad("曲には音声を使ってください", {"clips": [clip()], "audio": {"mode": "music", "song_id": "b"}})
        self.bad("曲の音量は -60〜0 dB", {"clips": [clip()], "audio": {"mode": "mix", "song_id": "s", "song_db": 6}})
        self.bad("フェードの秒数は 0 以上", {"clips": [clip()], "fade_in": -1})
        self.bad("フェードの合計が、動画の長さ(4.00 秒)を超えています", {"clips": [clip()], "fade_in": 2.5, "fade_out": 2})
        st = E.validate_state({"clips": [clip()], "audio": {"mode": "mix", "song_id": "s", "song_db": -20}, "fade_in": 1, "fade_out": 3}, SOURCES)
        self.assertEqual((st["audio"]["song_db"], st["fade_in"], st["fade_out"]), (-20.0, 1.0, 3.0))
        # 曲を使わないモードなら、曲の指定が古くても通る(曲の欄を残したまま切り替えられる)
        E.validate_state({"clips": [clip()], "audio": {"mode": "original", "song_id": "gone"}}, SOURCES)

    def test_used_ids_and_hash(self):
        st = state(audio={"mode": "music", "song_id": "s"})
        self.assertEqual(E.used_ids(st), ["a", "b", "p", "s"])
        self.assertEqual(E.used_ids({"clips": "x", "audio": 3}), [])
        self.assertEqual(E.state_hash(st), E.state_hash(json.loads(json.dumps(st))))
        self.assertNotEqual(E.state_hash(st), E.state_hash({**st, "fade_in": 1.0}))


class TimelineTest(unittest.TestCase):
    def test_timeline_is_plain_concatenation(self):
        st = state(transitions=[xfade(0, 1)])
        self.assertEqual(E.timeline(st), [{"index": 0, "start": 0.0, "end": 4.0}, {"index": 1, "start": 4.0, "end": 8.0}, {"index": 2, "start": 8.0, "end": 11.0}])
        self.assertEqual(E.plain_duration(st), 11.0)

    def test_export_duration_subtracts_overlaps(self):
        self.assertEqual(E.export_duration(state()), 11.0)
        self.assertEqual(E.export_duration(state(transitions=[xfade(0, 1)])), 10.0)
        self.assertEqual(E.export_duration(state(transitions=[xfade(0, 1), xfade(1, 0.5, "fadeblack")])), 9.5)
        self.assertEqual(E.export_duration(E.empty_state()), 0.0)
        two = state(transitions=[xfade(0, 1), xfade(1, 0.5, "fadeblack")])
        self.assertEqual(E.export_timeline(two), [{"index": 0, "start": 0.0, "end": 4.0}, {"index": 1, "start": 3.0, "end": 7.0}, {"index": 2, "start": 6.5, "end": 9.5}])

    def test_plain_to_export(self):
        st = state(transitions=[xfade(0, 1), xfade(1, 0.5)])
        f = E.plain_to_export
        self.assertEqual((f(st, 2, "start"), f(st, 2, "end")), (2, 2))
        self.assertEqual((f(st, 4, "start"), f(st, 4, "end")), (3.0, 4))       # 境目: 始まりは後ろのクリップ、終わりは前のクリップ
        self.assertEqual((f(st, 8, "start"), f(st, 8, "end")), (6.5, 7.0))
        self.assertEqual(f(st, 11, "end"), 9.5)
        self.assertEqual(f(st, 0, "start"), 0)
        self.assertEqual(f(st, 99, "end"), 9.5)                                # 範囲の外は端に寄せる


class OpsTest(unittest.TestCase):
    def spans(self, st):
        return [(c["id"], c["in"], c["out"]) for c in st["clips"]]

    def test_split_mid_clip(self):
        st = state(transitions=[xfade(0, 1), xfade(1, 1, "fadeblack")])
        new = E.split_at(st, 2.0)
        self.assertEqual(self.spans(new), [("a", 0.0, 2.0), ("a", 2.0, 4.0), ("b", 0.0, 4.0), ("p", 0.0, 3.0)])
        self.assertEqual(new["transitions"], [xfade(1, 1.0), xfade(2, 1.0, "fadeblack")])      # 分けたところは cut、後ろの境目は添字がずれる
        self.assertEqual(E.timeline(new)[2], {"index": 2, "start": 4.0, "end": 8.0})
        self.assertEqual(E.export_duration(new), E.export_duration(st))
        self.assertEqual(self.spans(st)[0], ("a", 0.0, 4.0))                                  # 元は変えない

    def test_split_uses_clip_in_offset_and_images(self):
        st = state([clip("a", 2, 6), {"id": "p", "kind": "image", "duration": 3}])
        new = E.split_at(st, 1.5)
        self.assertEqual(self.spans(new)[:2], [("a", 2.0, 3.5), ("a", 3.5, 6.0)])
        img = E.split_at(st, 5.0)                                                              # 画像は秒数を分ける(in は常に 0)
        self.assertEqual([(c["in"], c["out"], c["duration"]) for c in img["clips"][1:]], [(0.0, 1.0, 1.0), (0.0, 2.0, 2.0)])

    def test_split_noop_on_boundary_or_outside(self):
        st = state(transitions=[xfade(0, 1)])
        for t in (0, 4, 8, 11, 4.05, 3.95, -3, 50):
            self.assertEqual(E.split_at(st, t), st, t)

    def test_split_shrinks_transition_to_half_or_drops_it(self):
        st = state([clip("a", 0, 4), clip("b", 0, 2)], transitions=[xfade(0, 1)])
        new = E.split_at(st, 3.2)                          # a の後ろが 0.8 秒に → 境目は半分の 0.4 になり、0.5 に届かないので cut
        self.assertEqual(new["transitions"], [])
        new = E.split_at(st, 3.0)                          # 後ろが 1 秒 → 半分の 0.5 秒なら残る
        self.assertEqual(new["transitions"], [xfade(1, 0.5)])

    def test_delete_range_mid_clip_joins_remnants(self):
        st = state(transitions=[xfade(0, 1), xfade(1, 1, "fadeblack")],
                   effects=[{"type": "mono", "start": 2, "end": 9}, {"type": "contrast", "value": 2, "start": 9, "end": 11}, {"type": "brightness", "value": 0.2}])
        new = E.delete_range(st, 1.0, 6.0)
        self.assertEqual(self.spans(new), [("a", 0.0, 1.0), ("b", 2.0, 4.0), ("p", 0.0, 3.0)])
        self.assertEqual(new["transitions"], [xfade(1, 1.0, "fadeblack")])                     # b の右端は元のまま p につながる。a と b の間は新しい cut
        self.assertEqual(E.plain_duration(new), 6.0)
        self.assertEqual([(e["type"], e["start"], e["end"]) for e in new["effects"]],
                         [("mono", 1.0, 4.0), ("contrast", 4.0, 6.0), ("brightness", None, None)])    # 区間を詰める。全体の効果はそのまま

    def test_delete_range_whole_clip_and_effect_removed(self):
        st = state(transitions=[xfade(0, 1), xfade(1, 1)], effects=[{"type": "mono", "start": 4.5, "end": 7.5}])
        new = E.delete_range(st, 4.0, 8.0)
        self.assertEqual(self.spans(new), [("a", 0.0, 4.0), ("p", 0.0, 3.0)])
        self.assertEqual(new["transitions"], [])                                               # a と p が新しくつながる → cut
        self.assertEqual(new["effects"], [])                                                   # 区間が丸ごと消えた効果は外す

    def test_delete_range_keeps_untouched_transitions_and_trims_edges(self):
        st = state(transitions=[xfade(0, 1), xfade(1, 1)])
        tail = E.delete_range(st, 9.0, 11.0)                    # 最後の画像の後ろ 2 秒
        self.assertEqual(self.spans(tail)[2], ("p", 0.0, 1.0))
        self.assertEqual(tail["transitions"], [xfade(0, 1.0), xfade(1, 0.5)])                  # 画像が 1 秒に縮み、半分の 0.5 に収める
        head = E.delete_range(st, 0.0, 2.0)                     # 先頭の 2 秒: a の残りは右端が元のままなので境目は残る
        self.assertEqual(self.spans(head)[:2], [("a", 2.0, 4.0), ("b", 0.0, 4.0)])
        self.assertEqual(head["transitions"], [xfade(0, 1.0), xfade(1, 1.0)])
        self.assertEqual(E.delete_range(st, 5.0, 5.0), st)
        self.assertEqual(E.delete_range(st, 30, 40), st)
        self.assertEqual(E.delete_range(st, 0, 99)["clips"], [])                               # 全部消せば空(空の状態は有効)

    def test_delete_drops_tiny_remnants(self):
        st = state([clip("a", 0, 4), clip("b", 0, 4)])
        new = E.delete_range(st, 0.05, 4.0)                    # 先頭の 0.05 秒の切れ端は残さない
        self.assertEqual(self.spans(new), [("b", 0.0, 4.0)])

    def test_insert_mid_clip_and_effects_shift(self):
        st = state(transitions=[xfade(0, 1), xfade(1, 1, "fadeblack")], effects=[{"type": "mono", "start": 1, "end": 3}, {"type": "contrast", "value": 2, "start": 5, "end": 9}])
        new = E.insert_at(st, 2.0, {"id": "p", "kind": "image", "duration": 2})
        self.assertEqual(self.spans(new), [("a", 0.0, 2.0), ("p", 0.0, 2.0), ("a", 2.0, 4.0), ("b", 0.0, 4.0), ("p", 0.0, 3.0)])
        self.assertEqual(new["transitions"], [xfade(2, 1.0), xfade(3, 1.0, "fadeblack")])      # 分けたところ・入れたところの前後は cut。元の境目は後ろへずれる
        self.assertEqual([(e["start"], e["end"]) for e in new["effects"]], [(1.0, 5.0), (7.0, 11.0)])   # 区間をまたぐものは延び、後ろのものはずれる
        self.assertEqual(E.plain_duration(new), 13.0)

    def test_insert_at_boundaries(self):
        st = state(transitions=[xfade(0, 1)], effects=[{"type": "mono", "start": 4, "end": 8}])
        new_clip = {"id": "b", "kind": "video", "in": 1, "out": 3}
        mid = E.insert_at(st, 4.0, new_clip)
        self.assertEqual(self.spans(mid), [("a", 0.0, 4.0), ("b", 1.0, 3.0), ("b", 0.0, 4.0), ("p", 0.0, 3.0)])
        self.assertEqual(mid["transitions"], [])                                               # a と b の境目だったところに入れたので、その境目は無くなる
        self.assertEqual((mid["effects"][0]["start"], mid["effects"][0]["end"]), (6.0, 10.0))   # 境目の効果の始まりは、入れた後ろの内容に付く
        near = E.insert_at(st, 4.05, new_clip)                                                 # 境目の近くはその境目に入れる(0.05 秒の切れ端を作らない)
        self.assertEqual(self.spans(near), self.spans(mid))
        head, tail = E.insert_at(st, 0.0, new_clip), E.insert_at(st, 99.0, new_clip)
        self.assertEqual(self.spans(head)[:2], [("b", 1.0, 3.0), ("a", 0.0, 4.0)])
        self.assertEqual(head["transitions"], [xfade(1, 1.0)])
        self.assertEqual(self.spans(tail)[-2:], [("p", 0.0, 3.0), ("b", 1.0, 3.0)])
        self.assertEqual(tail["transitions"], [xfade(0, 1.0)])

    def test_insert_into_empty_and_errors(self):
        new = E.insert_at(E.empty_state(), 0.0, {"id": "p", "kind": "image"})
        self.assertEqual([(c["id"], c["duration"]) for c in new["clips"]], [("p", 3.0)])
        with self.assertRaises(ValueError):
            E.insert_at(E.empty_state(), 0.0, {"id": "a", "kind": "video", "in": 0})        # 動画は out が要る
        with self.assertRaises(ValueError):
            E.insert_at(E.empty_state(), 0.0, {"id": "", "kind": "image"})
        with self.assertRaises(ValueError):
            E.insert_at(E.empty_state(), 0.0, {"id": "a", "kind": "video", "in": 1, "out": 1.02})

    def test_ops_results_validate(self):
        """分割・削除・挿入の結果は、そのまま検証を通る(画面がそのまま PUT できる)。"""
        st = state(transitions=[xfade(0, 1), xfade(1, 1)], effects=[{"type": "mono", "start": 2, "end": 9}])
        for new in (E.split_at(st, 5.3), E.delete_range(st, 3.5, 9.5), E.insert_at(st, 6.0, clip("b", 1, 2))):
            self.assertEqual(E.validate_state(new, SOURCES), new)


class RenderArgsTest(unittest.TestCase):
    def test_graph_for_three_clips_with_one_crossfade_and_one_ranged_effect(self):
        st = state(transitions=[xfade(0, 1)], effects=[{"type": "mono", "start": 2, "end": 6}])
        g = graph(st)
        parts = g.split(";")
        # クリップごとの映像・音声(b は音声なし → anullsrc、画像も anullsrc)
        self.assertIn("[0:v]trim=duration=4,setpts=PTS-STARTPTS,fps=24,scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                      "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,setsar=1,format=yuv420p[v0]", parts)
        self.assertIn("[2:v]trim=duration=3,setpts=PTS-STARTPTS,fps=24,scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                      "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,setsar=1,format=yuv420p[v2]", parts)
        self.assertIn("[0:a]atrim=duration=4,asetpts=PTS-STARTPTS,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                      "apad=whole_dur=4,atrim=duration=4,asetpts=PTS-STARTPTS[a0]", parts)
        self.assertTrue(any(p.startswith("anullsrc=channel_layout=stereo:sample_rate=48000:duration=4,aformat=") and p.endswith("[a1]") for p in parts))
        self.assertTrue(any(p.startswith("anullsrc=") and p.endswith("[a2]") and "duration=3" in p for p in parts))
        # cut でつながる b + 画像は concat 1 つ、a との間は crossfade(offset = 4 - 1)
        self.assertIn("[v1][a1][v2][a2]concat=n=2:v=1:a=1[v1_2_raw][a1_2]", parts)
        self.assertIn("[v1_2_raw]fps=24[v1_2]", parts)
        self.assertIn("[v0][v1_2]xfade=transition=fade:duration=1:offset=3[v0_2]", parts)
        self.assertIn("[a0][a1_2]acrossfade=d=1[a0_2]", parts)
        # 効果の区間は plain の 2〜6 秒 → export では 2〜5 秒(b の内側にある終わりから、前の境目の縮み 1 秒を引く)
        self.assertIn("[v0_2]hue=s=0:enable='between(t,2,5)',format=yuv420p[vout]", parts)
        self.assertIn("[a0_2]anull[aout]", parts)
        self.assertEqual(len(parts), 12)       # 映像 3 + 音声 3 + concat・fps・xfade・acrossfade・vout・aout

    def test_cut_only_is_a_single_concat(self):
        g = graph(state())
        self.assertIn("[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[v0_2_raw][a0_2]", g)
        self.assertNotIn("xfade", g)
        self.assertNotIn("acrossfade", g)

    def test_fold_left_to_right_with_cumulative_offsets(self):
        st = state([clip("a", 0, 4), clip("b", 0, 4), clip("a", 4, 7), clip("b", 4, 8)], transitions=[xfade(0, 1), xfade(1, 0.5, "fadeblack"), xfade(2, 1.5)])
        g = graph(st)
        self.assertIn("[v0][v1]xfade=transition=fade:duration=1:offset=3[v0_1]", g)
        self.assertIn("[v0_1][v2]xfade=transition=fadeblack:duration=0.5:offset=6.5[v0_2]", g)      # 累積 4 + 4 - 1 = 7、7 - 0.5
        self.assertIn("[v0_2][v3]xfade=transition=fade:duration=1.5:offset=8[v0_3]", g)             # 累積 7 + 3 - 0.5 = 9.5、9.5 - 1.5
        self.assertEqual(E.export_duration(st), 4 + 4 + 3 + 4 - 1 - 0.5 - 1.5)

    def test_ranged_effect_times_are_mapped_past_overlaps(self):
        st = state(transitions=[xfade(0, 1), xfade(1, 0.5)],
                   effects=[{"type": "saturation", "value": 2, "start": 4, "end": 8}, {"type": "contrast", "value": 1.5, "start": 8, "end": 11},
                            {"type": "brightness", "value": -0.2, "start": None, "end": None}, {"type": "fade_to_black", "value": None, "start": 5, "end": 6}])
        g = graph(st)
        self.assertIn("eq=saturation=2:enable='between(t,3,7)'", g)             # 2 つ目のクリップだけ: 前の境目の 1 秒を引く
        self.assertIn("eq=contrast=1.5:enable='between(t,6.5,9.5)'", g)         # 3 つ目のクリップだけ: 1.5 秒を引く(全体は 9.5 秒)
        self.assertIn("eq=brightness=-0.2,", g)                                 # 全体は enable なし
        self.assertNotIn("brightness=-0.2:enable", g)
        self.assertIn("fade=t=out:st=4:d=1:enable='between(t,4,5)'", g)        # 黒へ(5〜6 秒 → export 4〜5 秒。区間の外は元のまま)
        order = [g.index(x) for x in ("eq=saturation", "eq=contrast", "eq=brightness", "fade=t=out:st=4")]
        self.assertEqual(order, sorted(order))                                  # 並びは state の順

    def test_start_and_end_fades(self):
        st = state(transitions=[xfade(0, 1)], fade_in=0.5, fade_out=2)
        g = graph(st)
        self.assertIn("fade=t=in:st=0:d=0.5,fade=t=out:st=8:d=2,format=yuv420p[vout]", g)       # 全体 10 秒
        self.assertIn("afade=t=in:st=0:d=0.5,afade=t=out:st=8:d=2,anull[aout]", g)
        self.assertNotIn("fade=t=in", graph(state()).replace("afade", ""))

    def test_audio_original_and_none(self):
        g, v, a = E.build_graph(state(), PATHS)
        self.assertEqual((v, a), ("vout", "aout"))
        self.assertIn("[0:a]atrim", g)
        g, v, a = E.build_graph(state(audio={"mode": "none"}), PATHS)
        self.assertEqual((v, a), ("vout", None))
        self.assertNotIn("atrim", g)                                            # 使わない音声のラベルは作らない(ffmpeg が失敗する)
        self.assertNotIn("anullsrc", g)
        self.assertNotIn("[aout]", g)
        argv = E.render_args("ffmpeg", state(audio={"mode": "none"}), PATHS, "/o/x.mp4.part")
        self.assertIn("-an", argv)
        self.assertNotIn("-c:a", argv)
        self.assertEqual(argv.count("-map"), 1)

    def test_audio_music_replaces_clip_audio(self):
        st = state(transitions=[xfade(0, 1)], audio={"mode": "music", "song_id": "s"})
        g = graph(st, song_len=30.0)
        self.assertNotIn("[0:a]", g)
        self.assertNotIn("anullsrc", g)
        self.assertNotIn("acrossfade", g)
        self.assertIn("[3:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,atrim=duration=10,asetpts=PTS-STARTPTS,"
                      "afade=t=out:st=8:d=2,apad=whole_dur=10[bgm]", g)         # 先頭から映像の長さ 10 秒で切り、末尾 2 秒をフェード
        self.assertIn("[bgm]anull[aout]", g)
        self.assertNotIn("volume=", g)                                          # music は音量を変えない
        short = graph(st, song_len=5.0)                                         # 曲が短ければ曲の終わりでフェードし、あとは無音(繰り返さない)
        self.assertIn("atrim=duration=5,asetpts=PTS-STARTPTS,afade=t=out:st=3:d=2,apad=whole_dur=10[bgm]", short)

    def test_audio_mix(self):
        st = state(audio={"mode": "mix", "song_id": "s", "song_db": -18})
        g = graph(st, song_len=60.0)
        self.assertIn("volume=-18dB", g)
        self.assertIn("[0:a]atrim", g)
        self.assertIn("[a0_2][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[amix]", g)
        self.assertIn("[amix]anull[aout]", g)
        default = graph(state(audio={"mode": "mix", "song_id": "s"}))
        self.assertIn("volume=-14dB", default)                                   # 既定は mvclip.BGM_DB

    def test_inputs_and_encoding_options(self):
        st = state([clip("a", 2, 6), {"id": "p", "kind": "image", "duration": 3}, clip("b", 0, 4)], audio={"mode": "music", "song_id": "s"})
        argv = E.render_args("ffmpeg", st, PATHS, "/o/x.mp4.part", preview=False, progress=True, song_len=30.0)
        self.assertEqual(argv[:6], ["ffmpeg", "-y", "-v", "error", "-progress", "pipe:1"])
        self.assertEqual(argv[6], "-nostats")
        self.assertEqual(argv[7:11], ["-ss", "2", "-t", "4"])
        i = argv.index("-loop")
        self.assertEqual(argv[i:i + 8], ["-loop", "1", "-framerate", "24", "-t", "3", "-i", "/m/p.png"])
        self.assertEqual(argv.count("-i"), 4)                                    # クリップ 3 + 曲
        self.assertEqual(argv[argv.index("/m/s.wav") - 1], "-i")
        self.assertEqual(argv[argv.index("-map") + 1], "[vout]")
        self.assertEqual(argv[-1], "/o/x.mp4.part")
        self.assertEqual(argv[-3:-1], ["-f", "mp4"])
        for flag, value in (("-crf", "18"), ("-b:a", "256k"), ("-preset", "medium"), ("-movflags", "+faststart")):
            self.assertEqual(argv[argv.index(flag) + 1], value, flag)
        self.assertEqual(argv[argv.index("-c:v") - 1], "11")                      # 出力の長さ(-t)
        self.assertNotIn("-progress", E.render_args("ffmpeg", st, PATHS, "x"))
        pre = E.render_args("ffmpeg", st, PATHS, "/o/x.mp4.part", preview=True, song_len=30.0)
        self.assertEqual((pre[pre.index("-crf") + 1], pre[pre.index("-b:a") + 1], pre[pre.index("-preset") + 1]), ("28", "96k", "veryfast"))
        self.assertIn("scale=854:480:", " ".join(pre))                            # 1280×720 の短辺を 480 に
        self.assertIn("scale=1280:720:", " ".join(argv))

    def test_preview_size_rules(self):
        self.assertEqual(E.render_size(state(), True), (854, 480))
        self.assertEqual(E.render_size(state(), False), (1280, 720))
        portrait = E.validate_state({"clips": [clip("a", 0, 1)]}, {"a": {"kind": "video", "length_s": 5, "width": 720, "height": 1280}})
        self.assertEqual(E.render_size(portrait, True), (480, 854))
        small = E.validate_state({"clips": [clip("b", 0, 1)]}, SOURCES)           # 元が 640×360 → 短辺 360 はそのまま
        self.assertEqual(E.render_size(small, True), (640, 360))

    def test_render_errors(self):
        with self.assertRaises(ValueError):
            E.build_graph(E.empty_state(), PATHS)
        with self.assertRaises(ValueError):
            E.render_args("ffmpeg", state(), {"a": "/m/a.mp4"}, "x")


FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


@unittest.skipUnless(FFMPEG, "ffmpeg / ffprobe が PATH に無い")
class RealFfmpegTest(unittest.TestCase):
    """合成の動画・画像・曲を実際の ffmpeg でつなぎ、長さ・ストリーム・効果を確かめる。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)

        def ff(*args):
            subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)
        ff("-f", "lavfi", "-i", "testsrc2=size=960x540:rate=24:duration=2", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest",
           "-pix_fmt", "yuv420p", "-c:a", "aac", str(cls.root / "a.mp4"))
        ff("-f", "lavfi", "-i", "testsrc=size=640x360:rate=24:duration=2", "-pix_fmt", "yuv420p", str(cls.root / "b.mp4"))       # 音声なし
        Image.new("RGB", (200, 200), (220, 30, 30)).save(cls.root / "p.png")
        ff("-f", "lavfi", "-i", "sine=frequency=880:duration=10", str(cls.root / "s.wav"))
        cls.paths = {"a": {"path": str(cls.root / "a.mp4"), "has_audio": True}, "b": {"path": str(cls.root / "b.mp4"), "has_audio": False},
                     "p": str(cls.root / "p.png"), "s": str(cls.root / "s.wav")}
        cls.sources = {"a": {"kind": "video", "length_s": 2.0, "width": 960, "height": 540}, "b": {"kind": "video", "length_s": 2.0, "width": 640, "height": 360},
                       "p": {"kind": "image", "length_s": None, "width": 200, "height": 200}, "s": {"kind": "audio", "length_s": 10.0}}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def render(self, raw: dict, name: str, preview: bool) -> tuple:
        st = E.validate_state(raw, self.sources)
        dst = self.root / name
        argv = E.render_args("ffmpeg", st, self.paths, dst, preview=preview, song_len=10.0)
        r = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(dst)],
                                         capture_output=True, text=True, check=True).stdout)
        return st, dst, info

    def frame_is_gray(self, mp4: Path, at: float) -> bool:
        png = self.root / f"frame_{at}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(at), "-i", str(mp4), "-frames:v", "1", str(png)], check=True)
        with Image.open(png) as im:
            px = list(im.convert("RGB").resize((32, 18)).getdata())
        return sum(abs(r - g) + abs(g - b) for r, g, b in px) / len(px) < 3

    def test_preview_with_crossfade_mono_range_and_music(self):
        raw = {"clips": [{"id": "a", "in": 0, "out": 2}, {"id": "b", "in": 0, "out": 2}, {"id": "p", "kind": "image", "duration": 1}],
               "transitions": [{"after": 0, "type": "crossfade", "duration": 0.5}],
               "effects": [{"type": "mono", "start": 0, "end": 1}],
               "audio": {"mode": "music", "song_id": "s"}}
        st, dst, info = self.render(raw, "preview.mp4", preview=True)
        self.assertAlmostEqual(E.export_duration(st), 4.5)
        streams = {s["codec_type"]: s for s in info["streams"]}
        self.assertEqual(set(streams), {"video", "audio"})
        self.assertAlmostEqual(float(info["format"]["duration"]), 4.5, delta=0.15)
        self.assertAlmostEqual(float(streams["audio"]["duration"]), 4.5, delta=0.15)            # 曲(10 秒)が映像の長さで切れている
        self.assertEqual((streams["video"]["width"], streams["video"]["height"]), (854, 480))     # 960×540 の短辺 480
        self.assertEqual(streams["video"]["codec_name"], "h264")
        self.assertEqual(streams["audio"]["codec_name"], "aac")
        self.assertTrue(self.frame_is_gray(dst, 0.5))                                             # 区間(0〜1 秒)の中はモノクロ
        self.assertFalse(self.frame_is_gray(dst, 3.0))                                            # 区間の外(b の中)は色がある

    def test_export_mix_fadeblack_image_and_silent_clip(self):
        raw = {"clips": [{"id": "a", "in": 0.5, "out": 2}, {"id": "p", "kind": "image", "duration": 2}, {"id": "b", "in": 0, "out": 2}],
               "transitions": [{"after": 0, "type": "fadeblack", "duration": 0.5}, {"after": 1, "type": "crossfade", "duration": 1}],
               "audio": {"mode": "mix", "song_id": "s", "song_db": -20}, "fade_in": 0.3, "fade_out": 0.3,
               "effects": [{"type": "saturation", "value": 2.5}, {"type": "fade_to_black", "start": 5, "end": 5.5}]}
        st, dst, info = self.render(raw, "export.mp4", preview=False)
        self.assertAlmostEqual(E.export_duration(st), 4.0)
        streams = {s["codec_type"]: s for s in info["streams"]}
        self.assertAlmostEqual(float(info["format"]["duration"]), 4.0, delta=0.15)
        self.assertEqual((streams["video"]["width"], streams["video"]["height"]), (960, 540))     # 書き出しは元の寸法(先頭の動画)
        self.assertAlmostEqual(float(streams["audio"]["duration"]), 4.0, delta=0.15)

    def test_preview_zoom_pan_and_negate_range_on_a_large_project(self):
        # 画像だけ(1024×576)のプレビュー(854×480)で、ズーム・パンと、区間つきの色の反転が ffmpeg に通る
        raw = {"clips": [{"id": "p", "kind": "image", "duration": 3}],
               "effects": [{"type": "zoom_in", "start": 0, "end": 1}, {"type": "pan_left", "start": 1, "end": 2},
                           {"type": "zoom_in_right", "start": 2, "end": 3}, {"type": "negate", "start": 0.5, "end": 1.5}]}
        for preview, size in ((True, (854, 480)), (False, (1024, 576))):
            st, dst, info = self.render(raw, f"kenburns_{preview}.mp4", preview=preview)
            video = next(s for s in info["streams"] if s["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), size)

    def test_original_audio_and_none(self):
        raw = {"clips": [{"id": "b", "in": 0, "out": 1}, {"id": "a", "in": 0, "out": 1}]}          # 先頭の動画は音声なし、2 つ目にはある
        st, dst, info = self.render(raw, "orig.mp4", preview=True)
        self.assertEqual(sorted(s["codec_type"] for s in info["streams"]), ["audio", "video"])
        self.assertAlmostEqual(float(info["format"]["duration"]), 2.0, delta=0.15)
        st, dst, info = self.render({**raw, "audio": {"mode": "none"}}, "none.mp4", preview=True)
        self.assertEqual([s["codec_type"] for s in info["streams"]], ["video"])


if __name__ == "__main__":
    unittest.main()
