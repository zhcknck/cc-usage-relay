# -*- coding: utf-8 -*-
"""把 Claude Code Stop hook 寫進 ~/.claude/settings.json（冪等，寫入前自動備份）。

用法：python scripts\\install_hook.py
效果：每次 Claude Code 對話結束，背景觸發 agent 即時推送用量（新 session 生效）。
"""

import json
import sys
from pathlib import Path

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")

SETTINGS = Path.home() / ".claude" / "settings.json"
TRIGGER = (Path(__file__).resolve().parent / "hook_trigger.cmd")

HOOK_ENTRY = {
    "hooks": [
        {
            "type": "command",
            "command": "cmd.exe",
            "args": ["/c", str(TRIGGER)],
            "async": True,
            "timeout": 30,
            "statusMessage": "同步 CC 用量到 Gist",
        }
    ]
}


def main():
    if not TRIGGER.exists():
        print("找不到 %s，請在 repo 內執行" % TRIGGER)
        sys.exit(1)

    if SETTINGS.exists():
        try:
            data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except ValueError:
            print("%s 不是有效 JSON，請先手動修復" % SETTINGS)
            sys.exit(1)
    else:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        data = {}

    stop_hooks = data.setdefault("hooks", {}).setdefault("Stop", [])
    for group in stop_hooks:
        for hook in group.get("hooks", []):
            if "hook_trigger.cmd" in json.dumps(hook.get("args", []) or [hook.get("command", "")]):
                print("Stop hook 已存在，未做變更：%s" % hook.get("args", hook.get("command")))
                return

    if SETTINGS.exists():
        backup = SETTINGS.with_suffix(".json.bak")
        backup.write_text(SETTINGS.read_text(encoding="utf-8"), encoding="utf-8")
        print("已備份原設定到 %s" % backup)

    stop_hooks.append(HOOK_ENTRY)
    SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已加入 Stop hook -> %s" % TRIGGER)
    print("新開的 Claude Code session 生效（或在 CC 內輸入 /hooks 重新載入）")


if __name__ == "__main__":
    main()
