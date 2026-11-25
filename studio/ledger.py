"""台帳: 生成物(outputs)とジョブを outputs/studio.db(SQLite 1 ファイル)に記録する。ギャラリーはここを見る。"""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from . import media

THUMB_WIDTH = 320
KINDS = ("image", "video", "audio")

SCHEMA = """
CREATE TABLE IF NOT EXISTS outputs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, tab TEXT NOT NULL, job_id TEXT,
    path TEXT NOT NULL, filename TEXT, width INTEGER, height INTEGER, duration_s REAL,
    created_at REAL NOT NULL, params_json TEXT, thumb_path TEXT, fps REAL
);
CREATE INDEX IF NOT EXISTS outputs_created ON outputs(created_at DESC);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, tab TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
    params_json TEXT, error TEXT, created_at REAL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at DESC);
CREATE TABLE IF NOT EXISTS mv_projects (
    id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL, state TEXT NOT NULL,
    params_json TEXT, scenes_json TEXT, output_id TEXT, error TEXT, message TEXT, kind TEXT NOT NULL DEFAULT 'mv'
);
CREATE INDEX IF NOT EXISTS mv_projects_created ON mv_projects(created_at DESC);
CREATE TABLE IF NOT EXISTS edit_projects (
    id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL, name TEXT NOT NULL,
    state_json TEXT NOT NULL, history_json TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0, preview TEXT, output_id TEXT
);
CREATE INDEX IF NOT EXISTS edit_projects_updated ON edit_projects(updated_at DESC);
CREATE TABLE IF NOT EXISTS presets (
    id TEXT PRIMARY KEY, tab TEXT NOT NULL, name TEXT NOT NULL, values_json TEXT NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS presets_tab ON presets(tab, created_at);
CREATE TABLE IF NOT EXISTS genres (
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, vibe TEXT NOT NULL, settings_json TEXT NOT NULL, emotions_json TEXT NOT NULL,
    viz TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""
MV_FIELDS = ("state", "output_id", "error", "message")     # update_mv_project がそのまま置き換える列
EDIT_HISTORY_MAX = 100                                       # 編集プロジェクトの履歴(元に戻す)の段数
EDIT_LIST_COLS = "id, created_at, updated_at, name, state_json, cursor, preview, output_id, json_array_length(history_json) AS history_len"


def make_thumb(src: Path, dst: Path, width: int = THUMB_WIDTH) -> tuple[int, int]:
    """画像から幅 width の JPEG サムネイルを作り、元画像の (幅, 高さ) を返す。"""
    from PIL import Image, ImageOps   # 画像を扱うときだけ読み込む
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        size = im.size
        if im.mode in ("RGBA", "LA", "P"):   # 透過は白地に載せる(JPEG に透過は無い)
            im = im.convert("RGBA")
            flat = Image.new("RGB", im.size, "white")
            flat.paste(im, mask=im.getchannel("A"))
            im = flat
        else:
            im = im.convert("RGB")
        if im.width > width:
            im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(dst, "JPEG", quality=85)
    return size


class Ledger:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.root / "studio.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.executescript(SCHEMA)
            # 第 3 版で足した kind 列(mv / short)。古い DB の mv_projects には無いので足す(既存の制作は mv)
            if "kind" not in {r["name"] for r in self._db.execute("PRAGMA table_info(mv_projects)")}:
                self._db.execute("ALTER TABLE mv_projects ADD COLUMN kind TEXT NOT NULL DEFAULT 'mv'")
            # 動画のコマ率(編集の書き出しが先頭の動画に合わせる)。古い DB の outputs には無いので足す(既存の行は空 = 分からない。編集は 24 として扱う)
            if "fps" not in {r["name"] for r in self._db.execute("PRAGMA table_info(outputs)")}:
                self._db.execute("ALTER TABLE outputs ADD COLUMN fps REAL")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- パス ----

    def abs_path(self, rel: str) -> Path:
        """outputs/ からの相対パスを絶対パスにする。外に出るパスは拒否する。"""
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root.resolve()):
            raise ValueError(f"outputs の外は扱えません: {rel}")
        return p

    def rel_path(self, path) -> str:
        p = Path(path)
        if not p.is_absolute():
            p = self.root / p
        return p.resolve().relative_to(self.root.resolve()).as_posix()

    # ---- 生成物 ----

    @staticmethod
    def _out(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["params"] = json.loads(d.pop("params_json") or "{}")
        return d

    def add_output(self, kind: str, path, tab: str, job_id: str = None, filename: str = None, params: dict = None,
                   width: int = None, height: int = None, duration_s: float = None, fps: float = None) -> dict:
        """生成物を登録する。画像なら幅・高さを読み、サムネイル(outputs/thumbs/<id>.jpg)も作る。
        動画・音声は ffprobe で長さ(動画は寸法とコマ率も。渡されていない分だけ)を読み、動画は 0.5 秒のコマをサムネイルにする(ffmpeg が無ければ空のまま)。"""
        if kind not in KINDS:
            raise ValueError(f"kind は {KINDS} のいずれかです: {kind}")
        rel = self.rel_path(path)
        oid = uuid.uuid4().hex[:12]
        thumb_rel = None
        if kind == "image":
            thumb_rel = f"thumbs/{oid}.jpg"
            width, height = make_thumb(self.abs_path(rel), self.abs_path(thumb_rel))
        elif kind in ("video", "audio"):
            if duration_s is None or (kind == "video" and (width is None or fps is None)):
                info = media.probe(self.abs_path(rel))
                duration_s = duration_s if duration_s is not None else info["duration_s"]
                if kind == "video":
                    width, height, fps = width or info["width"], height or info["height"], fps or info.get("fps")
            if kind == "video":
                thumb_rel = f"thumbs/{oid}.jpg"
                if not media.video_thumb(self.abs_path(rel), self.abs_path(thumb_rel), THUMB_WIDTH):
                    thumb_rel = None
        fps = fps if kind == "video" else None
        rec = {"id": oid, "kind": kind, "tab": tab, "job_id": job_id, "path": rel, "filename": filename or Path(rel).name,
               "width": width, "height": height, "duration_s": duration_s, "created_at": time.time(),
               "params": params or {}, "thumb_path": thumb_rel, "fps": fps}
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO outputs (id, kind, tab, job_id, path, filename, width, height, duration_s, created_at, params_json, thumb_path, fps)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, kind, tab, job_id, rel, rec["filename"], width, height, duration_s, rec["created_at"],
                 json.dumps(rec["params"], ensure_ascii=False), thumb_rel, fps))
        return rec

    def list_outputs(self, kind: str = None, tab: str = None, job_id: str = None, limit: int = 200) -> list:
        where, args = [], []
        for col, val in (("kind", kind), ("tab", tab), ("job_id", job_id)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        sql = "SELECT * FROM outputs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(sql, (*args, limit)).fetchall()
        return [self._out(r) for r in rows]

    def get_output(self, oid: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT * FROM outputs WHERE id = ?", (oid,)).fetchone()
        return self._out(row) if row else None

    def delete_output(self, oid: str) -> bool:
        """台帳の行とファイル(本体・サムネイル・隣に置いた字幕)を消す。無ければ False。
        字幕は、ショート動画の結合が最終の動画の隣に置く SRT(params.srt にその名前。同じフォルダのものだけ)。"""
        rec = self.get_output(oid)
        if rec is None:
            return False
        srt = str((rec["params"] or {}).get("srt") or "").strip()
        sidecar = (Path(rec["path"]).parent / Path(srt).name).as_posix() if srt else None
        for rel in (rec["path"], rec["thumb_path"], sidecar):
            if rel:
                try:
                    self.abs_path(rel).unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
        with self._lock, self._db:
            self._db.execute("DELETE FROM outputs WHERE id = ?", (oid,))
        return True

    def remove_stale_parts(self) -> int:
        """outputs/ の中の作りかけのファイル(*.part)を消し、消した数を返す。管理役の起動時に呼ぶ(まだ何も書いていない時。
        強制終了で残った書き出し・プレビュー・ダウンロード・分離の途中のファイルで、名前が .part なので、どこからも使われない)。"""
        n = 0
        for p in self.root.rglob("*.part"):
            try:
                if p.is_file():
                    p.unlink()
                    n += 1
            except OSError:
                pass
        return n

    # ---- ジョブ ----

    def save_job(self, job, params: dict = None) -> None:
        """終わったジョブを残す。params を渡せば、job.params の代わりにそれを残す(大きな params を、要る分だけにするとき)。"""
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO jobs (id, tab, kind, status, params_json, error, created_at, finished_at) VALUES (?,?,?,?,?,?,?,?)",
                (job.id, job.tab, job.kind, job.status, json.dumps(job.params if params is None else params, ensure_ascii=False), job.error,
                 job.created, job.finished))

    def _job_dict(self, r: sqlite3.Row) -> dict:
        """台帳の行から、画面向けのジョブ辞書を作る(メモリのジョブと同じ形。実行中の項目は空)。"""
        return {"id": r["id"], "tab": r["tab"], "kind": r["kind"], "status": r["status"], "error": r["error"],
                "params": json.loads(r["params_json"] or "{}"), "created": r["created_at"], "started": None,
                "finished": r["finished_at"], "progress": 1.0 if r["status"] == "completed" else 0.0,
                "message": "", "log_tail": "", "steps": [], "result": {}, "lane": None, "wait_reason": None,
                "outputs": [o["id"] for o in self.list_outputs(job_id=r["id"])]}

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job_dict(row) if row else None

    def list_jobs(self, tab: str = None, limit: int = 50) -> list:
        sql = "SELECT * FROM jobs" + (" WHERE tab = ?" if tab else "") + " ORDER BY created_at DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(sql, ((tab, limit) if tab else (limit,))).fetchall()
        return [self._job_dict(r) for r in rows]

    # ---- MV の制作(リップシンク MV・ショート動画のプロジェクト。kind = mv / short) ----

    @staticmethod
    def _mv(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["params"] = json.loads(d.pop("params_json") or "{}")
        d["scenes"] = json.loads(d.pop("scenes_json") or "[]")
        return d

    def create_mv_project(self, pid: str, state: str, params: dict, scenes: list = (), message: str = "", kind: str = "mv") -> dict:
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO mv_projects (id, created_at, updated_at, state, params_json, scenes_json, output_id, error, message, kind)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, now, now, state, json.dumps(params, ensure_ascii=False), json.dumps(list(scenes), ensure_ascii=False),
                 None, None, message, kind))
        return self.get_mv_project(pid)

    def get_mv_project(self, pid: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT * FROM mv_projects WHERE id = ?", (pid,)).fetchone()
        return self._mv(row) if row else None

    def list_mv_projects(self, limit: int = 200, kind: str = None) -> list:
        """新しい順。kind(mv / short)を渡せば、その種類だけ。"""
        sql = "SELECT * FROM mv_projects" + (" WHERE kind = ?" if kind else "") + " ORDER BY created_at DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(sql, ((kind, limit) if kind else (limit,))).fetchall()
        return [self._mv(r) for r in rows]

    def update_mv_project(self, pid: str, params_patch: dict = None, scene_patches: dict = None, params: dict = None,
                          scenes: list = None, **fields) -> Optional[dict]:
        """渡した項目だけを書き換える(updated_at は自動)。無ければ None。
        state / output_id / error / message / params(全体)/ scenes(全体)は置き換え、params_patch は params に混ぜ、
        scene_patches({シーン番号: {項目: 値}})は該当シーンに混ぜる。読んで書くまでを 1 回のロックで行う(別々の項目を書く同士が上書きしない)。"""
        bad = set(fields) - set(MV_FIELDS)
        if bad:
            raise ValueError(f"MV の制作に無い項目です: {sorted(bad)}")
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM mv_projects WHERE id = ?", (pid,)).fetchone()
            if row is None:
                return None
            cur = self._mv(row)
            new_params = dict(cur["params"] if params is None else params)
            new_params.update(params_patch or {})
            new_scenes = list(cur["scenes"] if scenes is None else scenes)
            for index, patch in (scene_patches or {}).items():
                for sc in new_scenes:
                    if sc.get("index") == int(index):
                        sc.update(patch)
            sets = {**fields, "updated_at": time.time(), "params_json": json.dumps(new_params, ensure_ascii=False),
                    "scenes_json": json.dumps(new_scenes, ensure_ascii=False)}
            self._db.execute(f"UPDATE mv_projects SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?", (*sets.values(), pid))
        return self.get_mv_project(pid)

    def delete_mv_project(self, pid: str) -> bool:
        """台帳の行だけを消す(作業フォルダと最終の MV は呼び出し側が扱う)。無ければ False。"""
        with self._lock, self._db:
            return self._db.execute("DELETE FROM mv_projects WHERE id = ?", (pid,)).rowcount > 0

    # ---- 編集プロジェクト(編集タブ。state の履歴を持ち、元に戻す・やり直す) ----

    @staticmethod
    def _edit(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["state"] = json.loads(d.pop("state_json"))
        d["preview"] = json.loads(d["preview"]) if d.get("preview") else None
        if "history_json" in d:
            d["history"] = json.loads(d.pop("history_json"))
            d["history_len"] = len(d["history"])
        d["can_undo"], d["can_redo"] = d["cursor"] > 0, d["cursor"] < d["history_len"] - 1
        return d

    def create_edit_project(self, pid: str, name: str, state: dict) -> dict:
        """新しい編集プロジェクト(履歴は state 1 つだけ)を作る。"""
        now = time.time()
        text = json.dumps(state, ensure_ascii=False)
        with self._lock, self._db:
            self._db.execute("INSERT INTO edit_projects (id, created_at, updated_at, name, state_json, history_json, cursor, preview, output_id)"
                             " VALUES (?,?,?,?,?,?,?,?,?)", (pid, now, now, name, text, json.dumps([state], ensure_ascii=False), 0, None, None))
        return self.get_edit_project(pid)

    def get_edit_project(self, pid: str, history: bool = False) -> Optional[dict]:
        """history=True なら履歴(state の配列)も "history" に入れる。無ければ、件数だけ(history_len)。"""
        cols = EDIT_LIST_COLS + (", history_json" if history else "")
        with self._lock:
            row = self._db.execute(f"SELECT {cols} FROM edit_projects WHERE id = ?", (pid,)).fetchone()
        return self._edit(row) if row else None

    def list_edit_projects(self, limit: int = 200) -> list:
        """更新が新しい順(履歴は含めない)。"""
        with self._lock:
            rows = self._db.execute(f"SELECT {EDIT_LIST_COLS} FROM edit_projects ORDER BY updated_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        return [self._edit(r) for r in rows]

    def push_edit_state(self, pid: str, state: dict) -> Optional[dict]:
        """操作の結果の state を履歴に積む(やり直しの枝は捨てる。最大 EDIT_HISTORY_MAX 段、古いものから落とす)。
        いまの state と同じなら積まない(履歴は増えない)。無ければ None。"""
        with self._lock, self._db:
            row = self._db.execute("SELECT history_json, cursor FROM edit_projects WHERE id = ?", (pid,)).fetchone()
            if row is None:
                return None
            history, cursor = json.loads(row["history_json"]), row["cursor"]
            if history[cursor] != state:
                history = [*history[:cursor + 1], state][-EDIT_HISTORY_MAX:]
                self._db.execute("UPDATE edit_projects SET state_json = ?, history_json = ?, cursor = ?, updated_at = ? WHERE id = ?",
                                 (json.dumps(state, ensure_ascii=False), json.dumps(history, ensure_ascii=False), len(history) - 1, time.time(), pid))
        return self.get_edit_project(pid)

    def move_edit_cursor(self, pid: str, step: int) -> Optional[dict]:
        """履歴の位置を step(-1 = 元に戻す、+1 = やり直す)動かし、その位置の state にする。端なら ValueError。無ければ None。"""
        with self._lock, self._db:
            row = self._db.execute("SELECT history_json, cursor FROM edit_projects WHERE id = ?", (pid,)).fetchone()
            if row is None:
                return None
            history, cursor = json.loads(row["history_json"]), row["cursor"] + step
            if not 0 <= cursor < len(history):
                raise ValueError("これ以上戻せません" if step < 0 else "これ以上やり直せません")
            self._db.execute("UPDATE edit_projects SET state_json = ?, cursor = ?, updated_at = ? WHERE id = ?",
                             (json.dumps(history[cursor], ensure_ascii=False), cursor, time.time(), pid))
        return self.get_edit_project(pid)

    def update_edit_project(self, pid: str, **fields) -> Optional[dict]:
        """name / preview(辞書 or None)/ output_id を置き換える(updated_at は、名前を変えたときだけ進める。プレビュー・書き出しの記録では動かさない)。無ければ None。"""
        bad = set(fields) - {"name", "preview", "output_id"}
        if bad:
            raise ValueError(f"編集プロジェクトに無い項目です: {sorted(bad)}")
        sets = dict(fields)
        if "preview" in sets:
            sets["preview"] = json.dumps(sets["preview"], ensure_ascii=False) if sets["preview"] else None
        if "name" in sets:
            sets["updated_at"] = time.time()
        if not sets:
            return self.get_edit_project(pid)
        with self._lock, self._db:
            cur = self._db.execute(f"UPDATE edit_projects SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?", (*sets.values(), pid))
            if cur.rowcount == 0:
                return None
        return self.get_edit_project(pid)

    def delete_edit_project(self, pid: str) -> bool:
        """台帳の行だけを消す(プレビューのファイルは呼び出し側が消す。書き出した動画はギャラリーに残る)。無ければ False。"""
        with self._lock, self._db:
            return self._db.execute("DELETE FROM edit_projects WHERE id = ?", (pid,)).rowcount > 0

    # ---- プリセット(各タブの入力欄の値に名前を付けたもの。同梱のものは台帳に入れず、リポジトリの presets/*.json にある) ----

    @staticmethod
    def _preset(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["values"] = json.loads(d.pop("values_json") or "{}")
        return d

    def create_preset(self, tab: str, name: str, values: dict) -> dict:
        pid, now = uuid.uuid4().hex[:12], time.time()
        with self._lock, self._db:
            self._db.execute("INSERT INTO presets (id, tab, name, values_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                             (pid, tab, name, json.dumps(values, ensure_ascii=False), now, now))
        return self.get_preset(pid)

    def get_preset(self, pid: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT * FROM presets WHERE id = ?", (pid,)).fetchone()
        return self._preset(row) if row else None

    def list_presets(self, tab: str = None) -> list:
        """作った順(古いものが先)。tab があれば、そのタブのものだけ。"""
        sql = "SELECT * FROM presets" + (" WHERE tab = ?" if tab else "") + " ORDER BY created_at, rowid"
        with self._lock:
            rows = self._db.execute(sql, (tab,) if tab else ()).fetchall()
        return [self._preset(r) for r in rows]

    def update_preset(self, pid: str, name: str = None, values: dict = None) -> Optional[dict]:
        """渡した項目(名前・値)だけを書き換える(updated_at は自動)。無ければ None。"""
        sets = {"updated_at": time.time()}
        if name is not None:
            sets["name"] = name
        if values is not None:
            sets["values_json"] = json.dumps(values, ensure_ascii=False)
        with self._lock, self._db:
            cur = self._db.execute(f"UPDATE presets SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?", (*sets.values(), pid))
            if cur.rowcount == 0:
                return None
        return self.get_preset(pid)

    def delete_preset(self, pid: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM presets WHERE id = ?", (pid,)).rowcount > 0

    # ---- ジャンル(音楽タブの自作のジャンル。同梱の 12 は台帳に入れず、songplan.GENRES にある) ----

    @staticmethod
    def _genre(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["settings"] = json.loads(d.pop("settings_json") or "[]")
        d["emotions"] = json.loads(d.pop("emotions_json") or "[]")
        return d

    def create_genre(self, name: str, vibe: str, settings: list, emotions: list, viz: str, description: str = "") -> dict:
        gid, now = uuid.uuid4().hex[:12], time.time()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO genres (id, name, vibe, settings_json, emotions_json, viz, description, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (gid, name, vibe, json.dumps(settings, ensure_ascii=False), json.dumps(emotions, ensure_ascii=False), viz, description, now, now))
        return self.get_genre(gid)

    def get_genre(self, gid: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT * FROM genres WHERE id = ?", (gid,)).fetchone()
        return self._genre(row) if row else None

    def list_genres(self) -> list:
        """作った順(古いものが先)。"""
        with self._lock:
            rows = self._db.execute("SELECT * FROM genres ORDER BY created_at, rowid").fetchall()
        return [self._genre(r) for r in rows]

    def update_genre(self, gid: str, name: str = None, vibe: str = None, settings: list = None, emotions: list = None,
                     viz: str = None, description: str = None) -> Optional[dict]:
        """渡した項目だけを書き換える(updated_at は自動)。無ければ None。"""
        sets = {"updated_at": time.time()}
        for key, val in (("name", name), ("vibe", vibe), ("viz", viz), ("description", description)):
            if val is not None:
                sets[key] = val
        for key, val in (("settings_json", settings), ("emotions_json", emotions)):
            if val is not None:
                sets[key] = json.dumps(val, ensure_ascii=False)
        with self._lock, self._db:
            cur = self._db.execute(f"UPDATE genres SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?", (*sets.values(), gid))
            if cur.rowcount == 0:
                return None
        return self.get_genre(gid)

    def delete_genre(self, gid: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM genres WHERE id = ?", (gid,)).rowcount > 0
