# -*- coding: utf-8 -*-
"""把「目前登入的 Claude 帳號」credentials 複製一份給 agent 追蹤。

用法（先在 Claude Code 登入要追蹤的帳號，再執行）：
    python scripts\\capture_account.py 帳號名

效果：複製 ~/.claude/.credentials.json -> agent/accounts/帳號名.credentials.json
agent 下次執行會自動把它當成額外帳號（自動續期、獨立通知）。
帳號名會顯示在 widget/dashboard/通知（機器名·帳號名）。

複製完記得「切回你平常用的主帳號」，否則 Claude Code 會停在這個帳號。
"""

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ACCOUNTS_DIR = BASE_DIR / "agent" / "accounts"
SRC = Path.home() / ".claude" / ".credentials.json"

INVALID = set('\\/:*?"<>|')


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("用法：python scripts\\capture_account.py 帳號名")
        print("（先在 Claude Code 登入要追蹤的帳號再執行）")
        sys.exit(1)

    name = sys.argv[1].strip()
    if any(c in INVALID for c in name):
        print("帳號名不能包含 \\ / : * ? \" < > | 等字元")
        sys.exit(1)

    if not SRC.exists():
        print("找不到 %s —— 請先在 Claude Code 登入一個帳號" % SRC)
        sys.exit(1)

    try:
        data = json.loads(SRC.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        expires = oauth.get("expiresAt")
    except (OSError, ValueError) as e:
        print("讀取 credentials 失敗：%s" % e)
        sys.exit(1)

    if not token or not expires:
        print("credentials 結構不符（缺 claudeAiOauth.accessToken/expiresAt）")
        sys.exit(1)
    if not oauth.get("refreshToken"):
        print("⚠️ 這份 credentials 沒有 refreshToken，token 過期後無法自動續期，")
        print("   到期後需重新登入該帳號再 capture 一次。仍繼續複製。")

    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = ACCOUNTS_DIR / (name + ".credentials.json")
    existed = dest.exists()
    dest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print("%s 帳號「%s」-> %s" % ("已更新" if existed else "已建立", name, dest))
    print("agent 下次執行（最多 5 分鐘）會自動納入此帳號。")
    print("➡️ 別忘了切回你平常用的主帳號。")


if __name__ == "__main__":
    main()
