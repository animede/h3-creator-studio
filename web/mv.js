// MV タブ(リップシンク MV): 曲とキャラクター画像から、口の合った MV を作る。
// 左 = 入力、右上 = いまの制作(進み・最終の動画・シーン)、右下 = 制作の履歴(ギャラリーに切り替え可)。ジョブは管理役の Director がつなぐので、ここは /api/mv を見て操作するだけ。
(() => {   // 他のタブと名前がぶつからないよう、タブごとに閉じる
  // 投稿先 × 画質の寸法(サーバの SIZES、docs/SPEC.md の表と同じ。実際の寸法はサーバが決める)
  const SIZES = {
    "16:9": { fast: [768, 448], standard: [1024, 576], high: [1344, 768] },
    "9:16": { fast: [448, 768], standard: [576, 1024], high: [768, 1344] },
    "1:1": { fast: [512, 512], standard: [768, 768], high: [960, 960] },
    "4:5": { fast: [512, 640], standard: [640, 800], high: [768, 960] },
  };
  const QUALITY_NOTE = { fast: "6〜12GB の GPU 向け。いちばん速い", standard: "16GB 以上の GPU 向け", high: "24GB 以上の GPU 向け。時間がかかります" };
  const STATE_LABEL = { analyzing: "分析中", scripting: "演出を作成中", generating: "シーンを生成中", assembling: "結合中", paused: "一時停止",
    completed: "完成", failed: "失敗", cancelled: "中止" };
  const SCENE_LABEL = { pending: "待機", running: "生成中", done: "完了", failed: "失敗" };
  const JOB_LABEL = { queued: "順番待ち", preparing: "準備中", running: "生成中" };
  const ACTIVE = ["analyzing", "scripting", "generating", "assembling"];
  const POLL_MS = 2000;
  const CHAR_MAX = 600, CONCEPT_MAX = 1000;

  const mvp = (id) => $("mv-" + id);
  let song = null, image = null;          // 入力(生成物のレコード)
  let projects = [];                       // /api/mv の一覧(新しい順)
  let current = null;                      // 選んでいる制作の ID
  let view = null;                         // その制作の /api/mv/{id}
  let tools = { available: true, hint: null };
  let creating = false, qualityTouched = false, timer = null;

  const isActive = (p) => ACTIVE.includes(p.state) || !!p.job_id;
  const pct = (p) => Math.round((p || 0) * 100);
  const fmtElapsed = (created) => {
    const s = Math.max(0, Math.round(Date.now() / 1000 - created));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };

  async function api(url, method = "GET", body) {
    const r = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined });
    const js = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof js.detail === "string" ? js.detail : "入力を確認してください");
    return js;
  }

  function showMsg(text) {
    mvp("msg").hidden = !text;
    mvp("msg").textContent = text || "";
  }

  // ---- 入力: 曲・キャラクター画像のドロップ欄 ----

  // 空(または自動で入れた文のまま)の欄にだけ、ドロップした生成物の中身を入れる(中身が無ければ、自動で入れた文を外す)。自分で書いた文は消さない
  function fill(ta, text) {
    if (ta.value.trim() && ta.value !== ta.dataset.auto) return;
    ta.value = ta.dataset.auto = text || "";
  }

  // 曲の台帳の params(音楽タブの曲なら theme / caption / genre)から、曲の内容・世界観の文。サーバの concept_from_song と同じ組み立て
  function conceptFrom(params) {
    return ["theme", "caption", "genre"].map((k) => String((params && params[k]) || "").trim()).filter(Boolean).join("\n").slice(0, CONCEPT_MAX);
  }

  function renderSong() {
    const body = mvp("song").querySelector(".frame-body");
    body.replaceChildren();
    if (!song) { body.append(el("div", "refs-empty", "ここに曲をドロップ")); return; }
    const c = el("div", "songitem");
    const text = el("div", "stext");
    text.append(el("div", "stitle", (song.params && song.params.theme) || song.filename || "曲"),
      el("div", "sinfo", [song.duration_s ? fmtDur(song.duration_s) : "", song.params && song.params.genre, song.filename].filter(Boolean).join("  ")));
    const a = el("audio");
    a.controls = true;
    a.src = song.url;
    text.append(a);
    const x = el("button", "rdel", "×");
    x.title = "外す";
    x.onclick = () => { song = null; renderSong(); };
    c.append(el("div", "aicon", "🎵"), text, x);
    makeDraggable(c, song.id);
    body.append(c);
  }

  function renderImage() {
    const body = mvp("image").querySelector(".frame-body");
    body.replaceChildren();
    if (!image) { body.append(el("div", "refs-empty", "ここにキャラクター画像をドロップ")); return; }
    const c = el("div", "frame-item");
    const img = el("img");
    img.src = image.thumb_url || image.url;
    img.title = image.filename || "";
    img.draggable = false;
    const x = el("button", "rdel", "×");
    x.title = "外す";
    x.onclick = () => { image = null; renderImage(); };
    c.append(img, x);
    makeDraggable(c, image.id);
    body.append(c);
  }

  function setSong(item) {
    if (item.kind !== "audio") { showMsg("曲には音声を落としてください(音楽タブの曲、または音声ファイル)"); return false; }
    song = item;
    showMsg("");
    fill(mvp("concept"), conceptFrom(item.params));
    renderSong();
    return true;
  }

  function setImage(item) {
    if (item.kind !== "image") { showMsg("キャラクターには画像を落としてください"); return false; }
    image = item;
    showMsg("");
    fill(mvp("char"), String((item.params && item.params.prompt) || "").trim().slice(0, CHAR_MAX));
    renderImage();
    return true;
  }

  // PC のファイルは、取り込んでからギャラリーの生成物として入れる(種類が合わないものはギャラリーに残すだけ)
  async function upload(files, box, kind, set) {
    try {
      const rec = (await uploadFiles(files, box)).find((r) => r.kind === kind);
      if (rec) set(rec); else showMsg(`${kind === "audio" ? "音声" : "画像"}のファイルではありません(ギャラリーには保存しました)`);
    } catch (e) { showMsg(`取り込めませんでした: ${e.message || e}`); }
  }

  const active = () => studio.tab === "mv";
  wireDrop(mvp("song"), { zone: mvp("song-fld"), active, msg: showMsg, onItem: setSong, onFiles: (files) => upload(files, mvp("song"), "audio", setSong) });
  wireDrop(mvp("image"), { zone: mvp("image-fld"), active, msg: showMsg, onItem: setImage, onFiles: (files) => upload(files, mvp("image"), "image", setImage) });

  studio.on.items.push(() => {       // ギャラリーから消されたものは入力欄から外す
    if (song && !byId(song.id)) { song = null; renderSong(); }
    if (image && !byId(image.id)) { image = null; renderImage(); }
  });

  // ---- 入力: 投稿先・画質・シード ----

  function renderSize() {
    const [w, h] = SIZES[mvp("aspect").value][mvp("quality").value];
    mvp("size").textContent = `(${w}×${h})`;
    mvp("qnote").textContent = QUALITY_NOTE[mvp("quality").value];
  }
  mvp("aspect").onchange = renderSize;
  mvp("quality").onchange = () => { qualityTouched = true; renderSize(); };
  mvp("random").onchange = () => { mvp("seed").disabled = mvp("random").checked; };

  // 既定の画質: VRAM が 16GiB 未満なら高速、それ以上なら標準(利用者が選んだ後は触らない)
  async function pickQuality() {
    try {
      const s = await fetch("/api/status").then((r) => r.json());
      if (!qualityTouched) mvp("quality").value = s.gpu && s.gpu.total_mib >= 16 * 1024 ? "standard" : "fast";
    } catch (e) { /* GPU の様子が分からなければ高速のまま */ }
    renderSize();
  }

  function updateGo() {
    mvp("go").disabled = creating || !tools.available;
  }

  async function loadTools() {
    try { tools = await api("/api/mv/tools"); } catch (e) { return; }
    mvp("tools").hidden = tools.available;
    mvp("tools").textContent = tools.available ? "" : (tools.hint || "MV に必要な道具が見つかりません");
    updateGo();
  }

  // ---- 作る ----

  async function generate() {
    if (!song) { showMsg("曲を入れてください(音楽タブの曲、ギャラリー、PC の音声ファイルをドロップ)"); return; }
    if (!image) { showMsg("キャラクター画像を入れてください"); return; }
    const min = +mvp("min").value, max = +mvp("max").value;
    if (!(min >= 5 && max <= 10 && min <= max)) { showMsg("シーンの長さは 5〜10 秒で、最短は最長以下にしてください"); return; }
    const seed = mvp("random").checked || mvp("seed").value === "" ? null : parseInt(mvp("seed").value, 10);
    showMsg("");
    creating = true;
    updateGo();
    try {
      const js = await api("/api/mv", "POST", { song_id: song.id, image_id: image.id, character: mvp("char").value.trim(), concept: mvp("concept").value.trim(),
        aspect: mvp("aspect").value, quality: mvp("quality").value, min_s: min, max_s: max, seed });
      await select(js.id);
    } catch (e) {
      showMsg(`MV を始められませんでした: ${e.message || e}`);
    } finally {
      creating = false;
      updateGo();
    }
  }
  mvp("go").onclick = generate;

  // ---- プリセット(入力欄の値だけ。曲・キャラクター画像は含めない) ----

  // 人物の説明・曲の内容が、曲や画像を落としたときに自動で入った文のままなら、プリセットには入れない(生成物の中身なので)
  const typed = (ta) => (ta.value === ta.dataset.auto ? "" : ta.value.trim());

  function getPresetValues() {
    return { character: typed(mvp("char")), concept: typed(mvp("concept")), aspect: mvp("aspect").value, quality: mvp("quality").value,
      min_s: +mvp("min").value, max_s: +mvp("max").value, seed: presetGetSeed(mvp("seed"), mvp("random")) };
  }

  // 書かれている項目だけ反映する(知らない項目は無視)。人物の説明・曲の内容は空でなければ置き換える(自分で書いた扱いなので、曲や画像を落としても消えない)
  function applyPresetValues(v) {
    const skipped = [];
    for (const [key, id] of [["character", "char"], ["concept", "concept"]]) if (typeof v[key] === "string" && v[key].trim()) presetSet(mvp(id), v[key]);
    for (const [key, label] of [["aspect", "投稿先"], ["quality", "画質"]]) if (key in v && !presetSet(mvp(key), v[key])) skipped.push(label);      // change で寸法の表示が合う
    for (const [key, id] of [["min_s", "min"], ["max_s", "max"]]) if (Number.isFinite(v[key])) presetSet(mvp(id), v[key]);
    if ("seed" in v) presetSetSeed(mvp("seed"), mvp("random"), v.seed);
    return skipped;
  }
  wirePresets({ tab: "mv", box: mvp("presets"), get: getPresetValues, apply: applyPresetValues });

  // ---- 制作の一覧・選択・更新 ----

  async function loadList() {
    try { projects = await api("/api/mv?limit=100"); } catch (e) { /* 次の周で再試行 */ }
  }

  async function loadView() {
    if (!current) { view = null; return; }
    try {
      const r = await fetch(`/api/mv/${current}`);
      if (r.status === 404) { current = null; view = null; }
      else if (r.ok) view = await r.json();
    } catch (e) { /* 一時的な通信失敗は次の周で */ }
  }

  async function refresh() {
    await loadList();
    await loadView();
    if (!current && projects.length) {      // まだ何も選んでいなければ、動いている(無ければ最新の)制作を開く
      current = (projects.find(isActive) || projects[0]).id;
      await loadView();
    }
    renderProject();
    renderCards();
  }

  // 動いている制作がある間は 2 秒おきに読む(MV タブを開いているときだけ)
  function schedule() {
    clearTimeout(timer);
    if (studio.tab !== "mv") return;
    if ((view && isActive(view)) || projects.some(isActive)) timer = setTimeout(async () => { await refresh(); schedule(); }, POLL_MS);
  }

  async function select(id) {
    current = id;
    view = null;
    renderProject();
    await refresh();
    schedule();
  }

  async function act(path, method = "POST", body) {
    ui.actionErr.hidden = true;
    try {
      view = await api(path, method, body);
      renderProject();
    } catch (e) {
      ui.actionErr.hidden = false;
      ui.actionErr.textContent = `できませんでした: ${e.message || e}`;
    }
    await refresh();
    schedule();
  }

  async function remove(id, title) {
    if (!confirm(`「${title}」の制作を削除しますか?\nシーンのクリップも消えます。完成した MV はギャラリーに残ります。`)) return;
    try { await api(`/api/mv/${id}`, "DELETE"); } catch (e) { alert(`削除できませんでした: ${e.message || e}`); return; }
    if (current === id) { current = null; view = null; }
    await refresh();
    schedule();
  }

  // ---- 右上: いまの制作 ----

  const ui = { id: null, rows: new Map() };      // 組み立て済みの画面の部品(同じ制作は、組み立て直さず中身だけ更新する。動画の再生が途切れないように)

  function button(text, onclick, title) {
    const b = el("button", "", text);
    b.type = "button";
    b.onclick = onclick;
    if (title) b.title = title;
    return b;
  }

  function buildProject(v) {
    const root = mvp("project");
    root.replaceChildren();
    ui.id = v ? v.id : null;
    ui.rows = new Map();
    ui.count = -1;
    if (!v) {
      root.append(el("div", "placeholder", "MV を作ると、ここに進み具合とシーンが並びます"));
      return;
    }
    ui.name = el("b", "mvname");
    ui.badge = el("span", "badge");
    ui.msg = el("div", "mvmsg");
    ui.bar = el("div", "pbar mvbar");
    ui.bar.append(el("i"));
    ui.meta = el("div", "mvmeta");
    ui.err = el("div", "mverr");
    ui.err.hidden = true;
    ui.actionErr = el("div", "mverr");
    ui.actionErr.hidden = true;
    ui.btns = {
      pause: button("一時停止", () => act(`/api/mv/${v.id}/pause`), "走っている仕事は終わるまで続け、順番待ちの仕事は取り下げて止めます"),
      resume: button("再開", () => act(`/api/mv/${v.id}/resume`), "できているところから続けます"),
      cancel: button("中止", () => act(`/api/mv/${v.id}/cancel`), "走っている仕事を止めます(あとで再開できます)"),
      reassemble: button("結合し直す", () => act(`/api/mv/${v.id}/reassemble`), "いまのクリップをつなぎ直して、新しい MV を作ります"),
      remove: button("削除", () => remove(v.id, ui.title || "MV"), "この制作とクリップを消します(完成した MV はギャラリーに残ります)"),
    };
    const head = el("div", "mvhead");
    const title = el("div", "mvtitle");
    title.append(ui.name, ui.badge);
    const btns = el("div", "mvbtns");
    btns.append(...Object.values(ui.btns));
    head.append(title, ui.msg, ui.bar, ui.meta, ui.err, ui.actionErr, btns);

    ui.final = el("div", "mvfinal");
    ui.final.hidden = true;
    ui.video = el("video");
    ui.video.controls = true;
    ui.video.preload = "metadata";
    ui.fnote = el("span", "mvnote", "古い版です。結合し直すと更新されます");
    ui.fnote.hidden = true;
    ui.finfo = el("span");
    ui.save = el("a", "btn", "保存");
    ui.save.download = "";
    const fbar = el("div", "fbar");
    fbar.append(ui.save, ui.finfo, ui.fnote);
    ui.final.append(ui.video, fbar);

    ui.scenes = el("div", "mvscenes");
    root.append(head, ui.final, ui.scenes);
  }

  function buildRow(v, s) {
    const row = { index: s.index, edit: false };
    row.el = el("div", "mvscene");
    const head = el("div", "mvs-head");
    row.status = el("span", "badge");
    row.kind = el("span", "badge");
    row.time = el("span", "mvs-time");
    row.meta = el("span", "mvs-meta");
    head.append(el("b", "", `シーン ${s.index}`), row.time, row.kind, row.status, row.meta);
    const body = el("div", "mvs-body");
    row.video = el("video", "mvs-video");
    row.video.controls = true;
    row.video.preload = "none";
    row.video.hidden = true;
    row.none = el("div", "mvs-none");
    const actions = el("div", "mvs-actions");
    row.retry = button("やり直す", () => retry(row, false), "別の seed で作り直します(全シーンができると、自動で結合し直します)");
    row.editBtn = button("プロンプトを編集", () => toggleEdit(row), "演出(H3 に渡すプロンプト)と seed を直して、そのシーンだけ作り直します");
    actions.append(row.retry, row.editBtn);
    body.append(row.video, row.none, actions);
    row.editor = el("div", "mvs-edit");
    row.editor.hidden = true;
    row.ta = el("textarea");
    row.ta.maxLength = 8000;
    row.seed = el("input");
    row.seed.type = "number";
    row.seed.min = "0";
    row.seed.placeholder = "seed";
    row.seed.setAttribute("aria-label", "seed");
    const inline = el("div", "inline");
    inline.append(row.seed, button("この内容でやり直す", () => retry(row, true)), button("閉じる", () => toggleEdit(row)));
    row.editor.append(row.ta, inline);
    row.err = el("div", "mvs-err");
    row.err.hidden = true;
    row.el.append(head, body, row.editor, row.err);
    return row;
  }

  function toggleEdit(row) {
    row.edit = !row.edit;
    row.editor.hidden = !row.edit;
    const s = view && view.scenes.find((x) => x.index === row.index);
    if (row.edit && s) { row.ta.value = s.prompt || ""; row.seed.value = s.seed == null ? "" : s.seed; }
  }

  function retry(row, edited) {
    const s = view && view.scenes.find((x) => x.index === row.index);
    if (!s) return;
    let body;
    if (edited) {      // 書き換えた分だけ送る(プロンプトが同じなら送らない)
      const prompt = row.ta.value.trim();
      body = { prompt: prompt !== (s.prompt || "") ? prompt : null, seed: row.seed.value === "" ? null : parseInt(row.seed.value, 10) };
      row.edit = false;
      row.editor.hidden = true;
    }
    act(`/api/mv/${view.id}/scenes/${row.index}/retry`, "POST", body);
  }

  function updateRow(row, v, s) {
    row.el.className = "mvscene " + s.status + (v.params.height > v.params.width ? " tall" : "");      // 縦長の MV は、小さい動画も少し高く
    row.time.textContent = `${fmtDur(s.start)}–${fmtDur(s.end)}(${s.duration.toFixed(1)} 秒)`;
    row.kind.className = "badge " + s.kind;
    row.kind.textContent = s.kind === "vocal" ? "歌唱" : "器楽";
    row.kind.title = `歌っている割合 ${Math.round((s.vocal_ratio || 0) * 100)}%`;
    row.status.className = "badge " + s.status;
    row.status.textContent = s.status === "running" && s.job_status ? JOB_LABEL[s.job_status] || SCENE_LABEL.running : SCENE_LABEL[s.status] || s.status;
    row.meta.textContent = [s.seed != null ? `seed ${s.seed}` : "", s.elapsed_s ? `生成 ${fmtSec(s.elapsed_s)}` : "", s.peak_vram_gb ? `VRAM ${(+s.peak_vram_gb).toFixed(1)}GB` : ""]
      .filter(Boolean).join(" · ");
    row.err.hidden = !s.error;
    row.err.textContent = s.error || "";
    row.el.style.setProperty("--ar", `${v.params.width} / ${v.params.height}`);       // 小さい動画の縦横比(縦長の MV は縦長に)
    if (s.clip_url) {
      row.video.hidden = false;
      row.none.hidden = true;
      if (row.video.dataset.src !== s.clip_url) { row.video.dataset.src = s.clip_url; row.video.poster = s.thumb_url || ""; row.video.src = s.clip_url; }
    } else {
      row.video.hidden = true;
      row.none.hidden = false;
      row.none.textContent = s.status === "running" ? "生成中…" : s.status === "failed" ? "失敗" : s.prompt ? "未生成" : "演出待ち";
      if (row.video.dataset.src) { row.video.removeAttribute("src"); row.video.load(); delete row.video.dataset.src; }
    }
    const idle = !!s.prompt && s.status !== "running" && v.state !== "assembling";
    row.retry.disabled = row.editBtn.disabled = !idle;
  }

  function renderProject() {
    const v = view;
    if ((v ? v.id : null) !== ui.id) buildProject(v);
    if (!v) return;
    ui.title = v.title;
    ui.name.textContent = v.title;
    ui.badge.className = "badge " + v.state;
    ui.badge.textContent = STATE_LABEL[v.state] || v.state;
    const live = ACTIVE.includes(v.state);
    ui.msg.textContent = v.message || "";
    const p = pct(v.progress);
    ui.bar.querySelector("i").style.width = p + "%";
    ui.bar.classList.toggle("err", v.state === "failed");
    ui.bar.classList.toggle("ok", v.state === "completed");
    ui.bar.title = `${p}%`;
    const a = v.analysis, pr = v.params;
    ui.meta.textContent = [`${p}%`, `${pr.width}×${pr.height}(${pr.aspect})`, v.scenes.length ? `${v.scenes.filter((s) => s.status === "done").length} / ${v.scenes.length} シーン` : "",
      a ? `曲 ${fmtDur(a.duration)}${a.bpm ? ` · BPM ${Math.round(a.bpm)}` : ""}${a.has_vocals === false ? " · 歌なし(口は閉じたまま)" : ""}` : "",
      live ? `経過 ${fmtElapsed(v.created_at)}` : ""].filter(Boolean).join("  ");
    if (v.error) { ui.err.hidden = false; ui.err.textContent = `失敗: ${v.error}`; }
    else if (ui.err.textContent.startsWith("失敗:")) { ui.err.hidden = true; ui.err.textContent = ""; }
    const done = v.scenes.length > 0 && v.scenes.every((s) => s.status === "done");
    const b = ui.btns;
    b.pause.hidden = !live;
    b.resume.hidden = !["paused", "failed", "cancelled"].includes(v.state);
    b.cancel.hidden = !(live || v.state === "paused");
    b.reassemble.hidden = !(done && !v.job_id && ["completed", "paused", "failed", "cancelled"].includes(v.state));
    // 最終の動画
    ui.final.hidden = !v.output;
    if (v.output) {
      if (ui.video.dataset.src !== v.output.url) {
        ui.video.dataset.src = v.output.url;
        ui.video.poster = v.output.thumb_url || "";
        ui.video.src = v.output.url;
      }
      ui.save.href = v.output.url;
      ui.save.download = v.output.filename || "mv.mp4";
      ui.finfo.textContent = [v.output.width ? `${v.output.width}×${v.output.height}` : "", v.output.duration_s ? fmtSec(v.output.duration_s) : ""].filter(Boolean).join("  ");
      ui.fnote.hidden = v.assembled;
    }
    // シーン
    if (ui.count !== v.scenes.length) {      // シーンの数が変わったときだけ、行を組み立て直す
      ui.count = v.scenes.length;
      ui.rows = new Map(v.scenes.map((s) => [s.index, buildRow(v, s)]));
      ui.scenes.replaceChildren(...(v.scenes.length ? [...ui.rows.values()].map((r) => r.el) : [el("div", "placeholder", "曲の分析が終わると、シーンがここに並びます")]));
    }
    for (const s of v.scenes) { const row = ui.rows.get(s.index); if (row) updateRow(row, v, s); }
  }

  // ---- 右下: 制作の履歴 ----

  function projectNode(p) {
    const t = el("div", "thumb mvcard");
    t.title = `${p.title}\n${p.message || ""}`;
    if (p.thumb_url) {
      const img = el("img");
      img.src = p.thumb_url;
      img.alt = p.title;
      img.loading = "lazy";
      img.draggable = false;
      t.append(img);
    } else {
      t.append(el("div", "nothumb video", "MV"));
    }
    t.append(el("span", "tbadge top", p.title));
    t.append(el("span", "tbadge state", `${STATE_LABEL[p.state] || p.state}${ACTIVE.includes(p.state) ? ` ${pct(p.progress)}%` : ""}`));
    t.classList.toggle("sel", p.id === current);
    t.onclick = () => select(p.id);
    const x = el("button", "tdel", "×");
    x.title = "削除";
    x.onclick = (e) => { e.stopPropagation(); remove(p.id, p.title); };
    t.append(x);
    if (p.output) makeDraggable(t, p.output.id);      // 完成した MV は、ふつうの動画として他のタブへ落とせる
    return t;
  }

  // 一覧が変わっていなければ描き直さない(2 秒おきの更新で、ドラッグ中のカードを作り直さないように)。タブの切り替えなどでは force で描く
  let cardsSig = "";
  function renderCards(force = false) {
    const grid = $("hist-grid");
    if (studio.tab !== "mv") return;
    const sig = JSON.stringify([current, projects.map((p) => [p.id, p.state, pct(p.progress), p.title, p.thumb_url, p.output && p.output.id])]);
    if (!force && sig === cardsSig) return;
    cardsSig = sig;
    grid.replaceChildren();
    if (!projects.length) { grid.append(el("div", "placeholder", "作った MV(制作)がここに並びます")); return; }
    for (const p of projects) grid.append(projectNode(p));
  }

  // 生成物ビューアと履歴を、この画面で自前に描く(gallery.js の studio.own)。ギャラリーで MV を選んだら、それを作った制作を開く
  studio.own.mv = {
    history: () => renderCards(true),
    open: (item) => {
      const pid = item.params && item.params.project_id;
      if (pid && projects.some((p) => p.id === pid)) select(pid);
    },
  };

  studio.on.tab.push((tab) => {
    if (tab === "mv") { loadTools(); refresh().then(schedule); }
    else clearTimeout(timer);
  });

  renderSong();
  renderImage();
  renderSize();
  updateGo();
  buildProject(null);
  pickQuality();
})();
