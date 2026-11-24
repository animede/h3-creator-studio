// 音楽タブ: ACE-Step の「簡単」(ジャンル・テーマ・歌詞・キャプション)と「おまかせ」(一言から)、詳細設定、生成中のプランの表示。
// ジャンルは、同梱のチップ 12 と、自作のチップ(AI が名前から定義を作る)と、チップの代わりに文で書く「自由に書く」から選ぶ。
(() => {   // 他のタブと名前がぶつからないよう、タブごとに閉じる
  const GENRES = ["J-POP", "Rock", "Ballad", "Anime", "Hip Hop", "R&B", "Enka", "EDM", "Reggaeton", "K-POP", "City Pop", "Jazz"];
  // 自作のジャンルのスペクトラムの雰囲気(曲のカードの色。web/viz.js の VIZ_MOOD_THEMES の 10 種)
  const VIZ_THEMES = [["energetic", "活気"], ["melancholic", "物憂い"], ["healing", "癒し"], ["epic", "壮大"], ["cyber", "電脳"],
    ["romantic", "ロマンチック"], ["dark", "闇"], ["festive", "祭り"], ["nostalgic", "ノスタルジック"], ["mystical", "神秘"]];
  const GENRE_BTNS = ["genre-add-go", "free-save", "free-name-go", "gedit-save", "gedit-del"];      // 通信中(AI がジャンルを作っている間など)は押せない
  const MOODS = [["bright and happy", "😊 明るい"], ["bittersweet and sad", "😢 切ない"], ["intense and powerful", "🔥 激しい"],
    ["calm and gentle", "🍃 穏やか"], ["mysterious and fantastical", "✨ 神秘的"], ["romantic and tender", "💕 ロマンチック"],
    ["playful and fun", "🎉 楽しい"], ["melancholic and dark", "🌙 メランコリック"], ["epic and grand", "🏔️ 壮大"], ["cool and stylish", "😎 クール"]];
  const VOCALS = [["female vocal", "👩 女性"], ["male vocal", "👨 男性"], ["powerful vocal", "🎤 力強い"],
    ["breathy whisper vocal", "🌬️ ウィスパー"], ["falsetto vocal", "🎵 ファルセット"], ["vocal harmonies", "👥 ハーモニー"]];
  const DEFAULT_NEGATIVE = "low quality, noisy, distorted, muddy, clipping, off-key, out of tune, amateur, poorly mixed";
  const PLAN_LINES = 8;           // プランの歌詞は、先頭の数行だけ見せる
  const MODEL_KEY = "studio.ace.model";
  let mode = "simple";
  let genre = "J-POP";            // 選んでいるチップの名前(同梱か自作)
  let genreMode = "chip";         // "chip": チップのジャンル / "free": 自由に書いた文をジャンルにする
  let custom = [];                // 自作のジャンル(/api/music/genres の builtin: false のもの)
  let gbusy = false;              // ジャンルの保存・削除・作成の通信中
  let editFor = "";               // 「ジャンルの中身」に入れているジャンルの印(同じ中身なら、書きかけの編集を消さない)
  let mood = "", vocal = "";
  let lastSeed = null;            // 前回の曲のシード(「同じ骨格で作り直す」に使う)
  let captionEdited = false;      // キャプションを利用者が書き換えた(AI が入れた文は、次の生成で作り直す)
  let fillPlan = false;           // 生成を押した直後: 届いたプランの中身を、テーマ・歌詞・キャプション欄に入れる
  let defaultModel = "";          // ACE の既定のモデル名(/api/music/models の default_model)。分かるまでは空

  const mu = (id) => $("mu-" + id);
  const val = (id) => mu(id).value.trim();
  const inst = () => mu("inst").checked;

  function showMsg(text) {
    mu("msg").hidden = !text;
    mu("msg").textContent = text || "";
  }

  // ---- 作り方(簡単 / おまかせ) ----

  function setMode(m) {
    mode = m;
    document.querySelectorAll("#mu-modes button").forEach((b) => b.classList.toggle("active", b.dataset.mode === m));
    showMsg("");
    render();
  }
  document.querySelectorAll("#mu-modes button").forEach((b) => { b.onclick = () => setMode(b.dataset.mode); });

  function render() {          // 作り方と「楽器のみ」に応じて、欄の出し入れをする
    const simple = mode === "simple";
    mu("omakase-box").hidden = simple;
    mu("simple-box").hidden = mu("simple-box2").hidden = !simple;
    // インストのときは、歌詞とボーカルは使わない
    const noVocal = inst();
    mu("lyrics").disabled = noVocal;
    mu("lyrics").placeholder = noVocal ? "インストなので歌詞はありません" : "自分の歌詞を使うときだけ書きます";
    mu("vocals").classList.toggle("off", noVocal);
    mu("regen").hidden = !(simple && !noVocal && val("lyrics"));
  }
  mu("inst").onchange = render;
  mu("lyrics").oninput = render;

  // ---- ムード・ボーカルのチップ(ジャンルのチップは、下の「ジャンル」) ----

  // 1 つだけ選べて、もう一度押すと外れるチップ。get() は今の値、set(v) は選び直し。戻り値は、印を今の値に付け直す関数
  function singleChips(box, items, get, set) {
    const mark = () => box.querySelectorAll(".chip").forEach((b) => b.classList.toggle("active", b.dataset.v === get()));
    for (const [v, label] of items) {
      const b = el("button", "chip", label);
      b.type = "button";
      b.dataset.v = v;
      b.onclick = () => { set(get() === v ? "" : v); mark(); };
      box.append(b);
    }
    return mark;
  }
  const markMood = singleChips(mu("moods"), MOODS, () => mood, (v) => { mood = v; });
  const markVocal = singleChips(mu("vocals"), VOCALS, () => vocal, (v) => { vocal = v; });

  // ---- 「作る」ボタン(管理役の LLM): テーマ / 歌詞 / キャプション ----

  const MAKERS = ["theme", "lyrics", "caption"];
  let making = false;
  let busy = false;

  function updateButtons() {
    for (const k of MAKERS) mu(k + "-go").disabled = making || busy;
    mu("lyrics-go").disabled = making || busy || inst();
    for (const id of GENRE_BTNS) mu(id).disabled = making || gbusy;
  }

  async function llm(name, body, label) {
    showMsg("");
    making = true;
    updateButtons();
    const btn = mu(name + "-go"), text = btn.textContent;
    btn.textContent = "作っています…";
    try {
      const r = await fetch("/api/music/" + name, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const js = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof js.detail === "string" ? js.detail : "作れませんでした");
      return js;
    } catch (e) {
      showMsg(`${label}を作れませんでした: ${e.message || e}`);
      return null;
    } finally {
      making = false;
      btn.textContent = text;
      updateButtons();
    }
  }

  // 「作る」ボタンの共通の項目。ジャンルは、チップの名前か自由に書いた文(自由欄が空なら、案内を出して null)
  const base = () => {
    const g = currentGenre();
    return g === null ? null : { genre: g, language: mu("lang").value };
  };

  // 利用者が書いたキャプション(AI が入れた文は渡さない)。テーマ・歌詞を AI が作るとき、その雰囲気に沿って書くための参考(生成のジョブにも同じものを渡す)
  const userCaption = () => (captionEdited ? val("caption") : "");

  mu("theme-go").onclick = async () => {
    const b = base();
    if (!b) return;
    const js = await llm("theme", { ...b, caption: userCaption() }, "テーマ");
    if (js) mu("theme").value = js.theme;
  };
  mu("lyrics-go").onclick = async () => {
    const b = base();
    if (!b) return;
    const js = await llm("lyrics", { ...b, theme: val("theme"), mood, vocal, caption: userCaption() }, "歌詞");
    if (!js) return;
    if (!val("theme")) mu("theme").value = js.theme;      // テーマが空だったときは、一緒に作ったテーマを入れる
    mu("lyrics").value = js.lyrics;
    mu("lyrics-note").textContent = `AI の推奨の長さ: 約 ${js.recommended_duration} 秒`;
    render();
  };
  mu("caption-go").onclick = async () => {
    const b = base();
    if (!b) return;
    const js = await llm("caption", { ...b, theme: val("theme"), lyrics: inst() ? "" : val("lyrics"), mood, vocal: inst() ? "" : vocal,
      thinking: mu("thinking").checked, instrumental: inst() }, "キャプション");
    if (js) { mu("caption").value = js.caption; captionEdited = true; }     // 作ったものは「手で書いた」扱い(生成で作り直さない)
  };
  mu("theme-clear").onclick = () => { mu("theme").value = ""; };
  mu("lyrics-clear").onclick = () => { mu("lyrics").value = ""; mu("lyrics-note").textContent = ""; render(); };
  mu("caption-clear").onclick = () => { mu("caption").value = ""; captionEdited = false; };
  mu("caption").oninput = () => { captionEdited = true; };

  // ---- ジャンル(同梱のチップ・自作のチップ・自由に書く) ----

  const customNamed = (name) => custom.find((g) => g.name === name);
  const genreNames = () => [...GENRES, ...custom.map((g) => g.name)];
  const selectedCustom = () => (genreMode === "chip" ? customNamed(genre) : undefined);      // 選んでいる自作のジャンル(チップが自作のときだけ)
  const genreViz = () => (selectedCustom() || {}).viz || "";                                  // 自作のジャンルのスペクトラムの雰囲気。同梱・自由な文は空(ムード・ジャンルから決まる)

  function genreSay(text, err = false) {
    mu("genre-note").hidden = !text;
    mu("genre-note").textContent = text || "";
    mu("genre-note").classList.toggle("err", err);
  }

  async function api(url, method = "GET", body) {
    const r = await fetch(url, { method, headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined });
    const js = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof js.detail === "string" ? js.detail : r.status === 422 ? "入力を確認してください" : `通信に失敗しました(${r.status})`);
    return js;
  }

  // いま使うジャンルの文: 自由に書いているときはその文(空白は 1 つに)、そうでなければチップの名前。自由欄が空なら、案内を出して null
  function currentGenre() {
    genreSay("");
    if (genreMode !== "free") return genre;
    const text = val("free").replace(/\s+/g, " ");
    if (!text) { genreSay("ジャンルを自由に書く欄が空です。書くか、チップを選んでください", true); mu("free").focus(); }
    return text || null;
  }

  function genreChip(name, mine) {
    const b = el("button", "chip" + (mine ? " mine" : ""), mine ? `✦ ${name}` : name);
    b.type = "button";
    b.dataset.genre = name;
    if (mine) b.title = customNamed(name).vibe;        // 自作のチップは、世界観の一行を吹き出しに
    b.onclick = () => selectGenre(name);
    return b;
  }

  function renderGenres() {
    const act = (kind, text, title, fn) => {
      const b = el("button", "chip act", text);
      b.type = "button";
      b.dataset.act = kind;
      b.title = title;
      b.onclick = fn;
      return b;
    };
    mu("genres").replaceChildren(...GENRES.map((g) => genreChip(g, false)), ...custom.map((g) => genreChip(g.name, true)),
      act("add", "＋ 追加", "ジャンルの名前から、AI が世界観・場面・感情の定義を作って、自分のチップに加えます", () => toggleAdd()),
      act("free", "✎ 自由に書く", "チップの代わりに、曲の様子を文で書きます(例: 90 年代のシティポップに三味線を混ぜた曲)", () => selectFree(genreMode !== "free")));
    markGenre();
  }

  // チップの印(選んでいるジャンル・自由に書く・追加の欄が開いている)と、自由欄・「ジャンルの中身」の出し入れ
  function markGenre() {
    mu("genres").querySelectorAll(".chip").forEach((b) => b.classList.toggle("active",
      b.dataset.act === "add" ? !mu("genre-add").hidden : b.dataset.act === "free" ? genreMode === "free" : genreMode === "chip" && b.dataset.genre === genre));
    mu("genre-free").hidden = genreMode !== "free";
    fillEdit();
  }

  function selectGenre(name) {
    genre = name;
    genreMode = "chip";
    genreSay("");
    markGenre();
  }

  function selectFree(on) {
    genreMode = on ? "free" : "chip";
    mu("free-name-row").hidden = true;
    genreSay("");
    markGenre();
    if (on) mu("free").focus();
  }

  function toggleAdd(open = mu("genre-add").hidden) {
    mu("genre-add").hidden = !open;
    genreSay("");
    markGenre();
    if (open) mu("genre-name").focus();
  }

  // 自作のジャンルの一覧を読む(動いていなければそのまま)。選んでいた自作のジャンルが別の画面で消されていたら、J-POP に戻す
  async function loadGenres() {
    try {
      custom = (await api("/api/music/genres")).filter((g) => !g.builtin);
    } catch (e) { return; }        // 管理役がまだ起きていない
    if (genreMode === "chip" && !genreNames().includes(genre)) genre = "J-POP";
    renderGenres();
  }

  // 名前(と説明)から、AI でジャンルの定義を作って保存する(CPU では 10〜60 秒かかる)。作れたら、そのチップを選んで中身を見せる。作れたら true
  async function createGenre(name, description) {
    if (making || gbusy) return false;
    genreSay("");
    gbusy = making = true;
    mu("genre-busy").hidden = false;
    updateButtons();
    try {
      const js = await api("/api/music/genres", "POST", { name, description });
      custom.push(js);
      genre = js.name;
      genreMode = "chip";
      mu("genre-add").hidden = true;
      renderGenres();
      mu("genre-edit").open = true;
      genreSay(`「${js.name}」を作りました。中身は直せます`);
      return true;
    } catch (e) {
      genreSay(`ジャンルを作れませんでした: ${e.message || e}`, true);
      return false;
    } finally {
      gbusy = making = false;
      mu("genre-busy").hidden = true;
      updateButtons();
    }
  }

  // 日本語の入力の変換を確定する Enter では、送らない
  const onEnter = (id, fn, esc) => mu(id).addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); fn(); }
    else if (e.key === "Escape" && esc) esc();
  });

  // ＋ 追加: 名前と、任意の一言の説明
  const addGenre = async () => {
    const name = val("genre-name");
    if (!name) { genreSay("ジャンルの名前を入れてください", true); mu("genre-name").focus(); return; }
    if (await createGenre(name, val("genre-desc"))) { mu("genre-name").value = ""; mu("genre-desc").value = ""; }
  };
  mu("genre-add-go").onclick = addGenre;
  mu("genre-add-cancel").onclick = () => toggleAdd(false);
  for (const id of ["genre-name", "genre-desc"]) onEnter(id, addGenre, () => toggleAdd(false));

  // ✎ 自由に書く: その文を「ジャンルとして保存」すると、短い名前を付けた自作のチップになる(文は説明になる)
  mu("free-save").onclick = () => {
    if (!val("free")) { genreSay("ジャンルとして保存する文を、先に書いてください", true); mu("free").focus(); return; }
    mu("free-name-row").hidden = false;
    mu("free-name").focus();
  };
  const saveFree = async () => {
    const name = val("free-name");
    if (!name) { genreSay("ジャンルの名前を入れてください", true); mu("free-name").focus(); return; }
    if (await createGenre(name, val("free").replace(/\s+/g, " "))) { mu("free-name-row").hidden = true; mu("free-name").value = ""; }
  };
  mu("free-name-go").onclick = saveFree;
  mu("free-name-cancel").onclick = () => { mu("free-name-row").hidden = true; };
  onEnter("free-name", saveFree, () => { mu("free-name-row").hidden = true; });

  // ジャンルの中身: 選んでいる自作のジャンルを入れる(同じ中身なら、書きかけの編集を消さない)。名前・世界観・場面・感情・雰囲気を直して保存、または削除
  for (const [v, label] of VIZ_THEMES) mu("gedit-viz").append(new Option(`${label} (${v})`, v));
  function fillEdit() {
    const g = selectedCustom();
    mu("genre-edit").hidden = !g;
    if (!g) { editFor = ""; return; }
    const stamp = JSON.stringify([g.id, g.name, g.vibe, g.settings, g.emotions, g.viz]);
    if (stamp === editFor) return;
    editFor = stamp;
    mu("gedit-title").textContent = `✦ ${g.name}`;
    mu("gedit-name").value = g.name;
    mu("gedit-vibe").value = g.vibe;
    mu("gedit-settings").value = g.settings.join("\n");
    mu("gedit-emotions").value = g.emotions.join("\n");
    mu("gedit-viz").value = g.viz;
  }

  // 通信中はボタンを止めて(gbusy)、失敗は欄の下に出す。成功なら true
  async function genreCall(fn) {
    gbusy = true;
    genreSay("");
    updateButtons();
    try {
      await fn();
      return true;
    } catch (e) {
      genreSay(e.message || String(e), true);
      return false;
    } finally {
      gbusy = false;
      updateButtons();
    }
  }

  mu("gedit-save").onclick = () => {
    const g = selectedCustom();
    if (!g || making || gbusy) return;
    const lines = (id) => mu(id).value.split("\n").map((s) => s.trim()).filter(Boolean);
    genreCall(async () => {
      const js = await api(`/api/music/genres/${encodeURIComponent(g.id)}`, "PUT",
        { name: val("gedit-name"), vibe: val("gedit-vibe"), settings: lines("gedit-settings"), emotions: lines("gedit-emotions"), viz: mu("gedit-viz").value });
      custom = custom.map((x) => (x.id === js.id ? js : x));
      genre = js.name;
      renderGenres();
      genreSay(`「${js.name}」を保存しました。次の曲から使われます`);
    });
  };

  mu("gedit-del").onclick = () => {
    const g = selectedCustom();
    if (!g || making || gbusy || !confirm(`「${g.name}」を削除しますか?\n元に戻せません。`)) return;
    genreCall(async () => {
      await api(`/api/music/genres/${encodeURIComponent(g.id)}`, "DELETE");
      custom = custom.filter((x) => x.id !== g.id);
      genre = "J-POP";
      renderGenres();
      genreSay(`「${g.name}」を削除しました`);
    });
  };

  renderGenres();
  studio.on.tab.push((t) => { if (t === "music") loadGenres(); });      // タブを開くたびに読み直す(別の画面や API で変わったぶんも拾う)

  // ---- 詳細 ----

  mu("random").onchange = () => { mu("seed").disabled = mu("random").checked; };
  mu("lastseed").onclick = () => { mu("seed").value = lastSeed; mu("random").checked = false; mu("seed").disabled = false; };
  mu("neg").value = DEFAULT_NEGATIVE;

  // モデル名なし(「ACE の既定」)は、既定のモデル名が分かればそれで見る。分からなければ turbo とみなす
  const isTurbo = (m) => { const n = m || defaultModel; return !n || n.toLowerCase().includes("turbo"); };

  // モデルを選んだら STEP を合わせる(turbo 8、それ以外 50)。SFT は 4 曲を選べない(VRAM)
  function onModel() {
    const m = mu("model").value;
    mu("steps").value = isTurbo(m) ? "8" : "50";
    const sft = (m || defaultModel).toLowerCase().includes("sft");
    for (const o of mu("batch").options) o.disabled = sft && +o.value > 2;
    if (sft && +mu("batch").value > 2) mu("batch").value = "2";
  }
  mu("model").onchange = () => {
    onModel();
    try { localStorage.setItem(MODEL_KEY, mu("model").value); } catch (e) { /* 保存できなくても動作は同じ */ }
  };

  // ACE のモデル一覧(動いていなければ「ACE の既定」だけ)
  async function loadModels() {
    const sel = mu("model"), keep = sel.value, known = defaultModel;
    let js = { models: [] };
    try {
      const r = await fetch("/api/music/models");
      if (r.ok) js = await r.json();
    } catch (e) { /* 管理役がまだ起きていない */ }
    defaultModel = js.default_model || "";
    sel.replaceChildren(new Option("ACE の既定", ""));
    const sorted = [...(Array.isArray(js.models) ? js.models : [])].sort((a, b) => (b.is_loaded - a.is_loaded) || (isTurbo(b.name) - isTurbo(a.name)) || a.name.localeCompare(b.name));
    for (const m of sorted) {
      const o = new Option(`${m.name}${m.is_loaded ? "  ✓読込済" : "  (未読込。選ぶと読み込みます)"}${m.vram_ok ? "" : "  (VRAM 不足)"}`, m.name);
      o.disabled = !m.vram_ok;
      sel.append(o);
    }
    let saved = "";
    try { saved = localStorage.getItem(MODEL_KEY) || ""; } catch (e) { /* 無ければ既定 */ }
    const ok = (v) => v && [...sel.options].some((o) => o.value === v && !o.disabled);
    sel.value = [keep, saved, js.default_model].find(ok) || "";      // いま選んでいるもの → 前回のもの → ACE の既定
    mu("model-note").textContent = js.note ? `${js.note}(既定のモデルで作ります)` : "選んだモデルが読み込まれていなければ、生成の前に自動で読み込みます(数十秒)";
    if (sel.value !== keep || (!sel.value && defaultModel !== known)) onModel();      // 「ACE の既定」のままでも、既定のモデルが分かった(変わった)なら STEP を合わせ直す
  }
  mu("adv").addEventListener("toggle", () => { if (mu("adv").open) loadModels(); });

  const advanced = () => ({
    model: mu("model").value || undefined, steps: +mu("steps").value, thinking: mu("thinking").checked, batch_size: +mu("batch").value,
    seed: mu("random").checked || mu("seed").value === "" ? undefined : +mu("seed").value,
    negative_prompt: val("neg"), shift: mu("shift").value, infer_method: mu("infer").value,
  });

  // ---- 曲のプラン(生成中に、AI が作ったテーマ・キャプション・歌詞を見せる) ----

  function showPlan(job) {
    const plan = job.result && job.result.plan;
    if (!plan) return;
    mu("plan").hidden = false;
    mu("plan-head").textContent = job.kind === "ace.omakase" ? "おまかせ" : "曲のプラン";
    mu("plan-theme").textContent = plan.theme;
    mu("plan-cap").textContent = plan.caption;
    const lines = (plan.lyrics || "").split("\n").filter((l) => l.trim());
    mu("plan-lyrics").textContent = !lines.length || plan.lyrics === "[Instrumental]" ? (lines.length ? "(インスト)" : "")
      : lines.slice(0, PLAN_LINES).join("\n") + (lines.length > PLAN_LINES ? "\n…" : "");
    if (fillPlan && job.kind === "ace.simple") {     // 作ったものを欄に残す(次は、それを使う / 歌詞だけ新しくする、が選べる)
      fillPlan = false;
      if (!val("theme")) mu("theme").value = plan.theme;
      if (!val("lyrics") && !inst() && plan.lyrics) mu("lyrics").value = plan.lyrics;
      if (!captionEdited && plan.caption) mu("caption").value = plan.caption;
      render();
    }
  }

  function onJob(job) {
    showPlan(job);
    if (["completed", "failed", "cancelled"].includes(job.status)) {
      if (job.status === "completed") rememberSeed(job);
      loadModels();        // 読み込み済みのモデルが変わったかもしれない
    }
  }

  function rememberSeed(job) {
    const seed = job.result && job.result.seed;
    if (seed === undefined || seed === null) return;
    lastSeed = seed;
    mu("lastseed").disabled = false;
  }

  // ---- 生成 ----

  function setBusy(b) {
    busy = b;
    mu("go").disabled = mu("regen").disabled = b;
    mu("stop").disabled = !b;
    updateButtons();
    render();
  }

  function generate() {
    showMsg("");
    const g = mode === "omakase" ? "" : currentGenre();       // 自由に書く欄が空なら、案内を出して始めない
    if (g === null) return;
    mu("plan").hidden = true;
    if (mode === "omakase") {
      if (!val("query")) { showMsg("どんな曲か、一言入力してください"); mu("query").focus(); return; }
      tracker.submit("ace.omakase", { query: val("query"), language: mu("lang").value, instrumental: inst(), duration: +mu("dur").value, ...advanced() });
      return;
    }
    fillPlan = true;
    tracker.submit("ace.simple", {
      genre: g, viz: genreViz(), theme: val("theme"), lyrics: inst() ? "" : val("lyrics"), caption: userCaption(),
      caption_auto: mu("caption-auto").checked, language: mu("lang").value, mood, vocal: inst() ? "" : vocal,
      instrumental: inst(), duration: +mu("dur").value, ...advanced(),
    });
  }

  // 歌詞を消して、テーマはそのままで(歌詞とキャプションを作り直して)生成する
  function regenerate() {
    if (currentGenre() === null) return;          // 歌詞を消す前に、ジャンルを確かめる
    mu("lyrics").value = "";
    mu("lyrics-note").textContent = "";
    if (!captionEdited) mu("caption").value = "";
    render();
    generate();
  }

  // ---- プリセット(入力欄の値と、作り方・ジャンル(チップか自由に書いた文)・ムード・ボーカルの選び。生成中のプランは含めない) ----

  function getPresetValues() {
    return {
      mode, genre_mode: genreMode, genre: genreMode === "free" ? val("free") : genre, language: mu("lang").value, duration: +mu("dur").value, instrumental: inst(),
      query: val("query"), theme: val("theme"), lyrics: val("lyrics"), caption: captionEdited ? val("caption") : "",      // AI が入れたキャプションは、生成で作り直すので入れない
      caption_auto: mu("caption-auto").checked, mood, vocal,
      model: mu("model").value, steps: +mu("steps").value, batch_size: +mu("batch").value, thinking: mu("thinking").checked,
      seed: presetGetSeed(mu("seed"), mu("random")), negative_prompt: val("neg"), shift: mu("shift").value, infer_method: mu("infer").value,
    };
  }

  // 書かれている項目だけ反映する(知らない項目は無視)。文字の欄(一言・テーマ・歌詞)は空でなければ置き換える(キャプションだけは下の通り)。
  // 選択肢に無い値(読み込まれていないモデルなど)は入れない。モデルを先に選び(STEP が合う)、そのあと STEP と曲数をプリセットの値にする
  function applyPresetValues(v) {
    const skipped = [];
    const text = (key, id) => { if (typeof v[key] === "string" && v[key].trim()) presetSet(mu(id), v[key]); };
    const pick = (key, id, label) => { if (key in v && !presetSet(mu(id), v[key])) skipped.push(label); };
    if (v.mode === "simple" || v.mode === "omakase") setMode(v.mode);
    if (typeof v.genre === "string") {        // チップの名前(同梱か自作)か、自由に書いた文。チップに無い名前(消した自作のジャンルなど。古いプリセットも)は、自由に書く扱い
      const g = v.genre.trim();
      if (v.genre_mode === "free" || (g && !genreNames().includes(g))) { presetSet(mu("free"), g); genreMode = "free"; genreSay(""); markGenre(); }
      else if (g) { genre = g; genreMode = "chip"; genreSay(""); markGenre(); }
    }
    if (v.mood === "" || MOODS.some(([k]) => k === v.mood)) { mood = v.mood; markMood(); }
    if (v.vocal === "" || VOCALS.some(([k]) => k === v.vocal)) { vocal = v.vocal; markVocal(); }
    pick("language", "lang", "歌の言語");
    pick("duration", "dur", "曲長");
    if ("instrumental" in v) presetSet(mu("inst"), v.instrumental);
    text("query", "query");
    text("theme", "theme");
    if (typeof v.lyrics === "string" && v.lyrics.trim()) { presetSet(mu("lyrics"), v.lyrics); mu("lyrics-note").textContent = ""; }
    if (typeof v.caption === "string") {      // キャプションは音の「型」なので、空でも反映する(前のプリセットのキャプションを引きずらない)
      presetSet(mu("caption"), v.caption);
      captionEdited = !!v.caption.trim();      // 書いてあれば「自分で書いた」扱いで生成にも使う。空なら、生成のときに AI が作る
    }
    if ("caption_auto" in v) presetSet(mu("caption-auto"), v.caption_auto);
    pick("model", "model", "モデル");
    pick("steps", "steps", "STEP");
    pick("batch_size", "batch", "曲数");
    if ("thinking" in v) presetSet(mu("thinking"), v.thinking);
    if ("seed" in v) presetSetSeed(mu("seed"), mu("random"), v.seed);
    if (typeof v.negative_prompt === "string") presetSet(mu("neg"), v.negative_prompt);
    pick("shift", "shift", "Shift");
    pick("infer_method", "infer", "Sampler");
    updateButtons();
    return skipped;
  }

  const tracker = createTracker("music", { setBusy, showMsg, onJob });
  wirePresets({ tab: "music", box: mu("presets"), get: getPresetValues, apply: applyPresetValues });
  mu("go").onclick = generate;
  mu("regen").onclick = regenerate;
  mu("stop").onclick = () => { mu("stop").disabled = true; tracker.stop(); };
  tracker.resume();
  render();
  // 前回のシード: 直近に完了した曲から拾う(ページを開き直しても「同じ骨格で作り直す」が使える)
  fetch("/api/jobs?tab=music&limit=20").then((r) => r.json()).then((jobs) => {
    const done = jobs.find((j) => j.status === "completed" && j.result && j.result.seed != null);
    if (done) rememberSeed(done);
  }).catch(() => {});
  loadModels();
  loadGenres();
})();
