# -*- coding: utf-8 -*-
"""假 claude CLI：压测/联调用，输出与真 CLI 同构。

支持两种格式：
    --output-format json         单行结果
    --output-format stream-json  先来一条 assistant/tool_use，最后来 result

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
prompt = ""
fmt = "json"
for i, a in enumerate(sys.argv):
    if a in ("-p", "--print") and i + 1 < len(sys.argv):
        prompt = sys.argv[i + 1]
    if a == "--output-format" and i + 1 < len(sys.argv):
        fmt = sys.argv[i + 1]

_fail = os.environ.get("STUB_FAIL", "")
is_error = _fail == "always" or (_fail == "1" and random.random() < 0.1)
sid = str(uuid.uuid4())

if fmt == "stream-json":
    # 模拟一次工具调用，用来验证"进度转发"链路
    print(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Read",
                                 "input": {"file_path": r"C:\Users\win11\notes.txt"}}]},
    }, ensure_ascii=False), flush=True)
    time.sleep(sleep_s)
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": is_error,
        "session_id": sid,
        "result": ("stub-ok: " + prompt[:40]) if not is_error else "stub-error",
        "total_cost_usd": 0.0001,
        "usage": {"input_tokens": 1200, "output_tokens": 40},
    }, ensure_ascii=False), flush=True)
else:
    time.sleep(sleep_s)
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": is_error,
        "session_id": sid,
        "result": ("stub-ok: " + prompt[:40]) if not is_error else "stub-error",
        "total_cost_usd": 0.0001,
        "usage": {"input_tokens": 1200, "output_tokens": 40},
    }, ensure_ascii=False))
