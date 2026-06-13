# -*- coding: utf-8 -*-
"""cc-usage-relay agent v3

讀取 Claude Code OAuth credentials（可多帳號）-> 呼叫 usage 端點 -> 白名單 payload
推 GitHub Gist（usage.json 為多機×多帳號陣列、history.json 為走勢紀錄）->
額度跨越閾值時推播通知（Discord embed / Bark / ntfy / Windows toast，
每個重置視窗每個閾值只發一次，視窗重置發解除通知）。

多帳號：config 的 accounts 列表可指向 credentials 副本檔；副本帳號 token 過期時
agent 會用 refresh token 自行續期並寫回副本（絕不碰 ~/.claude 的主帳號檔，
那份由 Claude Code 自己管理，避免互相覆寫）。

觸發來源：Windows 排程（每 5 分鐘兜底）+ Claude Code Stop hook（即時）。
內建節流（min_interval_seconds）+ 行程鎖（agent.lock）避免 hook 與排程並行。
所有路徑以本檔所在目錄解析。零第三方依賴（僅 Python 標準庫）。

指令：python agent.py [trigger]；trigger=test-notify 時發測試通知後結束。
"""

import base64
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOCK_PATH = BASE_DIR / "agent.lock"
LOG_PATH = BASE_DIR / "agent.log"
LOG_MAX_BYTES = 1024 * 1024
LOCK_STALE_SECONDS = 300  # 鎖檔超過此秒數視為前次異常殘留

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
GIST_API = "https://api.github.com/gists/{gist_id}"
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code 公開 client id
HTTP_TIMEOUT = 10
RETRY_DELAYS = (30, 60, 120)  # 429 退避秒數
CONN_RETRY_DELAY = 5          # 連線層失敗重試一次的間隔

TZ_LOCAL = timezone(timedelta(hours=8))  # 輸出 updated_at 用 +08:00

COLOR_RED = 0xFF453A
COLOR_ORANGE = 0xFF9F0A
COLOR_GREEN = 0x30D158

# extra_usage 僅放行這些 key 的純量值
EXTRA_ALLOWED = ("is_enabled", "enabled", "monthly_limit", "used_credits",
                 "used", "utilization", "amount", "limit", "remaining")

# Windows toast 借用 PowerShell 的 AppUserModelID（未註冊自有 AppId 也能顯示）
TOAST_APP_ID = ("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
                "\\WindowsPowerShell\\v1.0\\powershell.exe")

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


def atomic_write(path, text):
    """先寫 .tmp 再 os.replace，避免並行/斷電留下半截檔。"""
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------- HTTP（標準庫，零依賴） ----------

def http(method, url, headers=None, json_body=None, timeout=HTTP_TIMEOUT):
    """回傳 (status, parsed_json_or_None, text)；連線層失敗回 (None, None, "")。"""
    hdrs = {"User-Agent": "cc-usage-relay/3.0"}
    if headers:
        hdrs.update(headers)
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            text = e.read().decode("utf-8", "replace")
        except OSError:
            text = ""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("HTTP 連線失敗 (%s %s): %s",
                    method, url.split("?")[0], type(e).__name__)
        return None, None, ""
    parsed = None
    if text:
        try:
            parsed = json.loads(text)
        except ValueError:
            pass
    return status, parsed, text


# ---------- 設定與狀態 ----------

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
    cfg.setdefault("windows_toast", False)
    cfg.setdefault("threshold_5h_pct", 90)
    cfg.setdefault("threshold_weekly_pct", 90)
    cfg.setdefault("notify_5h_reset_always", False)
    cfg.setdefault("machine_name", "PC")
    cfg.setdefault("wsl_credentials_path", "")
    cfg.setdefault("user_agent_version", "2.0.0")
    cfg.setdefault("min_interval_seconds", 60)
    cfg.setdefault("history_hours", 48)
    cfg.setdefault("machine_ttl_hours", 48)
    cfg.setdefault("daily_summary_hour", None)
    cfg.setdefault("accounts", [])
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
    atomic_write(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2))


def account_state(state, label):
    """各帳號獨立的通知去重/last_payload 子狀態；自動遷移 v2 頂層舊鍵。"""
    accs = state.setdefault("accounts_state", {})
    st = accs.get(label)
    if st is None:
        st = {}
        for k in ("notify_5h", "notify_7d", "last_payload",
                  "stale_since", "stale_alerted"):
            if k in state:
                st[k] = state.pop(k)
        accs[label] = st
    return st


def acquire_lock():
    """O_EXCL 行程鎖；殘留逾 LOCK_STALE_SECONDS 自動接管。成功回 True。"""
    for _ in range(2):
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - LOCK_PATH.stat().st_mtime > LOCK_STALE_SECONDS:
                    LOCK_PATH.unlink()
                    continue  # 殘留鎖，移除後重試一次
            except OSError:
                pass
            return False
    return False


def release_lock():
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


# ---------- 帳號與 credentials ----------

ACCOUNTS_DIR = BASE_DIR / "accounts"


def load_account_list(cfg):
    """組合帳號清單（去重）：
    1. 預設帳號（本機 ~/.claude，由 Claude Code 自管，不續期）—
       track_default_account 為 false 時不納入（讓三個帳號全由 capture 明確命名，
       避免「當前登入帳號」自動冒出一張會跟著切換變動的 ZHCK 卡）
    2. config 的 accounts 明確指定者
    3. 自動掃描 agent/accounts/*.credentials.json（檔名即帳號名，自動續期）
    讓多帳號只需把 credentials 副本丟進資料夾，免改設定。"""
    out = []
    if cfg.get("track_default_account", True):
        out.append({"name": "", "credentials_path": "", "auto_refresh": False})
    seen_paths = set()

    accs = cfg.get("accounts")
    if isinstance(accs, list):
        for a in accs:
            if not isinstance(a, dict):
                continue
            name = str(a.get("name") or "").strip()
            path = str(a.get("credentials_path") or "").strip()
            if not name and not path:
                continue  # 空白條目＝預設帳號，已在 out[0]
            if path:
                seen_paths.add(str(Path(path).resolve()))
            out.append({"name": name, "credentials_path": path,
                        "auto_refresh": bool(a.get("auto_refresh"))})

    try:
        files = sorted(ACCOUNTS_DIR.glob("*.credentials.json")) if ACCOUNTS_DIR.is_dir() else []
    except OSError:
        files = []
    seen_names = {a["name"] for a in out}
    for f in files:
        if str(f.resolve()) in seen_paths:
            continue  # config 已明確指定，不重複
        name = f.name[:-len(".credentials.json")].strip()
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        out.append({"name": name, "credentials_path": str(f), "auto_refresh": True})
    if not out:
        # track_default_account=false 但還沒 capture 任何帳號 → 退回預設，避免空轉
        out.append({"name": "", "credentials_path": "", "auto_refresh": False})
    return out


def account_label(cfg, name):
    return cfg["machine_name"] + ("·" + name if name else "")


def load_account_credentials(cfg, acc):
    """回傳 {token, expires_at(秒), refresh_token, path, raw} 或 None。"""
    if acc["credentials_path"]:
        candidates = [Path(acc["credentials_path"])]
    else:
        candidates = [Path.home() / ".claude" / ".credentials.json"]
        wsl_path = (cfg.get("wsl_credentials_path") or "").strip()
        if wsl_path:
            candidates.append(Path(wsl_path))

    for path in candidates:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("credentials 讀取失敗 (%s): %s", path, e)
            continue
        oauth = data.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        expires_ms = oauth.get("expiresAt")
        if not token or not expires_ms:
            log.warning("credentials 缺少 claudeAiOauth.accessToken/expiresAt (%s)", path)
            continue
        return {"token": token, "expires_at": float(expires_ms) / 1000.0,
                "refresh_token": oauth.get("refreshToken"), "path": path, "raw": data}
    return None


def refresh_credentials(cred):
    """副本帳號 token 過期時自行續期並寫回副本檔。成功回更新後的 cred，失敗回 None。
    僅用於 credentials_path 指定的副本——主帳號檔由 Claude Code 自己刷新，不碰。"""
    rt = cred.get("refresh_token")
    if not rt:
        log.warning("無 refreshToken 可續期 (%s)", cred["path"].name)
        return None
    status, data, _ = http("POST", OAUTH_TOKEN_URL, json_body={
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": OAUTH_CLIENT_ID,
    })
    if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        log.warning("token 續期失敗 HTTP %s (%s)", status, cred["path"].name)
        return None
    oauth = cred["raw"].setdefault("claudeAiOauth", {})
    oauth["accessToken"] = data["access_token"]
    if data.get("refresh_token"):
        oauth["refreshToken"] = data["refresh_token"]
    try:
        expires_in = float(data.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    oauth["expiresAt"] = int((time.time() + expires_in) * 1000)
    try:
        atomic_write(cred["path"], json.dumps(cred["raw"], ensure_ascii=False, indent=2))
    except OSError as e:
        log.warning("credentials 副本寫回失敗 (%s): %s", cred["path"].name, e)
    log.info("token 已自動續期（%s）", cred["path"].name)
    out = dict(cred)
    out["token"] = oauth["accessToken"]
    out["expires_at"] = oauth["expiresAt"] / 1000.0
    out["refresh_token"] = oauth.get("refreshToken")
    return out


# ---------- usage 端點 ----------

def fetch_usage(token, cfg):
    """成功回傳 usage dict；429 退避重試、連線失敗重試一次後仍失敗、
    401 或其他狀態回傳 None（走 stale 路徑）。"""
    headers = {
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/" + str(cfg["user_agent_version"]),
    }
    conn_retried = False
    i = 0
    while i <= len(RETRY_DELAYS):
        status, data, _ = http("GET", USAGE_URL, headers=headers)
        if status is None:
            if not conn_retried:
                conn_retried = True
                time.sleep(CONN_RETRY_DELAY)
                continue
            return None
        if status == 200:
            return data if isinstance(data, dict) else None
        if status == 429:
            if i < len(RETRY_DELAYS):
                log.warning("usage 429，%d 秒後重試", RETRY_DELAYS[i])
                time.sleep(RETRY_DELAYS[i])
                i += 1
                continue
            log.warning("usage 429，重試次數用盡")
            return None
        if status == 401:
            log.warning("usage 401（token 無效或已撤銷）")
            return None
        log.warning("usage 端點回應 HTTP %d", status)
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


def describe_shape(obj, depth=2):
    """安全描述 JSON 結構：只記 key 與型別、不記實際值，
    用於診斷端點格式變動（例如額度用爆時 100% 的回應結構）。"""
    if isinstance(obj, dict):
        if depth <= 0:
            return "{...}"
        return "{" + ", ".join(
            "%s:%s" % (k, describe_shape(v, depth - 1))
            for k, v in list(obj.items())[:25]) + "}"
    if isinstance(obj, list):
        return "[%s]" % (describe_shape(obj[0], depth - 1) if obj else "")
    return type(obj).__name__


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


def build_payload(label, usage):
    return {
        "schema": 2,
        "machine": label,
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


def stale_entry(label, st, reason):
    """沿用該帳號的 last_payload 標 stale；附上原因供 widget/dashboard 顯示。"""
    last = st.get("last_payload")
    if isinstance(last, dict):
        payload = dict(last)
    else:
        payload = {"claude_code": None}
    payload["schema"] = 2
    payload["machine"] = label
    payload["updated_at"] = now_iso()
    payload["stale"] = True
    payload["stale_reason"] = reason
    st.setdefault("stale_since", time.time())
    return payload


# ---------- 機密掃描 ----------

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


def secret_pairs(cfg):
    return [
        ("github_token", cfg.get("github_token")),
        ("discord_webhook", cfg.get("discord_webhook")),
        ("bark_url", cfg.get("bark_url")),
    ]


# ---------- Gist ----------

def gist_headers(cfg):
    return {
        "Authorization": "Bearer " + cfg["github_token"],
        "Accept": "application/vnd.github+json",
    }


def fetch_gist_files(cfg):
    """讀回 gist 現有內容（多機合併用）。回傳 (usage_entries, history_entries)；失敗回 ([], [])。"""
    status, parsed, _ = http("GET", GIST_API.format(gist_id=cfg["gist_id"]),
                             headers=gist_headers(cfg))
    if status != 200 or not isinstance(parsed, dict):
        log.warning("gist 讀取失敗 HTTP %s（以單機模式覆寫）", status)
        return [], []
    files = parsed.get("files") or {}

    def parse(name):
        f = files.get(name)
        if not isinstance(f, dict):
            return []
        content = f.get("content")
        if f.get("truncated") and f.get("raw_url"):
            _, _, content = http("GET", f["raw_url"])
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


def merge_machines(existing, own_entries, ttl_hours):
    """自己的條目（多帳號各一）取代同名；其他來源 TTL 內的資料保留。"""
    own_labels = {e["machine"] for e in own_entries}
    merged = list(own_entries)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
    for entry in existing:
        if not isinstance(entry, dict) or entry.get("machine") in own_labels:
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
    """走勢紀錄：每筆只存 來源/時間/5hr%/週%；裁掉視窗外與超量的舊資料。"""
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
    """files: {檔名: 內容字串}；省略的檔案保持不變。失敗一律 raise RuntimeError。"""
    url = GIST_API.format(gist_id=cfg["gist_id"])
    body = {"files": {name: {"content": text} for name, text in files.items()}}
    status, _, _ = http("PATCH", url, headers=gist_headers(cfg), json_body=body)
    if status == 404:
        raise RuntimeError("Gist 更新失敗 HTTP 404（gist_id 可能填錯，或 token 看不到此 gist）")
    if status in (401, 403):
        raise RuntimeError("Gist 更新失敗 HTTP %d（github_token 無效或缺 gist 寫入權限）" % status)
    if status != 200:
        raise RuntimeError("Gist 更新失敗 HTTP %s" % status)


# ---------- 通知 ----------

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


def has_channels(cfg):
    return bool((cfg.get("discord_webhook") or "").strip()
                or (cfg.get("bark_url") or "").strip()
                or (cfg.get("ntfy_topic") or "").strip()
                or cfg.get("windows_toast"))


def notify_toast(title, body):
    """Windows 原生 toast（借 PowerShell 的 AppId，零依賴、零機密）。"""
    esc = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))
    xml = ('<toast><visual><binding template="ToastText02">'
           '<text id="1">%s</text><text id="2">%s</text>'
           '</binding></visual></toast>') % (esc(title), esc(body))
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        "$x.LoadXml('%s');"
        "$t = New-Object Windows.UI.Notifications.ToastNotification $x;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('%s').Show($t)"
    ) % (xml.replace("'", "''"), TOAST_APP_ID.replace("'", "''"))
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0:
            return True
        log.warning("Windows toast 失敗: %s",
                    (r.stderr or b"").decode("utf-8", "replace").strip()[:200])
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("Windows toast 失敗: %s", type(e).__name__)
    return False


def notify_all(cfg, title, body, color):
    """推到所有已設定渠道；任何一個成功即回 True。全部未設定則記 log 並視為已處理。"""
    attempted = delivered = False

    webhook = (cfg.get("discord_webhook") or "").strip()
    if webhook:
        attempted = True
        status, _, _ = http("POST", webhook, json_body={
            "embeds": [{"title": title, "description": body, "color": color}],
        })
        if status in (200, 204):
            delivered = True
        else:
            log.warning("Discord 通知失敗 HTTP %s", status)

    bark = (cfg.get("bark_url") or "").strip().rstrip("/")
    if bark:
        attempted = True
        status, _, _ = http("POST", bark, json_body={
            "title": title, "body": body, "group": "cc-usage-relay",
        })
        if status == 200:
            delivered = True
        else:
            log.warning("Bark 通知失敗 HTTP %s", status)

    ntfy = (cfg.get("ntfy_topic") or "").strip()
    if ntfy:
        attempted = True
        status, _, _ = http("POST", "https://ntfy.sh/", json_body={
            "topic": ntfy, "title": title, "message": body,
        })
        if status == 200:
            delivered = True
        else:
            log.warning("ntfy 通知失敗 HTTP %s", status)

    if cfg.get("windows_toast"):
        attempted = True
        if notify_toast(title, body):
            delivered = True

    if not attempted:
        log.warning("無通知渠道（discord_webhook / bark_url / ntfy_topic / windows_toast 皆未設定）：%s", title)
        return True
    return delivered


def is_new_window(old_iso, new_iso, tol_seconds=180):
    """resets_at 含秒/微秒級抖動（同一視窗每次查詢會差一兩秒），
    差距在 tol_seconds 內視為同一視窗，避免假性重置造成通知洗版。"""
    if not old_iso:
        return True
    try:
        a = datetime.fromisoformat(str(old_iso).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(new_iso).replace("Z", "+00:00"))
    except ValueError:
        return old_iso != new_iso
    return abs((a - b).total_seconds()) > tol_seconds


def fire_reset(cfg, rec, wlabel, label, always_reset):
    """發額度重置解除通知並標記 reset_done。
    時間觸發（notify_due_resets）與視窗換新觸發（process_notifications）共用，
    靠 reset_done 旗標避免同一視窗重複通知。"""
    if rec.get("reset_done"):
        return
    levels = rec.get("levels") or []
    if not levels and not always_reset:
        rec["reset_done"] = True  # 本視窗沒警告過也沒開「一律通知」→ 不發，但標記已處理
        return
    rec["reset_done"] = True
    if levels:
        body = "上一視窗曾達 %d%% 閾值，現已重置，可繼續使用。（%s）" % (max(levels), label)
    else:
        body = "%s額度已重置，可繼續使用。（%s）" % (wlabel, label)
    notify_all(cfg, "✅ Claude Code %s額度已重置" % wlabel, body, COLOR_GREEN)
    log.info("已發送 %s 重置通知（%s）", wlabel.strip(), label)


def notify_due_resets(cfg, state):
    """5hr 重置時間一到就主動通知——不依賴 API 回報新視窗，
    所以閒置等待或額度用爆（API 回應異常走 stale）時也能準時提醒。
    每視窗只發一次；每輪執行都檢查（不需抓資料成功）。"""
    if not has_channels(cfg):
        return
    always_5h = bool(cfg.get("notify_5h_reset_always"))
    now = time.time()
    for label, st in (state.get("accounts_state") or {}).items():
        rec = st.get("notify_5h")
        if not isinstance(rec, dict) or rec.get("reset_done") or not rec.get("resets_at"):
            continue
        try:
            target = datetime.fromisoformat(
                str(rec["resets_at"]).replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            continue
        if now >= target:
            fire_reset(cfg, rec, "5hr ", label, always_5h)


def process_notifications(cfg, st, payload, label):
    """多級閾值通知 + 視窗去重；resets_at 改變時清除紀錄並發解除通知。
    無渠道時整段跳過（不記錄已發送，之後補設渠道仍會在本視窗內補通知）。"""
    if not has_channels(cfg):
        return
    cc = payload.get("claude_code") or {}
    always_5h = bool(cfg.get("notify_5h_reset_always"))
    rules = [
        # (端點 key, 顯示名, 閾值, state key, 無警告也通知重置)
        ("five_hour", "5hr ", get_thresholds(cfg, "thresholds_5h_pct", "threshold_5h_pct"), "notify_5h", always_5h),
        ("seven_day", "週", get_thresholds(cfg, "thresholds_weekly_pct", "threshold_weekly_pct"), "notify_7d", False),
    ]
    for window, wlabel, thresholds, state_key, always_reset in rules:
        block = cc.get(window) or {}
        pct = block.get("pct")
        resets_at = block.get("resets_at")
        if pct is None or not resets_at:
            continue
        pct = float(pct)

        rec = st.get(state_key)
        if not isinstance(rec, dict):
            rec = {}
        if is_new_window(rec.get("resets_at"), resets_at):
            # 視窗換新：若上一視窗的重置尚未由時間觸發通知過，這裡補一則
            if rec.get("resets_at"):  # 首次觀測不算重置，避免 agent 啟動即誤報
                fire_reset(cfg, rec, wlabel, label, always_reset)
            rec = {"resets_at": resets_at, "levels": []}
        # 同一視窗內保留首次記錄的 resets_at（不隨抖動更新，避免累積漂移超出容差）

        crossed = [lv for lv in thresholds if pct >= lv and lv not in rec["levels"]]
        if crossed:
            top = max(crossed)
            color = COLOR_RED if top >= 90 else COLOR_ORANGE
            title = "⚠️ Claude Code %s額度 %d%%" % (wlabel, round(pct))
            body = "已超過 %d%% 閾值，重置於 %s（%s）" % (
                int(top), relative_time(resets_at), label)
            if notify_all(cfg, title, body, color):
                rec["levels"] = sorted(set(rec["levels"]) | set(int(x) for x in crossed))
                log.info("已發送 %s/%s 閾值 %d%% 通知（resets_at=%s）",
                         label, window, int(top), resets_at)
            else:
                log.warning("%s/%s 通知所有渠道皆失敗，下次重試", label, window)
        st[state_key] = rec


def alert_endpoint_change(cfg, state):
    """usage 端點回應格式異常 -> 告警（24 小時去重）。"""
    last = float(state.get("endpoint_alert_at") or 0)
    if time.time() - last < 86400:
        return
    notify_all(cfg, "⚠️ cc-usage-relay：usage 端點格式異常",
               "api.anthropic.com/api/oauth/usage 回應結構與預期不符，"
               "非官方端點可能已變動，請檢查 agent。", COLOR_ORANGE)
    state["endpoint_alert_at"] = time.time()


def maybe_daily_summary(cfg, state, history, own_labels):
    """每日一次的用量摘要（daily_summary_hour 起的第一次成功執行觸發）。"""
    hour = cfg.get("daily_summary_hour")
    if hour in (None, ""):
        return
    try:
        hour = int(hour)
    except (TypeError, ValueError):
        return
    now = datetime.now(TZ_LOCAL)
    today = now.strftime("%Y-%m-%d")
    if now.hour < hour or state.get("summary_date") == today:
        return
    if not has_channels(cfg):
        return
    lines = []
    for label in sorted(own_labels):
        pts = [e for e in history
               if isinstance(e, dict) and e.get("m") == label
               and str(e.get("t", "")).startswith(today)]
        h5s = [e["h5"] for e in pts if isinstance(e.get("h5"), (int, float))]
        d7s = [e["d7"] for e in pts if isinstance(e.get("d7"), (int, float))]
        if not h5s and not d7s:
            continue
        lines.append("%s：5hr 峰值 %d%%，週額度現為 %d%%" % (
            label, max(h5s) if h5s else 0, d7s[-1] if d7s else 0))
    if not lines:
        return
    if notify_all(cfg, "📊 Claude Code 今日用量摘要", "\n".join(lines), COLOR_GREEN):
        state["summary_date"] = today
        log.info("已發送每日摘要")


# ---------- 主流程 ----------

def run_once(cfg, state, trigger):
    accounts = load_account_list(cfg)
    secrets = secret_pairs(cfg)
    own_entries = []   # 本機所有帳號這一輪的條目（fresh 或 stale）
    fresh = []         # [(account_state, payload)] 僅成功取得新數據者

    for acc in accounts:
        label = account_label(cfg, acc["name"])
        st = account_state(state, label)
        cred = load_account_credentials(cfg, acc)
        if cred is None:
            log.warning("找不到可用的 credentials（%s）", label)
            own_entries.append(stale_entry(label, st, "找不到 credentials"))
            continue
        secrets.append(("access_token", cred["token"]))
        if cred.get("refresh_token"):
            secrets.append(("refresh_token", cred["refresh_token"]))

        if cred["expires_at"] <= time.time():
            refreshed = None
            # 僅副本帳號自行續期；主帳號檔由 Claude Code 管理，不碰
            if acc["auto_refresh"] and acc["credentials_path"]:
                refreshed = refresh_credentials(cred)
                if refreshed:
                    cred = refreshed
                    secrets.append(("access_token", cred["token"]))
                    if cred.get("refresh_token"):
                        secrets.append(("refresh_token", cred["refresh_token"]))
            if not refreshed:
                own_entries.append(stale_entry(label, st, "token 已過期"))
                continue

        usage = fetch_usage(cred["token"], cfg)
        if usage is None:
            own_entries.append(stale_entry(label, st, "usage 端點失敗"))
            continue
        if not usage_schema_ok(usage):
            # 記下結構（不含值）以便下次格式異常時對症修正——
            # 已知額度用爆 100% 時端點回應結構會與平常不同
            log.warning("usage 結構異常（%s），shape=%s", label, describe_shape(usage))
            alert_endpoint_change(cfg, state)
            own_entries.append(stale_entry(label, st, "usage 回應格式異常"))
            continue

        payload = build_payload(label, usage)
        fresh.append((st, payload))
        own_entries.append(payload)

    existing, history = fetch_gist_files(cfg)
    merged = merge_machines(existing, own_entries, float(cfg["machine_ttl_hours"]))
    for _, payload in fresh:
        history = append_history(cfg, history, payload)

    # 時間到的 5hr 重置主動通知——獨立於抓資料/推 gist 是否成功，
    # 確保閒置等待或 API 異常時也能準時提醒「可以再用了」
    notify_due_resets(cfg, state)

    usage_text = json.dumps(merged, ensure_ascii=False, indent=2)
    history_text = json.dumps(history, ensure_ascii=False)
    hits = scan_for_secrets(usage_text + history_text, secrets)
    if hits:
        log.error("payload 含機密標記，拒絕推送: %s", hits)
        save_state(state)
        return

    try:
        push_gist(cfg, {"usage.json": usage_text, "history.json": history_text})
    except RuntimeError as e:
        log.error("Gist 推送失敗: %s", e)
        for st, payload in fresh:
            st["last_payload"] = payload
        state["last_run_at"] = time.time()
        save_state(state)
        return

    now = time.time()
    for st, payload in fresh:
        process_notifications(cfg, st, payload, payload["machine"])
        st["last_payload"] = payload
        st.pop("stale_since", None)
        st.pop("stale_alerted", None)

    # stale 帳號連續逾 1 小時 -> 告警一次
    for entry in own_entries:
        if not entry.get("stale"):
            continue
        st = account_state(state, entry["machine"])
        since = float(st.get("stale_since") or now)
        if now - since > 3600 and not st.get("stale_alerted"):
            if notify_all(cfg, "⚠️ cc-usage-relay 已逾 1 小時未取得新數據",
                          "%s：%s。widget 將持續顯示過期資料。"
                          % (entry["machine"], entry.get("stale_reason", "未知原因")),
                          COLOR_ORANGE):
                st["stale_alerted"] = True

    own_labels = {e["machine"] for e in own_entries}
    maybe_daily_summary(cfg, state, history, own_labels)

    state["last_run_at"] = now
    save_state(state)

    parts = []
    for e in own_entries:
        if e.get("stale"):
            parts.append("%s=stale(%s)" % (e["machine"], e.get("stale_reason", "?")))
        else:
            cc = e.get("claude_code") or {}
            parts.append("%s=5hr:%s%%/7d:%s%%" % (
                e["machine"],
                (cc.get("five_hour") or {}).get("pct"),
                (cc.get("seven_day") or {}).get("pct")))
    log.info("完成（trigger=%s）：%s 條目數=%d 歷史筆數=%d",
             trigger, " ".join(parts), len(merged), len(history))


def main():
    setup_logging()
    trigger = sys.argv[1] if len(sys.argv) > 1 else "schedule"
    cfg = load_config()

    if trigger == "test-notify":
        ok = notify_all(cfg, "🔔 cc-usage-relay 測試通知",
                        "通知渠道運作正常（%s）" % cfg["machine_name"], COLOR_GREEN)
        log.info("test-notify 結果: %s", "已送達" if ok else "全部失敗")
        return

    state = load_state()

    # 節流：hook 連發時保護 API
    min_iv = float(cfg["min_interval_seconds"])
    if time.time() - float(state.get("last_run_at") or 0) < min_iv:
        log.info("節流：距上次執行不足 %d 秒，跳過（trigger=%s）", int(min_iv), trigger)
        return

    # 行程鎖：hook 與排程同時觸發時只跑一份
    if not acquire_lock():
        log.info("另一個 agent 實例執行中，跳過（trigger=%s）", trigger)
        return
    try:
        run_once(cfg, state, trigger)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
