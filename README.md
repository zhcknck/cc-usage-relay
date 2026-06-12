# cc-usage-relay

Windows 排程 agent 讀取本機 Claude Code 用量 → 推到 GitHub Gist →
iPhone Scriptable widget（鎖屏為主）顯示；額度超閾值推 Discord 通知。
另附 GitHub Pages dashboard。

```
agent/      agent.py + config.json.example（state.json / agent.log 為執行期產物，不入版控）
scripts/    install_task.ps1 / uninstall_task.ps1（Windows 排程）
ios/        cc_usage_widget.js（Scriptable widget）
dashboard/  index.html（GitHub Pages 用，含 PWA meta 與 icon）
```

資料流：`~/.claude/.credentials.json`（或 WSL 路徑）→ `api.anthropic.com/api/oauth/usage`
→ 白名單 payload（只含百分比與重置時間）→ secret gist `usage.json` → widget / dashboard。

## 安裝步驟

### 1. 建立 secret gist

1. 到 <https://gist.github.com> 建立 **secret** gist
2. 檔名填 `usage.json`，內容先放 `{}`
3. 建立後從網址記下 `gist_id`（`https://gist.github.com/<user>/<gist_id>`）

### 2. 建立 GitHub token

- Fine-grained token：<https://github.com/settings/personal-access-tokens> →
  Account permissions → **Gists: Read and write**（其餘全不勾）
- 或 classic token：<https://github.com/settings/tokens> → 只勾 **gist** scope

### 3. 建立 Discord webhook

Discord 伺服器 → 頻道設定 → 整合 → Webhook → 新增 → 複製 webhook URL。
（不想要通知可留空，agent 只會記 log。）

### 4. 填設定檔

```powershell
cd cc-usage-relay\agent
Copy-Item config.json.example config.json
notepad config.json
```

| 欄位 | 說明 |
|---|---|
| `gist_id` | 步驟 1 的 gist id |
| `github_token` | 步驟 2 的 token（僅 gist 權限） |
| `discord_webhook` | 步驟 3 的 URL，可留空 |
| `threshold_5h_pct` / `threshold_weekly_pct` | 通知閾值（%），預設 90 |
| `machine_name` | 顯示在 widget/dashboard 的機器名 |
| `wsl_credentials_path` | credentials 在 WSL 時填 UNC 路徑，如 `\\wsl$\Ubuntu\home\<user>\.claude\.credentials.json`；本機有檔則留空 |
| `user_agent_version` | usage API 的 User-Agent 版本字串，預設即可 |

### 5. 手動執行一次

```powershell
pip install requests
python agent\agent.py
```

確認 console / `agent\agent.log` 顯示「完成」，且 gist 的 `usage.json` 內容已更新。

### 6. 安裝排程（每 5 分鐘）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
```

腳本會自動偵測 `pythonw`、建立排程 `CCUsageRelay` 並立即觸發一次。
移除：`scripts\uninstall_task.ps1`。

### 7. iPhone Scriptable widget

照 `ios/cc_usage_widget.js` 檔案開頭註解操作：安裝 Scriptable → 貼入腳本 →
改 `GIST_RAW_URL`（gist 頁面點 Raw，去掉網址中的 commit hash 取
`https://gist.githubusercontent.com/<user>/<gist_id>/raw/usage.json`）→
加到鎖屏（矩形/圓形/inline）或主畫面（small）。

### 8. Dashboard（GitHub Pages，可選）

1. 把本 repo 推上 GitHub
2. 改 `dashboard/index.html` 內的 `GIST_RAW_URL`
3. repo Settings → Pages → Source 選 `main` branch、資料夾選 `/dashboard`
4. iPhone Safari 開啟後「加入主畫面」即得全螢幕 PWA

## 已知限制

- `api.anthropic.com/api/oauth/usage` 為**社群發現的非官方端點**，可能隨時變動或失效。
- token 過期時 agent 不會自行 refresh，改推 `stale=true` 的舊資料；
  下次在本機使用 Claude Code 時會自動更新 credentials 檔。
- secret gist 是「知道 URL 即可讀」，並非真正私有 —— 因此 payload 僅含
  百分比與重置時間，不含任何 token 或帳號資訊。
- 鎖屏 widget 為 iOS 系統單色渲染，無法自訂色彩（系統行為）。

## 驗收清單

1. 手動跑 `agent.py` → Gist 更新，且輸出 JSON grep 不到 `sk-` / `Token` / `Bearer`
2. `threshold_5h_pct` 暫調 1 → 跑一次收到 Discord 通知；再跑不重發；改回 90
3. 備份後把 credentials 的 `expiresAt` 改成過去值 → 跑 agent →
   gist 內 `stale=true` 且沿用上次數據；還原檔案
4. `cc_usage_widget.js` 在 Scriptable 內直接執行 → 預覽正常渲染
5. dashboard 本地開啟 → 卡片、倒數、60 秒刷新正常
