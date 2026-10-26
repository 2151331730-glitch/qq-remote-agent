# -*- coding: utf-8 -*-
"""发 QQ 空间说说。

## 走哪条路

NapCat 自带 `send_qzone_msg` 扩展动作（不是 OneBot 11 标准），参数：

    {"content": "文字", "images": ["本地路径", ...], "ugc_right": 4, "target_uins": []}

`ugc_right` 是对谁可见：

| 值 | 含义 |
|---|---|
| 1 | 所有人 |
| 4 | 好友（默认） |
| 16 | 部分好友（要配合 `target_uins`） |
| 64 | 仅自己（**第一次试水就用这个**，发坏了只有自己能看见） |
| 128 | 部分人不可见（配合 `target_uins`） |

删除用 `delete_qzone_msg`，列表用 `get_qzone_msg_list`；这两个先不做进命令，
需要时这里加一层就行。

## 为什么要「起草 → 确认才发」

说说发出去是**对外可见**的：同一个人看到一条机器人乱发的说说，比不回消息严重得多。
所以 `/说说 <文字>` 只起草，`/说说发` 才真发，中间给一次反悔机会。
草稿 10 分钟过期，避免第二天误发昨天的话。
"""
import time

DRAFT_TTL = 600          # 草稿有效期：10 分钟

RIGHT_LABELS = {
    1: "所有人",
    4: "好友",
    16: "部分好友",
    64: "仅自己",
    128: "部分人不可见",
}
DEFAULT_RIGHT = 4
MAX_CONTENT = 1000


def right_label(value) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value)
    return RIGHT_LABELS.get(n, f"ugc_right={n}")


def build_params(content: str, images=None, ugc_right=DEFAULT_RIGHT,
                 target_uins=None) -> dict:
    """拼 send_qzone_msg 的参数。空图片列表不传（有些版本对空数组会报错）。"""
    text = str(content or "").strip()
    if not text:
        raise ValueError("说说内容不能为空")
    if len(text) > MAX_CONTENT:
        raise ValueError(f"说说最多 {MAX_CONTENT} 字，现在 {len(text)} 字")
    params = {"content": text, "ugc_right": int(ugc_right)}
    imgs = [str(p) for p in (images or []) if str(p).strip()]
    if imgs:
        params["images"] = imgs
    uins = [int(u) for u in (target_uins or [])]
    if uins:
        params["target_uins"] = uins
    return params


def draft_ok(draft: dict, now: float | None = None) -> tuple[bool, str]:
    """草稿还能不能发：有没有内容、有没有过期。返回 (可用?, 说明)。"""
    if not draft or not str(draft.get("content") or "").strip():
        return False, "没有草稿。先发 /说说 <文字>。"
    now = time.time() if now is None else now
    age = now - float(draft.get("at") or 0)
    if age > DRAFT_TTL:
        return False, f"这条草稿 {int(age // 60)} 分钟前的，已经过期了。重新发 /说说 <文字>。"
    return True, ""


async def publish(client, content: str, images=None, ugc_right=DEFAULT_RIGHT,
                  target_uins=None) -> tuple[bool, str]:
    """真发。返回 (成功?, 给人看的一句话)。client 是 bridge.py 的 Client。"""
    try:
        params = build_params(content, images, ugc_right, target_uins)
    except ValueError as e:
        return False, str(e)
    try:
        res = await client.call("send_qzone_msg", params, timeout=30)
    except Exception as e:
        return False, f"发说说失败：{type(e).__name__}: {e}"
    code = res.get("retcode") if isinstance(res, dict) else None
    msg = ""
    if isinstance(res, dict):
        msg = str(res.get("message") or res.get("wording") or res.get("msg") or "")[:200]
    if code == 0:
        return True, f"说说发了（{right_label(ugc_right)}可见）"
    return False, f"发说说失败（retcode={code}）{msg}"
