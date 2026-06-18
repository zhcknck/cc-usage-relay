// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: orange; icon-glyph: tachometer-alt;
//
// cc-usage-relay loader — 永遠抓 GitHub 上最新的 widget 來跑，改完免重貼。
// 只需貼這一次。離線/抓失敗時自動用上次成功的快取。
//
// 用法：在 Scriptable 新增一支 script，整段貼入並存成 widget 的執行檔即可。
// 之後 push 到 repo，widget 下次刷新就會抓到新版（GitHub raw 有 ~5 分鐘 CDN 快取）。

const SRC_URL = "https://raw.githubusercontent.com/zhcknck/cc-usage-relay/master/ios/cc_usage_widget.js";
const CACHE = "cc_usage_widget_src.js";

const fm = FileManager.local();
const path = fm.joinPath(fm.cacheDirectory(), CACHE);

let code;
try {
  const req = new Request(SRC_URL);
  req.timeoutInterval = 10;
  code = await req.loadString();
  if (req.response.statusCode === 200 && code && code.length > 500) {
    fm.writeString(path, code);          // 抓成功才覆寫快取
  } else {
    throw new Error("bad response " + req.response.statusCode);
  }
} catch (e) {
  if (fm.fileExists(path)) {
    code = fm.readString(path);           // 抓失敗 → 用上次快取
  } else {
    throw e;                              // 第一次就失敗、無快取可退
  }
}

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
await new AsyncFunction(code)();
