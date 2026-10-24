# -*- coding: utf-8 -*-
"""沙箱后端的命令拼装（纯逻辑，不依赖本机真装了 WSL/Docker）。"""
import sandbox


def test_win_to_wsl_mapping():
    assert sandbox._win_to_wsl(r"C:\Users\win11") == "/mnt/c/Users/win11"
    assert sandbox._win_to_wsl("D:\\data") == "/mnt/d/data"
    assert sandbox._win_to_wsl("") == "/mnt/c"


def test_powershell_backend_sets_utf8():
    argv = sandbox.build_command("dir", r"C:\x", "powershell")
    assert argv[0] == "powershell"
    assert "Console]::OutputEncoding" in argv[-1]


def test_wsl_backend():
    argv = sandbox.build_command("ls -la", r"C:\Users\win11", "wsl")
    assert argv[:3] == ["wsl", "-e", "bash"]
    assert "/mnt/c/Users/win11" in argv[-1] and "ls -la" in argv[-1]


def test_docker_backend_is_networkless_and_mounts_cwd():
    argv = sandbox.build_command("echo hi", r"C:\work", "docker")
    assert argv[0] == "docker"
    assert "--network" in argv and "none" in argv
    assert "--rm" in argv
    assert any(a.endswith(":/work") for a in argv)
    assert "sh" in argv and "echo hi" in argv[-1]


def test_pick_explicit_backend():
    assert sandbox.pick_backend("powershell") == "powershell"


def test_available_always_has_powershell():
    assert "powershell" in sandbox.available_backends()
