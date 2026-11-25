// 音楽の再生中に出すスペクトラム(ビジュアライザ)。Easy Music(MIT、同じ作者 animede)の static/app.js から移植した。
// 雰囲気テーマ 10 種と、描画 10 種(スペクトラム・波形・リング・粒子・脈動・オーロラ・花火・マトリックス・軌道・炎)は、元の式のまま移した。
// Easy Music 固有のもの(JUKEBOX・背景画像・オーバーレイ・多言語・LLM での雰囲気取得)は持ち込んでいない。
// 元は画面いっぱいの canvas だったので、横長で低いカードに合わせて、スペクトラム・軌道・花火・炎は、寸法を canvas の大きさから決める
// (マトリックスは、効いていなかった半透明の黒の重ねを省いた)。
//
//   attachVisualizer(audio, canvas, { viz, mood, genre, caption, mode })  -> { setMode(m), getMode(), destroy() }
//   vizBox(audio, { viz, mood, genre, caption })                         -> canvas と表示の選択を入れた枠(音楽のカードに入れる)
//   detachVisualizers(root)                                -> root の中の分を片付ける(root の中身を入れ替える前に呼ぶ)

const VIZ_MODES = [["spectrum", t("スペクトラム")], ["wave", t("波形")], ["ring", t("リング")], ["particles", t("粒子")], ["pulse", t("脈動")],
  ["aurora", t("オーロラ")], ["fireworks", t("花火")], ["matrix", t("マトリックス")], ["orbit", t("軌道")], ["fire", t("炎")], ["auto", t("おまかせ")]];
const VIZ_MODE_KEY = "studio.viz.mode";
const VIZ_DEFAULT_MODE = "spectrum";
const VIZ_FFT = 1024;           // AnalyserNode の fftSize(スペクトルと波形は、その半分の 512 点)

// 雰囲気テーマ 10 種: 色相の範囲・彩度・明度・強さ・光・粒子の速さ・向いた表示(おまかせは、この中から選ぶ)
const VIZ_MOOD_THEMES = {
  energetic:   { hueMin: 30,  hueMax: 60,  sat: 90, lit: 60, intensity: 1.3, glow: 1.4, pSpeed: 1.5, modes: ["spectrum", "particles", "fireworks"] },
  melancholic: { hueMin: 220, hueMax: 260, sat: 65, lit: 50, intensity: 0.7, glow: 1.0, pSpeed: 0.7, modes: ["wave", "pulse", "aurora"] },
  healing:     { hueMin: 140, hueMax: 200, sat: 55, lit: 55, intensity: 0.5, glow: 0.8, pSpeed: 0.5, modes: ["wave", "aurora", "pulse"] },
  epic:        { hueMin: 0,   hueMax: 30,  sat: 85, lit: 55, intensity: 1.2, glow: 1.5, pSpeed: 1.2, modes: ["ring", "spectrum", "orbit", "fire"] },
  cyber:       { hueMin: 170, hueMax: 310, sat: 90, lit: 60, intensity: 1.0, glow: 1.3, pSpeed: 1.1, modes: ["particles", "ring", "matrix"] },
  romantic:    { hueMin: 330, hueMax: 360, sat: 70, lit: 60, intensity: 0.8, glow: 1.1, pSpeed: 0.8, modes: ["pulse", "wave", "orbit"] },
  dark:        { hueMin: 270, hueMax: 300, sat: 60, lit: 35, intensity: 1.0, glow: 0.9, pSpeed: 1.0, modes: ["ring", "particles", "fire"] },
  festive:     { hueMin: 0,   hueMax: 360, sat: 85, lit: 60, intensity: 1.4, glow: 1.5, pSpeed: 1.6, modes: ["particles", "fireworks", "spectrum"] },
  nostalgic:   { hueMin: 20,  hueMax: 50,  sat: 50, lit: 50, intensity: 0.6, glow: 0.8, pSpeed: 0.6, modes: ["wave", "pulse", "aurora"] },
  mystical:    { hueMin: 180, hueMax: 260, sat: 70, lit: 50, intensity: 0.8, glow: 1.2, pSpeed: 0.8, modes: ["ring", "pulse", "orbit", "aurora"] },
};

// ムードのチップ(music.js の MOODS の値)とジャンル -> 雰囲気。LLM は呼ばない
const MOOD_TO_VIZ = {
  "bright and happy": "energetic", "bittersweet and sad": "melancholic", "intense and powerful": "epic",
  "calm and gentle": "healing", "mysterious and fantastical": "mystical", "romantic and tender": "romantic",
  "playful and fun": "festive", "melancholic and dark": "dark", "epic and grand": "epic", "cool and stylish": "cyber",
};
const GENRE_TO_VIZ = [
  [/ballad|enka|folk|kayokyoku|new music/i, "nostalgic"], [/rock|metal|punk/i, "energetic"], [/edm|techno|house|trance|phonk|vocaloid/i, "cyber"],
  [/jazz|lo-?fi|chill|ambient|bossa/i, "healing"], [/r&b|soul|city pop/i, "romantic"], [/anime|game|orchestr|cinematic/i, "epic"],
  [/hip ?hop|rap|reggaeton|latin/i, "festive"], [/j-?pop|k-?pop|pop|idol/i, "energetic"],
];

// キャプション(ACE-Step に渡す音の説明。英語が多いが、日本語もある)の言葉 -> 雰囲気。英語は語頭が合えば当たり(活用・派生も拾う)、日本語は含まれていれば当たり。
// 当たった語の数が最も多い雰囲気を選び、同数なら下の並びの先(具体的な雰囲気が先で、既定の energetic は最後)。1 つも当たらなければ、ジャンルで決める。
// 別の意味の語を拾わないよう、fun は funk を、grand は grand piano を、切な は「大切な」を除いてある。g 付きなので、数えるのは String.match で(test では lastIndex が残る)
const CAPTION_TO_VIZ = [
  ["melancholic", /\b(?:sad(?:ness|ly)?\b|melanchol|bittersweet|wistful|longing|lonel)|切な[いくさげ]|悲し|寂し|哀愁|孤独|せつな|かなし|さびし|メランコリ/gi],      // i18n-skip 雰囲気の判定に使う語
  ["healing", /\b(?:calm|gentl|sooth|relax|peace|lo-?fi|chill|ambient)|癒|穏や|安らぎ|静か|落ち着|優し|やさし|おだや|ヒーリング|リラックス/gi],      // i18n-skip 雰囲気の判定に使う語
  ["epic", /\b(?:epic|grand(?:iose|ly)?\b(?!\s*piano)|heroic|cinematic|orchestr|anthem|intense|dramatic|triumph)|壮大|荘厳|勇壮|エピック|シネマティック|オーケストラ/gi],      // i18n-skip 雰囲気の判定に使う語
  ["cyber", /\b(?:electro|synth|futuris|cyber|edm\b|techno\b|neon)|電子|サイバー|ネオン|近未来|テクノ|シンセ|エレクトロ/gi],      // i18n-skip 雰囲気の判定に使う語
  ["romantic", /\b(?:romanti|lov(?:e|ing)|tender|sensual)|恋|愛|ロマンチック|ラブ/gi],      // i18n-skip 雰囲気の判定に使う語
  ["dark", /\b(?:dark|heavy|gloom|ominous)|暗|闇|ダーク/gi],      // i18n-skip 雰囲気の判定に使う語
  ["festive", /\b(?:party|festiv|celebrat|danc|playful|fun\b)|祭|楽し|賑やか|たのし|パーティ|ダンス/gi],      // i18n-skip 雰囲気の判定に使う語
  ["nostalgic", /\b(?:nostalg|retro|vintage|(?:19)?[789]0'?s\b|traditional)|懐かし|レトロ|昭和|[789]0\s*年代|なつかし|ノスタルジ/gi],      // i18n-skip 雰囲気の判定に使う語
  ["mystical", /\b(?:dream|ethereal|mystic|mysteri|magic|fantas|otherworld)|幻想|神秘|夢|ファンタジー|ミステリアス/gi],      // i18n-skip 雰囲気の判定に使う語
  ["energetic", /\b(?:upbeat|energ|driving|bright|happy|uplifting|cheer|joy)|元気|明る|軽快|爽やか|げんき|あかる|ハッピー|アップテンポ/gi],      // i18n-skip 雰囲気の判定に使う語
];

// キャプションの言葉から雰囲気の名前。当たる語が無い(キャプションが無い・文字でない)ときは ""(次の手がかりへ)
function vizThemeFromCaption(caption) {
  if (typeof caption !== "string") return "";
  let best = "", most = 0;
  for (const [id, re] of CAPTION_TO_VIZ) {
    const n = (caption.match(re) || []).length;
    if (n > most) { best = id; most = n; }        // 同数は、先に見た(並びの先の)雰囲気のまま
  }
  return best;
}

// 台帳の params(viz, mood, genre, caption)から雰囲気の名前: viz(自作のジャンルが持つ)-> ムード -> キャプションの言葉 -> ジャンル -> 既定(energetic)
function vizThemeId(mood, genre, viz, caption) {
  if (typeof viz === "string" && Object.hasOwn(VIZ_MOOD_THEMES, viz)) return viz;
  if (typeof mood === "string" && Object.hasOwn(MOOD_TO_VIZ, mood)) return MOOD_TO_VIZ[mood];
  const fromCaption = vizThemeFromCaption(caption);
  if (fromCaption) return fromCaption;
  for (const [re, id] of GENRE_TO_VIZ) if (re.test(String(genre || ""))) return id;
  return "energetic";
}

// 1 つの canvas ぶんの描き手。粒子などの状態は、Easy Music ではモジュールの変数だったのを、ここに閉じ込めた。
function makeVizPainter() {
  let theme = VIZ_MOOD_THEMES.energetic;
  const freq = new Uint8Array(VIZ_FFT / 2), wave = new Uint8Array(VIZ_FFT / 2);     // いまのコマのスペクトル(0..255)と波形(128 が無音)
  let particles = [], fwBursts = [], matrixDrops = [], orbitBodies = null, fireParticles = [];
  let auroraTime = 0, orbitTime = 0;
  let ds = 1;        // 画面の密度。ぼかし(shadowBlur)は変換の影響を受けないので、掛けて合わせる
  let k = 1;         // canvas の高さに応じた動きの倍率(花火・炎。元は高さ 1000 px 級の画面の動き)

  const level = (n) => { let s = 0; for (let i = 0; i < n; i++) s += freq[i]; return s / n / 255; };   // 先頭 n 点の平均(0..1)。低音は 8、中低域は 48
  const rrect = (ctx, x, y, w, h, r) => (ctx.roundRect ? ctx.roundRect(x, y, w, h, r) : ctx.rect(x, y, w, h));

  // テーマから色を決めるヘルパー。t = 0..1 で hueMin..hueMax を補間
  function moodHue(t) {
    if (theme.hueMin <= theme.hueMax) return theme.hueMin + t * (theme.hueMax - theme.hueMin);
    const range = (360 - theme.hueMin) + theme.hueMax;      // 360 をまたぐ範囲
    return (theme.hueMin + t * range) % 360;
  }
  const moodSat = () => theme.sat;
  const moodLit = () => theme.lit;
  const moodInt = () => theme.intensity;
  const moodGlow = () => theme.glow;

  // スペクトラムの棒の並べ方(止まっているときの 1 コマと共通)。元は中央に 48 本(幅 5・間 3)。横長のカードでは、幅いっぱいに本数を増やす
  function spectrumLayout(W) {
    const count = Math.max(32, Math.min(96, Math.round(W / 14)));
    const slot = W * 0.94 / count, barW = slot * 0.62;
    return { count, slot, barW, x0: (W - ((count - 1) * slot + barW)) / 2 };
  }

  // --- Spectrum (スペクトラム) --- 高さは canvas の高さに合わせる(元は min(W,H) の 0.27 倍)
  function drawSpectrum(ctx, W, H) {
    const { count, slot, barW, x0 } = spectrumLayout(W);
    const specY = H / 2;
    const barMaxH = Math.min(H * 0.94, H * 0.78 * moodInt());
    const specBins = Math.floor(freq.length * 0.75);
    for (let i = 0; i < count; i++) {
      const val = freq[Math.floor(i * specBins / count)] / 255;
      const barH = Math.max(2, val * barMaxH);       // 静かなところでも細い線は残す(止まっているときの 1 コマとつながる)
      const x = x0 + i * slot;
      const hue = moodHue(i / count);
      const saturation = moodSat() - 15 + val * 25;
      const lightness = moodLit() - 5 + val * 20;

      ctx.shadowBlur = (12 + val * 15) * moodGlow() * ds;
      ctx.shadowColor = `hsl(${hue}, ${saturation}%, ${lightness}%)`;
      const barGrad = ctx.createLinearGradient(x, specY - barH / 2, x, specY + barH / 2);
      barGrad.addColorStop(0, `hsl(${hue}, ${saturation}%, ${lightness}%)`);
      barGrad.addColorStop(1, `hsl(${hue}, ${saturation}%, ${lightness - 20}%)`);
      ctx.fillStyle = barGrad;
      ctx.beginPath();
      rrect(ctx, x, specY - barH / 2, barW, barH, 2);
      ctx.fill();
    }
    ctx.shadowBlur = 0;
  }

  // --- Wave (波形) ---
  function drawWave(ctx, W, H) {
    const cy = H / 2;
    const amplitude = H * 0.28 * moodInt();
    const sliceWidth = W / wave.length;

    for (let mirror = 0; mirror < 2; mirror++) {
      ctx.beginPath();
      const sign = mirror === 0 ? 1 : -1;
      for (let i = 0; i < wave.length; i++) {
        const v = (wave[i] / 128.0) - 1.0;
        const y = cy + sign * v * amplitude;
        const x = i * sliceWidth;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      const hue = moodHue(mirror * 0.3);
      ctx.strokeStyle = `hsla(${hue}, ${moodSat()}%, ${moodLit() + 5}%, 0.7)`;
      ctx.lineWidth = 2.5;
      ctx.shadowBlur = 15 * moodGlow() * ds;
      ctx.shadowColor = `hsla(${hue}, ${moodSat()}%, ${moodLit()}%, 0.5)`;
      ctx.stroke();
    }

    ctx.beginPath();
    ctx.moveTo(0, cy);
    ctx.lineTo(W, cy);
    const centerHue = moodHue(0.5);
    ctx.strokeStyle = `hsla(${centerHue}, ${moodSat()}%, ${moodLit()}%, 0.15)`;
    ctx.lineWidth = 1;
    ctx.shadowBlur = 0;
    ctx.stroke();
  }

  // --- Ring (リング) ---
  function drawRing(ctx, W, H) {
    const cx = W / 2;
    const cy = H / 2;
    const baseRadius = Math.min(W, H) * 0.2;
    const barCount = 64;
    const specBins = Math.floor(freq.length * 0.75);

    for (let i = 0; i < barCount; i++) {
      const val = freq[Math.floor(i * specBins / barCount)] / 255;
      const angle = (i / barCount) * Math.PI * 2 - Math.PI / 2;
      const barLen = val * baseRadius * 0.9 * moodInt();

      const x1 = cx + Math.cos(angle) * baseRadius;
      const y1 = cy + Math.sin(angle) * baseRadius;
      const x2 = cx + Math.cos(angle) * (baseRadius + barLen);
      const y2 = cy + Math.sin(angle) * (baseRadius + barLen);

      const hue = moodHue(i / barCount);
      const lightness = moodLit() + val * 20;

      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.strokeStyle = `hsla(${hue}, ${moodSat()}%, ${lightness}%, 0.8)`;
      ctx.lineWidth = 3;
      ctx.lineCap = "round";
      ctx.shadowBlur = (8 + val * 12) * moodGlow() * ds;
      ctx.shadowColor = `hsla(${hue}, ${moodSat()}%, ${lightness}%, 0.6)`;
      ctx.stroke();
    }

    const avgVal = level(32);
    const ringHue = moodHue(0.5);
    ctx.beginPath();
    ctx.arc(cx, cy, baseRadius * (0.95 + avgVal * 0.05), 0, Math.PI * 2);
    ctx.strokeStyle = `hsla(${ringHue}, ${moodSat()}%, ${moodLit()}%, ${0.2 + avgVal * 0.3})`;
    ctx.lineWidth = 1.5;
    ctx.shadowBlur = 20 * moodGlow() * ds;
    ctx.shadowColor = `hsla(${ringHue}, ${moodSat()}%, ${moodLit()}%, ${0.3 + avgVal * 0.3})`;
    ctx.stroke();
    ctx.shadowBlur = 0;
  }

  // --- Particles (粒子) ---
  function drawParticles(ctx, W, H) {
    const cx = W / 2;
    const cy = H / 2;
    const avgVal = level(48);
    const bassVal = level(8);
    const pSpd = theme.pSpeed;

    const spawnCount = Math.floor(avgVal * 4 * moodInt());
    for (let i = 0; i < spawnCount; i++) {
      const angle = Math.random() * Math.PI * 2;
      const speed = (1 + Math.random() * 3 + bassVal * 3) * pSpd;
      particles.push({
        x: cx, y: cy,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed,
        life: 1.0,
        decay: 0.008 + Math.random() * 0.015,
        size: 2 + Math.random() * 3 + avgVal * 3,
        hue: moodHue(Math.random()),
      });
    }

    for (let i = particles.length - 1; i >= 0; i--) {
      const p = particles[i];
      p.x += p.vx;
      p.y += p.vy;
      p.life -= p.decay;
      p.vx *= 0.995;
      p.vy *= 0.995;

      if (p.life <= 0 || p.x < -10 || p.x > W + 10 || p.y < -10 || p.y > H + 10) {
        particles.splice(i, 1);
        continue;
      }

      ctx.beginPath();
      ctx.arc(p.x, p.y, p.size * p.life, 0, Math.PI * 2);
      ctx.fillStyle = `hsla(${p.hue}, ${moodSat()}%, ${moodLit() + 5}%, ${p.life * 0.7})`;
      ctx.shadowBlur = 10 * moodGlow() * ds;
      ctx.shadowColor = `hsla(${p.hue}, ${moodSat()}%, ${moodLit()}%, ${p.life * 0.4})`;
      ctx.fill();
    }

    if (particles.length > 300) particles.splice(0, particles.length - 300);
    ctx.shadowBlur = 0;
  }

  // --- Pulse (脈動) ---
  function drawPulse(ctx, W, H) {
    const cx = W / 2;
    const cy = H / 2;
    const maxR = Math.min(W, H) * 0.35;

    const bands = [0, 0, 0, 0];
    const bandSize = Math.floor(freq.length / 4);
    for (let b = 0; b < 4; b++) {
      let sum = 0;
      for (let i = 0; i < bandSize; i++) sum += freq[b * bandSize + i];
      bands[b] = sum / bandSize / 255;
    }

    const hues = [moodHue(0), moodHue(0.25), moodHue(0.5), moodHue(0.75)];
    const radii = [0.9, 0.7, 0.5, 0.3];

    for (let b = 0; b < 4; b++) {
      const r = maxR * radii[b] * (0.6 + bands[b] * 0.5 * moodInt());
      const alpha = 0.15 + bands[b] * 0.25;

      const grad = ctx.createRadialGradient(cx, cy, r * 0.3, cx, cy, r);
      grad.addColorStop(0, `hsla(${hues[b]}, ${moodSat()}%, ${moodLit()}%, ${alpha})`);
      grad.addColorStop(1, `hsla(${hues[b]}, ${moodSat()}%, ${moodLit() - 20}%, 0)`);
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.fillStyle = grad;
      ctx.fill();

      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.strokeStyle = `hsla(${hues[b]}, ${moodSat()}%, ${moodLit() + 5}%, ${alpha + 0.1})`;
      ctx.lineWidth = 1.5 + bands[b] * 2;
      ctx.shadowBlur = (15 + bands[b] * 20) * moodGlow() * ds;
      ctx.shadowColor = `hsla(${hues[b]}, ${moodSat()}%, ${moodLit()}%, 0.4)`;
      ctx.stroke();
    }
    ctx.shadowBlur = 0;
  }

  // --- Aurora (オーロラ) ---
  function drawAurora(ctx, W, H) {
    auroraTime += 0.008;
    const avgVal = level(48);
    const layers = 5;

    for (let l = 0; l < layers; l++) {
      const phase = auroraTime + l * 0.7;
      const yBase = H * (0.25 + l * 0.12);
      const hue = moodHue((l / layers + auroraTime * 0.05) % 1);
      const alpha = (0.08 + avgVal * 0.15) * moodInt();

      ctx.beginPath();
      ctx.moveTo(0, H);
      for (let x = 0; x <= W; x += 4) {
        const wave1 = Math.sin(x * 0.005 + phase) * H * 0.08;
        const wave2 = Math.sin(x * 0.012 + phase * 1.3) * H * 0.04;
        const wave3 = Math.sin(x * 0.003 + phase * 0.7) * H * 0.06 * avgVal;
        ctx.lineTo(x, yBase + wave1 + wave2 + wave3);
      }
      ctx.lineTo(W, H);
      ctx.closePath();

      const grad = ctx.createLinearGradient(0, yBase - H * 0.15, 0, yBase + H * 0.25);
      grad.addColorStop(0, `hsla(${hue}, ${moodSat()}%, ${moodLit() + 10}%, 0)`);
      grad.addColorStop(0.3, `hsla(${hue}, ${moodSat()}%, ${moodLit() + 10}%, ${alpha})`);
      grad.addColorStop(0.6, `hsla(${hue + 30}, ${moodSat()}%, ${moodLit()}%, ${alpha * 0.6})`);
      grad.addColorStop(1, `hsla(${hue}, ${moodSat()}%, ${moodLit()}%, 0)`);
      ctx.fillStyle = grad;
      ctx.fill();
    }
  }

  // --- Fireworks (花火) --- 動きの大きさ(速さと重力)に k を掛けて、低い canvas からはみ出さないようにしてある
  function drawFireworks(ctx, W, H) {
    const bassVal = level(8);

    // 低音が閾値を超えたら打ち上げ
    if (bassVal > 0.55 && fwBursts.length < 6 && Math.random() < 0.3 * moodInt()) {
      const bx = W * (0.15 + Math.random() * 0.7);
      const by = H * (0.15 + Math.random() * 0.4);
      const count = 30 + Math.floor(Math.random() * 30);
      const burstHue = moodHue(Math.random());
      const burst = [];
      for (let i = 0; i < count; i++) {
        const angle = Math.random() * Math.PI * 2;
        const speed = (2 + Math.random() * 4) * theme.pSpeed * k;
        burst.push({
          x: bx, y: by,
          vx: Math.cos(angle) * speed,
          vy: Math.sin(angle) * speed - k,
          life: 1.0,
          decay: 0.012 + Math.random() * 0.01,
          size: 2 + Math.random() * 2.5,
          hue: burstHue + (Math.random() - 0.5) * 30,
          trail: [],
        });
      }
      fwBursts.push(burst);
    }

    // 描画 + 更新
    for (let bi = fwBursts.length - 1; bi >= 0; bi--) {
      const burst = fwBursts[bi];
      let allDead = true;
      for (let i = burst.length - 1; i >= 0; i--) {
        const p = burst[i];
        p.trail.push({ x: p.x, y: p.y, life: p.life });
        if (p.trail.length > 5) p.trail.shift();
        p.x += p.vx;
        p.y += p.vy;
        p.vy += 0.04 * k;      // 重力
        p.vx *= 0.98;
        p.vy *= 0.98;
        p.life -= p.decay;

        if (p.life <= 0) { burst.splice(i, 1); continue; }
        allDead = false;

        // 尾
        for (let t = 0; t < p.trail.length; t++) {
          const tr = p.trail[t];
          const ta = (t / p.trail.length) * p.life * 0.3;
          ctx.beginPath();
          ctx.arc(tr.x, tr.y, p.size * 0.5, 0, Math.PI * 2);
          ctx.fillStyle = `hsla(${p.hue}, ${moodSat()}%, ${moodLit()}%, ${ta})`;
          ctx.fill();
        }

        ctx.beginPath();
        ctx.arc(p.x, p.y, p.size * p.life, 0, Math.PI * 2);
        ctx.fillStyle = `hsla(${p.hue}, ${moodSat()}%, ${moodLit() + 15}%, ${p.life * 0.8})`;
        ctx.shadowBlur = 12 * moodGlow() * ds;
        ctx.shadowColor = `hsla(${p.hue}, ${moodSat()}%, ${moodLit() + 10}%, ${p.life * 0.5})`;
        ctx.fill();
      }
      if (allDead) fwBursts.splice(bi, 1);
    }
    ctx.shadowBlur = 0;
  }

  // --- Matrix (マトリックス) --- 元は、毎コマ clearRect したあとに薄い黒を重ねていた(軌跡は残らず、暗くなるだけ)ので、それは省いた
  function drawMatrix(ctx, W, H) {
    const avgVal = level(48);
    const colW = 14;
    const cols = Math.floor(W / colW);

    // 初期化
    if (matrixDrops.length !== cols) {
      matrixDrops = [];
      for (let c = 0; c < cols; c++) matrixDrops.push({ y: Math.random() * H, speed: 2 + Math.random() * 4 });
    }

    const chars = "アイウエオカキクケコ01サシスセソタチツテト10";      // i18n-skip 画面の飾りの文字
    const hue1 = moodHue(0.3);
    const hue2 = moodHue(0.7);
    ctx.font = "13px monospace";

    for (let c = 0; c < cols; c++) {
      const d = matrixDrops[c];
      d.y += d.speed * (0.5 + avgVal * 1.5) * moodInt();

      if (d.y > H + 50) {
        d.y = -20;
        d.speed = 2 + Math.random() * 4;
      }

      const ch = chars[Math.floor(Math.random() * chars.length)];
      const hue = c % 2 === 0 ? hue1 : hue2;
      const bright = moodLit() + avgVal * 20;

      ctx.fillStyle = `hsla(${hue}, ${moodSat()}%, ${bright}%, ${0.7 + avgVal * 0.3})`;
      ctx.shadowBlur = 6 * moodGlow() * ds;
      ctx.shadowColor = `hsla(${hue}, ${moodSat()}%, ${bright}%, 0.5)`;
      ctx.fillText(ch, c * colW, d.y);

      // 上の残像
      for (let t = 1; t <= 6; t++) {
        const ty = d.y - t * 16;
        if (ty < 0) break;
        const tc = chars[Math.floor(Math.random() * chars.length)];
        ctx.fillStyle = `hsla(${hue}, ${moodSat()}%, ${bright}%, ${0.15 - t * 0.02})`;
        ctx.shadowBlur = 0;
        ctx.fillText(tc, c * colW, ty);
      }
    }
    ctx.shadowBlur = 0;
  }

  // --- Orbit (軌道) --- 元は半径 60〜256 px の円を楕円(縦 0.6 倍)で回っていた。横長のカードでは、軌道の大きさを W と H に合わせた楕円にして、線も楕円で引く
  function drawOrbit(ctx, W, H) {
    orbitTime += 0.012;
    const cx = W / 2;
    const cy = H / 2;
    const avgVal = level(48);

    if (!orbitBodies) {
      orbitBodies = [];
      for (let i = 0; i < 8; i++) {
        orbitBodies.push({
          fx: 0.05 + i * 0.045,       // 横の半径(W に対する割合)
          fy: 0.1 + i * 0.08,         // 縦の半径(H / 2 に対する割合)
          speed: (0.3 + Math.random() * 0.5) * (i % 2 === 0 ? 1 : -1),
          size: 4 + Math.random() * 5,
          phase: Math.random() * Math.PI * 2,
          hueT: i / 8,
        });
      }
    }
    const grow = (0.85 + avgVal * 0.25) * moodInt();

    // 軌道線
    for (const b of orbitBodies) {
      ctx.beginPath();
      ctx.ellipse(cx, cy, b.fx * W * grow, b.fy * (H / 2) * grow, 0, 0, Math.PI * 2);
      ctx.strokeStyle = `hsla(${moodHue(b.hueT)}, ${moodSat()}%, ${moodLit()}%, 0.08)`;
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    // 球体
    for (const b of orbitBodies) {
      const angle = orbitTime * b.speed + b.phase;
      const bx = cx + Math.cos(angle) * b.fx * W * grow;
      const by = cy + Math.sin(angle) * b.fy * (H / 2) * grow;
      const sz = b.size * (0.8 + avgVal * 0.5);
      const hue = moodHue(b.hueT);

      // 光
      const grad = ctx.createRadialGradient(bx, by, 0, bx, by, sz * 3);
      grad.addColorStop(0, `hsla(${hue}, ${moodSat()}%, ${moodLit() + 15}%, ${0.4 + avgVal * 0.3})`);
      grad.addColorStop(1, `hsla(${hue}, ${moodSat()}%, ${moodLit()}%, 0)`);
      ctx.beginPath();
      ctx.arc(bx, by, sz * 3, 0, Math.PI * 2);
      ctx.fillStyle = grad;
      ctx.fill();

      // 芯
      ctx.beginPath();
      ctx.arc(bx, by, sz, 0, Math.PI * 2);
      ctx.fillStyle = `hsla(${hue}, ${moodSat()}%, ${moodLit() + 20}%, 0.9)`;
      ctx.shadowBlur = 15 * moodGlow() * ds;
      ctx.shadowColor = `hsla(${hue}, ${moodSat()}%, ${moodLit() + 10}%, 0.6)`;
      ctx.fill();
    }

    // 中心のコア
    const coreSize = 8 + avgVal * 12;
    const coreHue = moodHue(0.5);
    const coreGrad = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreSize * 2);
    coreGrad.addColorStop(0, `hsla(${coreHue}, ${moodSat()}%, ${moodLit() + 20}%, 0.6)`);
    coreGrad.addColorStop(1, `hsla(${coreHue}, ${moodSat()}%, ${moodLit()}%, 0)`);
    ctx.beginPath();
    ctx.arc(cx, cy, coreSize * 2, 0, Math.PI * 2);
    ctx.fillStyle = coreGrad;
    ctx.fill();
    ctx.shadowBlur = 0;
  }

  // --- Fire (炎) --- 上る速さに k を、粒の大きさに k の半分を掛けて、低い canvas に収める
  function drawFire(ctx, W, H) {
    const avgVal = level(48);
    const bassVal = level(8);
    const pSpd = theme.pSpeed;
    const ks = 0.5 + k / 2;

    // 下から炎の粒を出す
    const spawnCount = Math.floor(3 + avgVal * 8 * moodInt());
    for (let i = 0; i < spawnCount; i++) {
      const sx = W * 0.2 + Math.random() * W * 0.6;
      fireParticles.push({
        x: sx,
        y: H + 5,
        vx: (Math.random() - 0.5) * 2,
        vy: -(2 + Math.random() * 3 + bassVal * 3) * pSpd * k,
        life: 1.0,
        decay: 0.008 + Math.random() * 0.012,
        size: (4 + Math.random() * 6 + avgVal * 4) * ks,
        hueT: Math.random(),       // 0 = 芯、1 = 先
      });
    }

    for (let i = fireParticles.length - 1; i >= 0; i--) {
      const p = fireParticles[i];
      p.x += p.vx + (Math.random() - 0.5) * 0.8;
      p.y += p.vy;
      p.vy *= 0.99;
      p.vx *= 0.98;
      p.life -= p.decay;

      if (p.life <= 0 || p.y < -20) { fireParticles.splice(i, 1); continue; }

      // 色: 生きている割合で変える(下 = 明るい芯、上 = 暗い先)
      const lifeT = 1 - p.life;       // 0 = 生まれたて、1 = 消える直前
      const hue = moodHue(lifeT * 0.7 + p.hueT * 0.3);
      const lit = moodLit() + 20 * p.life - 10 * lifeT;
      const alpha = p.life * 0.6;

      ctx.beginPath();
      ctx.arc(p.x, p.y, p.size * (0.3 + p.life * 0.7), 0, Math.PI * 2);
      ctx.fillStyle = `hsla(${hue}, ${moodSat()}%, ${Math.max(lit, 20)}%, ${alpha})`;
      ctx.shadowBlur = (8 + p.life * 12) * moodGlow() * ds;
      ctx.shadowColor = `hsla(${hue}, ${moodSat()}%, ${moodLit() + 10}%, ${alpha * 0.5})`;
      ctx.fill();
    }

    if (fireParticles.length > 400) fireParticles.splice(0, fireParticles.length - 400);
    ctx.shadowBlur = 0;
  }

  // 止まっているとき(再生前・一時停止・終わり)の 1 コマ: 細い棒が横一列に並んだ、薄い平らなスペクトラム
  function drawIdle(ctx, W, H) {
    const { count, slot, barW, x0 } = spectrumLayout(W);
    for (let i = 0; i < count; i++) {
      ctx.fillStyle = `hsla(${moodHue(i / count)}, ${moodSat() - 15}%, ${moodLit() - 5}%, 0.32)`;
      ctx.beginPath();
      rrect(ctx, x0 + i * slot, H / 2 - 1, barW, 2, 1);
      ctx.fill();
    }
  }

  return {
    freq, wave,
    get theme() { return theme; },
    setTheme(t) { theme = t || VIZ_MOOD_THEMES.energetic; },
    // 表示を切り替えるとき、前の表示の粒子などを捨てる
    reset() { particles = []; fwBursts = []; matrixDrops = []; orbitBodies = null; fireParticles = []; },
    // 1 コマ描く。freq / wave には、呼ぶ前に AnalyserNode から読み込んでおく。W, H は CSS px、dpr は画面の密度
    draw(mode, ctx, W, H, dpr) {
      ds = dpr || 1;
      k = Math.max(0.35, Math.min(1, H / 540));
      switch (mode) {
        case "wave":      drawWave(ctx, W, H); break;
        case "ring":      drawRing(ctx, W, H); break;
        case "particles": drawParticles(ctx, W, H); break;
        case "pulse":     drawPulse(ctx, W, H); break;
        case "aurora":    drawAurora(ctx, W, H); break;
        case "fireworks": drawFireworks(ctx, W, H); break;
        case "matrix":    drawMatrix(ctx, W, H); break;
        case "orbit":     drawOrbit(ctx, W, H); break;
        case "fire":      drawFire(ctx, W, H); break;
        default:          drawSpectrum(ctx, W, H); break;
      }
    },
    idle(ctx, W, H) { drawIdle(ctx, W, H); },
  };
}

// ---- 音の経路と描画ループ ----

let vizContext = null;            // ページで 1 つの AudioContext(<audio> ごとに作ると、ブラウザの上限に当たる)
const vizLive = new Set();        // AnalyserNode をつないでいるコントローラ(外れた canvas の後始末に使う)

function vizAudioContext() {
  if (!vizContext) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) throw new Error(t("この環境では Web Audio が使えません"));
    vizContext = new AC();
  }
  return vizContext;
}

const vizIsMode = (m) => VIZ_MODES.some(([v]) => v === m);
function vizLoadMode() {
  try {
    const m = localStorage.getItem(VIZ_MODE_KEY);
    if (vizIsMode(m)) return m;
  } catch (e) { /* 保存されていなければ既定 */ }
  return VIZ_DEFAULT_MODE;
}
function vizSaveMode(m) {
  try { localStorage.setItem(VIZ_MODE_KEY, m); } catch (e) { /* 保存できなくても動作は同じ */ }
}

// 画面から外れた canvas のコントローラを片付ける(入れ替える側が detachVisualizers を呼び忘れたときの保険)
function vizSweep() {
  for (const c of [...vizLive]) if (!c.canvas.isConnected) c.destroy();
}

// root の中のスペクトラムを片付ける。root の中身(音楽のカード)を入れ替える前に呼ぶ。外すものなので、音の経路は残さない
function detachVisualizers(root) {
  for (const c of root.querySelectorAll("canvas")) if (c._viz) c._viz.destroy({ keepAudible: false });
}

// audio の再生に合わせて、canvas にスペクトラムを出す。再生(play)で描画ループを始め、一時停止・終わり・読み直し・タブが隠れたとき・canvas が画面から外れたときに止める。
// 止まっている間は、静かな 1 コマを出しておく。{ viz, mood, genre, caption } は台帳の params から(色合いが決まる)、mode は VIZ_MODES の値(省略すると、前に選んだもの)。
function attachVisualizer(audio, canvas, opts = {}) {
  vizSweep();
  if (canvas._viz) canvas._viz.destroy();
  if (audio._vizCtl) audio._vizCtl.destroy();       // 1 つの <audio> には 1 つ

  const g2d = canvas.getContext("2d");
  const painter = makeVizPainter();
  const themeId = vizThemeId(opts.mood, opts.genre, opts.viz, opts.caption);
  painter.setTheme(VIZ_MOOD_THEMES[themeId]);
  let mode = vizIsMode(opts.mode) ? opts.mode : vizLoadMode();
  let shown = null;                 // おまかせのとき、いま出している表示
  let graph = null;                 // 再生を始めたときに作る { ac, analyser, source }(音は source -> analyser -> destination を通る)
  let failed = false;               // 音の経路を作れなかった(動かし続けず、静かな 1 コマのまま。音は普通に鳴る)
  let raf = 0;                      // 描画ループの ID。0 は止まっている
  let tracking = false;
  let seen = false;                 // canvas が画面に載ったことがある(まだ載せていないうちに片付けない)
  let dead = false;
  let waking = false;               // 止まっている AudioContext が動くのを待っている
  let last = 0, frames = 0;
  let w = 0, h = 0, dpr = 1;

  function current() {
    if (mode !== "auto") return mode;
    if (!shown) { const list = painter.theme.modes; shown = list[Math.floor(Math.random() * list.length)]; }
    return shown;
  }

  // canvas の大きさ(CSS px)と画面の密度に、描画のサイズを合わせる。変わったら true(canvas は作り直しで空になる)
  function resize() {
    const cw = canvas.clientWidth, ch = canvas.clientHeight, ratio = window.devicePixelRatio || 1;
    if (cw === w && ch === h && ratio === dpr) return false;
    w = cw; h = ch; dpr = ratio;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    return true;
  }

  function begin() {
    g2d.setTransform(dpr, 0, 0, dpr, 0, 0);
    g2d.clearRect(0, 0, w, h);
  }

  function idleFrame() {
    if (!w || !h) return;           // 非表示(display:none)の間は描かない
    begin();
    painter.idle(g2d, w, h);
  }

  // 自動再生の制限などで止まっている AudioContext を動かし、動いたらつなぎ直す(sync)。
  // 動いていない AudioContext に <audio> をつなぐと音が出なくなるので、動くまでは <audio> に触らない(静かな 1 コマのまま、音は普通に鳴る)
  function wake(ac) {
    if (waking) return;
    waking = true;
    try {
      const p = ac.resume();
      if (p && p.then) p.then(() => { waking = false; if (ac.state === "running") sync(); }, () => { waking = false; });
      else waking = false;
    } catch (e) { waking = false; }
  }

  // AnalyserNode をつなぐ。createMediaElementSource は 1 つの <audio> に 1 回だけ(2 回目は例外)なので、作ったものは要素に覚えさせる。
  // つなぎ損ねても音が消えないよう、source ができたあとの失敗は、出力へ直接つなぎ直す
  function ensureGraph() {
    if (graph || failed) return !!graph;
    let source = audio._vizSource;
    try {
      const ac = vizAudioContext();
      if (ac.state !== "running") { wake(ac); return false; }
      const analyser = ac.createAnalyser();
      analyser.fftSize = VIZ_FFT;
      source = source || (audio._vizSource = ac.createMediaElementSource(audio));
      source.disconnect();          // 前の経路(直結や、前の analyser)を外してから
      source.connect(analyser);
      analyser.connect(ac.destination);
      graph = { ac, analyser, source };
      vizLive.add(ctl);
    } catch (e) {
      failed = true;
      console.warn("スペクトラムを出せません(音は普通に鳴ります):", e);      // i18n-skip ログ
      try { if (source) source.connect(vizContext.destination); } catch (e2) { /* 音の経路は、もう作れない */ }
    }
    return !!graph;
  }

  // つないだあとに AudioContext が止まったら(止まっていると、無音のまま進む)、動かし直す
  function resumeContext() {
    if (!graph || graph.ac.state !== "suspended") return;
    try {
      const p = graph.ac.resume();
      if (p && p.catch) p.catch(() => {});
    } catch (e) { /* 次の再生で、また試す */ }
  }

  function frame(now) {
    raf = requestAnimationFrame(frame);
    if (!canvas.isConnected) { destroy(); return; }
    if (now - last < 13) return;    // 60 fps まで(120 Hz 以上の画面で速くなりすぎない)
    last = now;
    if ((window.devicePixelRatio || 1) !== dpr) resize();
    if (!w || !h) return;
    try {
      graph.analyser.getByteFrequencyData(painter.freq);
      graph.analyser.getByteTimeDomainData(painter.wave);
      begin();
      painter.draw(current(), g2d, w, h, dpr);
    } catch (e) {
      console.error("スペクトラムの描画に失敗しました:", e);      // i18n-skip ログ
      failed = true;
      sync();
    }
    if ((frames++ & 31) === 0) resumeContext();     // 動かせていなければ、ときどき試し直す
  }

  const onVisibility = () => sync();
  const track = () => { if (!tracking) { tracking = true; document.addEventListener("visibilitychange", onVisibility); } };
  const untrack = () => { if (tracking) { tracking = false; document.removeEventListener("visibilitychange", onVisibility); } };

  // いまの状態(再生中か・タブが見えているか・canvas が画面にあるか)に、描画ループを合わせる
  function sync() {
    if (dead) return;
    if (canvas.isConnected) seen = true;
    else if (seen) { destroy(); return; }
    const playing = !audio.paused && !audio.ended;
    if (playing && !failed) ensureGraph();
    if (playing) { track(); resumeContext(); } else { untrack(); waking = false; }      // タブの表示・非表示を見るのは再生中だけ。動かす待ちは、止まったら捨てる(次の再生でやり直す)
    const run = playing && !!graph && !failed && !document.hidden;
    if (run && !raf) { last = 0; raf = requestAnimationFrame(frame); }
    else if (!run && raf) { cancelAnimationFrame(raf); raf = 0; }
    if (!run) idleFrame();          // 止まっている間は、静かな 1 コマ
  }

  // 片付け: ループ・イベント・監視を外し、つないだノードを切る。<audio> がまだ画面にあれば、音は出し続ける(keepAudible)
  function destroy({ keepAudible = audio.isConnected } = {}) {
    if (dead) return;
    dead = true;
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
    untrack();
    for (const ev of EVENTS) audio.removeEventListener(ev, sync);
    ro.disconnect();
    vizLive.delete(ctl);
    if (graph) {
      try {
        graph.analyser.disconnect();
        graph.source.disconnect();
        if (keepAudible) graph.source.connect(graph.ac.destination);
      } catch (e) { /* もう切れている */ }
      graph = null;
    }
    if (canvas._viz === ctl) canvas._viz = null;
    if (audio._vizCtl === ctl) audio._vizCtl = null;
  }

  function setMode(m) {
    if (dead || !vizIsMode(m)) return;
    mode = m;
    shown = null;                   // おまかせは選び直す
    painter.reset();
    if (!raf) idleFrame();
  }

  const EVENTS = ["play", "playing", "pause", "ended", "emptied"];
  const ro = new ResizeObserver(() => { resize(); sync(); });
  const ctl = { audio, canvas, theme: themeId, setMode, getMode: () => mode, destroy };
  canvas._viz = ctl;
  audio._vizCtl = ctl;
  for (const ev of EVENTS) audio.addEventListener(ev, sync);
  ro.observe(canvas);
  sync();                           // すでに再生中のものに付けたときも動く
  return ctl;
}

// canvas と、表示を選ぶ小さな選択を入れた枠を作る(音楽のカードの先頭に入れる)。選んだ表示は localStorage に覚える。
// スペクトラムは飾りなので、作れなくても(Web Audio が無いなど)カードの再生は壊さない
function vizBox(audio, opts = {}) {
  const box = document.createElement("div");
  box.className = "vizbox";
  try {
    const canvas = document.createElement("canvas");
    canvas.setAttribute("aria-hidden", "true");
    const ctl = attachVisualizer(audio, canvas, opts);
    const sel = document.createElement("select");
    sel.className = "vizmode";
    sel.title = t("スペクトラムの表示。おまかせは、曲の雰囲気に合う表示を選びます(選んだものは覚えておきます)");
    sel.setAttribute("aria-label", t("スペクトラムの表示"));
    for (const [v, label] of VIZ_MODES) sel.append(new Option(label, v));
    sel.value = ctl.getMode();
    sel.onchange = () => { ctl.setMode(sel.value); vizSaveMode(sel.value); };
    box.append(canvas, sel);
  } catch (e) {
    console.warn("スペクトラムを付けられませんでした:", e);      // i18n-skip ログ
    box.hidden = true;
  }
  return box;
}
