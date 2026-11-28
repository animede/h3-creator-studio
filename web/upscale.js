// 拡大(AI アップスケール)の共通部分: 道具の様子(GET /api/upscale/tools)・方式の導入(POST /api/upscale/tools/install)・選び(方式と倍率)の照らし合わせ・道具の欄。
// 編集タブの書き出し(edit.js)と、生成物ビューアの「拡大」(gallery.js)が使う。方式の名前・使えない理由は、サーバの答えのまま出す。
// 選んだ方式・倍率は、両方で同じ localStorage の edit-upscale({method, scale})に覚える(使えなくても動く)。
const UPSCALE = (() => {
  const KEY = "edit-upscale";
  const POLL_MS = 2000;                 // 導入の間は、この間隔で道具の様子を読み直す
  const subs = [];                      // 道具の様子が変わったときに呼ぶもの(onChange)
  let timer = null, inflight = null;
  const api = (...a) => apiJson(...a);  // JSON の API(web/project.js。呼ぶのは読み込みが済んだあと)
  const errText = (e) => String((e && e.message) || e);

  const U = {
    tools: null,                        // GET /api/upscale/tools の答え(読めていなければ null)
    error: "",                          // 読めなかった理由
    installErr: "",                     // 導入を断られた理由(400 / 409 など。次に入れるまで出す)
    pref: { method: "auto", scale: 1 },  // 選んでいる方式・倍率(編集タブの 1 は元のまま。ビューアは 2 未満なら 2)
  };
  try { const s = JSON.parse(localStorage.getItem(KEY) || "null"); if (s && typeof s === "object") U.pref = { method: String(s.method || "auto"), scale: Number(s.scale) || 1 }; } catch (e) { /* 保存できなくても動く */ }
  U.remember = () => { try { localStorage.setItem(KEY, JSON.stringify(U.pref)); } catch (e) { /* 保存できなくても動く */ } };

  U.method = (id) => ((U.tools && U.tools.methods) || []).find((m) => m.id === id) || null;
  // 方式の名前(画面の言語で)。方式のレコードでも ID でもよい(道具の様子が読めていなければ ID のまま)
  U.label = (x) => {
    const m = x && typeof x === "object" ? x : U.method(x);
    return m ? (LANG === "en" && m.label_en) || m.label || m.id : String(x || "");
  };
  U.onChange = (fn) => { subs.push(fn); return () => { const i = subs.indexOf(fn); if (i >= 0) subs.splice(i, 1); }; };
  const notify = () => subs.slice().forEach((fn) => { try { fn(U); } catch (e) { console.error(e); } });

  // 選び(方式 method・倍率 scale)を、道具の答え tools と照らす(DOM に触れない)。kind は拡大するものの種類(動画・画像だけ。省けば動画と同じ)。
  // auto は tools.auto_method に解決する(使える方式が無ければ null)。その方式で選べない倍率は 1(元のまま)に戻す。
  // 戻り: { method: 使う方式の ID か null、scale、scales: その方式で選べる倍率、size: 拡大後の [幅, 高さ](元の寸法が無ければ null)、
  //        tooBig: 長辺が max_edge を超える、usable: いま使えるか、hint: 使えない理由(サーバの文) }
  U.planFor = (tools, method, scale, w, h, kind) => {
    const methods = tools && Array.isArray(tools.methods) && (!kind || kind === "video" || kind === "image") ? tools.methods : [];      // 曲などは拡大しない
    const id = method === "auto" ? (tools && tools.auto_method) || null : method;
    const m = methods.find((x) => x.id === id) || null;
    const scales = m ? (m.scales || []).map(Number).filter((x) => x > 1) : [];
    const s = scales.includes(Number(scale)) ? Number(scale) : 1;
    const size = w > 0 && h > 0 ? [Math.round(w * s), Math.round(h * s)] : null;
    const maxEdge = Number((tools && tools.max_edge) || 0);
    return { method: m ? m.id : null, scale: s, scales, size, tooBig: !!(size && maxEdge && s > 1 && Math.max(size[0], size[1]) > maxEdge),
      usable: !!(m && m.usable), hint: (m && m.hint) || "" };
  };
  U.plan = (method, scale, w, h, kind) => U.planFor(U.tools, method, scale, w, h, kind);

  // 道具の様子を読む。読んでいる最中に呼ばれたら、その読みを待つ(重ねて読まない)。導入の間は 2 秒おきに読み直す
  U.load = () => {
    if (inflight) return inflight;
    clearTimeout(timer);
    inflight = (async () => {
      let tools = null, err = "";
      try { tools = await api("/api/upscale/tools"); } catch (e) { err = errText(e); }
      inflight = null;
      U.tools = tools && Array.isArray(tools.methods) ? tools : null;
      U.error = U.tools ? "" : t("拡大の方式を調べられませんでした: {err}", { err: err || t("答えの形が違います") });
      notify();
      if (U.tools && U.tools.installing) timer = setTimeout(U.load, POLL_MS);
    })();
    return inflight;
  };

  // 方式を入れる(setup と同じ導入を裏で始める)。RTX Video Super Resolution は NVIDIA の利用条件への同意を先に聞く(accept_license)
  U.install = async (x) => {
    const m = x && typeof x === "object" ? x : U.method(x) || { id: String(x || "") };
    if (!m.id) return;
    if (m.id === "rtxvsr" && !confirm(t("RTX Video Super Resolution は NVIDIA の独自ライセンスのソフトウェアです。入れると NVIDIA の利用条件に同意したことになります。\n{license_url}\n\n続けますか?", { license_url: m.license_url || "" }))) return;
    U.installErr = "";
    clearTimeout(timer);
    try {
      const r = await api("/api/upscale/tools/install", "POST", { method: m.id, accept_license: m.id === "rtxvsr" });
      if (r && Array.isArray(r.methods)) U.tools = r;
    } catch (e) { U.installErr = errText(e); }        // 400 / 409(別の導入が走っているなど)の理由は、道具の欄に出す
    notify();
    timer = setTimeout(U.load, U.tools && U.tools.installing ? POLL_MS : 0);
  };

  // 方式の選択肢(おすすめ(自動) + 方式)。道具の一覧が変わったときだけ作り直す(開いている選択肢を閉じない)。選んでいる値は呼ぶ側が入れる
  U.fillMethods = (sel) => {
    const methods = U.tools ? U.tools.methods : [];
    const sig = JSON.stringify(methods.map((m) => [m.id, m.label, m.label_en]));
    if (sel.dataset.upSig === sig) return;
    sel.dataset.upSig = sig;
    sel.replaceChildren();
    for (const [v, label] of [["auto", t("おすすめ(自動)")], ...methods.map((m) => [m.id, U.label(m)])]) { const o = el("option", "", label); o.value = v; sel.append(o); }
  };

  // 道具の欄(box。.msg): 入っていない方式ごとに「{名前}: {理由}」と「入れる」(入れられるものだけ)。導入中はその進み、導入の失敗の理由も。戻りは行の数
  U.renderTools = (box) => {
    const tools = U.tools, busy = tools && tools.installing;
    let rows = 0;
    box.replaceChildren();
    for (const m of (tools ? tools.methods : []).filter((x) => !x.installed)) {
      const row = el("div", "uptoolrow"), inst = busy === m.id;
      row.append(el("span", "", `${U.label(m)}: ${inst ? t("入れています…") + (tools.progress_text ? " " + tools.progress_text : "") : m.hint || t("入っていません")}`));
      if (!inst && m.installable !== false) {
        const b = el("button", "mini", t("入れる"));
        b.type = "button";
        b.disabled = !!busy;
        b.onclick = () => U.install(m);
        row.append(b);
      }
      box.append(row);
      rows++;
    }
    const err = U.installErr || (tools && tools.error) || "";
    if (err) { box.append(el("div", "uptoolrow", err)); rows++; }
    box.hidden = !rows;
    return rows;
  };

  return U;
})();
