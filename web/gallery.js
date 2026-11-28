// 生成物の共有部分: ギャラリー(全タブ共通)・履歴・生成物ビューア・タブ切り替え・ドラッグ元。
const OUT_MIME = "application/x-studio-output";     // ドラッグするデータ: 生成物の ID
const KIND_LABEL = { image: t("画像"), video: t("動画"), audio: t("音楽") };
const TAB_KIND = { image: "image", video: "video", music: "audio" };

const GALLERY_LIMIT = 500;   // 読む生成物の数(新しい順)。これより多ければ、古いものは studio.items に無い
const studio = {
  tab: "image",
  items: [],                 // /api/gallery の結果(新しい順。GALLERY_LIMIT まで)
  complete: false,           // items が全部か(GALLERY_LIMIT で切れていない)。切れていれば、items に無い生成物が消えたとは限らない
  viewing: {},               // タブ -> 生成物ビューアに出している ID(未指定なら、そのタブの最新)
  galleryKind: "all",
  actions: {},               // タブ -> (生成物) => [{label, run}]  生成物ビューアに出すボタン(各タブの js が入れる)
  own: {},                   // タブ -> {history(grid), open(生成物), ids(), remove(ids)}  右側の生成物ビューアと履歴を自前で描くタブ(MV・ショート動画・編集タブ)。open はギャラリーでクリックされたとき(true を返せばギャラリーは開いたまま)、ids と remove は選択モードで、履歴の ID の一覧と、選んだものの削除(消したら true)
  select: { on: false, ids: new Set() },     // 選択モード(履歴とギャラリーで共通): 選んでいる ID。MV・ショート動画・編集タブの履歴では制作(プロジェクト)の ID
  on: { tab: [], items: [] },   // 各タブの js が入れる: タブが変わったとき / 一覧が更新されたとき
};

const byId = (id) => studio.items.find((i) => i.id === id);
// 一覧を読み直したとき、入力欄に入れた生成物が消えたとみなすか(一覧が全部で、そこに無い)。切れた一覧では、古くて載っていないだけかもしれないので外さない
// (消したときは studio-deleted で外す)
const goneFromGallery = (id) => studio.complete && !byId(id);
const fmtSec = (s) => (s >= 60 ? t("{m}分{s}秒", { m: Math.floor(s / 60), s: Math.round(s % 60) }) : t("{s}秒", { s: s.toFixed(1) }));
const fmtDur = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;

async function loadGallery() {
  try {
    const items = await fetch(`/api/gallery?limit=${GALLERY_LIMIT}`, { headers: langHeaders() }).then((r) => r.json());
    if (!Array.isArray(items)) return;
    studio.items = items;
    studio.complete = items.length < GALLERY_LIMIT;
  } catch (e) { return; }
  renderHistory();
  renderGallery();
  renderViewer();
  renderSelectBar();
  studio.on.items.forEach((f) => f());
}

// ---- ドラッグ元 ----

function makeDraggable(node, id) {
  node.draggable = true;
  node.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData(OUT_MIME, id);
    e.dataTransfer.setData("text/plain", id);
    e.dataTransfer.effectAllowed = "copy";
    node.classList.add("dragging");
  });
  node.addEventListener("dragend", () => node.classList.remove("dragging"));
}

// サムネイル・カード(div。onclick で開く)をキーボードでも扱えるようにする: Tab で止まり、Enter / Space がクリックと同じ。label は読み上げの名前
// (中の × ボタンは button なので自分で受ける。そこからのキーは拾わない)
function keyClickable(node, label) {
  node.tabIndex = 0;
  node.setAttribute("role", "button");
  if (label) node.setAttribute("aria-label", label);
  node.addEventListener("keydown", (e) => {
    if (e.target !== node || (e.key !== "Enter" && e.key !== " ")) return;
    e.preventDefault();
    node.click();
  });
}

function thumbNode(item, onClick, onDelete) {
  const th = el("div", "thumb");
  th.title = (item.kind === "audio" && item.params && item.params.theme) || `${item.filename || ""}${item.width ? `  ${item.width}×${item.height}` : ""}`;
  keyClickable(th, th.title);
  if (item.thumb_url) {
    const img = el("img");
    img.src = item.thumb_url;
    img.alt = item.filename || "";
    img.loading = "lazy";
    img.draggable = false;          // ドラッグ元は外側の div にそろえる
    th.append(img);
  } else {
    th.append(el("div", "nothumb " + item.kind, item.kind === "audio" ? "🎵" : item.kind === "video" ? "▶" : KIND_LABEL[item.kind] || item.kind));
  }
  if (item.tab === "upload") th.append(el("span", "tbadge top", t("取込")));
  if (item.kind !== "image") {     // 動画は ▶ と長さ、音声は 🔊 と長さ
    const d = item.duration_s ? " " + fmtDur(item.duration_s) : "";
    th.append(el("span", "tbadge", (item.kind === "video" ? "▶" : "🎵") + d));
  }
  if (studio.select.on) { pickable(th, item.id); return th; }      // 選択モード: クリックは選び直し(×もドラッグも無し)
  th.onclick = () => onClick(item);
  if (onDelete) {
    const x = el("button", "tdel", "×");
    x.title = t("削除");
    x.onclick = (e) => { e.stopPropagation(); onDelete(item); };
    th.append(x);
  }
  makeDraggable(th, item.id);
  return th;
}

// ---- 履歴(このタブの生成物) ----

function tabItems() { return studio.items.filter((i) => i.tab === studio.tab); }

function renderHistory() {
  const grid = $("hist-grid");
  const own = studio.own[studio.tab];
  if (own) { own.history(grid); return; }
  const list = tabItems();
  const shown = currentItem();
  grid.replaceChildren();
  if (!list.length) { grid.append(el("div", "placeholder", t("このタブで作ったものがここに並びます"))); return; }
  for (const it of list) {
    const t = thumbNode(it, (x) => { studio.viewing[studio.tab] = x.id; renderHistory(); renderViewer(); }, deleteItem);
    t.classList.toggle("sel", !studio.select.on && !!shown && shown.id === it.id);
    grid.append(t);
  }
}

// ---- 生成物ビューア ----

// 音楽のカード: スペクトラム(再生中に動く。viz.js)・タイトル(テーマ)・キャプション・プレーヤー・歌詞。色合いは、自作のジャンルが持つ雰囲気(viz)、無ければ曲のムード、それも無ければキャプションの言葉、それも無ければジャンルで決まる
function audioCard(item) {
  const p = item.params || {};
  const box = el("div", "audiocard");
  const a = el("audio");
  a.controls = true;
  a.src = item.url;
  const head = el("div", "ahead");       // アイコンとタイトルは 1 行に(スペクトラムの高さを確保する)
  head.append(el("div", "aicon", "🎵"), el("div", "atitle", p.theme || item.filename || ""));
  box.append(vizBox(a, { viz: p.viz, mood: p.mood, genre: p.genre, caption: p.caption }), head);
  if (p.caption) box.append(el("div", "acap", p.caption));
  box.append(a);
  if (p.lyrics && p.lyrics !== "[Instrumental]") {
    const d = el("details", "alyrics");
    d.append(el("summary", "", t("歌詞")), el("pre", "", p.lyrics));
    box.append(d);
  }
  return box;
}

function currentItem() {
  const id = studio.viewing[studio.tab];
  return (id && byId(id)) || tabItems()[0] || null;
}

function renderViewer() {
  if (studio.own[studio.tab]) return;
  const item = currentItem();
  const v = $("viewer"), meta = $("vmeta");
  meta.hidden = !item;
  if (!item) {
    if (v.dataset.id !== "none") { v.dataset.id = "none"; detachVisualizers(v); v.replaceChildren(el("div", "placeholder", t("まだ生成物がありません"))); }
    renderUpscalePanel(null);
    return;
  }
  if (v.dataset.id !== item.id) {       // 同じものを出し直さない(動画の再生が途切れないように)
    v.dataset.id = item.id;
    detachVisualizers(v);                // 外す音楽のカードのスペクトラム(ループと音の経路)を片付ける
    let m;
    if (item.kind === "audio") { m = audioCard(item); }
    else {
      if (item.kind === "video") { m = el("video"); m.controls = true; }
      else { m = el("img"); m.alt = item.filename || ""; makeDraggable(m, item.id); }
      m.src = item.url;
    }
    v.replaceChildren(m);
  }
  const p = item.params || {};
  $("vname").textContent = (item.kind === "audio" && p.theme) || item.filename || "";
  const info = [];
  if (item.width) info.push(`${item.width}×${item.height}`);
  if (item.duration_s) info.push(fmtSec(item.duration_s));
  if (item.kind === "audio") {
    if (p.bpm) info.push(`BPM ${p.bpm}`);
    if (p.key_scale) info.push(p.key_scale);
  }
  const sec = item.params && item.params.result && item.params.result.generation_seconds;
  if (sec) info.push(t("生成 {time}", { time: fmtSec(sec) }));
  const up = p.upscale;          // 拡大して作ったもの: 寸法などの後ろに「×2 Real-ESRGAN(アニメ向け)」の印(編集タブの書き出しと同じ)
  const tag = up ? el("span", "tag ext edtag", `×${up.scale} ${UPSCALE.label(up.method)}`) : null;
  if (tag && p.source_filename) tag.title = t("拡大の元: {name}", { name: p.source_filename });
  if (up && !UPSCALE.tools && !UPSCALE.error) UPSCALE.load();      // 方式の名前は、道具の様子を読んでから(読めたら描き直す)
  $("vinfo").replaceChildren(info.join("  "), ...(tag ? [tag] : []));
  const save = $("vsave");
  save.href = item.url;
  save.download = item.filename || "";
  const acts = $("vacts");        // 「参照に使う」など、いまのタブごとのボタン
  acts.replaceChildren();
  for (const a of (studio.actions[studio.tab] ? studio.actions[studio.tab](item) : [])) {
    const b = el("button", "", a.label);
    b.onclick = a.run;
    acts.append(b);
  }
  renderUpscalePanel(item);
}

// ---- 拡大(AI アップスケール): ビューアの動画・画像を、AI で 2〜4 倍にした新しい生成物にする(POST /api/gallery/{id}/upscale → ジョブ)。元のものは残る ----
// 道具の様子・方式と倍率の選びは upscale.js(UPSCALE。編集タブの書き出しの拡大と共通)。走っている拡大は、見ている生成物やタブが変わっても追い続ける
// (進みの行はジョブのもので、拡大している元の名前を出す)。同時に走らせるのは 1 つだけ

const UP_KINDS = ["video", "image"];            // 拡大できるもの(曲は無し)
const vup = { open: false, job: null, last: null, timer: null, blocked: true };    // open: 欄を開いている、job: 走っている拡大のジョブ({...ジョブ, tab, name})、
                                                                                   // last: 直近の失敗・中止(次の操作まで残す)、blocked: いまの選びでは拡大できない
const canUpscale = (item) => !!item && UP_KINDS.includes(item.kind);

// いまの選び(方式・倍率)。倍率は 2〜4(覚えた倍率が 2 未満なら 2)。その方式に無い倍率は、近いもの(同じ近さなら小さいほう)にする
function upscaleChoice(item) {
  const U = UPSCALE, sel = $("vup-method");
  U.fillMethods(sel);
  sel.value = U.method(U.pref.method) ? U.pref.method : "auto";
  const want = Math.min(4, Math.max(2, Math.round(Number(U.pref.scale) || 2)));
  const scales = U.plan(sel.value, want, item.width, item.height, item.kind).scales;
  const scale = !scales.length || scales.includes(want) ? want : scales.reduce((a, b) => (Math.abs(b - want) < Math.abs(a - want) ? b : a));
  return { method: sel.value, scale, scales, plan: U.plan(sel.value, scale, item.width, item.height, item.kind) };
}

// 「拡大」のボタンと欄(動画・画像のときだけ)。欄の中身は、見ている生成物の寸法と、道具の様子から
function renderUpscalePanel(item) {
  const ok = canUpscale(item), open = ok && vup.open, btn = $("vup");
  btn.hidden = !ok;
  btn.classList.toggle("active", open);
  btn.setAttribute("aria-expanded", String(open));
  $("vup-panel").hidden = !open;
  if (!open) { renderUpscaleProgress(); return; }      // 閉じていても、ボタンと進みの行の状態はジョブに合わせておく
  const U = UPSCALE, tools = U.tools, c = upscaleChoice(item), plan = c.plan, sc = $("vup-scale");
  for (const o of Array.from(sc.options)) o.disabled = c.scales.length > 0 && !c.scales.includes(Number(o.value));
  sc.value = String(c.scale);
  const parts = [];
  if (U.error) parts.push(U.error);
  else if (!tools) parts.push(t("拡大の方式を調べています…"));
  else if (!plan.method && c.method === "auto") parts.push(t("拡大の方式が入っていません。下の「入れる」で入れてください"));
  else if (plan.method) {
    const how = c.method === "auto" ? U.label(plan.method) : "";       // おすすめ(自動)のときは、使う方式の名前も
    parts.push(plan.size && plan.scale > 1 ? `${item.width}×${item.height} → ${plan.size[0]}×${plan.size[1]}` + (how ? `  ${how}` : "") : how);
    if (plan.tooBig) parts.push(t("拡大後の長辺が {max}px を超えます", { max: tools.max_edge }));
    if (!plan.usable) parts.push(plan.hint || t("この方式はまだ使えません"));
    if (plan.method === "rtxvsr" && !(U.method("rtxvsr") || {}).installed) parts.push(t("NVIDIA の利用条件に同意して入れる方式です"));      // 入れる前の案内
  }
  $("vup-note").textContent = parts.filter(Boolean).join(" ");
  U.renderTools($("vup-tools"));        // 入っていない方式ごとに「入れる」。導入中はその進み、失敗の理由も
  vup.blocked = !plan.method || plan.scale < 2 || plan.tooBig || !plan.usable;
  renderUpscaleProgress();
}

// 進みの行(ジョブの進み、または直近の失敗・中止)と、拡大する / 中止 のボタン
function renderUpscaleProgress() {
  const j = vup.job || vup.last, box = $("vup-prog");
  $("vup-go").disabled = !!vup.job || vup.blocked;
  $("vup-stop").hidden = !(vup.job && vup.job.id);
  box.hidden = !j;
  if (!j) return;
  const failed = j.status === "failed", cancelled = j.status === "cancelled";
  const pct = j.status === "running" ? Math.round((j.progress || 0) * 100) : 0;
  box.classList.toggle("err", failed);
  const bar = box.querySelector(".pbar");
  bar.hidden = failed || cancelled;
  bar.classList.toggle("indet", j.status !== "running" || pct === 0);
  bar.querySelector("i").style.width = j.status === "running" && pct > 0 ? pct + "%" : "";
  box.querySelector(".vupmsg").textContent = failed ? j.text || t("失敗: {error}", { error: j.error || t("原因不明") }) : cancelled ? t("中止しました")
    : t("拡大中: {name} {msg}", { name: j.name, msg: (j.status === "queued" && j.wait_reason) || j.message || "" }) + (pct > 0 ? `  ${pct}%` : "");
  box.title = (failed && j.log_tail) || "";
}

async function startUpscale() {
  const item = currentItem();
  if (!canUpscale(item) || vup.job) return;
  const c = upscaleChoice(item), tab = studio.tab;
  vup.last = null;
  vup.job = { id: null, status: "queued", progress: 0, message: t("順番を待っています"), tab, name: item.filename || item.id };
  renderUpscaleProgress();
  try {
    const r = await fetch(`/api/gallery/${encodeURIComponent(item.id)}/upscale`, { method: "POST", headers: langHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ method: c.method, scale: c.scale, tab }) });
    const js = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof js.detail === "string" ? js.detail : r.status === 422 ? t("入力を確認してください") : t("通信に失敗しました({status})", { status: r.status }));
    vup.job = { ...vup.job, id: js.id };
  } catch (e) {
    vup.job = null;
    vup.last = { status: "failed", text: t("拡大を始められませんでした: {error}", { error: e.message || e }) };
    renderUpscaleProgress();
    return;
  }
  renderUpscaleProgress();
  clearTimeout(vup.timer);
  vup.timer = setTimeout(pollUpscale, 300);
}

async function pollUpscale() {
  const cur = vup.job;
  if (!cur || !cur.id) return;
  try {
    const r = await fetch(`/api/jobs/${cur.id}`, { headers: langHeaders() });
    if (r.status === 404) { vup.job = null; renderUpscaleProgress(); return; }      // ジョブが消えた(管理役の再起動など)
    if (r.ok) {
      const job = await r.json();
      if (["completed", "failed", "cancelled"].includes(job.status)) { await finishUpscale(cur, job); return; }
      vup.job = { ...job, tab: cur.tab, name: cur.name };
      renderUpscaleProgress();
    }
  } catch (e) { /* 一時的な通信失敗は次の周で再試行 */ }
  vup.timer = setTimeout(pollUpscale, 1000);
}

// 終わった: できたら一覧を読み直し、始めたタブのビューアで新しい生成物を開いて、欄を閉じる。失敗・中止は、次の操作まで進みの行に残す
async function finishUpscale(cur, job) {
  vup.job = null;
  if (job.status === "completed") {
    vup.last = null;
    vup.open = false;
    await loadGallery();
    if ((job.outputs || []).length) studio.viewing[cur.tab] = job.outputs[0];
    renderHistory();
    renderViewer();
  } else {
    vup.last = { ...job, name: cur.name };
    renderUpscaleProgress();
  }
}

$("vup").onclick = () => {
  vup.open = !vup.open;
  if (!vup.job) vup.last = null;        // 失敗・中止の知らせは、次の操作まで
  if (vup.open) UPSCALE.load();          // 道具の様子を読み直す(別の画面やコマンドで入れたかもしれない)
  renderUpscalePanel(currentItem());
};
$("vup-go").onclick = startUpscale;
$("vup-stop").onclick = async () => {
  const j = vup.job;
  if (!j || !j.id) return;
  try { await fetch(`/api/jobs/${j.id}/cancel`, { method: "POST", headers: langHeaders() }); } catch (e) { /* 次の周で状態が分かる */ }
};
$("vup-scale").onchange = () => { UPSCALE.pref.scale = Number($("vup-scale").value) || 2; UPSCALE.remember(); renderUpscalePanel(currentItem()); };
$("vup-method").onchange = () => { UPSCALE.pref.method = $("vup-method").value || "auto"; UPSCALE.remember(); renderUpscalePanel(currentItem()); };
UPSCALE.onChange(() => { if (!studio.own[studio.tab]) renderViewer(); });      // 道具の様子が変わった(読めた・導入が進んだ): 欄と、拡大の印の方式の名前を描き直す

// ---- ギャラリー(右側 2 枚を 1 枚に切り替える) ----

function setGalleryView(on) {
  if (studio.select.on) setSelecting(false);      // 選んだものは、履歴とギャラリーの間で持ち越さない
  $("workspace").classList.toggle("show-gallery", on);
  $("gallery").hidden = !on;
  if (on) loadGallery();
}

const galleryItems = () => studio.items.filter((i) => studio.galleryKind === "all" || i.kind === studio.galleryKind);

function renderGallery() {
  const grid = $("gal-grid");
  grid.replaceChildren();
  const list = galleryItems();
  if (!list.length) { grid.append(el("div", "placeholder", t("まだ何もありません"))); return; }
  for (const it of list) {
    grid.append(thumbNode(it, (x) => {      // クリック: その生成物を、いまのタブの生成物ビューアで開く(MV タブは、その MV を作った制作を開く)
      const own = studio.own[studio.tab];
      if (own) { const keep = own.open ? own.open(x) : false; if (!keep) setGalleryView(false); return; }      // open が true を返したら、ギャラリーは開いたまま(編集タブ: 続けてクリップを選べる)
      studio.viewing[studio.tab] = x.id;
      setGalleryView(false);
      renderHistory();
      renderViewer();
    }, deleteItem));
  }
}

// ---- 削除(1 件でも、まとめてでも) ----

// 生成物をサーバに消してもらう(1 回の通信)。まだ要るもの(使っているジョブや MV の制作があるもの)は断られるので、その理由を 1 回だけ知らせる
async function removeOutputs(items) {
  let js;
  try {
    const r = await fetch("/api/gallery/delete", { method: "POST", headers: langHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ ids: items.map((i) => i.id) }) });
    js = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof js.detail === "string" ? js.detail : t("入力を確認してください"));
  } catch (e) { alert(t("削除できませんでした: {error}", { error: e.message || e })); return; }
  await forget([...js.deleted, ...js.missing]);
  if (js.refused.length) alert(refusedText(js.refused));
}

// 断られた生成物を、理由ごとにまとめた文(1 回の alert で足りるように)
function refusedText(refused, head = t("{n} 件は削除できませんでした。", { n: refused.length })) {
  const by = new Map();
  for (const f of refused) {
    const it = byId(f.id);
    by.set(f.reason, [...(by.get(f.reason) || []), (it && (it.filename || it.id)) || f.id]);
  }
  return `${head}\n` + [...by].map(([reason, names]) => t("・{reason}\n  {names}", { reason, names: names.slice(0, 5).join(t("、")) + (names.length > 5 ? t(" ほか {n} 件", { n: names.length - 5 }) : "") })).join("\n");
}

// 消えた生成物の後始末: ビューアで見ていたものは外し(そのタブの、次に新しいものか、無ければ空の表示になる)、各タブの入力欄の参照も外させて、一覧を読み直す
async function forget(ids) {
  for (const k of Object.keys(studio.viewing)) if (ids.includes(studio.viewing[k])) delete studio.viewing[k];
  document.dispatchEvent(new CustomEvent("studio-deleted", { detail: { ids } }));
  await loadGallery();
}

async function deleteItem(item) {
  if (!confirm(t("「{name}」を削除しますか?\nファイルも消え、元に戻せません。", { name: item.filename || item.id }))) return;
  await removeOutputs([item]);
}

// 選んだものをまとめて消す(確認は 1 回)。消したら true、やめたら false
async function deleteItems(items) {
  if (!confirm(t("{n} 件を削除しますか?\nファイルも消え、元に戻せません。", { n: items.length }))) return false;
  await removeOutputs(items);
  return true;
}

$("vdel").onclick = () => { const item = currentItem(); if (item) deleteItem(item); };

// ---- 選択モード(履歴・ギャラリー共通): サムネイルを選んで、まとめて消す ----

// サムネイルを選べるようにする: クリックで選び直し、選んでいるものに ✓ を出す。ドラッグ元にはしない(MV タブの制作のカードも使う)
function pickable(node, id) {
  node.classList.add("picking");
  node.classList.toggle("picked", studio.select.ids.has(id));
  node.append(el("span", "tcheck", "✓"));
  node.onclick = () => {
    const ids = studio.select.ids;
    if (!ids.delete(id)) ids.add(id);
    node.classList.toggle("picked", ids.has(id));
    renderSelectBar();
  };
}

// いま見えているグリッドの ID(ギャラリー、または履歴。MV タブの履歴は制作の ID)
function shownIds() {
  const own = studio.own[studio.tab];
  if ($("gallery").hidden && own) return own.ids();
  return ($("gallery").hidden ? tabItems() : galleryItems()).map((i) => i.id);
}

const pickedIds = () => shownIds().filter((id) => studio.select.ids.has(id));       // 選んでいるもののうち、いま見えているもの(絞り込みで隠れたものは数えない)

// 見出しの「選択」と、選択モードの「N 件を選択 / すべて選択 / 削除 / やめる」の切り替え
function renderSelectBar() {
  const on = studio.select.on, n = pickedIds().length;
  document.querySelectorAll(".seltoggle").forEach((b) => { b.hidden = on; });
  document.querySelectorAll(".selbar").forEach((bar) => {
    bar.hidden = !on;
    bar.querySelector(".selcount").textContent = t("{n} 件を選択", { n });
    bar.querySelector(".seldel").disabled = !n;
  });
}

function setSelecting(on) {
  studio.select.on = on;
  studio.select.ids.clear();
  renderSelectBar();
  renderHistory();
  renderGallery();
}

async function deleteSelected() {
  const ids = pickedIds(), own = studio.own[studio.tab];
  if (!ids.length) return;
  const done = $("gallery").hidden && own ? await own.remove(ids) : await deleteItems(ids.map(byId));
  if (done) setSelecting(false);
}

document.querySelectorAll(".seltoggle").forEach((b) => { b.onclick = () => setSelecting(true); });
document.querySelectorAll(".selend").forEach((b) => { b.onclick = () => setSelecting(false); });
document.querySelectorAll(".seldel").forEach((b) => { b.onclick = deleteSelected; });
document.querySelectorAll(".selall").forEach((b) => {
  b.onclick = () => {
    shownIds().forEach((id) => studio.select.ids.add(id));
    renderHistory();
    renderGallery();
    renderSelectBar();
  };
});

$("to-gallery").onclick = () => setGalleryView(true);
$("from-gallery").onclick = () => setGalleryView(false);
document.querySelectorAll("#g-chips .chip").forEach((c) => c.addEventListener("click", () => {
  studio.galleryKind = c.dataset.kind;
  document.querySelectorAll("#g-chips .chip").forEach((x) => x.classList.toggle("active", x === c));
  renderGallery();
  renderSelectBar();      // 絞り込みで見えなくなったものは、選んだ数に入れない
}));

// ---- タブ ----

function setTab(tab) {
  studio.tab = tab;
  if (studio.select.on) setSelecting(false);      // 選択モードは、タブを越えて続けない
  $("workspace").dataset.tab = tab;
  document.querySelectorAll(".tab").forEach((x) => { x.classList.toggle("active", x.dataset.tab === tab); x.setAttribute("aria-selected", String(x.dataset.tab === tab)); });
  document.querySelectorAll(".tabpanel").forEach((p) => { p.hidden = p.id !== "tab-" + tab; });
  $("progress").hidden = true;      // 進捗は、そのタブのジョブがあるときだけ各タブの js が出す
  renderHistory();
  renderViewer();
  studio.on.tab.forEach((f) => f(tab));
}

// 画面の外にファイルを落としたとき、ブラウザがそのファイルを開いてしまうのを防ぐ
for (const ev of ["dragover", "drop"]) {
  document.addEventListener(ev, (e) => { if ([...(e.dataTransfer?.types || [])].includes("Files")) e.preventDefault(); });
}

loadGallery();
