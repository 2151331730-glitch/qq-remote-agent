# -*- coding: utf-8 -*-
"""命令执行后端：原生 PowerShell / WSL / Docker。

**能力边界（别夸大）**：
- 只有 `!!` 前缀的 `!` 命令会走沙箱后端；agent 任务（claude.exe）仍在 Windows 本体上跑，
  因为 Claude CLI 是 Windows 二进制，且要访问本机配置。
- WSL 后端：命令在 Ubuntu 里执行，看不到 Windows 注册表/服务，但 /mnt/c 仍可读写宿主文件。
- Docker 后端：`--network none --rm` 的一次性容器，只挂载当前工作目录，隔离最彻底；
  前提是本机装了 Docker。
- 这不是"安全沙箱"的完整方案，而是把**破坏半径**从"整台 Windows"缩小到"一个挂载目录"。

用法（QQ 里）：
    !dir C:\\            → Windows PowerShell（默认，全权限）
    !!ls -la             → 沙箱里执行（按当前可用后端）
"""
import shutil
import subprocess
import time
from pathlib import Path

DOCKER_IMAGE = "alpine:3.20"


DOCKER_CANDIDATES = (
    r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
    r"C:\Program Files\Docker\Docker\resources\bin\docker",
)


def docker_bin():
    """Docker Desktop 装好但没进 PATH 是常见情况，这里直接找固定路径。"""
    found = shutil.which("docker")
    if found:
        return found
    for p in DOCKER_CANDIDATES:
        if Path(p).exists():
            return p
    return None


_DOCKER_CACHE = {"ts": 0.0, "ok": False}


def docker_ready(force=False):
    """Docker 装了不等于引擎在跑（Docker Desktop 常关着省电）。探测结果缓存 60 秒。"""
    now = time.time()
    if not force and now - _DOCKER_CACHE["ts"] < 60:
        return _DOCKER_CACHE["ok"]
    bin_path = docker_bin()
    ok = False
    if bin_path:
        try:
            r = subprocess.run([bin_path, "version", "--format", "{{.Server.Version}}"],
                               capture_output=True, timeout=6)
            ok = r.returncode == 0 and bool((r.stdout or b"").strip())
        except Exception:
            ok = False
    _DOCKER_CACHE.update(ts=now, ok=ok)
    return ok


def _has(cmd):
    if cmd == "docker":
        return docker_ready()
    return shutil.which(cmd) is not None


def available_backends():
    """返回本机可用的后端，按隔离强度从高到低。"""
    out = []
    if _has("docker"):
        out.append("docker")
    if _has("wsl"):
        try:
            r = subprocess.run(["wsl", "-e", "true"], capture_output=True, timeout=10)
            if r.returncode == 0:
                out.append("wsl")
        except Exception:
            pass
    out.append("powershell")
    return out


def _win_to_wsl(path: str) -> str:
    p = (path or "").replace("\\", "/")
    if len(p) > 2 and p[1] == ":":
        return f"/mnt/{p[0].lower()}{p[2:]}"
    return p or "/mnt/c"


def build_command(cmd: str, cwd: str, backend: str):
    """把一条命令包装成对应后端的 argv。"""
    if backend == "docker":
        return [docker_bin() or "docker", "run", "--rm", "--network", "none",
                "-v", f"{cwd}:/work", "-w", "/work", DOCKER_IMAGE, "sh", "-lc", cmd]
    if backend == "wsl":
        return ["wsl", "-e", "bash", "-lc", f"cd {_win_to_wsl(cwd)} 2>/dev/null; {cmd}"]
    return ["powershell", "-NoProfile", "-Command",
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
            "$OutputEncoding=[System.Text.Encoding]::UTF8; " + cmd]


def pick_backend(preferred: str = "auto"):
    """选一个可用后端；preferred=auto 时优先 docker → wsl → powershell。

    Docker Desktop 没运行时会自动落到 WSL，不会因为"装了但没开"而失败。
    """
    if preferred and preferred != "auto":
        return preferred
    for b in ("docker", "wsl"):
        if b in available_backends():
            return b
    return "powershell"
