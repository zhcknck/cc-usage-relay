# cc-usage-relay

Windows agent 讀取本機 Claude Code 用量 → 推到 GitHub Gist →
iPhone Scriptable widget（鎖屏為主）顯示；額度跨越閾值推播通知
（Discord / Bark / ntfy）。另附 GitHub Pages dashboard（含 24h 走勢與燒速率預測）。

```
agent/      agent.py + config.json.example（state.json / agent.log 為執行期產物，不入版控）
scripts/    install_task.ps1 / uninstall_task.ps1（排程）、hook_trigger.cmd（Claude Code hook）
ios/        cc_usage_widget.js（Scriptable：鎖屏 3 種 + small + medium）
dashboard/  index.html（GitHub Pages 用，含 PWA meta 與 icon）
```

資料流：`~/.claude/.credentials.json`（或 WSL 路徑）→ `api.anthropic.com/api/oauth/usage`
→ 白名單 payload（只含百分比與重置時間）→ secret gist
（`usage.json` 多機陣列 + `history.json` 走勢）→ widget / dashboard。

觸發：**Claude Code Stop hook**（每次對話結束即時推，秒級延遲）+
**排程每 5 分鐘**兜底；內建 60 秒節流避免連發。

## 功能

- 鎖屏矩形/圓形/inline + 主畫面 small（彩色環形 gauge）/ medium（四條進度條）
- 點 widget 直接開 dashboard（設 `DASHBOARD_URL`）
- 多級閾值通知（預設 70%/90%，每視窗每級只發一次）＋視窗重置解除通知
- 通知渠道任選：Discord embed / Bark（iOS 原生推播）/ ntfy
- 24 小時用量走勢圖＋燒速率預測（「照此速度 X 後達上限」）
- 多機支援：多台電腦各跑一份 agent，自動合併進同一個 gist
- 自我監控：連續 1 小時拿不到數據、或非官方端點格式變動時主動告警

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

### 3. 通知渠道（三選任意，全部留空 = 不通知）

| 渠道 | 取得方式 | 填入欄位 |
|---|---|---|
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
| `discord_webhook` / `bark_url` / `ntfy_topic` | 步驟 3，可留空 |
| `thresholds_5h_pct` | 5hr 通知閾值列表，預設 `[70, 90]` |
| `thresholds_weekly_pct` | 週額度閾值列表，預設 `[90]` |
| `threshold_5h_pct` / `threshold_weekly_pct` | 舊版單值欄位，列表未填時才生效 |
| `min_interval_seconds` | 節流間隔（hook 連發保護），預設 60 |
| `history_hours` | 走勢保留時數，預設 48 |
| `machine_name` | 顯示在 widget/dashboard 的機器名（多機時各填不同名） |
| `wsl_credentials_path` | credentials 在 WSL 時填 UNC 路徑；本機有檔則留空 |
| `user_agent_version` | usage API 的 User-Agent 版本字串，預設即可 |

### 5. 手動執行一次

```powershell
pip install requests
python agent\agent.py
```

確認 log 顯示「完成」，且 gist 的 `usage.json`、`history.json` 已更新。

### 6. 安裝排程（每 5 分鐘兜底）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
```

移除：`scripts\uninstall_task.ps1`。

### 7. Claude Code 即時 hook（建議）

在 `~/.claude/settings.json` 的 `hooks` 加入（每次 Claude Code 回應結束即時推送）：

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "cmd.exe",
            "args": ["/c", "<repo絕對路徑>\\scripts\\hook_trigger.cmd"],
            "async": true,
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

新開的 Claude Code session 生效。WSL 內的 Claude Code 改用對應的 bash 指令觸發。

### 8. iPhone Scriptable widget

照 `ios/cc_usage_widget.js` 檔案開頭註解操作。檔頭常數：

- `GIST_RAW_URL`：`https://gist.githubusercontent.com/<user>/<gist_id>/raw/usage.json`
- `DASHBOARD_URL`：點 widget 要開的網頁（可留空）
- `MACHINE_NAME`：多機時指定顯示哪台（留空 = 最新一台）

### 9. Dashboard（GitHub Pages，可選）

1. 改 `dashboard/index.html` 內的 `GIST_RAW_URL`
2. repo 推上 GitHub → Settings → Pages → Source 選 `main` + `/dashboard`
3. iPhone Safari「加入主畫面」即得全螢幕 PWA

## 已知限制

- `api.anthropic.com/api/oauth/usage` 為**社群發現的非官方端點**，可能隨時變動
  （格式變動時 agent 會發告警通知）。
- token 過期時 agent 不會自行 refresh，改推 `stale=true` 的舊資料；
  下次在本機使用 Claude Code 時會自動更新 credentials 檔。
- secret gist 是「知道 URL 即可讀」—— payload 僅含百分比與重置時間。
- 鎖屏 widget 為 iOS 系統單色渲染（系統行為）；widget 刷新節奏由 iOS 決定，
  典型 5–15 分鐘，要看即時數據點開 dashboard 或在 Scriptable 內手動執行。
- 多機同時推送為 last-write-wins，極端情況下某機一輪更新被蓋掉，下一輪自癒。

## 驗收清單

1. 手動跑 `agent.py` → Gist 兩檔更新，且內容 grep 不到 `sk-` / `Token` / `Bearer`
2. `thresholds_5h_pct` 暫改 `[1]` → 跑一次收到通知；再跑不重發；改回
3. 備份後把 credentials 的 `expiresAt` 改成過去值 → 跑 agent →
   gist 內該機器 `stale=true` 且沿用上次數據；還原檔案
4. `cc_usage_widget.js` 在 Scriptable 內直接執行 → 預覽正常渲染
5. dashboard 本地開啟 → 卡片、倒數、走勢圖、刷新正常
