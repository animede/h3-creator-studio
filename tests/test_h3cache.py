"""参照モード(Ref2VA / Ref2I)に要る H3 のキャッシュ(studio/h3cache.py): 要る構成かの判定(エンジンの _group_stream_host_hint を写したもの)、
キャッシュの有無、作るコマンド、案内の文と英訳、/api/jobs の断り方、画面(案内の出し方)。"""
import io
import os
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image

from studio import h3cache, i18n, main, media
from studio.config import REPO_ROOT, Settings
from studio.handlers import HANDLERS
from studio.jobs import Ctx, JobRunner
from studio.ledger import Ledger

JP = re.compile(r"[぀-ヿ一-鿿]")
EN_HDR = {"Accept-Language": "en"}
WEB = REPO_ROOT / "web"
# エンジンが断るときの文(minimax-h3 の core/runner.py の _ensure_transformer_ref_group。HTTP の detail には「ref2va generation failed: 」が付く)
ENGINE_DETAIL = ("ref2va generation failed: Ref2VA / Ref2I needs the int8 cache of transformer_ref (C:\\AI\\minimax-h3\\models\\prequant\\"
                 "transformer_ref_int8), which is missing. On this host (file streaming, RAM below the pinned threshold) it cannot be quantized "
                 "from bf16 at load time. Build it once with `scripts/prepare_lowvram.py --ref` (downloads transformer_ref 66GB shard by shard, "
                 "writes ~32GB), then retry.")
COMMAND = "cd /h3 && venv/bin/python scripts/prepare_lowvram.py --ref"
MISSING = {"needed": True, "ready": False, "dir": "/h3", "cache_dir": "/h3/models/prequant/transformer_ref_int8", "command": COMMAND}


def touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    return p


def make_cache(d: Path) -> Path:
    touch(d / "config.json")
    touch(d / "meta.json")
    return d


class _CleanEnv:
    """このプロセスの環境から H3_* を外す(判定がエンジンの環境を読むので、開発機の設定に左右されないように)。"""

    def clean_env(self):
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        for k in [k for k in os.environ if k.startswith("H3_")]:
            del os.environ[k]


class PrequantDirTest(unittest.TestCase):
    def test_default_and_the_engine_overrides(self):
        d = Path("/e/minimax-h3")
        self.assertEqual(h3cache.prequant_dir(d, {}), d / "models" / "prequant")
        self.assertEqual(h3cache.prequant_dir(d, {"H3_TE_PREQUANT_DIR": "/big/pq"}), Path("/big/pq"))
        # transformer のキャッシュは H3_TRANSFORMER_PREQUANT_DIR が勝つ(無ければ H3_TE_PREQUANT_DIR。エンジンと同じ)
        self.assertEqual(h3cache.prequant_dir(d, {"H3_TE_PREQUANT_DIR": "/big/pq", "H3_TRANSFORMER_PREQUANT_DIR": "/t"}), Path("/t"))
        self.assertEqual(h3cache.prequant_dir(d, {"H3_TE_PREQUANT_DIR": "cache"}), d / "cache")        # 相対はエンジンのフォルダから
        self.assertEqual(h3cache.prequant_dir(d, {"H3_TE_PREQUANT_DIR": "  "}), d / "models" / "prequant")
        self.assertIsNone(h3cache.moved_prequant_dir(d, {}))
        self.assertEqual(h3cache.moved_prequant_dir(d, {"H3_TRANSFORMER_PREQUANT_DIR": "/t"}), Path("/t"))

    def test_a_cache_counts_only_when_finished(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            pq = d / "models" / "prequant"
            self.assertEqual(h3cache.cache_status(d, {}), {"base": False, "ref": False, "dir": pq})
            touch(pq / "transformer_ref_int8" / "config.json")                  # meta.json が無い: エンジンは読めない
            make_cache(pq / "transformer_int8.tmp")                             # 作りかけ(prepare は <名前>.tmp に作ってから名前を変える)
            self.assertEqual(h3cache.cache_status(d, {}), {"base": False, "ref": False, "dir": pq})
            make_cache(pq / "transformer_int8")
            touch(pq / "transformer_ref_int8" / "meta.json")
            self.assertEqual(h3cache.cache_status(d, {}), {"base": True, "ref": True, "dir": pq})


class SharedTextEncoderTest(unittest.TestCase):
    """動画のエンジンが作った 32B のテキストエンコーダ(画像のエンジンと同じもの。setup が画像のエンジンにも使わせる)。"""

    def test_found_only_when_finished_and_follows_the_engine_env(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "minimax-h3"
            self.assertEqual(h3cache.te_prequant_dir(d, {}), d / "models" / "prequant")
            self.assertEqual(h3cache.te_prequant_dir(d, {"H3_TE_PREQUANT_DIR": "/big/pq", "H3_TRANSFORMER_PREQUANT_DIR": "/t"}),
                             Path("/big/pq"))              # テキストエンコーダは H3_TE_PREQUANT_DIR だけを見る(エンジンと同じ)
            self.assertIsNone(h3cache.shared_te(d, {}))
            touch(d / "models" / "prequant" / "te_bnb-4bit_prune1.tmp" / "config.json")       # 作りかけ
            touch(d / "models" / "prequant" / "te_proj_Qwen--Qwen3-VL-4B-Instruct_bnb-4bit" / "config.json")   # 8〜16GB の級の代わりのエンコーダ
            self.assertIsNone(h3cache.shared_te(d, {}))
            touch(d / "models" / "prequant" / "te_bnb-4bit_prune1" / "config.json")
            self.assertEqual(h3cache.shared_te(d, {}), d / "models" / "prequant" / "te_bnb-4bit_prune1")
            other = Path(t) / "pq"
            touch(other / "te_bnb-4bit_prune1" / "config.json")
            self.assertEqual(h3cache.shared_te(d, {"H3_TE_PREQUANT_DIR": str(other)}), other / "te_bnb-4bit_prune1")


class LeftoverShardTest(unittest.TestCase):
    """準備で HF キャッシュに残った bf16 の原本: 見つけ方と消し方(file streaming の構成で、int8 キャッシュが完成した分だけ)。"""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.root = Path(self._t.name)
        self.h3 = self.root / "minimax-h3"
        self.hub = self.root / "hf" / "hub"
        self.snap = self.hub / h3cache.HF_MODEL_DIR / "snapshots" / "abc"
        self.env = {"HF_HUB_CACHE": str(self.hub), "H3_GROUP_STREAM_HOST": "file"}

    def shard(self, sub, n, link=True):
        name = f"diffusion_pytorch_model-{n:05d}-of-00014.safetensors"
        blob = touch(self.hub / h3cache.HF_MODEL_DIR / "blobs" / f"sha{sub}{n}")
        blob.write_bytes(b"x" * 1000)
        p = self.snap / sub / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if link:
            p.symlink_to(blob)
        else:
            p.write_bytes(b"y" * 1000)
        return p, blob

    def test_cache_location(self):
        self.assertEqual(h3cache.hf_hub_cache({}), Path.home() / ".cache" / "huggingface" / "hub")
        self.assertEqual(h3cache.hf_hub_cache({"HF_HOME": "/x"}), Path("/x/hub"))
        self.assertEqual(h3cache.hf_hub_cache({"HF_HUB_CACHE": "/y", "HF_HOME": "/x"}), Path("/y"))

    def test_only_finished_caches_and_streaming_hosts(self):
        p1, _ = self.shard("transformer", 1)
        r1, _ = self.shard("transformer_ref", 13, link=False)
        touch(self.snap / "transformer" / "config.json")
        self.assertEqual(h3cache.leftover_shards(self.h3, self.env, "linux"), {})          # キャッシュが無い: 原本はまだ要る
        make_cache(self.h3 / "models" / "prequant" / "transformer_int8")
        self.assertEqual(h3cache.leftover_shards(self.h3, self.env, "linux"), {"transformer": [p1]})
        make_cache(self.h3 / "models" / "prequant" / "transformer_ref_int8")
        self.assertEqual(h3cache.leftover_shards(self.h3, self.env, "linux"), {"transformer": [p1], "transformer_ref": [r1]})
        pinned = {**self.env, "H3_GROUP_STREAM_HOST": "pinned"}
        self.assertEqual(h3cache.leftover_shards(self.h3, pinned, "linux"), {})            # RAM の多い PC は bf16 を読むことがある
        self.assertEqual(h3cache.leftover_shards(self.h3, self.env, "win32", 97887), {})   # 96gb 級は bf16 を読む
        self.assertEqual(h3cache.leftover_shards(self.h3, {"HF_HUB_CACHE": str(self.hub)}, "win32", 12288), {"transformer": [p1], "transformer_ref": [r1]})

    def test_stale_snapshots_and_text_encoder(self):
        root = self.hub / h3cache.HF_MODEL_DIR
        old = root / "snapshots" / "0919old"
        big = touch(old / "transformer" / "diffusion_pytorch_model-00001-of-00014.safetensors")
        big.write_bytes(b"o" * 3000)
        self.assertEqual(h3cache.stale_snapshots(self.env), [])                 # refs/main が無ければ触らない
        touch(root / "refs" / "main").write_text("abc\n")
        self.assertEqual(h3cache.stale_snapshots(self.env), [])                 # いまの版のフォルダが無ければ触らない
        make_cache(self.h3 / "models" / "prequant" / "transformer_int8")
        p1, _ = self.shard("transformer", 1)
        self.assertEqual(h3cache.stale_snapshots(self.env), [old])
        self.assertEqual(h3cache.leftover_shards(self.h3, self.env, "win32", 12288), {"transformer": [p1]})     # 古い版の中は数えない
        te = self.snap / "text_encoder" / "model-00001-of-00014.safetensors"
        touch(te).write_bytes(b"t" * 500)
        self.assertNotIn("text_encoder", h3cache.leftover_shards(self.h3, self.env, "win32", 12288))            # 画像のエンジンの TE が無い
        self.assertEqual(h3cache.leftover_shards(self.h3, self.env, "win32", 12288, te_ready=True)["text_encoder"], [te])   # 12GB: 4B + 投影
        self.assertNotIn("text_encoder", h3cache.leftover_shards(self.h3, self.env, "win32", 32607, te_ready=True))         # 32GB 級は原本を読みうる
        touch(self.h3 / "models" / "prequant" / "te_bnb-4bit_prune1" / "config.json")
        self.assertIn("text_encoder", h3cache.leftover_shards(self.h3, self.env, "win32", 32607, te_ready=True))            # 量子化済みがあれば不要
        freed, failed = h3cache.sweep_leftovers(self.h3, self.env, "win32", 12288, te_ready=True)
        self.assertEqual((round(freed * 1e9), failed, old.exists(), te.exists(), p1.exists()), (4500, [], False, False, False))

    def test_sweep_removes_links_and_blobs(self):
        p1, b1 = self.shard("transformer", 1)
        r1, _ = self.shard("transformer_ref", 13, link=False)
        make_cache(self.h3 / "models" / "prequant" / "transformer_int8")
        make_cache(self.h3 / "models" / "prequant" / "transformer_ref_int8")
        small = touch(self.snap / "transformer" / "config.json")          # 小物はエンジンが使うので消さない
        freed, failed = h3cache.sweep_leftovers(self.h3, self.env, "linux")
        self.assertEqual((round(freed * 1e9), failed), (2000, []))
        self.assertFalse(p1.exists() or p1.is_symlink() or b1.exists() or r1.exists())
        self.assertTrue(small.exists())
        self.assertEqual(h3cache.sweep_leftovers(self.h3, self.env, "linux"), (0.0, []))


class NeededRuleTest(unittest.TestCase):
    """要るのは、エンジンが重みをキャッシュのファイルから流す構成(file)で、参照モードを group の経路で読むとき。"""

    def test_explicit_host_wins_even_on_windows(self):
        self.assertTrue(h3cache.file_streaming({"H3_GROUP_STREAM_HOST": "file"}, "linux"))
        self.assertTrue(h3cache.file_streaming({"H3_GROUP_STREAM_HOST": " FILE "}, "linux"))
        self.assertFalse(h3cache.file_streaming({"H3_GROUP_STREAM_HOST": "pinned"}, "win32"))

    def test_auto_is_always_file_on_windows(self):
        with mock.patch.object(h3cache, "total_ram_gb", return_value=128.0):
            self.assertTrue(h3cache.file_streaming({}, "win32"))
            self.assertTrue(h3cache.file_streaming({"H3_GROUP_STREAM_HOST": "auto"}, "win32"))

    def test_auto_elsewhere_follows_the_ram_threshold(self):
        for ram, want in ((33.5, True), (47.9, True), (48.0, False), (131.4, False), (None, False)):
            with mock.patch.object(h3cache, "total_ram_gb", return_value=ram):
                self.assertEqual(h3cache.file_streaming({}, "linux"), want, ram)
        with mock.patch.object(h3cache, "total_ram_gb", return_value=60.0):
            self.assertTrue(h3cache.file_streaming({"H3_GROUP_STREAM_FILE_BELOW_GB": "64"}, "linux"))
            self.assertFalse(h3cache.file_streaming({"H3_GROUP_STREAM_FILE_BELOW_GB": "x"}, "linux"))      # 読めなければ既定の 48

    def test_total_ram_uses_the_engine_units(self):
        meminfo = mock.mock_open(read_data="MemTotal:       32768000 kB\nMemFree:  100 kB\n")
        with mock.patch.object(h3cache, "open", meminfo, create=True):
            self.assertEqual(h3cache.total_ram_gb(), 32.8)                      # kB / 1e6(エンジンの ram_gb と同じ)
        fake_psutil = types.SimpleNamespace(virtual_memory=lambda: types.SimpleNamespace(total=34_359_738_368))
        with mock.patch.object(h3cache, "open", mock.Mock(side_effect=OSError), create=True), mock.patch.dict("sys.modules", {"psutil": fake_psutil}):
            self.assertEqual(h3cache.total_ram_gb(), 34.4)                      # /proc が無ければ psutil の total / 1e9
        broken = types.SimpleNamespace(virtual_memory=mock.Mock(side_effect=RuntimeError))
        with mock.patch.object(h3cache, "open", mock.Mock(side_effect=OSError), create=True), mock.patch.dict("sys.modules", {"psutil": broken}):
            self.assertIsNone(h3cache.total_ram_gb())

    def test_group_offload_unless_h3_lowvram_says_otherwise(self):
        for value, want in ((None, True), ("", True), ("group", True), (" GROUP ", True), ("1", False), ("0", False)):
            env = {} if value is None else {"H3_LOWVRAM": value}
            self.assertEqual(h3cache.group_offload(env), want, value)


class CommandTest(unittest.TestCase):
    def test_linux_and_windows_one_liners(self):
        self.assertEqual(h3cache.prepare_command(Path("/home/you/minimax-h3"), {}, "linux"),
                         "cd /home/you/minimax-h3 && venv/bin/python scripts/prepare_lowvram.py --ref")
        self.assertEqual(h3cache.prepare_command(Path("C:/AI/minimax-h3"), {}, "win32"),
                         "cd C:/AI/minimax-h3; .\\venv\\Scripts\\python.exe scripts\\prepare_lowvram.py --ref")

    def test_quoting_flags_and_the_moved_cache_dir(self):
        self.assertEqual(h3cache.prepare_command(Path("/home/you/my h3"), {}, "linux", ref=False, keep_source=True),
                         "cd '/home/you/my h3' && venv/bin/python scripts/prepare_lowvram.py --keep-source")
        self.assertEqual(h3cache.prepare_command(Path("C:/My AI/it's h3"), {}, "win32"),
                         "cd 'C:/My AI/it''s h3'; .\\venv\\Scripts\\python.exe scripts\\prepare_lowvram.py --ref")
        # キャッシュの場所を変えているときは、prepare にも同じ場所を渡す(prepare は H3_TE_PREQUANT_DIR だけを見る)
        self.assertEqual(h3cache.prepare_command(Path("/e/h3"), {"H3_TRANSFORMER_PREQUANT_DIR": "/big/pq"}, "linux"),
                         "cd /e/h3 && H3_TE_PREQUANT_DIR=/big/pq venv/bin/python scripts/prepare_lowvram.py --ref")
        self.assertEqual(h3cache.prepare_command(Path("C:/AI/h3"), {"H3_TE_PREQUANT_DIR": "/big/pq"}, "win32"),
                         "cd C:/AI/h3; $env:H3_TE_PREQUANT_DIR='/big/pq'; .\\venv\\Scripts\\python.exe scripts\\prepare_lowvram.py --ref")

    def test_the_venv_that_exists_is_used(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            touch(d / ".venv" / "bin" / "python")
            self.assertIn(" .venv/bin/python ", h3cache.prepare_command(d, {}, "linux"))
            touch(d / "venv" / "bin" / "python")
            self.assertIn(" venv/bin/python ", h3cache.prepare_command(d, {}, "linux"))      # venv が先(studio.components と同じ)
            touch(d / ".venv" / "Scripts" / "python.exe")
            self.assertIn(" .\\.venv\\Scripts\\python.exe ", h3cache.prepare_command(d, {}, "win32"))


class StateTest(unittest.TestCase, _CleanEnv):
    def setUp(self):
        self.clean_env()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.h3 = self.root / "minimax-h3"
        self.h3.mkdir()
        touch(self.h3 / "venv" / "Scripts" / "python.exe")
        touch(self.h3 / "scripts" / "prepare_lowvram.py")

    def cfg(self, **h3) -> Settings:
        return Settings({"components": {"h3": {"dir": str(self.h3), **h3}, "ace": {}}}, self.root)

    def test_windows_without_the_cache_is_needed_and_not_ready(self):
        st = h3cache.ref_cache_state(self.cfg(), plat="win32")
        self.assertEqual(st, {"needed": True, "ready": False, "dir": str(self.h3),
                              "cache_dir": str(self.h3 / "models" / "prequant" / "transformer_ref_int8"),
                              "command": f"cd {self.h3}; .\\venv\\Scripts\\python.exe scripts\\prepare_lowvram.py --ref"})
        make_cache(self.h3 / "models" / "prequant" / "transformer_ref_int8")
        self.assertEqual({k: v for k, v in h3cache.ref_cache_state(self.cfg(), plat="win32").items() if k in ("needed", "ready")},
                         {"needed": True, "ready": True})

    def test_linux_needs_it_only_below_48gb(self):
        for ram, want in ((31.9, True), (64.0, False)):
            with mock.patch.object(h3cache, "total_ram_gb", return_value=ram):
                self.assertEqual(h3cache.ref_cache_state(self.cfg(), plat="linux")["needed"], want, ram)

    def test_the_component_env_is_what_the_engine_sees(self):
        with mock.patch.object(h3cache, "total_ram_gb", return_value=128.0):
            self.assertTrue(h3cache.ref_cache_state(self.cfg(env={"H3_GROUP_STREAM_HOST": "file"}), plat="linux")["needed"])
            os.environ["H3_GROUP_STREAM_HOST"] = "file"          # 管理役の環境もエンジンに渡る(studio.toml の env が上書き)
            self.assertTrue(h3cache.ref_cache_state(self.cfg(), plat="linux")["needed"])
            self.assertFalse(h3cache.ref_cache_state(self.cfg(env={"H3_GROUP_STREAM_HOST": "pinned"}), plat="linux")["needed"])
        self.assertFalse(h3cache.ref_cache_state(self.cfg(env={"H3_GROUP_STREAM_HOST": "pinned"}), plat="win32")["needed"])
        self.assertFalse(h3cache.ref_cache_state(self.cfg(env={"H3_LOWVRAM": "1"}), plat="win32")["needed"])      # group で読まない
        moved = self.root / "pq"
        make_cache(moved / "transformer_ref_int8")
        st = h3cache.ref_cache_state(self.cfg(env={"H3_TRANSFORMER_PREQUANT_DIR": str(moved)}), plat="win32")
        self.assertEqual((st["needed"], st["ready"], st["cache_dir"]), (True, True, str(moved / "transformer_ref_int8")))
        self.assertIn(f"$env:H3_TE_PREQUANT_DIR='{moved}'", st["command"])

    def test_disabled_missing_or_old_engine_is_not_needed(self):
        self.assertFalse(h3cache.ref_cache_state(self.cfg(enabled=False), plat="win32")["needed"])
        (self.h3 / "scripts" / "prepare_lowvram.py").unlink()            # 2026-11-18 より古いエンジン: 作るコマンドが無く、この理由では断らない
        self.assertFalse(h3cache.ref_cache_state(self.cfg(), plat="win32")["needed"])
        shutil.rmtree(self.h3)
        self.assertFalse(h3cache.ref_cache_state(self.cfg(), plat="win32")["needed"])

    def test_check_raises_the_message_only_when_needed_and_missing(self):
        with mock.patch.object(h3cache, "ref_cache_state", return_value=MISSING):
            self.assertEqual(h3cache.ref_cache_error(), h3cache.missing_message(COMMAND))
            with self.assertRaises(ValueError) as cm:
                h3cache.check_ref_cache()
            self.assertEqual(str(cm.exception), h3cache.missing_message(COMMAND))
        for st in ({**MISSING, "ready": True}, {**MISSING, "needed": False}):
            with mock.patch.object(h3cache, "ref_cache_state", return_value=st):
                self.assertIsNone(h3cache.ref_cache_error())
                h3cache.check_ref_cache()


class MessageTest(unittest.TestCase):
    def test_the_command_is_kept_verbatim_in_both_languages(self):
        cmd = "cd C:\\Users\\ゆずき\\h3-creator-studio\\components\\minimax-h3; .\\venv\\Scripts\\python.exe scripts\\prepare_lowvram.py --ref"
        m = h3cache.missing_message(cmd)
        self.assertIn(f": {cmd}。", m)
        for text in (m, str(m)):                     # msg() のままでも、HTTP の detail(ただの文字)になってからでも
            en = i18n.tr(text, "en")
            self.assertIn(f": {cmd}. ", en)
            self.assertTrue(en.startswith("The MiniMax-H3 cache for the reference modes (transformer_ref_int8, about 32GB) is missing."), en)
            self.assertFalse(JP.search(en.replace(cmd, "")), en)
        self.assertIn(i18n._norm(h3cache.MISSING_TEMPLATE), {i18n._norm(k) for k in i18n.EN})
        self.assertEqual(i18n.tr(m, "ja"), m)

    def test_the_engine_refusal_is_recognized(self):
        self.assertTrue(h3cache.is_engine_refusal(ENGINE_DETAIL))
        for other in ("generation failed: CUDA out of memory", "", None):
            self.assertFalse(h3cache.is_engine_refusal(other))


class JobsApiTest(unittest.TestCase):
    """動画タブ: /api/jobs の h3.ref2va / h3.ref2i は、キャッシュが要るのに無ければ 400(normalize)。T2VA / FL2VA / T2I は通す。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.close)
        for name, ret in (("probe", {"duration_s": None, "width": None, "height": None}), ("video_thumb", False)):
            p = mock.patch.object(media, name, return_value=ret)
            p.start()
            self.addCleanup(p.stop)
        sched = types.SimpleNamespace(comps={"h3": types.SimpleNamespace(spec=None)})
        saved = getattr(main.app.state, "jobs", None)
        self.addCleanup(lambda: setattr(main.app.state, "jobs", saved))
        main.app.state.jobs = self.runner = JobRunner(Ctx(sched, self.ledger, self.root), HANDLERS)       # 始めない(submit で normalize するだけ)
        p = mock.patch.object(h3cache, "ref_cache_state", return_value=MISSING)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        img = self.root / "uploads" / "a.png"
        img.parent.mkdir()
        buf = io.BytesIO()
        Image.new("RGB", (64, 48)).save(buf, "PNG")
        img.write_bytes(buf.getvalue())
        self.img = self.ledger.add_output("image", img, "upload", filename="a.png")["id"]

    def post(self, kind, params, **kw):
        return self.client.post("/api/jobs", json={"tab": "video", "kind": kind, "params": params}, **kw)

    def test_reference_modes_are_refused_with_the_command(self):
        for kind, params in (("h3.ref2va", {"prompt": "p", "references": [self.img], "seconds": 5}),
                             ("h3.ref2i", {"prompt": "p", "references": [self.img]})):
            r = self.post(kind, params)
            self.assertEqual((r.status_code, r.json()["detail"]), (400, h3cache.missing_message(COMMAND)), kind)
            en = self.post(kind, params, headers=EN_HDR).json()["detail"]
            self.assertIn(COMMAND, en)
            self.assertFalse(JP.search(en), en)
        self.assertEqual(self.runner.jobs, {})

    def test_other_modes_still_go_through(self):
        for kind, params in (("h3.t2va", {"prompt": "p"}), ("h3.fl2va", {"prompt": "p", "first_image": self.img}), ("h3.t2i", {"prompt": "p"})):
            self.assertEqual(self.post(kind, params).status_code, 202, kind)
        self.assertEqual(len(self.runner.jobs), 3)


NODE_HARNESS = r"""
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const els = {};
const get = (id) => (els[id] = els[id] || { id, hidden: true, textContent: "", disabled: false, dataset: {}, addEventListener() {} });
const ctx = { t: (s) => s, document: { getElementById: get }, setTimeout: () => 1, clearTimeout() {}, console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[process.argv.length - 1], "utf8") + "\n;globalThis.__R = { renderToolsBox, refCacheHint };", ctx);
const { renderToolsBox, refCacheHint } = ctx.__R;
const ok = { needed: false, ready: true, hint: null };
renderToolsBox("mv", { available: true, ref_cache: ok }, false, "fb", () => {});
assert.strictEqual(get("mv-tools").hidden, true);                 // 道具もキャッシュもある: 箱ごと隠す
renderToolsBox("mv", { available: true, ref_cache: { needed: true, ready: false, hint: "HINT cmd" } }, false, "fb", () => {});
assert.strictEqual(get("mv-tools").hidden, false);                // キャッシュだけ無い: 2 行目だけ
assert.strictEqual(get("mv-tools-main").hidden, true);
assert.strictEqual(get("mv-tools-ref").hidden, false);
assert.strictEqual(get("mv-tools-ref").textContent, "HINT cmd");
renderToolsBox("mv", { available: false, hint: "TOOLS", installable: true, ref_cache: { hint: "HINT" } }, true, "fb", () => {});
assert.strictEqual(get("mv-tools-main").hidden, false);           // 両方無い: 両方の行
assert.strictEqual(get("mv-tools-text").textContent, "TOOLS");
assert.strictEqual(get("mv-tools-install").hidden, false);
assert.strictEqual(get("mv-tools-ref").textContent, "HINT");
renderToolsBox("mv", { available: true, ref_cache: ok }, false, "fb", () => {});
assert.strictEqual(get("mv-tools").hidden, true);                 // 作り終えたら消える
assert.strictEqual(get("mv-tools-ref").hidden, true);
renderToolsBox("sh", { available: true }, false, "fb", () => {});   // ref_cache の無い答え(古い管理役)でも動く
assert.strictEqual(get("sh-tools").hidden, true);
assert.strictEqual(refCacheHint(null), "");
assert.strictEqual(refCacheHint({ ref_cache: { hint: "x" } }), "x");
console.log("ok");
"""


class WebTest(unittest.TestCase):
    """画面: MV・ショート動画タブは道具の欄の 2 行目に、動画タブは作り方の下に案内を出す(ボタンは無い。文言はサーバが訳す)。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.js = {n: (WEB / n).read_text(encoding="utf-8") for n in ("project.js", "mv.js", "short.js", "video.js")}

    def test_the_hint_elements_exist(self):
        for pre in ("mv", "sh"):
            self.assertRegex(self.html, rf'<div class="msg" id="{pre}-tools" hidden><div id="{pre}-tools-main">.*?</div><div class="refhint" id="{pre}-tools-ref" hidden></div></div>')
        self.assertLess(self.html.index('id="vd-modes"'), self.html.index('id="vd-refcache"'))
        self.assertLess(self.html.index('id="vd-refcache"'), self.html.index('id="vd-prompt"'))
        self.assertIn('<div class="msg refhint" id="vd-refcache" hidden></div>', self.html)
        self.assertIn(".refhint { overflow-wrap: anywhere; }", (WEB / "style.css").read_text(encoding="utf-8"))

    def test_mv_blocks_short_only_shows_and_video_shows_for_reference_modes(self):
        self.assertIn("tools.ref_cache.hint", self.js["project.js"])
        self.assertIn("mvp(\"go\").disabled = creating || !tools.available || !!refCacheHint(tools);", self.js["mv.js"])
        self.assertNotIn("refCacheHint", self.js["short.js"])            # ショート動画は案内だけ(画像・セリフつきの作成はサーバが断る)
        self.assertIn("renderToolsBox(\"sh\"", self.js["short.js"])
        video = self.js["video.js"]
        self.assertIn('fetch("/api/mv/tools", { headers: langHeaders() })', video)
        self.assertIn('studio.on.tab.push((tab) => { if (tab === "video") loadRefHint(); });', video)
        self.assertIn('const refMode = mode === "ref2va" || (mode === "still" && refs.length > 0);', video)
        self.assertIn('vd("refcache").hidden = !(refHint && refMode);', video)

    @unittest.skipUnless(shutil.which("node"), "node が無い")
    def test_render_tools_box(self):
        r = subprocess.run(["node", "-e", NODE_HARNESS, str(WEB / "project.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ok", r.stdout)


if __name__ == "__main__":
    unittest.main()
