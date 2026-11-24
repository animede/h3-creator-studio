// 画像タブ: 入力フォーム・参照画像のドロップ欄・ジョブの投入と進捗表示。
(() => {   // 動画タブと名前がぶつからないよう、タブごとに閉じる
const MAX_REFS = 16;
const refs = [];                // 参照画像(生成物のレコード)。並び順が <Picture N> の N

const im = (id) => $("im-" + id);

function showMsg(text) {
  im("msg").hidden = !text;
  im("msg").textContent = text || "";
}

// ---- サイズ・シード ----

function syncSizeSelect() {
  const key = `${im("w").value}x${im("h").value}`;
  const opt = [...im("size").options].find((o) => o.value === key);
  im("size").value = opt ? key : "custom";
}

im("size").onchange = () => {
  if (im("size").value === "custom") return;
  const [w, h] = im("size").value.split("x");
  im("w").value = w;
  im("h").value = h;
};
for (const k of ["w", "h"]) {
  im(k).oninput = syncSizeSelect;
  im(k).onchange = () => {   // 32 の倍数に丸め、256〜4096 に収める(サーバも同じ処理をする)
    const v = Math.min(4096, Math.max(256, Math.round((parseInt(im(k).value, 10) || 1024) / 32) * 32));
    im(k).value = v;
    syncSizeSelect();
  };
}
im("random").onchange = () => { im("seed").disabled = im("random").checked; };

// ---- 参照画像 ----

function renderRefs() {
  const list = im("refs-list");
  list.replaceChildren();
  refs.forEach((r, i) => {
    const c = el("div", "ref");
    c.title = r.filename || "";
    const img = el("img");
    img.src = r.thumb_url || r.url;
    img.draggable = false;
    c.append(img, el("span", "num", String(i + 1)));
    const x = el("button", "rdel", "×");
    x.title = "外す";
    x.onclick = () => { refs.splice(i, 1); renderRefs(); };
    c.append(x);
    makeDraggable(c, r.id);
    list.append(c);
  });
  im("refs-empty").hidden = refs.length > 0;
  im("mode").textContent = refs.length ? `${refs.length} 枚` : "";
}

// ---- 作り方(H3-Image の略語): t2i = テキストから、edit = 参照画像で編集 ----
let mode = "t2i";
function setMode(m) {
  mode = m;
  document.querySelectorAll("#im-modes button").forEach((b) => b.classList.toggle("active", b.dataset.mode === m));
  im("refs-fld").hidden = m !== "edit";
  showMsg("");
}
document.querySelectorAll("#im-modes button").forEach((b) => { b.onclick = () => setMode(b.dataset.mode); });

function addRef(item) {
  if (item.kind !== "image") { showMsg("参照にできるのは画像だけです"); return false; }
  if (mode !== "edit") setMode("edit");      // 参照を入れたら編集モードへ
  if (refs.some((r) => r.id === item.id)) { showMsg("その画像はすでに参照に入っています"); return false; }
  if (refs.length >= MAX_REFS) { showMsg(`参照画像は最大 ${MAX_REFS} 枚です`); return false; }
  refs.push(item);
  showMsg("");
  renderRefs();
  return true;
}

studio.actions.image = (item) => (item.kind === "image" ? [{ label: "参照に使う", run: () => { setTab("image"); setMode("edit"); addRef(item); } }] : []);
// 消された画像は参照から外す(消したとき(studio-deleted)と、一覧を読み直したらギャラリーに無かったとき)
function dropGone(gone) {
  const before = refs.length;
  for (let i = refs.length - 1; i >= 0; i--) if (gone(refs[i].id)) refs.splice(i, 1);
  if (refs.length !== before) renderRefs();
}
studio.on.items.push(() => dropGone((id) => !byId(id)));
document.addEventListener("studio-deleted", (e) => dropGone((id) => e.detail.ids.includes(id)));

async function uploadAndAdd(files) {
  try {
    const skipped = [];
    for (const rec of await uploadFiles(files, im("refs"))) if (rec.kind === "image") addRef(rec); else skipped.push(rec.filename);
    if (skipped.length) showMsg(`画像以外(${skipped.join("、")})はギャラリーに保存しました(参照には使えません)`);
  } catch (e) {
    showMsg(`取り込めませんでした: ${e.message || e}`);
  }
}

// ドロップは操作パネル全体で受け、参照欄を光らせる
wireDrop(im("refs"), { zone: $("ops"), active: () => studio.tab === "image", onItem: addRef, onFiles: uploadAndAdd, msg: showMsg });

// ---- 生成 ----

function setBusy(busy) {
  im("go").disabled = busy;
  im("stop").disabled = !busy;
}

async function generate() {
  const prompt = im("prompt").value.trim();
  if (!prompt) { showMsg("プロンプトを入力してください"); im("prompt").focus(); return; }
  showMsg("");
  const seed = im("random").checked || im("seed").value === "" ? null : parseInt(im("seed").value, 10);
  if (mode === "edit" && !refs.length) { showMsg("edit は参照画像が要ります。画像を参照欄に落としてください"); return; }
  tracker.submit(mode === "edit" ? "h3image.edit" : "h3image.t2i", {
    prompt, width: +im("w").value, height: +im("h").value, seed, steps: im("steps").value ? +im("steps").value : null,
    references: mode === "edit" ? refs.map((r) => r.id) : [],
  });
}

const tracker = createTracker("image", { setBusy, showMsg });
im("go").onclick = generate;
im("stop").onclick = () => { im("stop").disabled = true; tracker.stop(); };
im("prompt").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !im("go").disabled) generate(); });

wireEnhance({ tab: "image", ta: im("prompt"), btn: im("enh"), undo: im("enh-undo"), note: im("enh-note"), showMsg,
  body: () => ({ references: mode === "edit" ? refs.map((r) => r.id) : [] }) });      // edit なら参照付きの強化

// ---- プリセット(入力欄の値だけ。参照画像は含めない) ----

function getPresetValues() {
  return { mode, prompt: im("prompt").value.trim(), width: +im("w").value, height: +im("h").value,
    steps: im("steps").value ? +im("steps").value : null, seed: presetGetSeed(im("seed"), im("random")) };
}

// 書かれている項目だけ反映する(知らない項目は無視)。プロンプトは空でなければ置き換える(サイズだけのプリセットで、書きかけの文を消さない)
function applyPresetValues(v) {
  const skipped = [];
  if (v.mode === "t2i" || v.mode === "edit") setMode(v.mode);
  if (typeof v.prompt === "string" && v.prompt.trim()) presetSet(im("prompt"), v.prompt);
  for (const [key, id] of [["width", "w"], ["height", "h"]]) if (Number.isFinite(v[key])) presetSet(im(id), v[key]);      // change で 32 の倍数に丸め、サイズの選択も合う
  if ("steps" in v && !presetSet(im("steps"), v.steps == null ? "" : v.steps)) skipped.push("ステップ数");
  if ("seed" in v) presetSetSeed(im("seed"), im("random"), v.seed);
  return skipped;
}
wirePresets({ tab: "image", box: $("im-presets"), get: getPresetValues, apply: applyPresetValues });

tracker.resume();
setMode("t2i");
renderRefs();
})();
