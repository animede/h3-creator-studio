"""部品のハンドラが共有する小さな道具: エラー文の取り出しと、生成物のダウンロード。"""
from pathlib import Path

import httpx

from ..jobs import JobFailed


def error_text(r: httpx.Response) -> str:
    try:
        detail = r.json().get("detail")
        if detail:
            return str(detail)
    except (ValueError, AttributeError):
        pass
    return f"HTTP {r.status_code}"


async def download(c: httpx.AsyncClient, url: str, dest: Path) -> None:
    """url を dest へ保存する(途中で失敗しても壊れたファイルを残さない)。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        async with c.stream("GET", url) as r:
            if r.status_code != 200:
                raise JobFailed(f"生成物の取得に失敗しました: HTTP {r.status_code}")
            with open(tmp, "wb") as f:
                async for chunk in r.aiter_bytes(1 << 16):
                    f.write(chunk)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
