# -*- coding: utf-8 -*-
"""cc-usage-relay agent

讀取 Claude Code OAuth credentials -> 呼叫 usage 端點 -> 白名單 payload 推 GitHub Gist
（usage.json 為多機陣列、history.json 為走勢紀錄）-> 額度跨越閾值時推播通知
（Discord embed / Bark / ntfy，每個重置視窗每個閾值只發一次，視窗重置發解除通知）。

觸發來源：Windows 排程（每 5 分鐘兜底）+ Claude Code Stop hook（即時）。
內建節流（min_interval_seconds）避免 hook 連發打爆 API。
所有路徑以本檔所在目錄解析。依賴：requests。
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

COLOR_RED = 0xFF453A
COLOR_ORANGE = 0xFF9F0A
COLOR_GREEN = 0x30D158

# extra_usage 僅放行這些 key 的純量值
EXTRA_ALLOWED = ("is_enabled", "enabled", "monthly_limit", "used_credits",
                 "used", "utilization", "amount", "limit", "remaining")

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
    cfg.setdefault("bark_url", "")
    cfg.setdefault("ntfy_topic", "")
    cfg.setdefault("threshold_5h_pct", 90)
    cfg.setdefault("threshold_weekly_pct", 90)
    cfg.setdefault("machine_name", "PC")
    cfg.setdefault("wsl_credentials_path", "")
    cfg.setdefault("user_agent_version", "2.0.0")
    cfg.setdefault("min_interval_seconds", 60)
    cfg.setdefault("history_hours", 48)
    return cfg


def get_thresholds(cfg, list_key, single_key):
    """thresholds_*（列表）優先；沒填則退回單值 threshold_*。"""
    lv = cfg.get(list_key)
    if isinstance(lv, list) and lv:
        return sorted(float(x) for x in lv)
    return [float(cfg.get(single_key, 90))]


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


def usage_schema_ok(usage):
    """端點格式驗證：five_hour.utilization 數值 + resets_at 字串才視為有效。"""
    if not isinstance(usage, dict):
        return False
    fh = usage.get("five_hour")
    return (isinstance(fh, dict)
            and isinstance(fh.get("utilization"), (int, float))
            and isinstance(fh.get("resets_at"), str))


def _pick_window(block):
    """白名單欄位：只取 utilization 與 resets_at。"""
    if not isinstance(block, dict):
        return None
    return {"pct": block.get("utilization"), "resets_at": block.get("resets_at")}


def _pick_extra(block):
    """extra_usage 白名單：只放行已知 key 的純量值。"""
    if not isinstance(block, dict):
        return None
    out = {k: v for k, v in block.items()
           if k in EXTRA_ALLOWED and isinstance(v, (int, float, bool))}
    return out or None


def build_payload(cfg, usage):
    return {
        "schema": 2,
        "machine": cfg["machine_name"],
        "updated_at": now_iso(),
        "stale": False,
        "claude_code": {
            "five_hour": _pick_window(usage.get("five_hour")),
            "seven_day": _pick_window(usage.get("seven_day")),
            "seven_day_opus": _pick_window(usage.get("seven_day_opus")),
            "seven_day_sonnet": _pick_window(usage.get("seven_day_sonnet")),
        },
        "extra_usage": _pick_extra(usage.get("extra_usage")),
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


def secret_pairs(cfg, token=None):
    pairs = [
        ("github_token", cfg.get("github_token")),
        ("discord_webhook", cfg.get("discord_webhook")),
        ("bark_url", cfg.get("bark_url")),
    ]
    if token:
        pairs.append(("access_token", token))
    return pairs


def gist_headers(cfg):
    return {
        "Authorization": "Bearer " + cfg["github_token"],
        "Accept": "application/vnd.github+json",
    }


def fetch_gist_files(cfg):
    """讀回 gist 現有內容（多機合併用）。回傳 (usage_entries, history_entries)；失敗回 ([], [])。"""
    try:
        resp = requests.get(GIST_API.format(gist_id=cfg["gist_id"]),
                            headers=gist_headers(cfg), timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            log.warning("gist 讀取失敗 HTTP %d（以單機模式覆寫）", resp.status_code)
            return [], []
        files = resp.json().get("files") or {}
    except (requests.RequestException, ValueError) as e:
        log.warning("gist 讀取失敗: %s（以單機模式覆寫）", type(e).__name__)
        return [], []

    def parse(name):
        f = files.get(name)
        if not isinstance(f, dict):
            return []
        content = f.get("content")
        if f.get("truncated") and f.get("raw_url"):
            try:
                content = requests.get(f["raw_url"], timeout=HTTP_TIMEOUT).text
            except requests.RequestException:
                return []
        try:
            data = json.loads(content or "null")
        except ValueError:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and data:
            return [data]  # 相容舊版單物件格式
        return []

    return parse("usage.json"), parse("history.json")


def merge_machines(existing, own):
    """自己的 payload 取代同名機器；其他機器 48 小時內的資料保留。"""
    merged = [own]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    for entry in existing:
        if not isinstance(entry, dict) or entry.get("machine") == own["machine"]:
            continue
        try:
            ts = datetime.fromisoformat(str(entry.get("updated_at")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts >= cutoff:
            merged.append(entry)
    merged.sort(key=lambda e: str(e.get("machine")))
    return merged


def append_history(cfg, history, payload):
    """走勢紀錄：每筆只存 機器/時間/5hr%/週%；裁掉視窗外與超量的舊資料。"""
    cc = payload.get("claude_code") or {}
    entry = {
        "m": payload["machine"],
        "t": payload["updated_at"],
        "h5": (cc.get("five_hour") or {}).get("pct"),
        "d7": (cc.get("seven_day") or {}).get("pct"),
    }
    cutoff = datetime.now(TZ_LOCAL) - timedelta(hours=float(cfg["history_hours"]))

    def keep(e):
        if not isinstance(e, dict):
            return False
        try:
            return datetime.fromisoformat(str(e.get("t")).replace("Z", "+00:00")) >= cutoff
        except ValueError:
            return False

    out = [e for e in history if keep(e)]
    out.append(entry)
    out.sort(key=lambda e: str(e.get("t")))
    return out[-2000:]


def push_gist(cfg, files):
    """files: {檔名: 內容字串}；省略的檔案保持不變。"""
    url = GIST_API.format(gist_id=cfg["gist_id"])
    body = {"files": {name: {"content": text} for name, text in files.items()}}
    resp = requests.patch(url, headers=gist_headers(cfg), json=body, timeout=HTTP_TIMEOUT)
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


def notify_all(cfg, title, body, color):
    """推到所有已設定渠道；任何一個成功即回 True。全部未設定則記 log 並視為已處理。"""
    attempted = delivered = False

    webhook = (cfg.get("discord_webhook") or "").strip()
    if webhook:
        attempted = True
        try:
            resp = requests.post(webhook, json={
                "embeds": [{"title": title, "description": body, "color": color}],
            }, timeout=HTTP_TIMEOUT)
            if resp.status_code in (200, 204):
                delivered = True
            else:
                log.warning("Discord 通知失敗 HTTP %d", resp.status_code)
        except requests.RequestException as e:
            log.warning("Discord 通知失敗: %s", type(e).__name__)

    bark = (cfg.get("bark_url") or "").strip().rstrip("/")
    if bark:
        attempted = True
        try:
            resp = requests.post(bark, json={
                "title": title, "body": body, "group": "cc-usage-relay",
            }, timeout=HTTP_TIMEOUT)
            if resp.status_code == 200:
                delivered = True
            else:
                log.warning("Bark 通知失敗 HTTP %d", resp.status_code)
        except requests.RequestException as e:
            log.warning("Bark 通知失敗: %s", type(e).__name__)

    ntfy = (cfg.get("ntfy_topic") or "").strip()
    if ntfy:
        attempted = True
        try:
            resp = requests.post("https://ntfy.sh/", json={
                "topic": ntfy, "title": title, "message": body,
            }, timeout=HTTP_TIMEOUT)
            if resp.status_code == 200:
                delivered = True
            else:
                log.warning("ntfy 通知失敗 HTTP %d", resp.status_code)
        except requests.RequestException as e:
            log.warning("ntfy 通知失敗: %s", type(e).__name__)

    if not attempted:
        log.warning("無通知渠道（discord_webhook / bark_url / ntfy_topic 皆未設定）：%s", title)
        return True
    return delivered


def process_notifications(cfg, state, payload):
    """多級閾值通知 + 視窗去重；resets_at 改變時清除紀錄並發解除通知。"""
    cc = payload.get("claude_code") or {}
    rules = [
        ("five_hour", "5hr ", get_thresholds(cfg, "thresholds_5h_pct", "threshold_5h_pct"), "notify_5h"),
        ("seven_day", "週", get_thresholds(cfg, "thresholds_weekly_pct", "threshold_weekly_pct"), "notify_7d"),
    ]
    for window, label, thresholds, state_key in rules:
        block = cc.get(window) or {}
        pct = block.get("pct")
        resets_at = block.get("resets_at")
        if pct is None or not resets_at:
            continue
        pct = float(pct)

        rec = state.get(state_key)
        if not isinstance(rec, dict):
            rec = {}
        if rec.get("resets_at") != resets_at:
            # 視窗重置：曾發過警告才發解除通知
            if rec.get("levels"):
                notify_all(cfg, "✅ Claude Code %s額度已重置" % label,
                           "上一視窗曾達 %d%% 閾值，現已重置。" % max(rec["levels"]),
                           COLOR_GREEN)
            rec = {"resets_at": resets_at, "levels": []}

        crossed = [lv for lv in thresholds if pct >= lv and lv not in rec["levels"]]
        if crossed:
            top = max(crossed)
            color = COLOR_RED if top >= 90 else COLOR_ORANGE
            title = "⚠️ Claude Code %s額度 %d%%" % (label, round(pct))
            body = "已超過 %d%% 閾值，重置於 %s" % (int(top), relative_time(resets_at))
            if notify_all(cfg, title, body, color):
                rec["levels"] = sorted(set(rec["levels"]) | set(int(x) for x in crossed))
                log.info("已發送 %s 閾值 %d%% 通知（resets_at=%s）", window, int(top), resets_at)
            else:
                log.warning("%s 通知所有渠道皆失敗，下次重試", window)
        state[state_key] = rec


def alert_endpoint_change(cfg, state):
    """usage 端點回應格式異常 -> 告警（24 小時去重）。"""
    last = float(state.get("endpoint_alert_at") or 0)
    if time.time() - last < 86400:
        return
    notify_all(cfg, "⚠️ cc-usage-relay：usage 端點格式異常",
               "api.anthropic.com/api/oauth/usage 回應結構與預期不符，"
               "非官方端點可能已變動，請檢查 agent。", COLOR_ORANGE)
    state["endpoint_alert_at"] = time.time()


def run_stale(cfg, state, reason):
    """token 過期或 API 失敗：沿用 last_payload 標 stale 推 Gist，不發額度通知。
    連續 stale 超過 1 小時發一次 agent 斷線告警。"""
    last = state.get("last_payload")
    if isinstance(last, dict):
        payload = dict(last)
    else:
        log.warning("無 last_payload 可沿用，推送空白 stale payload")
        payload = {"claude_code": None}
    payload["schema"] = 2
    payload["machine"] = cfg["machine_name"]
    payload["updated_at"] = now_iso()
    payload["stale"] = True

    existing, _ = fetch_gist_files(cfg)
    merged = merge_machines(existing, payload)
    payload_text = json.dumps(merged, ensure_ascii=False, indent=2)
    hits = scan_for_secrets(payload_text, secret_pairs(cfg))
    if hits:
        log.error("stale payload 含機密標記，拒絕推送: %s", hits)
        sys.exit(1)
    try:
        push_gist(cfg, {"usage.json": payload_text})
    except (requests.RequestException, RuntimeError) as e:
        log.error("stale 路徑 Gist 推送失敗: %s", e)
        sys.exit(1)

    now = time.time()
    if not state.get("stale_since"):
        state["stale_since"] = now
    if now - float(state["stale_since"]) > 3600 and not state.get("stale_alerted"):
        notify_all(cfg, "⚠️ cc-usage-relay 已逾 1 小時未取得新數據",
                   "最近原因：%s。widget 將持續顯示過期資料。" % reason, COLOR_ORANGE)
        state["stale_alerted"] = True
    state["last_run_at"] = now
    save_state(state)
    log.info("stale payload 已推送（原因：%s）", reason)
    sys.exit(0)


def main():
    setup_logging()
    trigger = sys.argv[1] if len(sys.argv) > 1 else "schedule"
    cfg = load_config()
    state = load_state()

    # 節流：hook 連發時保護 API
    min_iv = float(cfg["min_interval_seconds"])
    if time.time() - float(state.get("last_run_at") or 0) < min_iv:
        log.info("節流：距上次執行不足 %d 秒，跳過（trigger=%s）", int(min_iv), trigger)
        return

    token, expires_at = load_credentials(cfg)

    if expires_at <= time.time():
        # token 過期：不打 API，Claude Code 下次被使用時會自行 refresh 該檔案
        run_stale(cfg, state, "token 已過期")

    usage = fetch_usage(token, cfg)
    if usage is None:
        run_stale(cfg, state, "usage 端點失敗")

    if not usage_schema_ok(usage):
        alert_endpoint_change(cfg, state)
        run_stale(cfg, state, "usage 回應格式異常")

    payload = build_payload(cfg, usage)

    existing, history = fetch_gist_files(cfg)
    merged = merge_machines(existing, payload)
    history = append_history(cfg, history, payload)
    usage_text = json.dumps(merged, ensure_ascii=False, indent=2)
    history_text = json.dumps(history, ensure_ascii=False)

    hits = scan_for_secrets(usage_text + history_text, secret_pairs(cfg, token))
    if hits:
        log.error("payload 含機密標記，拒絕推送: %s", hits)
        sys.exit(1)

    try:
        push_gist(cfg, {"usage.json": usage_text, "history.json": history_text})
    except (requests.RequestException, RuntimeError) as e:
        log.error("Gist 推送失敗: %s", e)
        state["last_payload"] = payload
        save_state(state)
        sys.exit(1)

    process_notifications(cfg, state, payload)

    state["last_payload"] = payload
    state["last_run_at"] = time.time()
    state.pop("stale_since", None)
    state.pop("stale_alerted", None)
    save_state(state)
    log.info("完成（trigger=%s）：5hr=%s%% 7d=%s%% 機器數=%d 歷史筆數=%d",
             trigger,
             (payload["claude_code"]["five_hour"] or {}).get("pct"),
             (payload["claude_code"]["seven_day"] or {}).get("pct"),
             len(merged), len(history))


if __name__ == "__main__":
    main()
