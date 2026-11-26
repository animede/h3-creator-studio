// サンプルの欄(各タブの左上、プリセットの欄の下): 同梱の「作り方の見本」(samples/<id>/)を開き、読み込むと入力欄と素材(画像・曲)が見本のとおりになる。
// 読み込みで素材(参照画像・曲)をギャラリーに 1 回だけ登録する(もう一度読み込んでも増えない。登録は管理役の POST /api/samples/{id}/load)。
// 入力欄への反映だけで、生成はしない。サンプルが無いタブは、GET /api/samples?tab= が空なので、ボタンごと隠れたままになる。
// 各タブは wireSamples に、apply(values, assets)(入力欄への反映。assets は [{role, output(ギャラリーの生成物)}])を渡す。

// box(index.html の .samplebox。隠れている)にサンプルの欄を組み立てる。tab は image / video / music / mv / short
function wireSamples({ tab, box, apply }) {
  let list = [];
  let busy = false;
  let loaded = false;             // 一覧を読めたか(タブを開くたびに読み直す。途中で増えた・消えたぶんを拾う)

  const toggle = el("button", "mini", t("サンプル"));
  toggle.type = "button";
  toggle.title = t("同梱の作り方の見本を開きます(読み込むと、入力欄と素材が見本のとおりになります。生成はしません)");
  toggle.setAttribute("aria-expanded", "false");
  const panel = el("div", "sample-panel");
  panel.hidden = true;
  const note = el("div", "preset-note");
  note.hidden = true;
  box.setAttribute("role", "group");
  box.setAttribute("aria-label", t("サンプル"));
  box.append(toggle, panel, note);

  function say(text, err = false) {
    note.hidden = !text;
    note.textContent = text || "";
    note.classList.toggle("err", err);
  }

  toggle.onclick = () => {
    panel.hidden = !panel.hidden;
    toggle.setAttribute("aria-expanded", String(!panel.hidden));
  };

  async function api(url, method = "GET") {
    const r = await fetch(url, { method, headers: langHeaders() });
    const js = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof js.detail === "string" ? js.detail : t("通信に失敗しました({status})", { status: r.status }));
    return js;
  }

  // 素材の数(「画像 1 枚・曲 1 曲」)
  function assetSummary(s) {
    const n = (kind) => s.assets.filter((a) => a.kind === kind).length;
    return [n("image") ? t("画像 {n} 枚", { n: n("image") }) : "", n("audio") ? t("曲 {n} 曲", { n: n("audio") }) : ""].filter(Boolean).join(t("・")) || t("素材なし(物語の文章だけ)");
  }

  function card(s, goBtns) {
    const c = el("div", "sample-card");
    const head = el("div", "sample-head");
    if (s.thumb_url) {
      const img = el("img", "sample-thumb");
      img.src = s.thumb_url;
      img.alt = "";
      img.loading = "lazy";
      head.append(img);
    }
    const text = el("div", "sample-text");
    text.append(el("div", "sample-title", t(s.title)), el("div", "sample-desc", s.description ? t(s.description) : ""), el("div", "note", t("素材: {assets}", { assets: assetSummary(s) })));
    head.append(text);
    const go = el("button", "mini", t("読み込む"));
    go.type = "button";
    go.title = t("入力欄と素材を、このサンプルのとおりにします(生成はしません)");
    go.onclick = () => load(s);
    goBtns.push(go);
    c.append(head, go);
    if (s.video_url) {
      const v = el("video", "sample-video");
      v.controls = true;
      v.preload = "none";
      if (s.thumb_url) v.poster = s.thumb_url;
      v.src = s.video_url;
      c.append(v);
    }
    if (s.license) c.append(el("div", "note", t("ライセンス: {license}", { license: t(s.license) })));      // 値(sample.json の日本語)も題・説明と同じく訳す
    return c;
  }

  const buttons = [];
  function render() {
    buttons.length = 0;
    panel.replaceChildren(...list.map((s) => card(s, buttons)));
    box.hidden = !list.length;
    if (!list.length) panel.hidden = true;
  }

  async function load(s) {
    if (busy) return;
    busy = true;
    buttons.forEach((b) => { b.disabled = true; });
    say(t("素材を取り込んでいます…"));
    try {
      const js = await api(`/api/samples/${encodeURIComponent(s.id)}/load`, "POST");
      apply(js.values || {}, js.assets || []);
      say(t("「{title}」を読み込みました", { title: t(s.title) }));
      loadGallery();            // 取り込んだ素材(画像・曲)がギャラリーに出る
    } catch (e) {
      say(t("「{title}」を読み込めませんでした: {err}", { title: t(s.title), err: e.message || e }), true);
    } finally {
      busy = false;
      buttons.forEach((b) => { b.disabled = false; });
    }
  }

  async function refresh() {
    try {
      list = await api(`/api/samples?tab=${encodeURIComponent(tab)}`);
    } catch (e) {
      if (!loaded) box.hidden = true;       // 読めなければ出さない(サンプルは無くても困らない)
      return;
    }
    loaded = true;
    render();
  }

  studio.on.tab.push((t) => { if (t === tab) refresh(); });
  refresh();
}
