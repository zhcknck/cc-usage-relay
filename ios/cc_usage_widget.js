// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: green; icon-glyph: tachometer-alt;
/*
cc-usage-relay — Claude Code 用量 widget（鎖屏為主力）

安裝步驟：
1) App Store 安裝 Scriptable（免費）
2) Scriptable 內新增 script，貼入本檔，把下方 GIST_RAW_URL 改成你的 Gist raw 連結
   （https://gist.githubusercontent.com/<user>/<gist_id>/raw/usage.json）
3) 鎖屏 widget：長按鎖定畫面 → 自訂 → 鎖定畫面 → 點時鐘下方 widget 區
   → 選 Scriptable → 選此 script（矩形或圓形）
4) 主畫面 widget：長按桌面 → 編輯 → 加入 widget → Scriptable small 或 medium
   → 長按該 widget → 編輯 → Script 選此檔

備註：鎖屏 widget 由 iOS 系統單色渲染，僅以透明度區分層次，屬系統行為。
*/

// ===== 使用者需要改的地方 =====
const GIST_RAW_URL = "https://gist.githubusercontent.com/zhcknck/9585fb88cf03a9982de3e2b9b2fc0299/raw/usage.json";
const STALE_MINUTES = 20;
// 點 widget 開啟的網址（GitHub Pages dashboard），留空 = 開 Scriptable
const DASHBOARD_URL = "";
// 多機時指定要顯示哪台（對應 config 的 machine_name），留空 = 取最新一台
const MACHINE_NAME = "";
// ==============================

const CACHE_FILE = "cc_usage_cache.json";
const HISTORY_CACHE_FILE = "cc_usage_history_cache.json";

// ---------- 資料 ----------

async function fetchJsonCached(url, cacheName) {
  const fm = FileManager.local();
  const cachePath = fm.joinPath(fm.libraryDirectory(), cacheName);
  try {
    const req = new Request(url + "?t=" + Date.now()); // 破 CDN 快取
    req.timeoutInterval = 10;
    const data = await req.loadJSON();
    fm.writeString(cachePath, JSON.stringify(data));
    return data;
  } catch (e) {
    if (fm.fileExists(cachePath)) {
      try { return JSON.parse(fm.readString(cachePath)); } catch (e2) {}
    }
    return null;
  }
}

async function loadPayloads() {
  const data = await fetchJsonCached(GIST_RAW_URL, CACHE_FILE);
  if (!data) return [];
  if (Array.isArray(data)) return data.filter(p => p && p.claude_code !== undefined);
  return data.claude_code !== undefined ? [data] : [];
}

async function loadHistory() {
  const url = GIST_RAW_URL.replace(/usage\.json$/, "history.json");
  const data = await fetchJsonCached(url, HISTORY_CACHE_FILE);
  return Array.isArray(data) ? data : [];
}

// 燒速率：最近 60 分鐘 5hr% 線性擬合 -> 預估幾分鐘後達 100%（不會達則回 null）
function burnEtaMinutes(history, machine, currentP5, resetsAt) {
  if (currentP5 == null) return null;
  const cutoff = Date.now() - 60 * 60 * 1000;
  const pts = history
    .filter(e => e && e.m === machine && typeof e.h5 === "number")
    .map(e => ({ t: new Date(e.t).getTime(), v: e.h5 }))
    .filter(p => isFinite(p.t) && p.t >= cutoff);
  if (pts.length < 3) return null;
  const n = pts.length;
  const mt = pts.reduce((s, p) => s + p.t, 0) / n;
  const mv = pts.reduce((s, p) => s + p.v, 0) / n;
  let num = 0, den = 0;
  for (const p of pts) { num += (p.t - mt) * (p.v - mv); den += (p.t - mt) ** 2; }
  if (!den) return null;
  const slopePerMin = (num / den) * 60000;
  if (slopePerMin < 0.05) return null;
  const eta = (100 - currentP5) / slopePerMin;
  const resetMin = resetsAt ? (new Date(resetsAt).getTime() - Date.now()) / 60000 : Infinity;
  return eta < resetMin ? eta : null; // 重置前不會達上限就不顯示
}

function fmtMinutes(m) {
  if (m < 60) return Math.round(m) + "m";
  return Math.floor(m / 60) + "h" + Math.round(m % 60) + "m";
}

function pickPayload(payloads) {
  if (!payloads.length) return null;
  if (MACHINE_NAME) {
    const hit = payloads.find(p => p.machine === MACHINE_NAME);
    if (hit) return hit;
  }
  return payloads.slice().sort((a, b) =>
    new Date(b.updated_at || 0) - new Date(a.updated_at || 0))[0];
}

function isStale(p) {
  if (!p) return true;
  if (p.stale === true) return true;
  const t = new Date(p.updated_at).getTime();
  return !isFinite(t) || (Date.now() - t) > STALE_MINUTES * 60 * 1000;
}

// resets_at(UTC) -> 相對時間：<60m "33m"；<24h "3h20m"；其餘 "2d22h"
function relTime(iso) {
  if (!iso) return "--";
  const diff = new Date(iso).getTime() - Date.now();
  const mins = Math.max(0, Math.floor(diff / 60000));
  if (mins < 60) return mins + "m";
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (mins < 1440) return m > 0 ? h + "h" + m + "m" : h + "h";
  const d = Math.floor(h / 24);
  const hr = h % 24;
  return hr > 0 ? d + "d" + hr + "h" : d + "d";
}

function hhmm(iso) {
  const d = new Date(iso);
  if (!isFinite(d.getTime())) return "--:--";
  const p = (n) => (n < 10 ? "0" : "") + n;
  return p(d.getHours()) + ":" + p(d.getMinutes());
}

function pctOf(block) {
  if (!block || typeof block.pct !== "number") return null;
  return Math.max(0, block.pct);
}

function barRatio(pct) {
  return Math.min(100, pct == null ? 0 : pct) / 100; // 進度條上限 100
}

function extraActive(p) {
  const ex = p && p.extra_usage;
  if (!ex || typeof ex !== "object") return false;
  if (ex.is_enabled === true || ex.enabled === true) return true;
  for (const k of ["used_credits", "used", "utilization", "amount"]) {
    if (typeof ex[k] === "number" && ex[k] > 0) return true;
  }
  return false;
}

const GRAY = new Color("#98989f");

function tone(pct, stale) {
  if (stale || pct == null) return GRAY;
  if (pct >= 90) return new Color("#ff453a");
  if (pct >= 70) return new Color("#ff9f0a");
  return new Color("#30d158");
}

// ---------- 繪圖 ----------

function roundedRect(x, y, w, h, r) {
  const p = new Path();
  p.addRoundedRect(new Rect(x, y, w, h), r, r);
  return p;
}

function drawBar(width, height, ratio, fgColor, bgColor) {
  const ctx = new DrawContext();
  ctx.size = new Size(width, height);
  ctx.opaque = false;
  ctx.respectScreenScale = true;
  const r = height / 2;
  ctx.setFillColor(bgColor);
  ctx.addPath(roundedRect(0, 0, width, height, r));
  ctx.fillPath();
  if (ratio > 0) {
    const w = Math.max(height, width * Math.min(1, ratio));
    ctx.setFillColor(fgColor);
    ctx.addPath(roundedRect(0, 0, w, height, r));
    ctx.fillPath();
  }
  return ctx.getImage();
}

// 圓環 gauge：以小圓點沿弧線排列近似（DrawContext 無原生弧線 API）
// opts: { fill, track, text, sub }（sub 為中央數字下的小字，可省略）
function drawRing(size, ratio, centerText, opts) {
  const ctx = new DrawContext();
  ctx.size = new Size(size, size);
  ctx.opaque = false;
  ctx.respectScreenScale = true;
  const lineW = size * 0.115;
  const radius = (size - lineW) / 2 - 1;
  const c = size / 2;

  const dot = (deg, color) => {
    const rad = ((deg - 90) * Math.PI) / 180; // 自 12 點鐘起算
    const x = c + radius * Math.cos(rad) - lineW / 2;
    const y = c + radius * Math.sin(rad) - lineW / 2;
    ctx.setFillColor(color);
    ctx.fillEllipse(new Rect(x, y, lineW, lineW));
  };

  for (let a = 0; a < 360; a += 3) dot(a, opts.track);
  const sweep = 360 * Math.min(1, ratio);
  for (let a = 0; a <= sweep; a += 2) dot(a, opts.fill); // 順時針

  ctx.setTextAlignedCenter();
  ctx.setTextColor(opts.text);
  if (opts.sub) {
    ctx.setFont(Font.boldSystemFont(size * 0.28));
    ctx.drawTextInRect(centerText, new Rect(0, size * 0.24, size, size * 0.36));
    ctx.setFont(Font.systemFont(size * 0.13));
    ctx.setTextColor(opts.subColor || opts.text);
    ctx.drawTextInRect(opts.sub, new Rect(0, size * 0.58, size, size * 0.2));
  } else {
    ctx.setFont(Font.boldSystemFont(size * 0.32));
    ctx.drawTextInRect(centerText, new Rect(0, size * 0.30, size, size * 0.40));
  }
  return ctx.getImage();
}

// ---------- 各 family 渲染 ----------

function renderNoData(widget, msg) {
  const t = widget.addText(msg || "無資料");
  t.font = Font.systemFont(12);
  t.textOpacity = 0.8;
}

// (A) 鎖屏矩形：標題 + 5hr 粗條 + weekly 細條 + 資訊行
function renderRectangular(widget, p) {
  widget.addAccessoryWidgetBackground = true;
  const cc = p && p.claude_code;
  if (!cc) { renderNoData(widget, "CC 無資料"); return; }
  const stale = isStale(p);
  const p5 = pctOf(cc.five_hour);
  const p7 = pctOf(cc.seven_day);

  const row1 = widget.addStack();
  row1.centerAlignContent();
  let sym = SFSymbol.named("gauge.with.needle");
  if (!sym || !sym.image) sym = SFSymbol.named("gauge");
  if (sym && sym.image) {
    const img = row1.addImage(sym.image);
    img.imageSize = new Size(16, 16);
    img.tintColor = Color.white();
    row1.addSpacer(4);
  }
  const title = row1.addText("CC " + (p5 == null ? "--" : Math.round(p5)) + "%");
  title.font = Font.boldSystemFont(17);

  widget.addSpacer(3);
  const bar5 = widget.addImage(drawBar(150, 4, barRatio(p5),
    new Color("#ffffff", 1.0), new Color("#ffffff", 0.3)));
  bar5.imageSize = new Size(150, 4);
  bar5.leftAlignImage();

  widget.addSpacer(2);
  const bar7 = widget.addImage(drawBar(150, 2, barRatio(p7),
    new Color("#ffffff", 0.85), new Color("#ffffff", 0.25)));
  bar7.imageSize = new Size(150, 2);
  bar7.leftAlignImage();

  widget.addSpacer(3);
  let line3;
  if (stale) {
    line3 = "過期 · " + hhmm(p.updated_at);
  } else {
    line3 = "W " + (p7 == null ? "--" : Math.round(p7)) + "% · ↻" +
      relTime(cc.five_hour && cc.five_hour.resets_at);
  }
  const t3 = widget.addText(line3);
  t3.font = Font.systemFont(11);
  t3.textOpacity = 0.7;
}

// (B) 鎖屏圓形
function renderCircular(widget, p) {
  widget.addAccessoryWidgetBackground = true;
  const cc = p && p.claude_code;
  const p5 = cc ? pctOf(cc.five_hour) : null;
  const stale = isStale(p);
  const text = p5 == null ? "--" : String(Math.round(p5));
  const img = widget.addImage(drawRing(76, barRatio(p5), text, {
    fill: new Color("#ffffff", 1.0),
    track: new Color("#ffffff", 0.25),
    text: Color.white(),
    sub: stale ? "!" : null,
  }));
  img.imageSize = new Size(76, 76);
  img.centerAlignImage();
}

// (C) 鎖屏一行（時鐘上方）
function renderInline(widget, p) {
  const cc = p && p.claude_code;
  if (!cc) { widget.addText("CC 無資料"); return; }
  const p5 = pctOf(cc.five_hour);
  const p7 = pctOf(cc.seven_day);
  const prefix = isStale(p) ? "⚠ " : "";
  widget.addText(prefix + "CC " + (p5 == null ? "--" : Math.round(p5)) + "% · W " +
    (p7 == null ? "--" : Math.round(p7)) + "%");
}

function addHeader(widget, p, stale, label) {
  const header = widget.addStack();
  header.centerAlignContent();
  const title = header.addText(label);
  title.font = Font.systemFont(12);
  title.textColor = GRAY;
  header.addSpacer();
  if (extraActive(p)) {
    const dollar = header.addText("$ ");
    dollar.font = Font.boldSystemFont(11);
    dollar.textColor = new Color("#ff9f0a");
  }
  if (stale) {
    const badge = header.addText("STALE");
    badge.font = Font.boldSystemFont(9);
    badge.textColor = GRAY;
  }
}

// (D) 主畫面 small：彩色環形 gauge + Weekly
function renderSmall(widget, p, etaMin) {
  widget.backgroundColor = new Color("#1c1c1e");
  widget.setPadding(12, 12, 12, 12);
  const cc = p && p.claude_code;
  if (!cc) { renderNoData(widget, "無資料"); return; }
  const stale = isStale(p);
  const p5 = pctOf(cc.five_hour);
  const p7 = pctOf(cc.seven_day);
  const mainColor = tone(p5, stale);
  const weeklyColor = stale ? GRAY : new Color("#6e8cae");
  const trackColor = new Color("#3a3a3c");

  addHeader(widget, p, stale, "Claude Code");
  widget.addSpacer(4);

  const mid = widget.addStack();
  mid.centerAlignContent();
  const ring = mid.addImage(drawRing(64, barRatio(p5),
    p5 == null ? "--" : String(Math.round(p5)), {
      fill: mainColor, track: trackColor, text: Color.white(),
      sub: "5hr", subColor: GRAY,
    }));
  ring.imageSize = new Size(64, 64);
  mid.addSpacer();
  const col = mid.addStack();
  col.layoutVertically();
  const wl = col.addText("Weekly");
  wl.font = Font.systemFont(10);
  wl.textColor = GRAY;
  const wv = col.addText((p7 == null ? "--" : Math.round(p7)) + "%");
  wv.font = Font.boldSystemFont(20);
  wv.textColor = stale ? GRAY : Color.white();
  col.addSpacer(4);
  const wr = col.addText("↻" + relTime(cc.seven_day && cc.seven_day.resets_at));
  wr.font = Font.systemFont(9);
  wr.textColor = GRAY;

  widget.addSpacer(6);
  const bar7 = widget.addImage(drawBar(124, 6, barRatio(p7), weeklyColor, trackColor));
  bar7.imageSize = new Size(124, 6);
  bar7.leftAlignImage();

  widget.addSpacer(5);
  let footText, footColor = GRAY;
  if (stale) {
    footText = "更新於 " + hhmm(p.updated_at);
  } else if (etaMin != null) {
    footText = "🔥 ~" + fmtMinutes(etaMin) + " 後達上限";
    footColor = new Color("#ff9f0a");
  } else {
    footText = "↻ 5hr " + relTime(cc.five_hour && cc.five_hour.resets_at) +
      " · 7d " + relTime(cc.seven_day && cc.seven_day.resets_at);
  }
  const foot = widget.addText(footText);
  foot.font = Font.systemFont(9);
  foot.textColor = footColor;
}

// (E) 主畫面 medium：四條進度條（5hr / Weekly / Opus / Sonnet）
function renderMedium(widget, p, etaMin) {
  widget.backgroundColor = new Color("#1c1c1e");
  widget.setPadding(14, 16, 12, 16);
  const cc = p && p.claude_code;
  if (!cc) { renderNoData(widget, "無資料"); return; }
  const stale = isStale(p);
  const trackColor = new Color("#3a3a3c");
  const weeklyColor = stale ? GRAY : new Color("#6e8cae");

  addHeader(widget, p, stale,
    "Claude Code" + (p.machine ? " · " + p.machine : ""));
  widget.addSpacer(6);

  const rows = [
    ["5hr", pctOf(cc.five_hour), tone(pctOf(cc.five_hour), stale)],
    ["Weekly", pctOf(cc.seven_day), weeklyColor],
    ["Opus", pctOf(cc.seven_day_opus), weeklyColor],
    ["Sonnet", pctOf(cc.seven_day_sonnet), weeklyColor],
  ];
  for (const [label, pct, color] of rows) {
    if (pct == null && label !== "5hr" && label !== "Weekly") continue; // Opus/Sonnet 沒資料就略過
    const row = widget.addStack();
    row.centerAlignContent();
    const lt = row.addText(label);
    lt.font = Font.systemFont(10);
    lt.textColor = GRAY;
    lt.lineLimit = 1;
    row.addSpacer();
    const bar = row.addImage(drawBar(200, 6, barRatio(pct), color, trackColor));
    bar.imageSize = new Size(200, 6);
    row.addSpacer(8);
    const vt = row.addText((pct == null ? "--" : Math.round(pct)) + "%");
    vt.font = Font.boldSystemFont(12);
    vt.textColor = stale ? GRAY : Color.white();
    widget.addSpacer(4);
  }

  widget.addSpacer();
  let footText = stale
    ? "資料過期 · 更新於 " + hhmm(p.updated_at)
    : "↻ 5hr " + relTime(cc.five_hour && cc.five_hour.resets_at) +
      " · 7d " + relTime(cc.seven_day && cc.seven_day.resets_at) +
      " · 更新 " + hhmm(p.updated_at);
  if (!stale && etaMin != null) footText = "🔥 ~" + fmtMinutes(etaMin) + " 後達上限 · " + footText;
  const foot = widget.addText(footText);
  foot.font = Font.systemFont(9);
  foot.textColor = (!stale && etaMin != null) ? new Color("#ff9f0a") : GRAY;
}

// ---------- 主流程 ----------

async function run() {
  const family = config.widgetFamily || "small"; // App 內預覽預設 small
  const widget = new ListWidget();
  widget.refreshAfterDate = new Date(Date.now() + 5 * 60 * 1000); // 建議值，iOS 自行調度
  if (DASHBOARD_URL) widget.url = DASHBOARD_URL; // 點擊跳轉 dashboard

  if (GIST_RAW_URL.indexOf("<user>") !== -1) {
    renderNoData(widget, "請先在腳本頂部設定 GIST_RAW_URL");
    if (config.runsInWidget) { Script.setWidget(widget); } else { await widget.presentSmall(); }
    Script.complete();
    return;
  }

  const payloads = await loadPayloads();
  const payload = pickPayload(payloads);

  // 燒速率只在主畫面 family 計算（多一個請求，鎖屏不需要）
  let etaMin = null;
  if ((family === "small" || family === "medium") && payload && !isStale(payload)) {
    const cc = payload.claude_code || {};
    etaMin = burnEtaMinutes(await loadHistory(), payload.machine,
      pctOf(cc.five_hour), cc.five_hour && cc.five_hour.resets_at);
  }

  if (family === "accessoryRectangular") {
    renderRectangular(widget, payload);
  } else if (family === "accessoryCircular") {
    renderCircular(widget, payload);
  } else if (family === "accessoryInline") {
    renderInline(widget, payload);
  } else if (family === "small") {
    renderSmall(widget, payload, etaMin);
  } else if (family === "medium") {
    renderMedium(widget, payload, etaMin);
  } else {
    renderNoData(widget, "請使用鎖屏、small 或 medium widget");
  }

  if (config.runsInWidget) {
    Script.setWidget(widget);
  } else if (family === "accessoryRectangular") {
    await widget.presentAccessoryRectangular();
  } else if (family === "accessoryCircular") {
    await widget.presentAccessoryCircular();
  } else if (family === "accessoryInline") {
    await widget.presentAccessoryInline();
  } else if (family === "medium") {
    await widget.presentMedium();
  } else {
    await widget.presentSmall();
  }
  Script.complete();
}

await run();
