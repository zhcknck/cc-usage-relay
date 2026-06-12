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
4) 主畫面 widget：長按桌面 → 編輯 → 加入 widget → Scriptable small
   → 長按該 widget → 編輯 → Script 選此檔

備註：鎖屏 widget 由 iOS 系統單色渲染，僅以透明度區分層次，屬系統行為。
*/

// ===== 使用者唯一需要改的地方 =====
const GIST_RAW_URL = "https://gist.githubusercontent.com/<user>/<id>/raw/usage.json";
const STALE_MINUTES = 20;
// =================================

const CACHE_FILE = "cc_usage_cache.json";

// ---------- 資料 ----------

async function loadPayload() {
  const fm = FileManager.local();
  const cachePath = fm.joinPath(fm.libraryDirectory(), CACHE_FILE);
  try {
    const req = new Request(GIST_RAW_URL + "?t=" + Date.now()); // 破 CDN 快取
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

// ---------- 繪圖 ----------

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

function roundedRect(x, y, w, h, r) {
  const p = new Path();
  p.addRoundedRect(new Rect(x, y, w, h), r, r);
  return p;
}

// 圓環 gauge：以小圓點沿弧線排列近似（DrawContext 無原生弧線 API）
function drawRing(size, ratio, centerText, stale) {
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

  const bg = new Color("#ffffff", 0.25);
  for (let a = 0; a < 360; a += 3) dot(a, bg);
  const sweep = 360 * Math.min(1, ratio);
  const fg = new Color("#ffffff", 1.0);
  for (let a = 0; a <= sweep; a += 2) dot(a, fg); // 順時針

  ctx.setTextAlignedCenter();
  ctx.setTextColor(Color.white());
  if (stale) {
    ctx.setFont(Font.boldSystemFont(size * 0.30));
    ctx.drawTextInRect(centerText, new Rect(0, size * 0.20, size, size * 0.40));
    ctx.setFont(Font.boldSystemFont(size * 0.18));
    ctx.drawTextInRect("!", new Rect(0, size * 0.56, size, size * 0.30));
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

// (A) 鎖屏矩形
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
  const bar = widget.addImage(drawBar(150, 4, barRatio(p5),
    new Color("#ffffff", 1.0), new Color("#ffffff", 0.3)));
  bar.imageSize = new Size(150, 4);
  bar.leftAlignImage();

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
  const img = widget.addImage(drawRing(76, barRatio(p5), text, stale));
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

// (D) 主畫面 small（全彩）
function renderSmall(widget, p) {
  widget.backgroundColor = new Color("#1c1c1e");
  widget.setPadding(12, 12, 12, 12);
  const cc = p && p.claude_code;
  if (!cc) { renderNoData(widget, "無資料"); return; }
  const stale = isStale(p);
  const p5 = pctOf(cc.five_hour);
  const p7 = pctOf(cc.seven_day);

  const gray = new Color("#98989f");
  const tone = (pct) => {
    if (stale || pct == null) return gray;
    if (pct >= 90) return new Color("#ff453a");
    if (pct >= 70) return new Color("#ff9f0a");
    return new Color("#30d158");
  };
  const mainColor = tone(p5);
  const weeklyColor = stale ? gray : new Color("#6e8cae");
  const trackColor = new Color("#3a3a3c");

  const header = widget.addStack();
  header.centerAlignContent();
  const title = header.addText("Claude Code");
  title.font = Font.systemFont(13);
  title.textColor = gray;
  if (stale) {
    header.addSpacer();
    const badge = header.addText("STALE");
    badge.font = Font.boldSystemFont(9);
    badge.textColor = gray;
  }

  widget.addSpacer(4);
  const big = widget.addText((p5 == null ? "--" : Math.round(p5)) + "%");
  big.font = Font.boldSystemFont(34);
  big.textColor = mainColor;

  widget.addSpacer(4);
  const bar5 = widget.addImage(drawBar(130, 6, barRatio(p5), mainColor, trackColor));
  bar5.imageSize = new Size(130, 6);
  bar5.leftAlignImage();

  widget.addSpacer(8);
  const wLabel = widget.addText("Weekly " + (p7 == null ? "--" : Math.round(p7)) + "%");
  wLabel.font = Font.systemFont(11);
  wLabel.textColor = stale ? gray : Color.white();
  widget.addSpacer(3);
  const bar7 = widget.addImage(drawBar(130, 6, barRatio(p7), weeklyColor, trackColor));
  bar7.imageSize = new Size(130, 6);
  bar7.leftAlignImage();

  widget.addSpacer();
  const foot = widget.addText(stale
    ? "更新於 " + hhmm(p.updated_at)
    : "↻ 5hr " + relTime(cc.five_hour && cc.five_hour.resets_at) +
      " · 7d " + relTime(cc.seven_day && cc.seven_day.resets_at));
  foot.font = Font.systemFont(9);
  foot.textColor = gray;
}

// ---------- 主流程 ----------

async function run() {
  const payload = await loadPayload();
  const family = config.widgetFamily || "small"; // App 內預覽預設 small
  const widget = new ListWidget();
  widget.refreshAfterDate = new Date(Date.now() + 5 * 60 * 1000); // 建議值，iOS 自行調度

  if (family === "accessoryRectangular") {
    renderRectangular(widget, payload);
  } else if (family === "accessoryCircular") {
    renderCircular(widget, payload);
  } else if (family === "accessoryInline") {
    renderInline(widget, payload);
  } else if (family === "small") {
    renderSmall(widget, payload);
  } else {
    renderNoData(widget, "請使用鎖屏或 small widget");
  }

  if (config.runsInWidget) {
    Script.setWidget(widget);
  } else if (family === "accessoryRectangular") {
    await widget.presentAccessoryRectangular();
  } else if (family === "accessoryCircular") {
    await widget.presentAccessoryCircular();
  } else if (family === "accessoryInline") {
    await widget.presentAccessoryInline();
  } else {
    await widget.presentSmall();
  }
  Script.complete();
}

await run();
