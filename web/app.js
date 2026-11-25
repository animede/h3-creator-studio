// 管理役の画面: メモリ計・エンジンの状態表示(折りたたみ可)。タブ・生成物・ギャラリーは gallery.js / image.js。
const STATE_LABEL = { stopped: t("停止"), starting: t("起動中"), ready: t("準備完了"), unloaded: t("退避中"), stopping: t("停止中"), error: t("エラー") };
const LANE_LABEL = { h3: "H3", h3image: "H3-Image", ace: "ACE", cpu: "CPU" };      // レーン(エンジン)の短い名前
const rows = {};          // エンジン名 -> {el, parts}
const openLogs = new Set();
let timer = null;
let partsCollapsed = false;       // エンジンパネルの折りたたみ(利用者が触るまでは自動)
let partsTouched = false;
let lastStatus = null;
let doneCounter = null;           // 終わったジョブの数。変わったら、自分が追っていないジョブ(API や別のブラウザ)も含めて一覧を読み直す

const $ = (id) => document.getElementById(id);
const gib = (mib) => (mib / 1024).toFixed(1);

function setGauge(id, used, total, usedText) {
  const g = $(id);
  const pct = total ? Math.min(100, (used / total) * 100) : 0;
  const bar = g.querySelector(".bar i");
  bar.style.width = pct + "%";
  bar.classList.toggle("hi", pct >= 90);
  g.querySelector(".g-num").textContent = usedText;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

async function post(url) {
  const r = await fetch(url, { method: "POST", headers: langHeaders() });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

function buildRow(name, c) {
  const row = el("div", "row");
  const parts = {
    label: el("span", "label", c.label), badge: el("span", "badge"), tag: el("span", "tag", "GPU"),
    ext: el("span", "tag ext", t("外部")), port: el("span", "meta"), rss: el("span", "meta"),
    btns: el("span", "btns"), log: el("pre", "log"),
  };
  parts.log.hidden = true;
  const mk = (text, fn) => { const b = el("button", "", text); b.onclick = fn; parts.btns.append(b); return b; };
  parts.bStart = mk(t("起動"), () => act(name, "start"));
  parts.bStop = mk(LANG === "en" ? "Stop" : "停止", () => act(name, "stop"));      // i18n-skip 状態の「停止」(Stopped)と同じ鍵になるので、ボタンだけ別に
  parts.bEnsure = mk(t("準備"), () => act(name, "ensure"));
  mk(t("ログ"), async () => {
    if (openLogs.has(name)) { openLogs.delete(name); parts.log.hidden = true; }
    else { openLogs.add(name); parts.log.hidden = false; await refreshLog(name); }
  });
  row.append(parts.label, parts.badge, parts.tag, parts.ext, parts.port, parts.rss, parts.btns, parts.log);
  $("rows").append(row);
  rows[name] = parts;
}

async function act(name, action) {
  try { await post(`/api/components/${name}/${action}`); } catch (e) { showOp({ steps: [], error: String(e.message || e) }); }
  poll();
}

async function refreshLog(name) {
  const r = await fetch(`/api/components/${name}/log?lines=100`, { headers: langHeaders() }).then((x) => x.json());
  const pre = rows[name].log;
  const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
  pre.textContent = r.log || t("(ログはまだありません)");
  if (atEnd) pre.scrollTop = pre.scrollHeight;
}

function showOp(op) {
  const line = $("opline");
  line.replaceChildren();
  line.classList.toggle("err", !!(op && op.error));
  if (!op) return;
  const last = op.steps.length ? op.steps[op.steps.length - 1].msg : "";
  line.append(document.createTextNode(op.error ? t("失敗: {error}", { error: op.error }) : last));
  if (op.error && op.log_tail) line.append(el("pre", "", op.log_tail));
}

// 状態バーの 2 行目: レーンごとの実行中のジョブ(種類と進み)と順番待ちの数。share のときは、GPU に載っているエンジンと利用枠も出す
function renderLanes(s) {
  const share = s.gpu_mode === "share";
  const items = [el("span", "lane", share ? t("GPU: 共有(複数のエンジンを同時に載せる)") : t("GPU: エンジン 1 つずつ"))];
  for (const [name, l] of Object.entries(s.lanes || {})) {
    const jobs = l.running_jobs || [];
    if (!jobs.length && !l.queued) continue;
    const parts = jobs.map((j) => j.kind.split(".").pop() + " " + (j.status === "running" ? Math.round((j.progress || 0) * 100) + "%" : t("準備中")));
    if (l.queued) parts.push(t("待ち {n}", { n: l.queued }));
    items.push(el("span", "lane run", `${LANE_LABEL[name] || name}: ${parts.join(" / ")}`));
  }
  if (share) for (const r of s.residents || []) items.push(el("span", "lane res" + (r.busy ? " busy" : ""), (r.busy ? t("{name} 利用枠 {gib} GiB(仕事中)", { name: LANE_LABEL[r.name] || r.name, gib: gib(r.budget_mib) }) : t("{name} 利用枠 {gib} GiB", { name: LANE_LABEL[r.name] || r.name, gib: gib(r.budget_mib) }))));
  $("lanes").replaceChildren(...items);
}

function render(s) {
  const gpu = s.gpu;
  if (gpu) setGauge("g-vram", gpu.used_mib, gpu.total_mib, `${gib(gpu.used_mib)} / ${gib(gpu.total_mib)} GiB`);
  else setGauge("g-vram", 0, 0, t("GPU なし"));
  $("g-shared").hidden = s.shared_gpu_mib == null;
  if (s.shared_gpu_mib != null) {      // Windows の共有 GPU メモリ。上限はサーバが知っていればそれ(ふつう CPU メモリの半分)、無ければ 4GiB で目安にする
    const total = s.shared_gpu_total_mib || 4096;
    setGauge("g-shared", s.shared_gpu_mib, total, `${gib(s.shared_gpu_mib)} / ${gib(total)} GiB`);
  }
  if (s.ram) setGauge("g-ram", s.ram.used_mib, s.ram.total_mib, `${gib(s.ram.used_mib)} / ${gib(s.ram.total_mib)} GiB`);
  const w = $("warning");
  w.hidden = !s.warning;
  w.textContent = s.warning || "";
  renderLanes(s);

  for (const [name, c] of Object.entries(s.components)) {
    if (!rows[name]) buildRow(name, c);
    const r = rows[name];
    r.badge.textContent = STATE_LABEL[c.state] || c.state;
    r.badge.className = "badge " + c.state;
    r.tag.hidden = !c.gpu_resident;
    r.tag.textContent = c.budget_mib ? `GPU ${gib(c.budget_mib)} GiB` : "GPU";      // share のときは利用枠つき
    r.ext.hidden = !c.external;
    r.port.textContent = `:${c.port}`;
    r.rss.textContent = c.rss_mib != null ? `RSS ${gib(c.rss_mib)} GiB` : "";
    r.rss.title = c.error || "";
    const running = ["starting", "ready", "unloaded"].includes(c.state);
    r.bStart.disabled = s.busy || running;
    r.bStop.disabled = s.busy || c.state === "stopped" || c.external || c.busy;       // 仕事中のエンジンは止めない
    r.bEnsure.disabled = s.busy;
    if (openLogs.has(name)) refreshLog(name);
  }
  showOp(s.operation);
  updateParts(s);
}

// エンジンパネル: 準備が整ったら自動で畳み、見出しに 1 行で状況を出す(利用者が開閉したら以後は自動にしない)
function summarize(s) {
  const comps = Object.values(s.components);
  const res = comps.filter((c) => c.gpu_resident);       // share では複数載っている
  const op = s.operation && !s.operation.done ? s.operation : null;
  let text = res.length ? `GPU: ${res.map((c) => `${c.label}(${STATE_LABEL[c.state] || c.state})`).join(t("、"))}` : t("GPU: 空き");
  const starting = comps.find((c) => c.state === "starting" || c.state === "stopping");
  if (op && op.steps.length) text += " - " + op.steps[op.steps.length - 1].msg;
  else if (starting) text += ` - ${starting.label}: ${STATE_LABEL[starting.state]}`;
  return { text, error: !!(s.operation && s.operation.error) || comps.some((c) => c.state === "error") };
}

function updateParts(s) {
  lastStatus = s;
  const sum = summarize(s);
  $("parts-summary").textContent = sum.text;
  $("parts-summary").classList.toggle("err", sum.error);
  if (!partsTouched) partsCollapsed = !sum.error && Object.values(s.components).some((c) => c.state === "ready");
  $("parts-body").hidden = partsCollapsed;
  $("parts-head").setAttribute("aria-expanded", String(!partsCollapsed));
  $("parts-head").querySelector(".caret").textContent = partsCollapsed ? "▸" : "▾";
}

function toggleParts() { partsTouched = true; partsCollapsed = !partsCollapsed; if (lastStatus) updateParts(lastStatus); }
$("parts-head").addEventListener("click", toggleParts);
$("parts-head").addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleParts(); } });

async function poll() {
  clearTimeout(timer);
  let busy = false;
  try {
    const s = await fetch("/api/status", { headers: langHeaders() }).then((r) => r.json());
    render(s);
    busy = s.busy || Object.values(s.lanes || {}).some((l) => (l.running_jobs || []).length || l.queued);     // 仕事が走っている間は 1 秒おき
    if (s.jobs_done_counter != null) {
      if (doneCounter !== null && s.jobs_done_counter !== doneCounter) loadGallery();   // 履歴・ビューアも更新される
      doneCounter = s.jobs_done_counter;
    }
  } catch (e) {
    $("warning").hidden = false;
    $("warning").textContent = t("管理役に接続できません");
  }
  timer = setTimeout(poll, busy ? 1000 : 2000);
}

document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));   // setTab は gallery.js

poll();
