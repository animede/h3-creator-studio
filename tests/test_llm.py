import asyncio
import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import httpx

from studio import llm as llm_mod
from studio.config import Settings
from studio.llm import Llm, LlmError, parse_gpu_setting


def make_cfg(root: Path, **llm) -> Settings:
    return Settings({"llm": {"tools_dir": str(root / "tools"), **llm},
                     "components": {"ace": {"dir": str(root / "ace")}, "h3": {"dir": str(root / "h3")}}}, root)


def touch(p: Path, text: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


class FakePopen:
    returncode = None

    def poll(self):
        return self.returncode


def fake_proc():
    return llm_mod.procman.Proc(spec=None, popen=FakePopen(), pid=1)


def tar_gz(path: Path, files: dict) -> None:
    with tarfile.open(path, "w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))


class SettingsTest(unittest.TestCase):
    def test_gpu_setting(self):
        self.assertEqual([parse_gpu_setting(v) for v in ("auto", "AUTO", "", "on", "off", "false", "cpu", "all", "20", 33, "???")],
                         ["auto", "auto", "auto", "auto", "off", "off", "off", "all", 20, 33, "auto"])

    def test_use_gpu(self):
        free = lambda field: 4000
        self.assertFalse(llm_mod.use_gpu("off", "linux", "x86_64", free))
        self.assertTrue(llm_mod.use_gpu("auto", "linux", "x86_64", free))
        self.assertTrue(llm_mod.use_gpu("auto", "win32", "AMD64", free))
        self.assertFalse(llm_mod.use_gpu("auto", "linux", "x86_64", lambda f: None))    # nvidia-smi が無い
        self.assertTrue(llm_mod.use_gpu("all", "linux", "x86_64", lambda f: None))      # 明示すれば nvidia-smi 無しでも CUDA 版
        self.assertFalse(llm_mod.use_gpu("auto", "linux", "aarch64", free))             # CUDA 版は x64 だけ
        self.assertFalse(llm_mod.use_gpu(20, "darwin", "x86_64", free))

    def test_release_asset(self):
        self.assertEqual(llm_mod.release_asset(True, "b1", "linux", "x86_64"),
                         ("llama-b1-bin-ubuntu-cuda-12.8-x64.tar.gz", "llama-server", "cudart-llama-b1-bin-ubuntu-cuda-12.8-x64.tar.gz"))
        self.assertEqual(llm_mod.release_asset(True, "b1", "win32", "AMD64"),
                         ("llama-b1-bin-win-cuda-12.4-x64.zip", "llama-server.exe", "cudart-llama-bin-win-cuda-12.4-x64.zip"))
        self.assertEqual(llm_mod.release_asset(False, "b1", "linux", "x86_64")[0], "llama-b1-bin-ubuntu-x64.tar.gz")
        self.assertIsNone(llm_mod.release_asset(False, "b1", "darwin", "arm64"))

    def test_build_command(self):
        auto = llm_mod.build_command(Path("/x/llama-server"), Path("/m.gguf"), 8701, True, "auto")
        self.assertEqual(auto[auto.index("--fit"):], ["--fit", "on", "--fit-target", "1536"])
        self.assertEqual(auto[auto.index("--reasoning") + 1], "off")
        self.assertEqual(auto[auto.index("-c") + 1], "8192")
        layers = llm_mod.build_command(Path("/x/llama-server"), Path("/m.gguf"), 8701, True, 20)
        self.assertEqual(layers[layers.index("-ngl"):], ["-ngl", "20", "--fit", "off"])
        self.assertNotIn("--fit", llm_mod.build_command(Path("/x/llama-server"), Path("/m.gguf"), 8701, False, "auto"))


class FilesTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def test_extract_flattens_single_top_dir(self):
        a = self.root / "a.tar.gz"
        tar_gz(a, {"llama-b1/llama-server": b"exe", "llama-b1/libggml.so": b"lib"})
        z = self.root / "b.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("llama-b1/cudart64_12.dll", b"dll")
        dest = self.root / "out"
        llm_mod.extract_archive(a, dest)
        llm_mod.extract_archive(z, dest)             # 2 つ目(cudart)も同じ場所へ
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ["cudart64_12.dll", "libggml.so", "llama-server"])

    def test_extract_keeps_flat_archive(self):
        a = self.root / "a.tar.gz"
        tar_gz(a, {"llama-server": b"exe", "libggml.so": b"lib"})
        llm_mod.extract_archive(a, self.root / "out")
        self.assertEqual(sorted(p.name for p in (self.root / "out").iterdir()), ["libggml.so", "llama-server"])

    def test_install_binary_with_fake_fetch(self):
        fetched = []

        def fetch(url, dest):
            fetched.append(url.rsplit("/", 1)[1])
            tar_gz(dest, {"llama-b1/llama-server": b"exe"} if "cudart" not in url else {"libcudart.so.12": b"lib"})

        dest = self.root / "b1-cuda"
        with mock.patch("sys.platform", "linux"), mock.patch("platform.machine", return_value="x86_64"):
            exe = llm_mod.install_binary(dest, True, fetch, tag="b1")
        self.assertEqual(exe, dest / "llama-server")
        self.assertTrue((dest / "libcudart.so.12").is_file())
        self.assertFalse((dest / "_download").exists())          # 取得したアーカイブは消す
        self.assertEqual(fetched, ["llama-b1-bin-ubuntu-cuda-12.8-x64.tar.gz", "cudart-llama-b1-bin-ubuntu-cuda-12.8-x64.tar.gz"])

    def test_install_binary_unsupported_platform(self):
        with mock.patch("sys.platform", "darwin"), mock.patch("platform.machine", return_value="arm64"):
            with self.assertRaises(LlmError) as cm:
                llm_mod.install_binary(self.root / "x", False, lambda u, d: None)
        self.assertIn("llama_server", cm.exception.message)

    def test_find_gguf_order_and_override(self):
        cfg = make_cfg(self.root)
        self.assertIsNone(llm_mod.find_gguf(cfg))
        h3 = touch(self.root / "h3" / "models" / "llm" / llm_mod.MODEL_FILE)
        self.assertEqual(llm_mod.find_gguf(cfg), h3)
        ace = touch(self.root / "ace" / "easy-music" / "models" / llm_mod.MODEL_FILE)
        self.assertEqual(llm_mod.find_gguf(cfg), ace)            # ACE のコピーを h3 より先に
        own = touch(self.root / "tools" / llm_mod.MODEL_FILE)
        self.assertEqual(llm_mod.find_gguf(cfg), own)            # 自分で取得したものが最優先
        explicit = touch(self.root / "my.gguf")
        self.assertEqual(llm_mod.find_gguf(make_cfg(self.root, gguf=str(explicit))), explicit)
        with self.assertRaises(LlmError):
            llm_mod.find_gguf(make_cfg(self.root, gguf=str(self.root / "none.gguf")))

    def test_find_binary_prefers_override_then_tools_dir(self):
        cfg = make_cfg(self.root)
        self.assertIsNone(llm_mod.find_binary(cfg, True))
        local = touch(self.root / "tools" / (llm_mod.TAG + "-cuda") / "llama-server")
        self.assertEqual(llm_mod.find_binary(cfg, True), local)
        mine = touch(self.root / "bin" / "llama-server")
        self.assertEqual(llm_mod.find_binary(make_cfg(self.root, llama_server=str(mine)), True), mine)
        with self.assertRaises(LlmError):
            llm_mod.find_binary(make_cfg(self.root, llama_server=str(self.root / "nope")), True)


def chat_transport(calls: list, health_after: int = 0):
    """/health は health_after 回目から 200。/v1/chat/completions は思考チャネル付きで返す。"""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health":
            calls.append("health")
            return httpx.Response(200 if calls.count("health") > health_after else 503)
        calls.append(req.url.path)
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think>考え中</think>\nこんにちは"}}]})
    return httpx.MockTransport(handler)


class LlmTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.root = Path(self._t.name)

    def make(self, calls=None, **kw) -> Llm:
        return Llm(make_cfg(self.root), transport=chat_transport(calls if calls is not None else []), poll_s=0.01, settle_s=0, **kw)

    async def test_chat_waits_while_generating(self):
        busy = {"v": True}
        llm = self.make(busy=lambda: busy["v"])      # すでに別の llama-server が応答している(外部)
        task = asyncio.create_task(llm.chat("sys", "user"))
        await asyncio.sleep(0.1)
        self.assertFalse(task.done())                # 生成中は待つ(GPU で起こさない)
        self.assertEqual(llm.starts, 0)
        busy["v"] = False
        self.assertEqual(await asyncio.wait_for(task, 2), "こんにちは")     # 思考ブロックは取り除く
        self.assertEqual(llm._users, 0)

    async def test_release_stops_cuda_child_and_keeps_cpu_child(self):
        llm = self.make()
        with mock.patch.object(llm_mod.procman, "stop") as stop, mock.patch.object(llm_mod.procman, "is_alive", return_value=True):
            llm._proc, llm._gpu = fake_proc(), True
            self.assertTrue(await llm.release_before_gpu())
            stop.assert_called_once()
            self.assertIsNone(llm._proc)
            self.assertFalse(await llm.release_before_gpu())        # もう居ない

            stop.reset_mock()
            llm._proc, llm._gpu = fake_proc(), False                # CPU だけの子は VRAM を持たないので止めない
            self.assertFalse(await llm.release_before_gpu())
            stop.assert_not_called()

    async def test_release_waits_for_chat_in_progress(self):
        llm = self.make()
        with mock.patch.object(llm_mod.procman, "stop") as stop, mock.patch.object(llm_mod.procman, "is_alive", return_value=True):
            llm._proc, llm._gpu = fake_proc(), True
            async with llm.session():
                task = asyncio.create_task(llm.release_before_gpu())
                await asyncio.sleep(0.1)
                self.assertFalse(task.done())
                stop.assert_not_called()                           # 使っている最中は止めない
            self.assertTrue(await asyncio.wait_for(task, 2))
            stop.assert_called_once()

    async def test_ensure_running_starts_child_with_fit(self):
        touch(self.root / "tools" / (llm_mod.TAG + "-cuda") / "llama-server")
        touch(self.root / "tools" / llm_mod.MODEL_FILE)
        calls = []
        llm = self.make(calls)
        llm.transport = chat_transport(calls, health_after=3)          # 起動直後は応じず、少しして 200
        started = []

        def fake_start(spec):
            started.append(spec)
            return fake_proc()
        with mock.patch.object(llm_mod, "use_gpu", return_value=True), mock.patch.object(llm_mod.procman, "start", fake_start), \
                mock.patch.object(llm_mod.procman, "is_alive", return_value=True), mock.patch.object(llm_mod, "smi_mib", return_value=9000):
            base = await llm.ensure_running()
            self.assertEqual(base, "http://127.0.0.1:8701")
            self.assertEqual(await llm.ensure_running(), base)         # 2 回目は起動しない
        self.assertEqual(llm.starts, 1)
        self.assertEqual(len(started), 1)
        self.assertEqual((started[0].name, started[0].dir.name), ("llm", llm_mod.TAG + "-cuda"))
        self.assertIn("--fit-target", started[0].cmd)
        self.assertTrue(llm._gpu)

    async def test_missing_files_raise_japanese_error(self):
        llm = self.make()
        llm.transport = httpx.MockTransport(lambda r: httpx.Response(503))
        with mock.patch.object(llm_mod, "use_gpu", return_value=True), \
                mock.patch.object(llm_mod, "install_binary", side_effect=OSError("network down")):
            with self.assertRaises(LlmError) as cm:
                await llm.ensure_running()
        self.assertIn("[llm]", cm.exception.message)
        self.assertEqual(llm._error, cm.exception.message)


if __name__ == "__main__":
    unittest.main()
