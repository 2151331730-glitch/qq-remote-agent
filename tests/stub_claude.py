# -*- coding: utf-8 -*-
"""假 claude CLI：压测用，输出与 `claude -p --output-format json` 同构的 JSON。

用法（由 bridge 的 claude_cmd 调用）：
    python stub_claude.py -p "<prompt>" --output-format json --dangerously-skip-permissions
环境变量：
    STUB_SLEEP  每次调用模拟耗时（秒），默认 0.3
    STUB_FAIL   1 = 随机 10% 输出 is_error
"""
import json
import os
import random
import sys
import time
import uuid

sleep_s = float(os.environ.get("STUB_SLEEP", "0.3"))
time.sleep(sleep_s)

prompt = ""
for i, a in enumerate(sys.argv):
    if a in ("-p", "--print") and i + 1 < len(sys.argv):
        prompt = sys.argv[i + 1]

is_error = os.environ.get("STUB_FAIL") == "1" and random.random() < 0.1
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": is_error,
    "session_id": str(uuid.uuid4()),
    "result": ("stub-ok: " + prompt[:40]) if not is_error else "stub-error",
    "total_cost_usd": 0.0001,
    "usage": {"input_tokens": 1200, "output_tokens": 40},
}, ensure_ascii=False))
