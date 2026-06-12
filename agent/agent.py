# -*- coding: utf-8 -*-
"""cc-usage-relay agent

讀取 Claude Code OAuth credentials -> 呼叫 usage 端點 -> 白名單 payload 推 GitHub Gist
-> 額度超閾值推 Discord 通知（每個重置視窗只發一次）。

設計為 Windows 工作排程器每 5 分鐘執行一次；所有路徑以本檔所在目錄解析。
依賴：requests（pip install requests）。
"""

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "agent.log"
LOG_MAX_BYTES = 1024 * 1024

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
GIST_API = "https://api.github.com/gists/{gist_id}"
HTTP_TIMEOUT = 10
RETRY_DELAYS = (30, 60, 120)  # 429 退避秒數

TZ_LOCAL = timezone(timedelta(hours=8))  # 輸出 updated_at 用 +08:00

log = logging.getLogger("cc-usage-relay")


def setup_logging():
    # 超過 1MB 截斷重寫
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            LOG_PATH.unlink()
    except OSError:
        pass
    handlers = [logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")]
    if sys.stdout is not None:  # pythonw 下 stdout 為 None
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def now_iso():
    return datetime.now(TZ_LOCAL).isoformat(timespec="seconds")


def load_config():
    if not CONFIG_PATH.exists():
        log.error("config.json 不存在，請複製 config.json.example 後填值")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in ("gist_id", "github_token") if not cfg.get(k)]
    if missing:
        log.error("config.json 缺少必填欄位: %s", ", ".join(missing))
        sys.exit(1)
    cfg.setdefault("discord_webhook", "")
    cfg.setdefault("threshold_5h_pct", 90)
    cfg.setdefault("threshold_weekly_pct", 90)
    cfg.setdefault("machine_name", "PC")
    cfg.setdefault("wsl_credentials_path", "")
    cfg.setdefault("user_agent_version", "2.0.0")
    return cfg


def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
            if isinstance(state, dict):
                return state
        except (OSError, ValueError):
            log.warning("state.json 無法解析，視為空白狀態")
    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_credentials(cfg):
    """回傳 (access_token, expires_at_epoch_seconds)；找不到檔案則 exit 1。"""
    candidates = [Path.home() / ".claude" / ".credentials.json"]
    wsl_path = (cfg.get("wsl_credentials_path") or "").strip()
    if wsl_path:
        candidates.append(Path(wsl_path))

    for path in candidates:
        try:
            if not path.exists():
                continue
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("credentials 讀取失敗 (%s): %s", path, e)
            continue
        oauth = data.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        expires_ms = oauth.get("expiresAt")
        if not token or not expires_ms:
            log.warning("credentials 缺少 claudeAiOauth.accessToken/expiresAt (%s)", path)
            continue
        return token, float(expires_ms) / 1000.0  # epoch 毫秒 -> 秒

    log.error("找不到可用的 credentials（已嘗試 %d 個路徑）", len(candidates))
    sys.exit(1)


def fetch_usage(token, cfg):
    """成功回傳 usage dict；429 退避重試後仍失敗、401 或其他例外回傳 None（走 stale 路徑）。"""
    headers = {
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/" + str(cfg["user_agent_version"]),
    }
    attempts = 1 + len(RETRY_DELAYS)
    for i in range(attempts):
        try:
            resp = requests.get(USAGE_URL, headers=headers, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            log.warning("usage 端點連線失敗: %s", type(e).__name__)
            return None
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                log.warning("usage 回應非 JSON")
                return None
        if resp.status_code == 429:
            if i < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[i]
                log.warning("usage 429，%d 秒後重試", delay)
                time.sleep(delay)
                continue
            log.warning("usage 429，重試次數用盡")
            return None
        if resp.status_code == 401:
            log.warning("usage 401（token 無效或已撤銷）")
            return None
        log.warning("usage 端點回應 HTTP %d", resp.status_code)
        return None
    return None


def _pick_window(block):
    """白名單欄位：只取 utilization 與 resets_at。"""
    if not isinstance(block, dict):
        return None
    return {"pct": block.get("utilization"), "resets_at": block.get("resets_at")}


def build_payload(cfg, usage):
    return {
        "schema": 1,
        "machine": cfg["machine_name"],
        "updated_at": now_iso(),
        "stale": False,
        "claude_code": {
            "five_hour": _pick_window(usage.get("five_hour")),
            "seven_day": _pick_window(usage.get("seven_day")),
            "seven_day_opus": _pick_window(usage.get("seven_day_opus")),
            "seven_day_sonnet": _pick_window(usage.get("seven_day_sonnet")),
        },
    }


def scan_for_secrets(payload_text, secrets):
    """輸出前自我檢查：通用標記 + 實際機密值，任何命中都不得推送。"""
    hits = []
    for marker in ("sk-", "Token", "Bearer"):
        if marker in payload_text:
            hits.append("marker:" + marker)
    for name, value in secrets:
        if value and value in payload_text:
            hits.append("secret:" + name)
    return hits


def push_gist(cfg, payload_text):
    url = GIST_API.format(gist_id=cfg["gist_id"])
    headers = {
        "Authorization": "Bearer " + cfg["github_token"],
        "Accept": "application/vnd.github+json",
    }
    body = {"files": {"usage.json": {"content": payload_text}}}
    resp = requests.patch(url, headers=headers, json=body, timeout=HTTP_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError("Gist 更新失敗 HTTP %d" % resp.status_code)


def relative_time(resets_at_iso):
    """resets_at(UTC ISO8601) -> 與現在的差值：<60m '33m'；<24h '3h20m'；其餘 '2d22h'。"""
    try:
        target = datetime.fromisoformat(str(resets_at_iso).replace("Z", "+00:00"))
    except ValueError:
        return "?"
    mins = int((target - datetime.now(timezone.utc)).total_seconds() // 60)
    if mins <= 0:
        return "0m"
    if mins < 60:
        return "%dm" % mins
    hours, m = divmod(mins, 60)
    if mins < 1440:
        return "%dh%dm" % (hours, m) if m else "%dh" % hours
    days, h = divmod(hours, 24)
    return "%dd%dh" % (days, h) if h else "%dd" % days


def send_discord(webhook, content):
    resp = requests.post(webhook, json={"content": content}, timeout=HTTP_TIMEOUT)
    if resp.status_code not in (200, 204):
        raise RuntimeError("Discord webhook HTTP %d" % resp.status_code)


def process_notifications(cfg, state, payload):
    """超閾值通知 + resets_at 去重；視窗重置自然解鎖並清除舊 key。"""
    webhook = cfg.get("discord_webhook") or ""
    cc = payload.get("claude_code") or {}
    rules = [
        ("five_hour", float(cfg["threshold_5h_pct"]), "notified_5h_resets_at",
         "⚠️ Claude Code 5hr 額度 {pct}%，重置於 {rel}"),
        ("seven_day", float(cfg["threshold_weekly_pct"]), "notified_7d_resets_at",
         "⚠️ Claude Code 週額度 {pct}%，重置於 {rel}"),
    ]
    for window, threshold, state_key, template in rules:
        block = cc.get(window) or {}
        pct = block.get("pct")
        resets_at = block.get("resets_at")
        if pct is None or not resets_at:
            continue
        # 視窗已重置（resets_at 改變）-> 清除舊 key，自然解鎖
        if state.get(state_key) and state[state_key] != resets_at:
            state.pop(state_key, None)
        if float(pct) < threshold or state.get(state_key) == resets_at:
            continue
        if not webhook:
            log.warning("%s 超過閾值但未設定 discord_webhook", window)
            continue
        text = template.format(pct=round(float(pct)), rel=relative_time(resets_at))
        try:
            send_discord(webhook, text)
        except (requests.RequestException, RuntimeError) as e:
            log.warning("Discord 通知失敗（下次再試）: %s", e)
            continue
        state[state_key] = resets_at
        log.info("已發送 %s 通知（resets_at=%s）", window, resets_at)


def run_stale(cfg, state, reason):
    """token 過期或 API 失敗：沿用 last_payload 標 stale 推 Gist，不發通知。"""
    last = state.get("last_payload")
    if isinstance(last, dict):
        payload = dict(last)
    else:
        log.warning("無 last_payload 可沿用，推送空白 stale payload")
        payload = {"claude_code": None}
    payload["schema"] = 1
    payload["machine"] = cfg["machine_name"]
    payload["updated_at"] = now_iso()
    payload["stale"] = True

    payload_text = json.dumps(payload, ensure_ascii=False, indent=2)
    hits = scan_for_secrets(payload_text, [("github_token", cfg["github_token"]),
                                           ("discord_webhook", cfg.get("discord_webhook"))])
    if hits:
        log.error("stale payload 含機密標記，拒絕推送: %s", hits)
        sys.exit(1)
    try:
        push_gist(cfg, payload_text)
    except (requests.RequestException, RuntimeError) as e:
        log.error("stale 路徑 Gist 推送失敗: %s", e)
        sys.exit(1)
    log.info("stale payload 已推送（原因：%s）", reason)
    sys.exit(0)


def main():
    setup_logging()
    cfg = load_config()
    state = load_state()
    token, expires_at = load_credentials(cfg)

    if expires_at <= time.time():
        # token 過期：不打 API，Claude Code 下次被使用時會自行 refresh 該檔案
        run_stale(cfg, state, "token 已過期")

    usage = fetch_usage(token, cfg)
    if usage is None:
        run_stale(cfg, state, "usage 端點失敗")

    payload = build_payload(cfg, usage)
    payload_text = json.dumps(payload, ensure_ascii=False, indent=2)

    hits = scan_for_secrets(payload_text, [
        ("access_token", token),
        ("github_token", cfg["github_token"]),
        ("discord_webhook", cfg.get("discord_webhook")),
    ])
    if hits:
        log.error("payload 含機密標記，拒絕推送: %s", hits)
        sys.exit(1)

    try:
        push_gist(cfg, payload_text)
    except (requests.RequestException, RuntimeError) as e:
        log.error("Gist 推送失敗: %s", e)
        state["last_payload"] = payload
        save_state(state)
        sys.exit(1)

    process_notifications(cfg, state, payload)

    state["last_payload"] = payload
    save_state(state)
    log.info("完成：5hr=%s%% 7d=%s%%",
             (payload["claude_code"]["five_hour"] or {}).get("pct"),
             (payload["claude_code"]["seven_day"] or {}).get("pct"))


if __name__ == "__main__":
    main()
