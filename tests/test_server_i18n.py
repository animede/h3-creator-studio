"""サーバの文言の英訳(studio/i18n.py と main.py のミドルウェア)。日本語の画面にはこれまでどおり日本語を返す。"""
import ast
import re
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from studio import i18n, main
from studio.i18n import msg, tr
from studio.jobs import Job
from tests import test_edit_api, test_gallery_api, test_mv_api

EN_HDR = {"Accept-Language": "en"}
STUDIO = Path(__file__).resolve().parent.parent / "studio"
JP = re.compile(r"[぀-ヿ一-鿿]")

# 文言を画面に返す口: 例外(HTTP のエラー・ジョブの失敗になる)と、進み具合・経過の文
RAISERS = {"ValueError", "LookupError", "JobFailed", "HTTPException", "SwitchError", "Busy", "NothingFound", "UserError", "LlmError",
           "EnhanceError", "AnalyzeError", "MvAudioError", "MvToolsMissing", "ExportBusy", "PresetError", "SampleError", "GenreError",
           "SongPlanError", "ScenarioError", "TtsError"}


def templates(node) -> list:
    """式から、文の型(差し込む値を {} にしたもの)を全部出す。f-string・+ でつないだ文・条件式の両方の枝。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value.replace("{", "{{").replace("}", "}}")]
    if isinstance(node, ast.JoinedStr):
        return ["".join(v.value.replace("{", "{{").replace("}", "}}") if isinstance(v, ast.Constant) else "{}" for v in node.values)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [a + b for a in templates(node.left) for b in templates(node.right)]
    if isinstance(node, ast.IfExp):
        return templates(node.body) + templates(node.orelse)
    return ["{}"]


def _name(f):
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def message_sites():
    """studio/ の中の、画面に届く文言の (ファイル:行, 型) を全部。`# i18n-skip` の行は除く。"""
    for path in sorted(STUDIO.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        for node in ast.walk(ast.parse(src)):
            exprs = []
            if isinstance(node, ast.Call) and (_name(node.func) in RAISERS or _name(node.func) == "add"):
                exprs = node.args[:2]
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Attribute) and t.attr == "message":
                        exprs = [node.value]
                    elif isinstance(t, ast.Tuple) and isinstance(node.value, ast.Tuple):
                        exprs += [v for e, v in zip(t.elts, node.value.elts) if isinstance(e, ast.Attribute) and e.attr == "message"]
            if not exprs or "# i18n-skip" in lines[node.lineno - 1]:
                continue
            for e in exprs:
                for t in templates(e):
                    if JP.search(t):
                        yield f"{path.relative_to(STUDIO.parent)}:{node.lineno}", t


class TrTest(unittest.TestCase):
    def test_exact_and_template_and_nested(self):
        self.assertEqual(tr("MV ができました", "en"), "The MV is ready")
        self.assertEqual(tr("MV ができました", "ja"), "MV ができました")
        self.assertEqual(tr("シーン 2 / 5: 生成中 50%", "en"), "Scene 2 / 5: Generating 50%")         # 型の中の値も訳す
        self.assertEqual(tr("シーン 2 / 5: 生成中 2/4", "en"), "Scene 2 / 5: Generating 2/4")
        self.assertEqual(tr("失敗: H3 が動画を返しませんでした", "en"), "Failed: H3 returned no video")
        self.assertEqual(tr("境目 2 の指定が正しくありません", "en"), "Transition 2 is invalid")            # 決まった文字の多い型が勝つ
        self.assertEqual(tr("クリップ 3の終了は開始より後にしてください", "en"), "The end of Clip 3 must be after its start")
        self.assertEqual(tr("プリセットは {tab, name, values} の形にしてください", "en"), "A preset must have the form {tab, name, values}")

    def test_unknown_text_stays(self):
        for s in ("夜明けの駅", "hello", "", None, 3):
            self.assertEqual(tr(s, "en"), s)

    def test_msg_is_the_japanese_string_and_translates_with_its_values(self):
        m = msg("シーン {i} / {n}: {inner}", i=2, n=5, inner="生成中")
        self.assertEqual(m, "シーン 2 / 5: 生成中")
        self.assertIsInstance(m, str)
        self.assertEqual(tr(m, "en"), "Scene 2 / 5: Generating")
        self.assertEqual(tr(msg("{x}: 準備しています", x=msg("シーン {i} / {n}", i=1, n=3)), "en"), "Scene 1 / 3: Preparing")

    def test_accept_language(self):
        for h, lang in ((None, "ja"), ("", "ja"), ("ja", "ja"), ("en", "en"), ("en-US,en;q=0.9", "en"), ("fr, en", "ja")):
            self.assertEqual(i18n.parse_accept_language(h), lang, h)

    def test_payload_translates_only_message_keys(self):
        data = {"title": "完了しました", "message": "完了しました", "steps": ["準備完了", "起動しました"],
                "scenes": [{"error": "H3 が動画を返しませんでした", "prompt": "準備完了"}]}
        self.assertEqual(i18n.translate_payload(data, "en"),
                         {"title": "完了しました", "message": "Completed", "steps": ["Ready", "Started"],
                          "scenes": [{"error": "H3 returned no video", "prompt": "準備完了"}]})
        self.assertIs(i18n.translate_payload(data, "ja"), data)

    def test_english_templates_use_only_the_key_placeholders(self):
        for k, v in i18n.EN.items():
            n = len(i18n._fields(k))
            v.format(*["x"] * n)                    # 番号が足りないと IndexError
            self.assertNotIn("、", v, k)        # 和文の読点が残っていない
            self.assertFalse(JP.search(v), k)

    def test_every_message_site_has_an_english_entry(self):
        keys = {i18n._norm(k) for k in i18n.EN}
        missing = [f"{where}: {t}" for where, t in message_sites() if i18n._norm(t) not in keys]
        self.assertEqual(missing, [], "EN に無い文言(訳を足すか、行に # i18n-skip を書く)")


class _Client:
    def en(self, method, url, **kw):
        return self.client.request(method, url, headers=EN_HDR, **kw)


class MvEnglishTest(unittest.TestCase, _Client):
    setUp, add, body = test_mv_api.MvApiTest.setUp, test_mv_api.MvApiTest.add, test_mv_api.MvApiTest.body

    def test_bad_aspect_is_english_only_for_english_clients(self):
        r = self.en("POST", "/api/mv", json=self.body(aspect="2:1"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], "Aspect must be one of 16:9 / 9:16 / 1:1 / 4:5 / 5:4 / custom")
        r = self.client.post("/api/mv", json=self.body(aspect="2:1"))
        self.assertEqual(r.json()["detail"], "画角は 16:9 / 9:16 / 1:1 / 4:5 / 5:4 / custom のいずれかです")

    def test_project_view_message_and_data(self):
        pid = self.en("POST", "/api/mv", json=self.body()).json()["id"]
        js = self.en("GET", f"/api/mv/{pid}").json()
        self.assertEqual(js["title"], self.client.get(f"/api/mv/{pid}").json()["title"])      # 題(利用者のデータ)は訳さない
        self.assertFalse(JP.search(js["message"] or ""), js["message"])
        self.assertEqual(self.en("GET", "/api/mv/nope").json()["detail"], "That MV project was not found (it may have been deleted)")


class EditEnglishTest(unittest.TestCase, _Client):
    setUp, add, state = test_edit_api.EditApiTest.setUp, test_edit_api.EditApiTest.add, test_edit_api.EditApiTest.state

    def test_bad_state(self):
        pid = self.client.post("/api/edit", json={"name": "x"}).json()["id"]
        r = self.client.put(f"/api/edit/{pid}/state", json={"state": {"clips": "x"}}, headers=EN_HDR)
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "clips must be an array"))


class GalleryEnglishTest(unittest.TestCase, _Client):
    setUp, add = test_gallery_api.GalleryDeleteTest.setUp, test_gallery_api.GalleryDeleteTest.add

    def test_in_use_refusal(self):
        rec = self.add("image", "a.png")
        self.runner.add("running", {"references": [rec["id"]]})
        r = self.en("DELETE", f"/api/gallery/{rec['id']}")
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "In use by a running or queued job"))
        js = self.en("POST", "/api/gallery/delete", json={"ids": [rec["id"]]}).json()
        self.assertEqual(js["refused"], [{"id": rec["id"], "reason": "In use by a running or queued job"}])
        self.assertEqual(self.client.delete(f"/api/gallery/{rec['id']}").json()["detail"], test_gallery_api.JOB_IN_USE)


class _Jobs:
    def __init__(self, job):
        self.job = job

    def get(self, job_id):
        return self.job if job_id == self.job.id else None


class JobAndSampleEnglishTest(unittest.TestCase, _Client):
    def setUp(self):
        self.saved = {k: getattr(main.app.state, k, None) for k in ("jobs", "samples")}
        self.addCleanup(lambda: [setattr(main.app.state, k, v) for k, v in self.saved.items()])
        self.client = TestClient(main.app)

    def test_job_progress_is_translated_when_serialized(self):
        job = Job(id="j1", tab="mv", kind="mv.scene", params={"prompt": "準備完了"}, status="running",
                  message="シーン 2 / 5: 生成中 50%", wait_reason="GPU の空きを待っています(ACE-Step(音楽) が生成中)",
                  steps=["MiniMax-H3(動画・静止画) を GPU から降ろしています", "準備完了"])
        main.app.state.jobs = _Jobs(job)
        js = self.en("GET", "/api/jobs/j1").json()
        self.assertEqual((js["message"], js["wait_reason"], js["steps"]),
                         ("Scene 2 / 5: Generating 50%", "Waiting for free GPU memory (ACE-Step (music) generating)",
                          ["Unloading MiniMax-H3 (video / stills) from the GPU", "Ready"]))
        self.assertEqual(js["params"], {"prompt": "準備完了"})
        self.assertEqual(self.client.get("/api/jobs/j1").json()["message"], "シーン 2 / 5: 生成中 50%")
        self.assertEqual(job.message, "シーン 2 / 5: 生成中 50%")      # 記録は日本語のまま

    def test_unknown_sample(self):
        main.app.state.samples = []
        r = self.en("POST", "/api/samples/nope/load")
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "That sample was not found"))


if __name__ == "__main__":
    unittest.main()
