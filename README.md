# cc-usage-relay

Windows agent 讀取本機 Claude Code 用量（可多帳號）→ 推到 GitHub Gist →
iPhone Scriptable widget（鎖屏＋主畫面）顯示；額度跨越閾值推播通知
（Discord / Bark / ntfy / Windows toast）。另附 GitHub Pages dashboard
（24h 走勢、5hr 燒速率與週額度耗盡預測）。**零第三方依賴**（僅 Python 標準庫）。

```
agent/      agent.py + config.json.example（state.json / agent.log / agent.lock 為執行期產物，不入版控）
scripts/    install_task.ps1 / uninstall_task.ps1（排程）、install_hook.py + hook_trigger.cmd（Claude Code hook）
ios/        cc_usage_widget.js（Scriptable：鎖屏 3 種 + small / medium / large，橘色主題）
docs/       dashboard（GitHub Pages 從 /docs 發佈，含 PWA 殼層）
```

資料流：`~/.claude/.credentials.json`（或 accounts 副本 / WSL 路徑）
→ `api.anthropic.com/api/oauth/usage` → 白名單 payload（只含百分比與重置時間）
→ secret gist（`usage.json` 多機×多帳號陣列 + `history.json` 走勢）→ widget / dashboard。

觸發：**Claude Code Stop hook**（每次對話結束即時推，秒級延遲）+
**排程每 5 分鐘**兜底；內建 60 秒節流 + `agent.lock` 行程鎖防並行。

## 功能

- 鎖屏矩形/圓形/inline + 主畫面 small / medium / large（橘色主題，≥90% 轉紅）
- 點 widget 直接開 dashboard（`DASHBOARD_URL`）
- 多級閾值通知（預設 70%/90%，每視窗每級只發一次）＋視窗重置解除通知
- 通知渠道任選並存：Discord embed / Bark（iOS 推播）/ ntfy / **Windows toast**（零設定）
- 5hr 燒速率預測（「照此速度 X 後達上限」，視窗感知不受重置斷崖干擾）
- **週額度耗盡預測**（dashboard：「照此速度，週額度約 X 後耗盡」）
- 每日用量摘要通知（`daily_summary_hour`，預設關閉）
- **多帳號**：同一台機器追蹤多個 Claude 帳號，副本 token 過期自動續期
- 多機支援：多台電腦各跑一份 agent，自動合併進同一個 gist
- stale 標記附原因（token 過期 / 連線失敗），widget 與 dashboard 都會顯示
- 自我監控：帳號連續 1 小時拿不到數據、或非官方端點格式變動時主動告警

## 安裝步驟

### 1. 建立 secret gist

1. 到 <https://gist.github.com> 建立 **secret** gist
2. 檔名填 `usage.json`，內容先放 `{}`
3. 從網址記下 `gist_id`（`https://gist.github.com/<user>/<gist_id>`）
   （`history.json` 會由 agent 自動建立，不用手動加）

### 2. 建立 GitHub token

- Fine-grained：<https://github.com/settings/personal-access-tokens> →
  Account permissions → **Gists: Read and write**（其餘全不勾）
- 或 classic：<https://github.com/settings/tokens> → 只勾 **gist** scope

### 3. 通知渠道（任選並存，全部留空 = 不通知）

| 渠道 | 取得方式 | 填入欄位 |
|---|---|---|
| Windows toast | 免設定，本機原生通知 | `windows_toast: true` |
| Discord | 頻道設定 → 整合 → Webhook → 複製 URL | `discord_webhook` |
| Bark（iOS 原生推播，推薦） | App Store 裝 Bark → 開啟 App 複製形如 `https://api.day.app/<key>` 的網址 | `bark_url` |
| ntfy | 想一個獨特 topic 名稱，手機裝 ntfy App 訂閱該 topic | `ntfy_topic` |

### 4. 填設定檔

```powershell
cd cc-usage-relay\agent
Copy-Item config.json.example config.json
notepad config.json
```

| 欄位 | 說明 |
|---|---|
| `gist_id` / `github_token` | 步驟 1、2 |
| `discord_webhook` / `bark_url` / `ntfy_topic` / `windows_toast` | 步驟 3，可留空/false |
| `thresholds_5h_pct` | 5hr 通知閾值列表，預設 `[70, 90]` |
| `thresholds_weekly_pct` | 週額度閾值列表，預設 `[90]` |
| `threshold_5h_pct` / `threshold_weekly_pct` | 舊版單值欄位，列表未填時才生效 |
| `min_interval_seconds` | 節流間隔（hook 連發保護），預設 60 |
| `history_hours` | 走勢保留時數，預設 48 |
| `machine_ttl_hours` | 其他機器條目幾小時沒更新就從 gist 淘汰，預設 48 |
| `daily_summary_hour` | 每日摘要通知的整點（0–23），`null` = 關閉 |
| `machine_name` | 顯示在 widget/dashboard 的機器名（多機時各填不同名） |
| `wsl_credentials_path` | credentials 在 WSL 時填 UNC 路徑；本機有檔則留空 |
| `accounts` | 多帳號設定，見下節；留 `[]` = 只追蹤本機登入帳號 |
| `user_agent_version` | usage API 的 User-Agent 版本字串，預設即可 |

### 5. 手動執行一次

```powershell
python agent\agent.py
```

確認 log 顯示「完成」，且 gist 的 `usage.json`、`history.json` 已更新。
測通知渠道：`python agent\agent.py test-notify`。

### 6. 安裝排程（每 5 分鐘兜底）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
```

移除：`scripts\uninstall_task.ps1`。

### 7. Claude Code 即時 hook（建議）

```powershell
python scripts\install_hook.py
```

冪等腳本：自動把 Stop hook 合併進 `~/.claude/settings.json`（寫入前備份），
之後每次 Claude Code 對話結束即時推送用量。新開的 CC session 生效。
WSL 內的 Claude Code 需另行配置對應的 bash 觸發指令。

### 8. iPhone Scriptable widget

照 `ios/cc_usage_widget.js` 檔案開頭註解操作。檔頭常數：

- `GIST_RAW_URL`：`https://gist.githubusercontent.com/<user>/<gist_id>/raw/usage.json`
- `DASHBOARD_URL`：點 widget 要開的網頁（可留空）
- `MACHINE_NAME`：多機/多帳號時指定顯示哪個來源（留空 = 最新一個）

### 9. Dashboard（GitHub Pages，可選）

1. 改 `docs/index.html` 內的 `GIST_RAW_URL`
2. repo 推上 GitHub → Settings → Pages → Source 選預設分支 + `/docs` 資料夾
3. iPhone Safari「加入主畫面」即得全螢幕 PWA

## 多帳號

同一台機器追蹤兩三個 Claude 帳號的原理：Claude Code 一次只登入一個帳號
（`~/.claude/.credentials.json` 會被切換覆寫），所以其他帳號要用 **credentials 副本**：

1. 在 Claude Code 登入帳號 B → 複製 `%USERPROFILE%\.claude\.credentials.json`
   到 `agent\accounts\b.credentials.json`（`accounts/` 已在 .gitignore）
2. 切回主帳號，config 填：

```json
"accounts": [
  { "name": "",  "credentials_path": "", "auto_refresh": false },
  { "name": "B", "credentials_path": "C:\\\\...\\\\agent\\\\accounts\\\\b.credentials.json", "auto_refresh": true }
]
```

- 第一條是預設帳號（本機 `~/.claude`，由 Claude Code 自己刷新 token，agent 不碰）
- 副本帳號設 `auto_refresh: true`：token 過期時 agent 用 refresh token
  自動續期並寫回副本檔，長期免維護
- 每個帳號在 gist 是獨立條目，名稱為 `機器名·帳號名`（如 `ZHCK·B`）；
  dashboard 一帳號一卡片，widget 用 `MACHINE_NAME` 挑選或自動取最新
- 通知去重按帳號獨立計算，內文會標註來源名稱

## 已知限制

- `api.anthropic.com/api/oauth/usage` 為**社群發現的非官方端點**，可能隨時變動
  （格式變動時 agent 會發告警通知）。token 續期端點同屬非官方行為。
- 主帳號 token 過期時 agent 不會代刷（避免與 Claude Code 互搶），改推
  `stale=true` + 原因；下次在本機使用 Claude Code 時自動恢復。
- secret gist 是「知道 URL 即可讀」—— payload 僅含百分比與重置時間。
- 鎖屏 widget 為 iOS 系統單色渲染（系統行為）；widget 刷新節奏由 iOS 決定，
  典型 5–15 分鐘，要看即時數據點開 dashboard 或在 Scriptable 內手動執行。
- 多機同時推送為 last-write-wins，極端情況下某機一輪更新被蓋掉，下一輪自癒。
- 多機/多帳號共用同一個 Claude 帳號時額度是帳號級的——同帳號只在一處填
  通知渠道，避免重複警告（通知內文會標註來源）。
- `scripts/*.ps1` 含中文，必須存成 **UTF-8 with BOM**（Windows PowerShell 5.1
  對無 BOM 檔以 ANSI 解析會炸）；`hook_trigger.cmd` 則必須純 ASCII。

## 驗收清單

1. 手動跑 `agent.py` → Gist 兩檔更新，且內容 grep 不到 `sk-` / `Token` / `Bearer`
2. `python agent\agent.py test-notify` → 已設定的渠道各收到一則測試通知
3. `thresholds_5h_pct` 暫改 `[1]` → 跑一次收到通知；再跑不重發；改回
4. 把某帳號 credentials 的 `expiresAt` 改成過去值（副本帳號直接改副本檔）→
   跑 agent → gist 該條目 `stale=true` 且帶 `stale_reason`；還原
5. `cc_usage_widget.js` 在 Scriptable 內直接執行 → 預覽正常渲染
6. dashboard 本地開啟 → 卡片、倒數、走勢圖、刷新正常
