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
    created_at REAL NOT NULL, params_json TEXT, thumb_path TEXT
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
                   width: int = None, height: int = None, duration_s: float = None) -> dict:
        """生成物を登録する。画像なら幅・高さを読み、サムネイル(outputs/thumbs/<id>.jpg)も作る。
        動画・音声は ffprobe で長さ(動画は寸法も)を読み、動画は 0.5 秒のコマをサムネイルにする(ffmpeg が無ければ空のまま)。"""
        if kind not in KINDS:
            raise ValueError(f"kind は {KINDS} のいずれかです: {kind}")
        rel = self.rel_path(path)
        oid = uuid.uuid4().hex[:12]
        thumb_rel = None
        if kind == "image":
            thumb_rel = f"thumbs/{oid}.jpg"
            width, height = make_thumb(self.abs_path(rel), self.abs_path(thumb_rel))
        elif kind in ("video", "audio"):
            if duration_s is None or (kind == "video" and width is None):
                info = media.probe(self.abs_path(rel))
                duration_s = duration_s if duration_s is not None else info["duration_s"]
                if kind == "video":
                    width, height = width or info["width"], height or info["height"]
            if kind == "video":
                thumb_rel = f"thumbs/{oid}.jpg"
                if not media.video_thumb(self.abs_path(rel), self.abs_path(thumb_rel), THUMB_WIDTH):
                    thumb_rel = None
        rec = {"id": oid, "kind": kind, "tab": tab, "job_id": job_id, "path": rel, "filename": filename or Path(rel).name,
               "width": width, "height": height, "duration_s": duration_s, "created_at": time.time(),
               "params": params or {}, "thumb_path": thumb_rel}
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO outputs (id, kind, tab, job_id, path, filename, width, height, duration_s, created_at, params_json, thumb_path)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, kind, tab, job_id, rel, rec["filename"], width, height, duration_s, rec["created_at"],
                 json.dumps(rec["params"], ensure_ascii=False), thumb_rel))
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
        """台帳の行とファイル(本体・サムネイル)を消す。無ければ False。"""
        rec = self.get_output(oid)
        if rec is None:
            return False
        for rel in (rec["path"], rec["thumb_path"]):
            if rel:
                try:
                    self.abs_path(rel).unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
        with self._lock, self._db:
            self._db.execute("DELETE FROM outputs WHERE id = ?", (oid,))
        return True

    # ---- ジョブ ----

    def save_job(self, job) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO jobs (id, tab, kind, status, params_json, error, created_at, finished_at) VALUES (?,?,?,?,?,?,?,?)",
                (job.id, job.tab, job.kind, job.status, json.dumps(job.params, ensure_ascii=False), job.error,
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
