// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: orange; icon-glyph: tachometer-alt;
/*
cc-usage-relay — Claude Code 用量 widget（橘色版）

安裝步驟：
1) App Store 安裝 Scriptable（免費）
2) Scriptable 內新增 script，貼入本檔，把下方 GIST_RAW_URL 改成你的 Gist raw 連結
   （https://gist.githubusercontent.com/<user>/<gist_id>/raw/usage.json）
3) 鎖屏 widget：長按鎖定畫面 → 自訂 → 鎖定畫面 → 點時鐘下方 widget 區
   → 選 Scriptable → 選此 script（矩形或圓形）
4) 主畫面 widget：長按桌面 → 編輯 → 加入 widget → Scriptable small / medium / large
   → 長按該 widget → 編輯 → Script 選此檔

備註：鎖屏 widget 由 iOS 系統單色渲染，僅以透明度區分層次，屬系統行為。
*/

// ===== 使用者需要改的地方 =====
const GIST_RAW_URL = "https://gist.githubusercontent.com/zhcknck/9585fb88cf03a9982de3e2b9b2fc0299/raw/usage.json";
const STALE_MINUTES = 20;
// 點 widget 開啟的網址（GitHub Pages dashboard），留空 = 開 Scriptable
const DASHBOARD_URL = "https://zhcknck.github.io/cc-usage-relay/";
// 多機時指定要顯示哪台（對應 config 的 machine_name），留空 = 取最新一台
const MACHINE_NAME = "";
// ==============================

const CACHE_FILE = "cc_usage_cache.json";
const HISTORY_CACHE_FILE = "cc_usage_history_cache.json";

// ---------- 配色 ----------

const ORANGE = new Color("#ff9c33");   // 主色：5hr
const TEAL = new Color("#3cc5ae");     // Weekly
const RED = new Color("#ff453a");      // >= 90%
const GRAY = new Color("#98989f");
const TRACK = new Color("#3a3a3c");
const CARD_BG = new Color("#1c1c1e");
const PILL_BG = new Color("#2c2c2e");

function tone(pct, stale) {
  if (stale || pct == null) return GRAY;
  if (pct >= 90) return RED;
  return ORANGE;
}

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
// 只取本視窗內的點，避免跨重置斷崖把斜率拉成負值
function burnEtaMinutes(history, machine, currentP5, resetsAt) {
  if (currentP5 == null) return null;
  const winStart = resetsAt ? new Date(resetsAt).getTime() - 5 * 3600 * 1000 : -Infinity;
  const cutoff = Math.max(Date.now() - 60 * 60 * 1000, winStart);
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

function pctText(pct) {
  return pct == null ? "--" : String(Math.round(pct));
}

// stale 原因（agent 寫進 payload 的 stale_reason）-> 簡短顯示
function staleReason(p) {
  const r = p && p.stale_reason;
  if (!r) return "";
  return String(r).indexOf("token") !== -1 ? "token過期" : "連線失敗";
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

// ---------- 共用元件 ----------

function renderNoData(widget, msg) {
  const t = widget.addText(msg || "無資料");
  t.font = Font.systemFont(12);
  t.textOpacity = 0.8;
}

// 「● Claude Code」標題列 + 右側 $ / STALE 徽章
function addHeader(stack, p, stale, label) {
  const header = stack.addStack();
  header.centerAlignContent();
  const dotT = header.addText("●");
  dotT.font = Font.boldSystemFont(8);
  dotT.textColor = stale ? GRAY : ORANGE;
  header.addSpacer(5);
  const title = header.addText(label);
  title.font = Font.boldSystemFont(12);
  title.textColor = Color.white();
  title.lineLimit = 1;
  header.addSpacer();
  if (extraActive(p)) {
    const dollar = header.addText("$ ");
    dollar.font = Font.boldSystemFont(11);
    dollar.textColor = ORANGE;
  }
  if (stale) {
    const badge = header.addText("STALE");
    badge.font = Font.boldSystemFont(9);
    badge.textColor = GRAY;
  }
  return header;
}

// 大百分比「62%」：數字大、% 小，底部對齊
function addBigPct(stack, pct, color, numSize) {
  const row = stack.addStack();
  row.bottomAlignContent();
  const num = row.addText(pctText(pct));
  num.font = Font.heavySystemFont(numSize);
  num.textColor = color;
  row.addSpacer(2);
  const unit = row.addStack();
  unit.layoutVertically();
  const sym = unit.addText("%");
  sym.font = Font.boldSystemFont(Math.round(numSize * 0.42));
  sym.textColor = color;
  unit.addSpacer(Math.round(numSize * 0.12)); // 讓 % 稍微浮起，貼近基線
  return row;
}

// 灰字 + 亮色時間的兩段文字，例如「重置 3h30m」
function addDuoText(stack, dimText, brightText, size, brightColor) {
  const row = stack.addStack();
  row.bottomAlignContent();
  const a = row.addText(dimText);
  a.font = Font.systemFont(size);
  a.textColor = GRAY;
  const b = row.addText(brightText);
  b.font = Font.boldSystemFont(size);
  b.textColor = brightColor || Color.white();
  return row;
}

// 「標籤 ……… nn%」+ 下方進度條
function addBarGroup(stack, label, sub, pct, color, barWidth, barHeight, stale) {
  const lr = stack.addStack();
  lr.bottomAlignContent();
  const lt = lr.addText(label);
  lt.font = Font.systemFont(10);
  lt.textColor = GRAY;
  lt.lineLimit = 1;
  if (sub) {
    const st = lr.addText(" · " + sub);
    st.font = Font.systemFont(10);
    st.textColor = GRAY;
    st.textOpacity = 0.75;
    st.lineLimit = 1;
  }
  lr.addSpacer();
  const vt = lr.addText(pctText(pct) + "%");
  vt.font = Font.boldSystemFont(12);
  vt.textColor = stale ? GRAY : color;
  stack.addSpacer(4);
  const bar = stack.addImage(drawBar(barWidth, barHeight, barRatio(pct),
    stale ? GRAY : color, TRACK));
  bar.imageSize = new Size(barWidth, barHeight);
  bar.leftAlignImage();
}

// ---------- 鎖屏 family ----------

// (A) 鎖屏矩形：大字 5hr%＋重置倒數恆顯示 + 雙進度條 + 週額度/燒速率
function renderRectangular(widget, p, etaMin) {
  widget.addAccessoryWidgetBackground = true;
  const cc = p && p.claude_code;
  if (!cc) { renderNoData(widget, "CC 無資料"); return; }
  const stale = isStale(p);
  const p5 = pctOf(cc.five_hour);
  const p7 = pctOf(cc.seven_day);

  // 第一行：大字 5hr% + 右側重置倒數（不會被其他資訊擠掉）
  const row1 = widget.addStack();
  row1.bottomAlignContent();
  const big = row1.addText("CC " + pctText(p5) + "%");
  big.font = Font.heavySystemFont(18);
  big.lineLimit = 1;
  row1.addSpacer();
  const rst = row1.addText("↻" + relTime(cc.five_hour && cc.five_hour.resets_at));
  rst.font = Font.boldSystemFont(12);
  rst.textOpacity = 0.85;

  widget.addSpacer(3);
  const bar5 = widget.addImage(drawBar(150, 5, barRatio(p5),
    new Color("#ffffff", 1.0), new Color("#ffffff", 0.28)));
  bar5.imageSize = new Size(150, 5);
  bar5.leftAlignImage();

  widget.addSpacer(3);
  const bar7 = widget.addImage(drawBar(150, 3, barRatio(p7),
    new Color("#ffffff", 0.8), new Color("#ffffff", 0.22)));
  bar7.imageSize = new Size(150, 3);
  bar7.leftAlignImage();

  widget.addSpacer(3);
  let line4;
  if (stale) {
    const sr = staleReason(p);
    line4 = "過期 " + hhmm(p.updated_at) + (sr ? " · " + sr : "");
  } else if (etaMin != null) {
    line4 = "W " + pctText(p7) + "% · 約" + fmtMinutes(etaMin) + "達上限";
  } else {
    line4 = "W " + pctText(p7) + "% · 7日 ↻" + relTime(cc.seven_day && cc.seven_day.resets_at);
  }
  const t4 = widget.addText(line4);
  t4.font = Font.systemFont(11);
  t4.textOpacity = 0.75;
  t4.lineLimit = 1;
}

// (B) 鎖屏圓形：環 + 中央數字 + 重置倒數小字
function renderCircular(widget, p) {
  widget.addAccessoryWidgetBackground = true;
  const cc = p && p.claude_code;
  const p5 = cc ? pctOf(cc.five_hour) : null;
  const stale = isStale(p);
  const reset = cc ? relTime(cc.five_hour && cc.five_hour.resets_at) : "--";
  const img = widget.addImage(drawRing(76, barRatio(p5), pctText(p5), {
    fill: new Color("#ffffff", 1.0),
    track: new Color("#ffffff", 0.25),
    text: Color.white(),
    sub: stale ? "!" : "↻" + reset,
  }));
  img.imageSize = new Size(76, 76);
  img.centerAlignImage();
}

// (C) 鎖屏一行（時鐘上方）
function renderInline(widget, p, etaMin) {
  const cc = p && p.claude_code;
  if (!cc) { widget.addText("CC 無資料"); return; }
  const p5 = pctOf(cc.five_hour);
  const p7 = pctOf(cc.seven_day);
  const stale = isStale(p);
  let text;
  if (stale) {
    text = "⚠ CC " + pctText(p5) + "% · " + hhmm(p.updated_at);
  } else if (etaMin != null) {
    text = "● CC " + pctText(p5) + "% · " + fmtMinutes(etaMin) + " 達上限";
  } else {
    text = "● CC " + pctText(p5) + "% · ↻" + relTime(cc.five_hour && cc.five_hour.resets_at);
  }
  widget.addText(text);
}

// ---------- 主畫面 family（全彩、橘色版）----------

// (D) small：標題 + 大% + 5HR/WK 進度條 + 重置
function renderSmall(widget, p, etaMin) {
  widget.backgroundColor = CARD_BG;
  widget.setPadding(12, 12, 12, 12);
  const cc = p && p.claude_code;
  if (!cc) { renderNoData(widget, "無資料"); return; }
  const stale = isStale(p);
  const p5 = pctOf(cc.five_hour);
  const p7 = pctOf(cc.seven_day);
  const mainColor = tone(p5, stale);
  const weeklyColor = stale ? GRAY : TEAL;

  addHeader(widget, p, stale, "Claude Code");
  widget.addSpacer(2);
  addBigPct(widget, p5, mainColor, 34);
  widget.addSpacer();

  const mkRow = (label, pct, color) => {
    const row = widget.addStack();
    row.centerAlignContent();
    const lt = row.addText(label);
    lt.font = Font.systemFont(9);
    lt.textColor = GRAY;
    row.addSpacer(6);
    const bar = row.addImage(drawBar(104, 5, barRatio(pct), color, TRACK));
    bar.imageSize = new Size(104, 5);
  };
  mkRow("5HR", p5, mainColor);
  widget.addSpacer(6);
  mkRow("WK ", p7, weeklyColor);

  widget.addSpacer(8);
  if (stale) {
    const sr = staleReason(p);
    addDuoText(widget, "更新於 ", hhmm(p.updated_at) + (sr ? " · " + sr : ""), 10, GRAY);
  } else if (etaMin != null) {
    addDuoText(widget, "約 ", fmtMinutes(etaMin) + " 後達上限", 10, ORANGE);
  } else {
    addDuoText(widget, "重置 ", relTime(cc.five_hour && cc.five_hour.resets_at), 10, ORANGE);
  }
}

// (E) medium：左側大% + 燒速率；右側 5hr/Weekly 進度條（含重置時間）
function renderMedium(widget, p, etaMin) {
  widget.backgroundColor = CARD_BG;
  widget.setPadding(14, 16, 12, 16);
  const cc = p && p.claude_code;
  if (!cc) { renderNoData(widget, "無資料"); return; }
  const stale = isStale(p);
  const p5 = pctOf(cc.five_hour);
  const p7 = pctOf(cc.seven_day);
  const mainColor = tone(p5, stale);
  const weeklyColor = stale ? GRAY : TEAL;

  const outer = widget.addStack();
  outer.topAlignContent();

  const left = outer.addStack();
  left.layoutVertically();
  addHeader(left, p, stale, "Claude Code");
  left.addSpacer(8);
  addBigPct(left, p5, mainColor, 36);
  left.addSpacer();
  if (stale) {
    const sr = staleReason(p);
    addDuoText(left, "更新於 ", hhmm(p.updated_at) + (sr ? " · " + sr : ""), 10, GRAY);
  } else if (etaMin != null) {
    addDuoText(left, "約 ", fmtMinutes(etaMin) + " 後達上限", 10, ORANGE);
  } else {
    addDuoText(left, "重置 ", relTime(cc.five_hour && cc.five_hour.resets_at), 10, ORANGE);
  }

  outer.addSpacer(18);

  const right = outer.addStack();
  right.layoutVertically();
  addBarGroup(right, "5hr", "重置 " + relTime(cc.five_hour && cc.five_hour.resets_at),
    p5, mainColor, 176, 6, stale);
  right.addSpacer(10);
  addBarGroup(right, "Weekly", "重置 " + relTime(cc.seven_day && cc.seven_day.resets_at),
    p7, weeklyColor, 176, 6, stale);
  // Opus / Sonnet 有資料才顯示（細條）
  for (const [key, label] of [["seven_day_opus", "Opus"], ["seven_day_sonnet", "Sonnet"]]) {
    const pct = pctOf(cc[key]);
    if (pct == null) continue;
    right.addSpacer(8);
    addBarGroup(right, label, null, pct, weeklyColor, 176, 4, stale);
  }
  right.addSpacer();
  const footer = right.addStack();
  footer.addSpacer();
  const ft = footer.addText((p.machine ? p.machine + " · " : "") + "更新 " + hhmm(p.updated_at));
  ft.font = Font.systemFont(8);
  ft.textColor = GRAY;
  ft.textOpacity = 0.8;
}

// (F) large：標題 + 大% + 全寬進度條 + 重置藥丸 + 燒速率提示框
function renderLarge(widget, p, etaMin) {
  widget.backgroundColor = CARD_BG;
  widget.setPadding(16, 16, 14, 16);
  const cc = p && p.claude_code;
  if (!cc) { renderNoData(widget, "無資料"); return; }
  const stale = isStale(p);
  const p5 = pctOf(cc.five_hour);
  const p7 = pctOf(cc.seven_day);
  const mainColor = tone(p5, stale);
  const weeklyColor = stale ? GRAY : TEAL;
  const W = 304; // 338 - 左右 padding

  const top = widget.addStack();
  top.bottomAlignContent();
  const tl = top.addStack();
  tl.layoutVertically();
  addHeader(tl, p, stale, "Claude Code");
  tl.addSpacer(3);
  const subT = tl.addText(stale ? "資料過期" : "5hr 額度 · 進行中");
  subT.font = Font.systemFont(10);
  subT.textColor = GRAY;
  top.addSpacer();
  addBigPct(top, p5, mainColor, 32);

  widget.addSpacer(14);
  addBarGroup(widget, "5hr", null, p5, mainColor, W, 8, stale);
  widget.addSpacer(12);
  addBarGroup(widget, "Weekly", null, p7, weeklyColor, W, 8, stale);
  for (const [key, label] of [["seven_day_opus", "Weekly · Opus"], ["seven_day_sonnet", "Weekly · Sonnet"]]) {
    const pct = pctOf(cc[key]);
    if (pct == null) continue;
    widget.addSpacer(10);
    addBarGroup(widget, label, null, pct, weeklyColor, W, 6, stale);
  }

  widget.addSpacer(14);
  const pills = widget.addStack();
  pills.centerAlignContent();
  const mkPill = (dim, bright) => {
    const pill = pills.addStack();
    pill.backgroundColor = PILL_BG;
    pill.cornerRadius = 9;
    pill.setPadding(5, 9, 5, 9);
    pill.centerAlignContent();
    const a = pill.addText(dim);
    a.font = Font.systemFont(10);
    a.textColor = GRAY;
    const b = pill.addText(bright);
    b.font = Font.boldSystemFont(10);
    b.textColor = Color.white();
  };
  mkPill("5hr 重置 ", relTime(cc.five_hour && cc.five_hour.resets_at));
  pills.addSpacer(8);
  mkPill("7日 重置 ", relTime(cc.seven_day && cc.seven_day.resets_at));
  pills.addSpacer();

  if (!stale && etaMin != null) {
    widget.addSpacer(12);
    const box = widget.addStack();
    box.backgroundColor = new Color("#ff9c33", 0.14);
    box.cornerRadius = 10;
    box.setPadding(8, 10, 8, 10);
    box.centerAlignContent();
    const dotT = box.addText("● ");
    dotT.font = Font.boldSystemFont(9);
    dotT.textColor = ORANGE;
    const bt = box.addText("依目前速度，約 " + fmtMinutes(etaMin) + " 後達 5hr 上限");
    bt.font = Font.boldSystemFont(11);
    bt.textColor = ORANGE;
    bt.lineLimit = 1;
  }

  widget.addSpacer();
  const footer = widget.addStack();
  footer.addSpacer();
  const ft = footer.addText((p.machine ? p.machine + " · " : "") + "更新 " + hhmm(p.updated_at));
  ft.font = Font.systemFont(9);
  ft.textColor = GRAY;
  ft.textOpacity = 0.8;
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

  // 燒速率（圓形鎖屏放不下，不用多抓一次 history）
  let etaMin = null;
  if (family !== "accessoryCircular" && payload && !isStale(payload)) {
    const cc = payload.claude_code || {};
    etaMin = burnEtaMinutes(await loadHistory(), payload.machine,
      pctOf(cc.five_hour), cc.five_hour && cc.five_hour.resets_at);
  }

  if (family === "accessoryRectangular") {
    renderRectangular(widget, payload, etaMin);
  } else if (family === "accessoryCircular") {
    renderCircular(widget, payload);
  } else if (family === "accessoryInline") {
    renderInline(widget, payload, etaMin);
  } else if (family === "small") {
    renderSmall(widget, payload, etaMin);
  } else if (family === "medium") {
    renderMedium(widget, payload, etaMin);
  } else if (family === "large" || family === "extraLarge") {
    renderLarge(widget, payload, etaMin);
  } else {
    renderNoData(widget, "請使用鎖屏、small / medium / large widget");
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
  } else if (family === "large" || family === "extraLarge") {
    await widget.presentLarge();
  } else {
    await widget.presentSmall();
  }
  Script.complete();
}

await run();
