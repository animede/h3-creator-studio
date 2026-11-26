// MV タブ(リップシンク MV): 曲とキャラクター画像から、口の合った MV を作る。
// 左 = 入力、右上 = いまの制作(進み・最終の動画・シーン)、右下 = 制作の履歴(ギャラリーに切り替え可)。ジョブは管理役の Director がつなぐので、ここは /api/mv を見て操作するだけ。
// 人物の説明・曲の内容は、「画像から作る」「曲から作る」で入力欄に自動で入れて、確かめてから作れる(空のまま作っても、制作の中で自動で入る)。
(() => {   // 他のタブと名前がぶつからないよう、タブごとに閉じる
  const SIZES = VIDEO_SIZES, QUALITY_NOTE = VIDEO_QUALITY_NOTE;      // 画角 × 画質の寸法と、画質の説明(web/project.js。ショート動画タブと共通)
  const CHAR_MAX = 600, CONCEPT_MAX = 1000;
  const LISTEN_LABEL = [["language", t("言語")], ["genre", t("ジャンル")], ["mood", t("雰囲気")], ["instruments", t("楽器")], ["vocal", t("ボーカル")], ["tempo", t("テンポ")]];
  // 制作に出す「自動で入れた説明」の出どころ(サーバの character_source / concept_source。書いた説明 typed は出さない)
  const AUTO_HOW = { character: { vision: t("画像を見て自動で作りました"), image: t("画像のプロンプトから入れました"),
    none: t("画像を読めなかったので、空のままです(演出は、画像の人物としか書きません)") },
  concept: { listen: t("曲を聞いて自動で作りました"), song: t("曲のテーマ・キャプションから入れました") } };

  const mvp = (id) => $("mv-" + id);
  let song = null, image = null;          // 入力(生成物のレコード)
  let tools = { available: true, hint: null };
  let creating = false, qualityTouched = false;

  const api = apiJson;      // JSON の API(web/project.js)

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
    updateAuto();
    if (!song) { body.append(el("div", "refs-empty", t("ここに曲をドロップ"))); return; }
    const c = el("div", "songitem");
    const text = el("div", "stext");
    const info = el("div", "sinfo", [song.duration_s ? fmtDur(song.duration_s) : "", song.params && song.params.genre, song.filename].filter(Boolean).join("  "));
    info.title = info.textContent;      // ジャンルが自由に書いた長い文のことがある(1 行に収め、全文は吹き出しに)
    text.append(el("div", "stitle", (song.params && song.params.theme) || song.filename || t("曲")), info);
    const a = el("audio");
    a.controls = true;
    a.src = song.url;
    text.append(a);
    const x = el("button", "rdel", "×");
    x.title = t("外す");
    x.onclick = clearSong;
    c.append(el("div", "aicon", "🎵"), text, x);
    makeDraggable(c, song.id);
    body.append(c);
  }

  function renderImage() {
    const body = mvp("image").querySelector(".frame-body");
    body.replaceChildren();
    updateAuto();
    if (!image) { body.append(el("div", "refs-empty", t("ここにキャラクター画像をドロップ"))); return; }
    const c = el("div", "frame-item");
    const img = el("img");
    img.src = image.thumb_url || image.url;
    img.title = image.filename || "";
    img.draggable = false;
    const x = el("button", "rdel", "×");
    x.title = t("外す");
    x.onclick = () => { image = null; renderImage(); };      // 画像を見ている最中なら、その答えは使わない(describeImage が見分ける)
    c.append(img, x);
    makeDraggable(c, image.id);
    body.append(c);
  }

  function setSong(item) {
    if (item.kind !== "audio") { showMsg(t("曲には音声を落としてください(音楽タブの曲、または音声ファイル)")); return false; }
    song = item;
    showMsg("");
    showListenInfo(null);
    mvp("concept-note").hidden = true;
    fill(mvp("concept"), conceptFrom(item.params));
    renderSong();
    return true;
  }

  function setImage(item) {
    if (item.kind !== "image") { showMsg(t("キャラクターには画像を落としてください")); return false; }
    image = item;
    showMsg("");
    mvp("char-note").hidden = true;
    fill(mvp("char"), descPrompt(item).slice(0, CHAR_MAX));
    renderImage();
    return true;
  }

  // PC のファイルは、取り込んでからギャラリーの生成物として入れる(種類が合わないものはギャラリーに残すだけ)
  async function upload(files, box, kind, set) {
    try {
      const rec = (await uploadFiles(files, box)).find((r) => r.kind === kind);
      if (rec) set(rec); else showMsg(kind === "audio" ? t("音声のファイルではありません(ギャラリーには保存しました)") : t("画像のファイルではありません(ギャラリーには保存しました)"));
    } catch (e) { showMsg(t("取り込めませんでした: {err}", { err: e.message || e })); }
  }

  const active = () => studio.tab === "mv";
  wireDrop(mvp("song"), { zone: mvp("song-fld"), active, msg: showMsg, onItem: setSong, onFiles: (files) => upload(files, mvp("song"), "audio", setSong) });
  wireDrop(mvp("image"), { zone: mvp("image-fld"), active, msg: showMsg, onItem: setImage, onFiles: (files) => upload(files, mvp("image"), "image", setImage) });

  // 消された曲・画像は入力欄から外す。自動で入れた説明の文(曲の内容・人物の説明)も、自分で書き直していなければ外す
  // (消したとき(studio-deleted)と、一覧を読み直したらギャラリーに無かったとき)
  function dropGone(gone) {
    if (song && gone(song.id)) { clearSong(); fill(mvp("concept"), ""); }
    if (image && gone(image.id)) { image = null; fill(mvp("char"), ""); renderImage(); }
  }
  studio.on.items.push(() => dropGone(goneFromGallery));      // 一覧が全部のときだけ(web/gallery.js の goneFromGallery)
  document.addEventListener("studio-deleted", (e) => dropGone((id) => e.detail.ids.includes(id)));

  // ---- 入力: 自動の説明(画像から人物の説明、曲から曲の内容・世界観) ----

  const busy = { char: false, concept: false };

  const hasPrompt = (item) => !!descPrompt(item);      // 画像タブで作った画像か(台帳に画像のプロンプトがある)

  // ボタン(落としたものがあって、通信中でないときだけ)と、「空のまま作ると、自動で入れます」の案内
  // (外から持ち込んだ=台帳に手がかりが無い画像・曲で、その欄がまだ空のときだけ)
  function updateAuto() {
    mvp("char-go").disabled = !image || busy.char;
    mvp("concept-go").disabled = !song || busy.concept;
    mvp("char-hint").hidden = !(image && !hasPrompt(image) && !mvp("char").value.trim());
    mvp("concept-hint").hidden = !(song && !conceptFrom(song.params) && !mvp("concept").value.trim());
  }
  mvp("char").addEventListener("input", updateAuto);
  mvp("concept").addEventListener("input", updateAuto);

  // 通信中の表示(くるくる・文言・経過時間)。label() が文言を返す。戻り値は、終わらせる関数
  function startBusy(which, label) {
    const t0 = Date.now();
    const draw = () => {
      const sec = Math.round((Date.now() - t0) / 1000);
      mvp(which + "-busy-text").textContent = t("{label}(経過 {time})", { label: label(), time: `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}` });
    };
    busy[which] = true;
    mvp(which + "-busy").hidden = false;
    draw();
    const timer = setInterval(draw, 1000);
    updateAuto();
    return () => { clearInterval(timer); busy[which] = false; mvp(which + "-busy").hidden = true; updateAuto(); };
  }

  function setNote(which, text) {
    mvp(which + "-note").hidden = !text;
    mvp(which + "-note").textContent = text || "";
  }

  // 自動で作った文を欄に入れる(自動で入れた文の印を付けるので、プリセットには入らない)。書いた文があれば、置き換えてよいか確かめる。入れたら true
  function putAuto(ta, text) {
    if (ta.value.trim() && ta.value !== ta.dataset.auto && !confirm(t("書いた内容を、自動で作った文に置き換えますか?"))) return false;
    ta.value = ta.dataset.auto = text;
    updateAuto();
    return true;
  }

  async function describeImage() {
    if (!image || busy.char) return;
    const target = image;
    showMsg("");
    setNote("char", "");
    const stop = startBusy("char", () => t("画像を見ています…"));
    try {
      const js = await api("/api/mv/describe/image", "POST", { image_id: target.id });
      if (image !== target) return;       // 待っている間に、画像が入れ替わった
      if (js.text && !putAuto(mvp("char"), js.text)) return;
      setNote("char", js.source === "vision" ? t("画像を見て作りました。確かめて、直してから MV を作れます") : js.note);
    } catch (e) {
      showMsg(t("人物の説明を作れませんでした: {err}", { err: e.message || e }));
    } finally {
      stop();
    }
  }
  mvp("char-go").onclick = describeImage;

  // 曲の聞き取り(参考): 言語・ジャンル・雰囲気・楽器・ボーカル・テンポ(閉じておく。歌詞は聞き取らない)
  function showListenInfo(r) {
    mvp("listen-info").hidden = !r;
    if (!r) return;
    mvp("listen-body").replaceChildren(...LISTEN_LABEL.filter(([key]) => r[key]).map(([key, label]) => {
      const row = el("div", "li-row");
      row.append(el("b", "", label), r[key]);
      return row;
    }));
  }

  function clearSong() {
    song = null;
    showListenInfo(null);
    setNote("concept", "");
    renderSong();
  }

  async function describeSong() {
    if (!song || busy.concept) return;
    const target = song;
    showMsg("");
    setNote("concept", "");
    showListenInfo(null);
    const stop = startBusy("concept", () => (conceptFrom(target.params) ? t("曲の内容を考えています…") : t("曲を聞いています…")));      // 音楽タブの曲は、聞かずに書く
    try {
      const js = await api("/api/mv/describe/song", "POST", { song_id: target.id });
      if (song !== target) return;       // 待っている間に、曲が入れ替わった
      if (!putAuto(mvp("concept"), js.text)) return;
      showListenInfo(js.listen);
      setNote("concept", js.source === "listen" ? t("曲を聞いて作りました。確かめて、直してから MV を作れます") : t("曲のテーマ・キャプションから作りました。確かめて、直してから MV を作れます"));
    } catch (e) {
      showMsg(t("曲の内容を作れませんでした: {err}", { err: e.message || e }));
    } finally {
      stop();
    }
  }
  mvp("concept-go").onclick = describeSong;

  // ---- 入力: 画角・画質・シード ----

  const customSize = () => mvp("aspect").value === "custom";
  function renderSize() {
    mvp("custom").hidden = !customSize();
    const [w, h] = videoSize(mvp("aspect").value, mvp("quality").value, mvp("cw").value, mvp("ch").value);
    mvp("size").textContent = `(${w}×${h})`;
    mvp("qnote").textContent = customSize() ? t("幅・高さを直接指定(256〜1344、32 の倍数に丸めます)。画質の欄は使いません") : QUALITY_NOTE[mvp("quality").value];
  }
  mvp("aspect").onchange = renderSize;
  mvp("cw").oninput = mvp("ch").oninput = renderSize;
  mvp("quality").onchange = () => { qualityTouched = true; renderSize(); };
  mvp("random").onchange = () => { mvp("seed").disabled = mvp("random").checked; };
  // 歌わせないときだけ、「曲の内容」に出したくないものを書く案内を出す(プリセットの反映も presetSet が change を流すので、ここで追従する)
  const renderLipsyncNote = () => { mvp("lipsync-note").hidden = mvp("lipsync").value !== "false"; };
  mvp("lipsync").onchange = renderLipsyncNote;
  renderLipsyncNote();

  // 既定の画質: VRAM が 16GiB 未満なら高速、それ以上なら標準(利用者が選んだ後は触らない)
  async function pickQuality() {
    try {
      const s = await fetch("/api/status", { headers: langHeaders() }).then((r) => r.json());
      if (!qualityTouched) mvp("quality").value = s.gpu && s.gpu.total_mib >= 16 * 1024 ? "standard" : "fast";
    } catch (e) { /* GPU の様子が分からなければ高速のまま */ }
    renderSize();
  }

  // 道具が無いか、参照モードに要る H3 のキャッシュが無い(MV のシーンは全部 Ref2VA)ときは作れない。どちらも案内はフォームの上(renderToolsBox)
  function updateGo() {
    mvp("go").disabled = creating || !tools.available || !!refCacheHint(tools);
  }

  async function loadTools() {
    try { tools = await api("/api/mv/tools"); } catch (e) { return; }
    renderToolsBox("mv", tools, !tools.available, t("MV に必要な道具が見つかりません"), loadTools);
    updateGo();
  }

  // ---- 作る ----

  async function generate() {
    if (!song) { showMsg(t("曲を入れてください(音楽タブの曲、ギャラリー、PC の音声ファイルをドロップ)")); return; }
    if (!image) { showMsg(t("キャラクター画像を入れてください")); return; }
    const min = +mvp("min").value, max = +mvp("max").value;
    if (!(min >= 5 && max <= 10 && min <= max)) { showMsg(t("シーンの長さは 5〜10 秒で、最短は最長以下にしてください")); return; }
    const seed = mvp("random").checked || mvp("seed").value === "" ? null : parseInt(mvp("seed").value, 10);
    showMsg("");
    creating = true;
    updateGo();
    try {
      const js = await api("/api/mv", "POST", { song_id: song.id, image_id: image.id, character: mvp("char").value.trim(), concept: mvp("concept").value.trim(),
        aspect: mvp("aspect").value, quality: mvp("quality").value, width: customSize() ? +mvp("cw").value : null, height: customSize() ? +mvp("ch").value : null,
        min_s: min, max_s: max, seed, background_text: mvp("bgtext").value, style: mvp("style").value,
        lipsync: mvp("lipsync").value !== "false" });
      await select(js.id);
    } catch (e) {
      showMsg(t("MV を始められませんでした: {err}", { err: e.message || e }));
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
      ...(customSize() ? { width: +mvp("cw").value, height: +mvp("ch").value } : {}),
      min_s: +mvp("min").value, max_s: +mvp("max").value, seed: presetGetSeed(mvp("seed"), mvp("random")), background_text: mvp("bgtext").value,
      style: mvp("style").value, lipsync: mvp("lipsync").value !== "false" };
  }

  // 書かれている項目だけ反映する(知らない項目は無視)。人物の説明・曲の内容は空でなければ置き換える(自分で書いた扱いなので、曲や画像を落としても消えない)
  function applyPresetValues(v) {
    const skipped = [];
    for (const [key, id] of [["character", "char"], ["concept", "concept"]]) if (typeof v[key] === "string" && v[key].trim()) presetSet(mvp(id), v[key]);
    for (const [key, label] of [["aspect", t("画角")], ["quality", t("画質")]]) if (key in v && !presetSet(mvp(key), v[key])) skipped.push(label);      // change で寸法の表示が合う
    for (const [key, id] of [["width", "cw"], ["height", "ch"]]) if (Number.isFinite(v[key])) presetSet(mvp(id), v[key]);
    if ("background_text" in v && !presetSet(mvp("bgtext"), v.background_text)) skipped.push(t("背景の文字"));
    if ("style" in v && !presetSet(mvp("style"), v.style || "none")) skipped.push(t("画風"));
    presetSet(mvp("lipsync"), String(v.lipsync !== false));      // 無ければ歌わせる(歌わせ方はプリセットが必ず決める。イメージ MV の後に別のを選んだら戻る)
    for (const [key, id] of [["min_s", "min"], ["max_s", "max"]]) if (Number.isFinite(v[key])) presetSet(mvp(id), v[key]);
    if ("seed" in v) presetSetSeed(mvp("seed"), mvp("random"), v.seed);
    return skipped;
  }
  wireStyleNote(mvp("style"), mvp("style-note"));
  wirePresets({ tab: "mv", box: mvp("presets"), get: getPresetValues, apply: applyPresetValues });

  // ---- サンプル(samples/<id>/ の入力欄の値と素材。読み込むと、いまの曲・キャラクター画像・説明は、サンプルのものに置き換わる。生成はしない) ----

  // values = プリセットと同じ項目(character / concept を含む)。assets = [{role: song | person, output(ギャラリーの生成物)}]
  function applySample(values, assets) {
    values = { style: "none", ...values };    // 画風はサンプルに無ければ「なし」
    song = null;
    image = null;
    for (const id of ["char", "concept"]) { mvp(id).dataset.auto = ""; presetSet(mvp(id), ""); }      // 自動で入れた文の印も消す
    for (const a of assets) {
      if (a.role === "song") setSong(a.output);
      else if (a.role === "person") setImage(a.output);
    }
    const skipped = applyPresetValues(values);     // 人物の説明・曲の内容は、素材から自動で入った文よりサンプルの文を優先する
    renderSong();
    renderImage();
    showMsg(t("サンプルを読み込みました。「MV を作る」で同じ条件で作れます") + (skipped.length ? t("(選べない値だったので、そのままにした項目: {items})", { items: skipped.join(t("、")) }) : ""));
  }
  wireSamples({ tab: "mv", box: mvp("samples"), apply: applySample });

  // ---- 制作の一覧・選択・更新・右側の画面(ショート動画タブと共通。web/project.js) ----

  // 自動で入れた説明(画像を見て作った人物の説明・曲を聞いて作った曲の内容など)を、どれが自動かが分かるように出す。書いた説明は出さない
  function autoRows(v) {
    return [["character", t("人物の説明"), v.params.character], ["concept", t("曲の内容・世界観"), v.params.concept]].flatMap(([key, label, text]) => {
      const src = (v.sources || {})[key], note = (v.source_notes || {})[key];
      let how = AUTO_HOW[key][src] || "";
      if (key === "concept" && !how && note) how = t("曲を聞けなかったので、曲の内容なしで作りました");      // 聞き取れなかった(理由は note)
      return how || note ? [{ label, how, text: how && src !== "none" ? text || "" : "", note: note || "" }] : [];
    });
  }

  const panel = makeProjectPanel({
    tab: "mv", base: "/api/mv", noun: "MV", root: mvp("project"), dialog: "proj", cardLabel: "MV", outName: "mv.mp4",
    placeholder: t("MV を作ると、ここに進み具合とシーンが並びます"), scenesPlaceholder: t("曲の分析が終わると、シーンがここに並びます"), historyPlaceholder: t("作った MV(制作)がここに並びます"),
    meta: (v, m) => {
      const a = v.analysis, pr = v.params;
      return [m.pct, m.size, pr.lipsync === false ? t("歌わせない(イメージ MV)") : "", m.scenes,
        a ? t("曲 {dur}", { dur: fmtDur(a.duration) }) + (a.bpm ? ` · BPM ${Math.round(a.bpm)}` : "") + (a.has_vocals === false && pr.lipsync !== false ? t(" · 歌なし(口は閉じたまま)") : "") : "", m.elapsed];
    },
    autoRows,
    onOpen: loadTools,
  });
  const select = panel.select;


  renderSong();
  renderImage();
  renderSize();
  updateGo();
  pickQuality();
})();
