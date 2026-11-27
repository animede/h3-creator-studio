import asyncio
import base64
import contextlib
import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import httpx
from PIL import Image

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
        self.assertEqual(auto[auto.index("-c") + 1], "16384")
        layers = llm_mod.build_command(Path("/x/llama-server"), Path("/m.gguf"), 8701, True, 20)
        self.assertEqual(layers[layers.index("-ngl"):], ["-ngl", "20", "--fit", "off"])
        self.assertNotIn("--fit", llm_mod.build_command(Path("/x/llama-server"), Path("/m.gguf"), 8701, False, "auto"))

    def test_build_command_cpu_only(self):
        cpu = llm_mod.build_command(Path("/x/llama-server"), Path("/m.gguf"), 8701, True, "auto", cpu_only=True)
        self.assertEqual(cpu[cpu.index("-ngl"):], ["-ngl", "0", "--fit", "off"])      # CUDA 版でも、GPU には 1 層も載せない
        self.assertNotIn("--fit-target", cpu)
        plain = llm_mod.build_command(Path("/x/llama-server"), Path("/m.gguf"), 8701, False, "auto", cpu_only=True)
        self.assertNotIn("-ngl", plain)                                                # CPU 版には、もともと GPU の指定が無い


    def test_build_command_mmproj(self):
        proj = Path("/m/mmproj.gguf")
        for gpu, cpu_only in ((True, False), (True, True), (False, False)):      # GPU 版・CUDA 版の CPU 専用・CPU 版: どれでも付ける
            cmd = llm_mod.build_command(Path("/x/llama-server"), Path("/m.gguf"), 8701, gpu, "auto", cpu_only, proj)
            self.assertEqual(cmd[cmd.index("--mmproj"):cmd.index("--mmproj") + 2], ["--mmproj", str(proj)], (gpu, cpu_only))
        for args in ((True, "auto", False), (True, "auto", True), (False, "auto", False)):
            self.assertNotIn("--mmproj", llm_mod.build_command(Path("/x/llama-server"), Path("/m.gguf"), 8701, args[0], args[1], args[2]))      # 投影ファイルが無ければ付けない


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

    def test_http_fetch_reports_progress(self):
        class FakeResponse:
            headers = {"content-length": str(3 << 20)}

            def raise_for_status(self):
                pass

            def iter_bytes(self, size):
                yield from (b"x" * (1 << 20) for _ in range(3))

        seen = []
        with mock.patch.object(llm_mod.httpx, "stream", return_value=contextlib.nullcontext(FakeResponse())):
            llm_mod.http_fetch("https://example.invalid/a.zip", self.root / "a.zip", lambda d, t: seen.append((d, t)))
        self.assertEqual(seen, [(1 << 20, 3 << 20), (2 << 20, 3 << 20), (3 << 20, 3 << 20)])
        self.assertEqual((self.root / "a.zip").stat().st_size, 3 << 20)

    def test_hf_progress_class_reports_the_larger_of_two_bars(self):
        # Xet の取得は受信と書き込みの 2 本のバーを作る。多い方を知らせるので、数字が戻らない
        seen = []
        cls = llm_mod.hf_progress_class(lambda d, t: seen.append((d, t)))
        received, written = cls(total=100), cls(total=100)
        received.update(30)
        written.update(10)
        written.update(30)
        received.update(70)
        self.assertEqual(seen, [(30, 100), (30, 100), (40, 100), (100, 100)])

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

    def test_find_mmproj_order_override_and_missing(self):
        cfg = make_cfg(self.root)
        self.assertIsNone(llm_mod.find_mmproj(cfg))                                   # 無くてもテキストの chat は動く: None
        own = touch(self.root / "tools" / llm_mod.MMPROJ_FILE)
        self.assertEqual(llm_mod.find_mmproj(cfg), own)                               # tools_dir(取得先)
        ace = touch(self.root / "ace" / "easy-music" / "models" / llm_mod.MODEL_FILE)
        self.assertEqual(llm_mod.find_gguf(cfg), ace)
        self.assertEqual(llm_mod.find_mmproj(cfg), own)                               # 見つかった GGUF の隣に無ければ、tools_dir
        beside = touch(ace.parent / llm_mod.MMPROJ_FILE)
        self.assertEqual(llm_mod.find_mmproj(cfg), beside)                            # GGUF の隣が先
        self.assertEqual(llm_mod.find_mmproj(cfg, ace), beside)
        explicit = touch(self.root / "my-mmproj.gguf")
        self.assertEqual(llm_mod.find_mmproj(make_cfg(self.root, mmproj=str(explicit))), explicit)    # 設定が最優先
        with self.assertRaises(LlmError) as cm:                                       # 設定が間違っていれば、find_gguf と同じ LlmError
            llm_mod.find_mmproj(make_cfg(self.root, mmproj=str(self.root / "none.gguf")))
        self.assertIn("[llm] mmproj", cm.exception.message)
        self.assertIsNone(llm_mod.find_mmproj(make_cfg(self.root, tools_dir=str(self.root / "empty")), self.root / "elsewhere" / "m.gguf"))

    def test_download_mmproj_goes_to_the_tools_dir(self):
        cfg = make_cfg(self.root)
        fake = mock.Mock(side_effect=lambda repo_id, filename, local_dir: str(touch(Path(local_dir) / filename)))
        with mock.patch("huggingface_hub.hf_hub_download", fake):
            got = llm_mod.download_mmproj(cfg)
            model = llm_mod.download_model(cfg)
        self.assertEqual((got, model), (self.root / "tools" / llm_mod.MMPROJ_FILE, self.root / "tools" / llm_mod.MODEL_FILE))
        self.assertEqual([c.kwargs["filename"] for c in fake.call_args_list], [llm_mod.MMPROJ_FILE, llm_mod.MODEL_FILE])
        self.assertEqual({c.kwargs["repo_id"] for c in fake.call_args_list}, {llm_mod.MODEL_REPO})
        self.assertEqual(llm_mod.find_mmproj(cfg), got)
        self.assertEqual((llm_mod.MMPROJ_FILE, llm_mod.MODEL_REPO), ("mmproj-gemma-4-E4B-it-Q8_0.gguf", "ggml-org/gemma-4-E4B-it-GGUF"))

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

    async def test_chat_does_not_wait_while_generating(self):
        llm = self.make(busy=lambda: True)           # GPU の仕事が走っている。すでに別の llama-server が応答している(外部)
        self.assertEqual(await asyncio.wait_for(llm.chat("sys", "user"), 2), "こんにちは")     # 待たずに使う。思考ブロックは取り除く
        self.assertEqual((llm.starts, llm._users), (0, 0))

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

    async def test_release_leaves_cpu_only_child_alone_even_while_in_use(self):
        llm = self.make()
        with mock.patch.object(llm_mod.procman, "stop") as stop, mock.patch.object(llm_mod.procman, "is_alive", return_value=True):
            llm._proc, llm._gpu, llm._cpu_only = fake_proc(), False, True
            async with llm.session():
                self.assertFalse(await asyncio.wait_for(llm.release_before_gpu(), 1))     # VRAM を持たない子は、使用中でも待たず、止めない
            stop.assert_not_called()

    async def test_new_chat_waits_only_while_the_gpu_child_is_being_stopped(self):
        llm = self.make()
        with mock.patch.object(llm_mod.procman, "stop") as stop, mock.patch.object(llm_mod.procman, "is_alive", return_value=True):
            llm._proc, llm._gpu = fake_proc(), True

            async def enter():
                async with llm.session():
                    return True
            async with llm.session():                       # 使用中の chat の終わりを、止める側が待っている
                releasing = asyncio.create_task(llm.release_before_gpu())
                await asyncio.sleep(0.05)
                entering = asyncio.create_task(enter())
                await asyncio.sleep(0.1)
                self.assertFalse(entering.done())           # 止まる子には、新しい chat を載せない
            self.assertTrue(await asyncio.wait_for(releasing, 2))
            self.assertTrue(await asyncio.wait_for(entering, 2))      # 止まったら入れる(次は CPU 専用の子で動く)
            stop.assert_called_once()
            self.assertFalse(llm._releasing)

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

    def gpu_files(self):
        touch(self.root / "tools" / (llm_mod.TAG + "-cuda") / "llama-server")
        touch(self.root / "tools" / llm_mod.MODEL_FILE)

    def launcher(self, calls, started):
        def fake_start(spec):
            started.append(spec)
            return fake_proc()
        return (mock.patch.object(llm_mod, "use_gpu", return_value=True), mock.patch.object(llm_mod.procman, "start", fake_start),
                mock.patch.object(llm_mod.procman, "is_alive", return_value=True), mock.patch.object(llm_mod, "smi_mib", return_value=9000))

    async def test_chat_during_gpu_work_starts_cpu_only_child_instead_of_waiting(self):
        self.gpu_files()
        calls, started, busy = [], [], {"v": True}
        llm = self.make(calls, busy=lambda: busy["v"])
        llm.transport = chat_transport(calls, health_after=3)
        patches = self.launcher(calls, started)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(await asyncio.wait_for(llm.chat("sys", "user"), 5), "こんにちは")      # GPU の仕事が走っていても、待たずに答える
        (spec,) = started
        self.assertEqual(spec.cmd[spec.cmd.index("-ngl"):], ["-ngl", "0", "--fit", "off"])
        self.assertNotIn("--fit-target", spec.cmd)
        self.assertEqual(spec.env(None, None), {"CUDA_VISIBLE_DEVICES": "-1"})                  # CUDA を隠す(CUDA 版のまま、VRAM を持たない)
        self.assertEqual((llm._gpu, llm._cpu_only, llm.starts), (False, True, 1))
        st = llm.status()
        self.assertEqual((st["gpu"], st["cpu_only"], st["gpu_work"]), (False, True, True))
        self.assertEqual(await llm.ensure_running(), llm.base_url)                            # GPU の仕事が続くかぎり、そのまま使う
        self.assertEqual(llm.starts, 1)

    async def test_returns_to_gpu_mode_only_when_no_gpu_work_is_running_or_queued(self):
        self.gpu_files()
        calls, started, busy = [], [], {"v": True}
        llm = self.make(calls, busy=lambda: busy["v"])
        llm.transport = chat_transport(calls, health_after=3)
        for p in self.launcher(calls, started):
            p.start()
            self.addCleanup(p.stop)
        await llm.ensure_running()
        self.assertTrue(llm._cpu_only)
        with mock.patch.object(llm_mod.procman, "stop") as stop:
            await llm.ensure_running()                           # まだ仕事が走っている(か、順番を待っている): 戻さない
            stop.assert_not_called()
            busy["v"] = False
            async with llm.session():
                async with llm.session():                        # ほかの chat が CPU 専用の子を使っている最中も、戻さない
                    await llm.ensure_running()
                stop.assert_not_called()
                self.assertEqual(llm.starts, 1)
                await llm.ensure_running()                       # 仕事が無く、自分だけなら、GPU 版へ戻す
            stop.assert_called_once()
        self.assertEqual(llm.starts, 2)
        gpu_spec = started[1]
        self.assertEqual(gpu_spec.cmd[gpu_spec.cmd.index("--fit"):], ["--fit", "on", "--fit-target", "1536"])
        self.assertEqual(gpu_spec.env(None, None), {})
        self.assertEqual((llm._gpu, llm._cpu_only), (True, False))
        busy["v"] = True
        await llm.ensure_running()                               # GPU 版は、生成が始まっても止めない(止めるのは release_before_gpu だけ)
        self.assertEqual(llm.starts, 2)

    async def test_gpu_setting_off_never_hides_cuda(self):
        touch(self.root / "tools" / llm_mod.TAG / "llama-server")
        touch(self.root / "tools" / llm_mod.MODEL_FILE)
        calls, started = [], []
        llm = self.make(calls, busy=lambda: True)
        llm.transport = chat_transport(calls, health_after=3)
        for p in self.launcher(calls, started):
            p.start()
            self.addCleanup(p.stop)
        with mock.patch.object(llm_mod, "use_gpu", return_value=False):      # [llm] gpu = "off"(CPU 版)
            await llm.ensure_running()
        (spec,) = started
        self.assertNotIn("-ngl", spec.cmd)
        self.assertEqual(spec.env(None, None), {})
        self.assertEqual((llm._gpu, llm._cpu_only), (False, False))

    # ---- 画像を読む(chat_image) ----

    def recorder(self, seen: list, status=200, text=""):
        """/health は 200。それ以外(chat)は本文を seen に残して、status が 200 なら答えを返す。"""
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/health":
                return httpx.Response(200)
            seen.append(json.loads(req.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "<think>x</think>\n見た答え"}}]}) if status == 200 else httpx.Response(status, text=text)
        return httpx.MockTransport(handler)

    @staticmethod
    def png(size=(64, 48), mode="RGB", color=(10, 120, 200), fmt="PNG", **save) -> bytes:
        buf = io.BytesIO()
        Image.new(mode, size, color).save(buf, fmt, **save)
        return buf.getvalue()

    @staticmethod
    def uri_image(payload: dict):
        """メッセージの画像(data URI)を (MIME, バイト列) にする。"""
        url = payload["messages"][1]["content"][1]["image_url"]["url"]
        head, _, b64 = url.partition(",")
        return head[len("data:"):-len(";base64")], base64.b64decode(b64)

    async def test_chat_image_sends_text_and_image_parts_as_a_data_uri(self):
        seen = []
        llm = Llm(make_cfg(self.root), transport=self.recorder(seen), poll_s=0.01, settle_s=0)         # すでに別の llama-server が応答している(外部)
        png = self.png()
        with mock.patch.object(llm_mod, "download_mmproj") as download:
            answer = await llm.chat_image("sys", "この画像は?", png, "image/png", max_tokens=321, temperature=0.25)
        self.assertEqual(answer, "見た答え")                                                          # 思考ブロックは取り除く
        download.assert_not_called()                                                                   # 外部の llama-server には、こちらの投影ファイルは関係しない
        (payload,) = seen
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "sys"})
        user = payload["messages"][1]
        self.assertEqual((user["role"], [part["type"] for part in user["content"]]), ("user", ["text", "image_url"]))
        self.assertEqual(user["content"][0]["text"], "この画像は?")
        self.assertEqual((payload["max_tokens"], payload["temperature"]), (321, 0.25))
        self.assertEqual(self.uri_image(payload), ("image/png", png))                                  # 小さな PNG は、そのまま
        self.assertEqual(llm._users, 0)

    async def test_chat_image_shrinks_large_images_and_converts_unreadable_formats(self):
        seen = []
        llm = Llm(make_cfg(self.root), transport=self.recorder(seen), poll_s=0.01, settle_s=0)
        cases = {
            "large png": (self.png((3000, 1500)), "image/png", (1024, 512)),                            # 長辺 1024 に縮める(縦横比は保つ)
            "tall jpeg": (self.png((900, 2400), fmt="JPEG"), "image/jpeg", (384, 1024)),
            "webp": (self.png((100, 80), fmt="WEBP"), "image/webp", (100, 80)),                         # llama.cpp は WebP を読めない
            "alpha png": (self.png((100, 80), "RGBA", (255, 0, 0, 0)), "image/png", (100, 80)),         # 透明は白で埋める
        }
        for name, (data, mime, size) in cases.items():
            seen.clear()
            await llm.chat_image("s", "u", data, mime)
            got_mime, got = self.uri_image(seen[0])
            im = Image.open(io.BytesIO(got))
            self.assertEqual((got_mime, im.format, im.size), ("image/jpeg", "JPEG", size), name)
        self.assertEqual(Image.open(io.BytesIO(got)).convert("RGB").getpixel((5, 5)), (255, 255, 255))   # 透明な赤は、白になる
        # 小さな PNG / JPEG はそのまま。画像として読めないものも、そのまま(LLM が断れば、その理由が出る)
        small_jpeg, junk = self.png((200, 100), fmt="JPEG"), b"not an image"
        for data, mime, want in ((small_jpeg, "image/png", "image/jpeg"), (junk, "image/gif", "image/gif")):
            seen.clear()
            await llm.chat_image("s", "u", data, mime)
            self.assertEqual(self.uri_image(seen[0]), (want, data))

    def test_shrink_image_applies_the_exif_orientation_and_is_exact_for_small_files(self):
        exif = Image.Exif()
        exif[0x0112] = 6                                                                                # 時計回りに 90 度回して見る写真
        data, mime = llm_mod.shrink_image(self.png((200, 100), fmt="JPEG", exif=exif), "image/jpeg")
        self.assertEqual((mime, Image.open(io.BytesIO(data)).size), ("image/jpeg", (100, 200)))
        plain = self.png((64, 48))
        self.assertEqual(llm_mod.shrink_image(plain, "image/png"), (plain, "image/png"))
        self.assertEqual(llm_mod.shrink_image(plain, "image/png", max_side=32)[1], "image/jpeg")        # 上限を変えられる

    async def test_chat_image_without_the_projector_fetches_it_once_and_starts_with_it(self):
        self.gpu_files()
        calls, started, fetched = [], [], []

        seen_download = []

        def fake_download(cfg, progress):
            fetched.append(1)
            progress(300 << 20, 600 << 20)                                                              # 取得の途中: 画面に進み具合が出る
            seen_download.append((llm.panel()["download"], llm.status()["download"]))
            return touch(self.root / "tools" / llm_mod.MMPROJ_FILE)
        llm = self.make(calls)
        llm.transport = chat_transport(calls, health_after=3)                                           # 自分で起こす(外部の llama-server は居ない)
        for patch in self.launcher(calls, started):
            patch.start()
            self.addCleanup(patch.stop)
        with mock.patch.object(llm_mod, "download_mmproj", fake_download):
            self.assertEqual(await asyncio.wait_for(llm.chat_image("s", "u", self.png()), 5), "こんにちは")
            await llm.chat_image("s", "u", self.png())                                                  # 2 回目は、取得しない
        self.assertEqual((len(fetched), llm.starts, llm._with_projector), (1, 1, True))
        want = {"what": "mmproj", "done": 300 << 20, "total": 600 << 20}
        with mock.patch.object(llm_mod, "smi_mib", return_value=None):
            self.assertEqual(seen_download, [(want, want)])
            self.assertIsNone(llm.panel()["download"])                                                   # 終わったら消える
        (spec,) = started
        self.assertEqual(spec.cmd[spec.cmd.index("--mmproj") + 1], str(self.root / "tools" / llm_mod.MMPROJ_FILE))

    async def test_running_child_without_the_projector_is_restarted_for_images(self):
        self.gpu_files()
        calls, started = [], []
        llm = self.make(calls)
        llm.transport = chat_transport(calls, health_after=3)
        for patch in self.launcher(calls, started):
            patch.start()
            self.addCleanup(patch.stop)
        with mock.patch.object(llm_mod.procman, "stop") as stop:
            await llm.chat("s", "u")                                                                    # 投影ファイルが無い間に、テキストで起こした子
            self.assertEqual((llm.starts, llm._with_projector, "--mmproj" in started[0].cmd), (1, False, False))
            touch(self.root / "tools" / llm_mod.MMPROJ_FILE)                                            # そのあと投影ファイルが用意できた(setup --llm など)
            async with llm.session():                                                                   # ほかの chat が使っている最中は、切らない
                async with llm.session():
                    await llm.ensure_running(projector=True)
                stop.assert_not_called()
                self.assertEqual(llm.starts, 1)
            await llm.chat_image("s", "u", self.png())                                                  # 他に誰も居なければ、投影ファイルつきで起こし直す
            stop.assert_called_once()
        self.assertEqual((llm.starts, llm._with_projector), (2, True))
        self.assertIn("--mmproj", started[1].cmd)
        await llm.chat_image("s", "u", self.png())                                                      # もう起こし直さない
        self.assertEqual(llm.starts, 2)

    async def test_a_projector_that_cannot_be_loaded_falls_back_to_text_only(self):
        self.gpu_files()
        touch(self.root / "tools" / llm_mod.MMPROJ_FILE)
        calls, started, procs = [], [], []

        def fake_start(spec):
            started.append(spec)
            procs.append(fake_proc())
            return procs[-1]

        def alive(proc):
            return "--mmproj" not in started[procs.index(proc)].cmd                              # 投影ファイルつきの子は、読み込めずにすぐ終了する
        llm = self.make(calls)
        llm.transport = chat_transport(calls, health_after=3)
        for patch in (mock.patch.object(llm_mod, "use_gpu", return_value=True), mock.patch.object(llm_mod.procman, "start", fake_start),
                      mock.patch.object(llm_mod.procman, "is_alive", alive), mock.patch.object(llm_mod, "smi_mib", return_value=9000),
                      mock.patch.object(llm_mod.procman, "log_tail", return_value="")):
            patch.start()
            self.addCleanup(patch.stop)
        self.assertEqual(await asyncio.wait_for(llm.chat("s", "u"), 5), "こんにちは")             # テキストの chat は、画像なしで起動し直して動く
        self.assertEqual((llm.starts, llm._with_projector, llm._projector_failed), (2, False, True))
        self.assertIn("--mmproj", started[0].cmd)
        self.assertNotIn("--mmproj", started[1].cmd)
        with self.assertRaises(LlmError) as cm:                                                  # 画像は、わけを伝えて断る(起動し直しを繰り返さない)
            await llm.chat_image("s", "u", self.png())
        self.assertIn("画像を読めません", cm.exception.message)
        self.assertIn("logs/llm.log", cm.exception.message)
        self.assertEqual(llm.starts, 2)
        self.assertEqual(await llm.chat("s", "u"), "こんにちは")

    async def test_a_startup_failure_without_a_projector_is_still_an_error(self):
        self.gpu_files()
        calls = []
        llm = self.make(calls)
        for patch in (mock.patch.object(llm_mod, "use_gpu", return_value=True), mock.patch.object(llm_mod.procman, "start", lambda spec: fake_proc()),
                      mock.patch.object(llm_mod.procman, "is_alive", return_value=False), mock.patch.object(llm_mod, "smi_mib", return_value=9000),
                      mock.patch.object(llm_mod.procman, "log_tail", return_value="")):
            patch.start()
            self.addCleanup(patch.stop)
        llm.transport = chat_transport(calls, health_after=99)
        with self.assertRaises(LlmError) as cm:                                                  # 投影ファイルが無ければ、これまでどおり(やり直さない)
            await llm.chat("s", "u")
        self.assertIn("途中で終了しました", cm.exception.message)
        self.assertEqual((llm.starts, llm._projector_failed), (1, False))

    async def test_projector_download_failure_gives_a_hint_and_text_chat_still_works(self):
        self.gpu_files()
        calls, started = [], []
        llm = self.make(calls)
        llm.transport = chat_transport(calls, health_after=3)
        for patch in self.launcher(calls, started):
            patch.start()
            self.addCleanup(patch.stop)
        with mock.patch.object(llm_mod, "download_mmproj", side_effect=OSError("network down")) as download:
            with self.assertRaises(LlmError) as cm:
                await llm.chat_image("s", "u", self.png())
        download.assert_called_once()                                                                   # 取得は 1 回だけ
        for word in (llm_mod.MMPROJ_FILE, "約 0.56GB", "network down", "setup --llm", "[llm] mmproj"):
            self.assertIn(word, cm.exception.message)
        self.assertEqual(await llm.chat("s", "u"), "こんにちは")                                          # 投影ファイルが無くても、テキストの chat は動く
        self.assertEqual(llm._users, 0)

    async def test_wrong_mmproj_setting_only_disables_images(self):
        self.gpu_files()
        calls, started = [], []
        llm = Llm(make_cfg(self.root, mmproj=str(self.root / "nope.gguf")), transport=chat_transport(calls, health_after=3), poll_s=0.01, settle_s=0)
        for patch in self.launcher(calls, started):
            patch.start()
            self.addCleanup(patch.stop)
        self.assertEqual(await llm.chat("s", "u"), "こんにちは")                                          # テキストは動く(警告だけ)
        self.assertNotIn("--mmproj", started[0].cmd)
        with mock.patch.object(llm_mod, "download_mmproj") as download:
            with self.assertRaises(LlmError) as cm:
                await llm.chat_image("s", "u", self.png())
        self.assertIn("[llm] mmproj のファイルがありません", cm.exception.message)                       # 設定の間違いを、そのまま伝える(勝手に取得しない)
        download.assert_not_called()
        self.assertIsNone(llm.status()["mmproj"])

    async def test_chat_image_explains_a_server_without_the_projector(self):
        seen = []
        text = "image input is not supported - hint: if this is unexpected, you may need to provide the mmproj"
        llm = Llm(make_cfg(self.root), transport=self.recorder(seen, 500, text), poll_s=0.01, settle_s=0)
        with self.assertRaises(LlmError) as cm:
            await llm.chat_image("s", "u", self.png())
        self.assertIn("画像を読めません", cm.exception.message)
        self.assertIn("mmproj", cm.exception.message)
        with self.assertRaises(LlmError) as cm:                                                         # ほかのエラーは、そのまま
            await Llm(make_cfg(self.root), transport=self.recorder(seen, 503, "busy"), poll_s=0.01, settle_s=0).chat_image("s", "u", self.png())
        self.assertIn("HTTP 503", cm.exception.message)

    @staticmethod
    def wav(seconds=1.0, rate=16000) -> bytes:
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"\x00\x00" * int(rate * seconds))
        return buf.getvalue()

    async def test_chat_audio_sends_the_text_and_every_clip_as_input_audio(self):
        seen = []
        llm = Llm(make_cfg(self.root), transport=self.recorder(seen), poll_s=0.01, settle_s=0)         # すでに別の llama-server が応答している(外部)
        first, second = self.wav(1.0), self.wav(2.0)
        with mock.patch.object(llm_mod, "download_mmproj") as download:
            answer = await llm.chat_audio("sys", "どんな曲?", [first, second], max_tokens=222, temperature=0.1)
        self.assertEqual(answer, "見た答え")                                                          # 思考ブロックは取り除く
        download.assert_not_called()
        (payload,) = seen
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "sys"})
        user = payload["messages"][1]
        self.assertEqual([part["type"] for part in user["content"]], ["text", "input_audio", "input_audio"])
        self.assertEqual(user["content"][0]["text"], "どんな曲?")
        for part, clip in zip(user["content"][1:], (first, second)):
            self.assertEqual(part["input_audio"]["format"], "wav")
            self.assertEqual(base64.b64decode(part["input_audio"]["data"]), clip)                      # 渡した WAV のまま
        self.assertEqual((payload["max_tokens"], payload["temperature"]), (222, 0.1))
        self.assertEqual(llm._users, 0)

    async def test_chat_audio_fetches_the_same_projector_and_starts_with_it(self):
        self.gpu_files()
        calls, started, fetched = [], [], []

        def fake_download(cfg, progress=None):
            fetched.append(1)
            return touch(self.root / "tools" / llm_mod.MMPROJ_FILE)
        llm = self.make(calls)
        llm.transport = chat_transport(calls, health_after=3)
        for patch in self.launcher(calls, started):
            patch.start()
            self.addCleanup(patch.stop)
        with mock.patch.object(llm_mod, "download_mmproj", fake_download):
            self.assertEqual(await asyncio.wait_for(llm.chat_audio("s", "u", [self.wav()]), 5), "こんにちは")
            await llm.chat_image("s", "u", self.png())                                                  # 画像も同じ投影ファイル: もう取得しない・起動し直さない
        self.assertEqual((len(fetched), llm.starts, llm._with_projector), (1, 1, True))
        self.assertEqual(started[0].cmd[started[0].cmd.index("--mmproj") + 1], str(self.root / "tools" / llm_mod.MMPROJ_FILE))

    async def test_chat_audio_explains_a_server_without_the_projector(self):
        seen = []
        text = "audio input is not supported - hint: if this is unexpected, you may need to provide the mmproj"
        llm = Llm(make_cfg(self.root), transport=self.recorder(seen, 500, text), poll_s=0.01, settle_s=0)
        with self.assertRaises(LlmError) as cm:
            await llm.chat_audio("s", "u", [self.wav()])
        self.assertIn("音声を読めません", cm.exception.message)
        self.assertIn("mmproj", cm.exception.message)
        with self.assertRaises(LlmError) as cm:                                                         # 画像は画像と言う
            await Llm(make_cfg(self.root), transport=self.recorder(seen, 500, "image input is not supported"), poll_s=0.01, settle_s=0).chat_image("s", "u", self.png())
        self.assertIn("画像を読めません", cm.exception.message)

    async def test_chat_audio_after_a_projector_that_could_not_be_loaded_says_audio(self):
        self.gpu_files()
        touch(self.root / "tools" / llm_mod.MMPROJ_FILE)
        calls, started, procs = [], [], []

        def fake_start(spec):
            started.append(spec)
            procs.append(fake_proc())
            return procs[-1]
        llm = self.make(calls)
        llm.transport = chat_transport(calls, health_after=3)
        for patch in (mock.patch.object(llm_mod, "use_gpu", return_value=True), mock.patch.object(llm_mod.procman, "start", fake_start),
                      mock.patch.object(llm_mod.procman, "is_alive", lambda proc: "--mmproj" not in started[procs.index(proc)].cmd),
                      mock.patch.object(llm_mod, "smi_mib", return_value=9000), mock.patch.object(llm_mod.procman, "log_tail", return_value="")):
            patch.start()
            self.addCleanup(patch.stop)
        with self.assertRaises(LlmError) as cm:
            await llm.chat_audio("s", "u", [self.wav()])
        self.assertIn("音声を読めません", cm.exception.message)
        self.assertEqual(llm.starts, 2)                                                                 # 投影ファイルつき → なし(1 回だけ起動し直す)

    async def test_model_download_progress_shows_while_starting(self):
        llm = self.make()
        llm.transport = httpx.MockTransport(lambda r: httpx.Response(503))
        seen = []

        def fake_model(cfg, progress):
            progress(1 << 30, 4 << 30)
            seen.append(llm.panel()["download"])
            raise OSError("network down")
        with mock.patch.object(llm_mod, "use_gpu", return_value=False), \
                mock.patch.object(llm_mod, "find_binary", return_value=self.root / "llama-server"), \
                mock.patch.object(llm_mod, "find_gguf", return_value=None), mock.patch.object(llm_mod, "download_model", fake_model):
            with self.assertRaises(LlmError):
                await llm.ensure_running()
        self.assertEqual(seen, [{"what": "model", "done": 1 << 30, "total": 4 << 30}])
        self.assertIsNone(llm.panel()["download"])                  # 失敗しても残さない

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
