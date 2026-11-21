// ドラッグ&ドロップの共有部分: ドロップ欄の配線と、OS から落とされたファイルのアップロード。
// ギャラリー・履歴の生成物(OUT_MIME)も、PC のファイル(Files)も、同じドロップ欄に落とせる。

function dragType(e) {
  const t = [...(e.dataTransfer?.types || [])];
  return t.includes(OUT_MIME) ? "output" : t.includes("Files") ? "files" : null;
}

// box をドロップ欄にする。zone(既定は box 自身)の上へ落とされたものを受け、box を光らせる。
//   active() が false の間は何もしない / onItem(生成物) / onFiles(ファイルの配列) / msg(文言)
function wireDrop(box, { zone = box, active = () => true, onItem, onFiles, msg = () => {} }) {
  let depth = 0;
  const on = (e) => active() && dragType(e);
  zone.addEventListener("dragenter", (e) => { if (on(e)) { e.preventDefault(); depth++; box.classList.add("over"); } });
  zone.addEventListener("dragover", (e) => { if (on(e)) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; } });
  zone.addEventListener("dragleave", (e) => { if (on(e) && --depth <= 0) { depth = 0; box.classList.remove("over"); } });
  zone.addEventListener("drop", (e) => {
    if (!on(e)) return;
    e.preventDefault();
    depth = 0;
    box.classList.remove("over");
    const id = e.dataTransfer.getData(OUT_MIME);
    const files = [...e.dataTransfer.files];     // drop の中で同期的に取り出す
    if (id) { const it = byId(id); if (it) onItem(it); else msg("その生成物は見つかりません"); }
    else if (files.length) onFiles(files);
  });
}

// ファイルを /api/uploads へ送り、台帳に載ったレコードの配列を返す(失敗は Error)。box には送信中だけ busy を付ける。
async function uploadFiles(files, box) {
  if (box) box.classList.add("busy");
  try {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    const r = await fetch("/api/uploads", { method: "POST", body: fd });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return await r.json();
  } finally {
    if (box) box.classList.remove("busy");
    loadGallery();
  }
}
