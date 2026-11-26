// プリセットの欄(画像・動画・音楽・MV・ショート動画タブ共通): 入力欄の値に名前を付けて保存し、選ぶと入力欄へ反映する。
// 同梱のプリセット(presets/*.json)は変更できず(複製して編集する)、自分のプリセットは台帳(/api/presets)に入る。
// 反映は入力欄を書き換えるだけで、生成はしない。参照画像・曲・フレームなどの生成物はプリセットに含めない。
// 各タブは wirePresets に、get()(いまの入力欄の値の辞書)と apply(values)(入力欄への反映。反映できなかった項目名の配列を返す)を渡す。

// 入力欄 1 つへ値を入れ、画面の処理(サイズの表示・欄の出し入れ・STEP の連動など)が追従するよう input / change を流す。
// 選べない値(選択肢に無い・無効)は入れずに false を返す。文字の欄は maxlength で切る
function presetSet(node, value) {
  if (node.type === "checkbox") {
    node.checked = !!value;
  } else if (node.tagName === "SELECT") {
    const opt = [...node.options].find((o) => o.value === String(value) && !o.disabled);
    if (!opt) return false;
    node.value = opt.value;
  } else {
    const text = value == null ? "" : String(value);
    node.value = node.maxLength > 0 ? text.slice(0, node.maxLength) : text;
  }
  node.dispatchEvent(new Event("input", { bubbles: true }));
  node.dispatchEvent(new Event("change", { bubbles: true }));
  return true;
}

// シード: 保存は、ランダム(か空)なら null、固定なら数。反映は、入力欄とランダムの印を合わせる(欄の無効化は各タブの change の処理)
function presetGetSeed(seed, random) {
  return random.checked || seed.value === "" ? null : parseInt(seed.value, 10);
}
function presetSetSeed(seed, random, value) {
  const fixed = Number.isInteger(value) && value >= 0;
  if (fixed) presetSet(seed, value);
  presetSet(random, !fixed);
}

// 画風の選択(画像・動画・MV・ショート動画タブ共通)。プロンプトには入れず、送る params の style になる。
// なし以外を選んだら、プロンプト・物語の欄の下に「画風: アニメ(生成時に指示を足します)」と出す
function wireStyleNote(sel, note) {
  const render = () => {
    const opt = sel.options[sel.selectedIndex];
    note.hidden = !opt || sel.value === "none";
    note.textContent = note.hidden ? "" : t("画風: {name}(生成時に指示を足します)", { name: opt.textContent.trim() });
  };
  sel.addEventListener("change", render);
  render();
}

// アングルの選択(画像タブの edit だけ)。画風と同じく、プロンプトには入れず params の angle になる。
// active() が false(t2i)のあいだは注記を出さない。返す関数は、作り方を切り替えたときに呼び直す
function wireAngleNote(sel, note, active = () => true) {
  const render = () => {
    const opt = sel.options[sel.selectedIndex];
    note.hidden = !opt || sel.value === "none" || !active();
    note.textContent = note.hidden ? "" : t("アングル: {name}(生成時に指示を足します)", { name: opt.textContent.trim() });
  };
  sel.addEventListener("change", render);
  render();
  return render;
}

// box にプリセットの欄を組み立てる。tab は image / video / music / mv / short
function wirePresets({ tab, box, get, apply }) {
  let list = [];                  // このタブのプリセット(同梱が先、自分のものが後)
  let selected = "";              // 選んでいるプリセットの ID
  let busy = false;
  let onName = null;              // 名前の欄で OK(Enter)したときの処理(新しい名前での保存 / 名前の変更)
  const current = () => list.find((p) => p.id === selected) || null;
  const pname = (p) => (p.builtin ? t(p.name) : p.name);      // 表示用の名前(同梱のものは訳す。自分のものはそのまま)

  async function api(url, method = "GET", body) {
    const r = await fetch(url, { method, headers: langHeaders(body !== undefined ? { "Content-Type": "application/json" } : undefined),
      body: body !== undefined ? JSON.stringify(body) : undefined });
    const js = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof js.detail === "string" ? js.detail : r.status === 422 ? t("入力を確認してください") : t("通信に失敗しました({status})", { status: r.status }));
    return js;
  }

  box.setAttribute("role", "group");
  box.setAttribute("aria-label", t("プリセット"));
  const select = el("select");
  select.setAttribute("aria-label", t("プリセットを選ぶ"));
  const mk = (text, title, fn) => {
    const b = el("button", "mini", text);
    b.type = "button";
    b.title = title;
    b.onclick = fn;
    return b;
  };
  const btn = {
    save: mk(t("保存"), t("自分のプリセットを選んでいれば、いまの入力で上書きします。そうでなければ、新しい名前で保存します"), () => save()),
    saveAs: mk(t("別名で保存"), t("いまの入力を、新しい名前のプリセットとして保存します"), () => { const p = current(); saveAs(p ? t("{name} のコピー", { name: pname(p) }) : ""); }),
    rename: mk(t("名前の変更"), t("選んでいる自分のプリセットの名前を変えます"), () => rename()),
    del: mk(t("削除"), t("選んでいる自分のプリセットを削除します"), () => remove()),
    export: mk(t("書き出し"), t("自分のプリセットを JSON のファイルに書き出します"), () => exportFile()),
    import: mk(t("読み込み"), t("書き出した JSON のファイルから、プリセットを読み込みます"), () => file.click()),
  };
  const btns = el("div", "preset-btns");
  btns.append(...Object.values(btn));
  const nameIn = el("input");
  nameIn.type = "text";
  nameIn.maxLength = 60;
  nameIn.placeholder = t("プリセットの名前");
  nameIn.setAttribute("aria-label", t("プリセットの名前"));
  const okBtn = mk("OK", t("この名前で確定します(Enter)"), () => submitName());
  const nameRow = el("div", "inline preset-name");
  nameRow.hidden = true;
  nameRow.append(nameIn, okBtn, mk(t("取消"), t("名前の入力をやめます(Esc)"), () => closeName()));
  const note = el("div", "preset-note");
  note.hidden = true;
  const file = el("input");
  file.type = "file";
  file.accept = ".json,application/json";
  file.hidden = true;
  box.append(select, btns, nameRow, note, file);

  function say(text, err = false) {
    note.hidden = !text;
    note.textContent = text || "";
    note.classList.toggle("err", err);
  }

  function updateButtons() {
    const p = current();
    select.disabled = btn.save.disabled = btn.saveAs.disabled = btn.export.disabled = btn.import.disabled = okBtn.disabled = busy;
    btn.rename.disabled = btn.del.disabled = busy || !p || p.builtin;
  }

  // 通信中はボタンを止める。失敗は欄の下に出し(名前の欄は開いたまま)、成功なら true
  async function run(fn) {
    busy = true;
    updateButtons();
    try {
      await fn();
      return true;
    } catch (e) {
      say(e.message || String(e), true);
      return false;
    } finally {
      busy = false;
      updateButtons();
    }
  }

  function renderSelect() {
    select.replaceChildren(new Option(t("プリセット…"), ""));
    for (const [label, items] of [[t("同梱"), list.filter((p) => p.builtin)], [t("自分のプリセット"), list.filter((p) => !p.builtin)]]) {
      if (!items.length) continue;
      const g = el("optgroup");
      g.label = label;
      g.append(...items.map((p) => new Option(pname(p), p.id)));
      select.append(g);
    }
    if (!current()) selected = "";
    select.value = selected;
    updateButtons();
  }

  async function load() {
    try {
      list = await api(`/api/presets?tab=${tab}`);
    } catch (e) {
      say(t("プリセットを読み込めません: {err}", { err: e.message || e }), true);
      return;
    }
    renderSelect();
  }

  // 選ぶと入力欄へ反映する(生成はしない)
  select.onchange = () => {
    selected = select.value;
    updateButtons();
    const p = current();
    if (!p) { say(""); return; }
    try {
      const skipped = apply(p.values) || [];
      say(t("「{name}」を入力欄に反映しました", { name: pname(p) }) + (skipped.length ? t("(選べない値だったので、そのままにした項目: {items})", { items: skipped.join(t("、")) }) : ""), skipped.length > 0);
    } catch (e) {
      say(t("「{name}」を反映できませんでした: {err}", { name: pname(p), err: e.message || e }), true);
    }
  };

  // ---- 名前の欄(window.prompt は使わず、欄の中に出す) ----

  function askName(initial, fn) {
    onName = fn;
    nameIn.value = initial;
    nameRow.hidden = false;
    nameIn.focus();
    nameIn.select();
  }
  function closeName() {
    onName = null;
    nameRow.hidden = true;
  }
  async function submitName() {
    const name = nameIn.value.trim();
    if (busy) return;
    if (!name) { say(t("名前を入れてください"), true); nameIn.focus(); return; }
    if (onName && await run(() => onName(name))) closeName();
  }
  nameIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submitName(); }
    else if (e.key === "Escape") closeName();
  });

  // ---- 保存・名前の変更・削除 ----

  function save() {
    const p = current();
    if (!p || p.builtin) { saveAs(p ? t("{name} のコピー", { name: pname(p) }) : ""); return; }      // 同梱のものは上書きできないので、別の名前で保存する
    if (!confirm(t("「{name}」を、いまの入力で上書きしますか?", { name: p.name }))) return;
    run(async () => {
      const saved = await api(`/api/presets/${encodeURIComponent(p.id)}`, "PUT", { values: get() });
      await load();
      say(t("「{name}」を上書き保存しました", { name: saved.name }));
    });
  }

  function saveAs(initial) {
    askName(initial, async (name) => {
      const created = await api("/api/presets", "POST", { tab, name, values: get() });
      selected = created.id;
      await load();
      say(t("「{name}」として保存しました", { name: created.name }) + (created.name !== name ? t("(同じ名前があったので、番号を付けました)") : ""));
    });
  }

  function rename() {
    const p = current();
    if (!p || p.builtin) return;
    askName(p.name, async (name) => {
      if (name === p.name) return;
      const saved = await api(`/api/presets/${encodeURIComponent(p.id)}`, "PUT", { name });
      await load();
      say(t("「{old}」を「{name}」に変えました", { old: p.name, name: saved.name }));
    });
  }

  function remove() {
    const p = current();
    if (!p || p.builtin || !confirm(t("「{name}」を削除しますか?\n元に戻せません。", { name: p.name }))) return;
    run(async () => {
      await api(`/api/presets/${encodeURIComponent(p.id)}`, "DELETE");
      selected = "";
      await load();
      say(t("「{name}」を削除しました", { name: p.name }));
    });
  }

  // ---- 書き出し・読み込み(JSON のファイル) ----

  function exportFile() {
    if (!list.some((p) => !p.builtin)) { say(t("書き出せる自分のプリセットがありません"), true); return; }
    const a = el("a");
    a.href = `/api/presets/export?tab=${tab}`;
    a.download = "";
    document.body.append(a);
    a.click();
    a.remove();
    say(t("自分のプリセットを JSON に書き出しました"));
  }

  file.onchange = () => {
    const f = file.files[0];
    file.value = "";                // 同じファイルをもう一度選べるように
    if (!f) return;
    run(async () => {
      let data;
      try { data = JSON.parse(await f.text()); } catch (e) { throw new Error(t("{file} は JSON として読めません", { file: f.name })); }
      const created = await api("/api/presets/import", "POST", data);
      await load();
      const mine = created.filter((p) => p.tab === tab).length;
      say(t("{n} 件のプリセットを読み込みました", { n: created.length }) + (mine < created.length ? t("(このタブ用は {mine} 件。残りは、ほかのタブで選べます)", { mine }) : ""));
    });
  };

  studio.on.tab.push((t) => { if (t === tab) load(); });      // タブを開くたびに読み直す(別の画面や API で変わったぶんも拾う)
  load();
}
