# QQ Remote Agent

用手机 QQ 遥控一台 Windows 机器：发一条消息，本机的 `claude` CLI 执行任务，结果分段发回 QQ。

```
手机 QQ ──私聊──▶ NapCat（注入 QQ NT，OneBot11 WS）
                        │ 127.0.0.1:3001
                        ▼
                  bridge.py（asyncio）
                   ├─ SQLite 任务队列（幂等、可恢复）
                   ├─ worker pool（按工作目录加锁）
                   ├─ guard.py（危险操作先要 /确认）
                   ├─ panel.py（:3099 看板 + /metrics）
                   └─▶ claude.exe -p（全权限）
                              │
                              └──▶ 结果切段回 QQ
```

## 为什么值得看

| 问题 | 做法 |
|---|---|
| 进程崩溃 / 重启，排队任务全丢？ | 队列落 SQLite，`queued` 任务重启后继续；`running` 的标成 `interrupted` 并主动通知 |
| QQ 断线重连后重投同一条消息 | `message_id` 做唯一键，重复投递直接丢弃（恰好一次执行） |
| 两个任务同时改同一个目录 | worker pool + 按 cwd 加锁，同目录串行、不同目录并行 |
| 不知道系统现在什么状态 | `/metrics`（Prometheus 文本）、`/api/state`（p50/p95、成功率）、审计 JSONL |
| 一条命令把电脑搞崩 | 危险词/危险命令识别 → 先在 QQ 里要 `/确认`，5 分钟有效 |
| 改了代码没生效 | 进程启动时间 vs 文件修改时间的自动校验（`restart-bridge.ps1`） |
| `--resume` 越聊越贵（实测单次到 205k token） | 记录真实 token；超过 `session_token_limit` 自动开新会话，启动时也会丢弃超限的旧会话 |
| 想横向扩、多台机器一起消费 | `store_backend=redis`：两个进程共享队列，Lua 原子 claim，实测 40 条任务 20/20 分摊、零重复 |
| 一条命令把整台机器搞坏 | `!!` 前缀走沙箱后端（Docker/WSL），只挂载当前目录；边界写在 `ARCHITECTURE.md` |
| 不知道能扛多少 | `docs/benchmarks.md`：4 worker 跨目录 3.6 倍吞吐，同目录刻意不加速 |
| NapCat 断一下，结果就丢了 | **发件箱**：结果先落库再发送，重连后自动补发未送达的消息 |
| 长任务只有"还在执行" | **流式进度**：`stream-json` 解析工具调用，转发成 `🔧 PowerShell: …`（默认 15 秒一条） |
| 失败了不知道哪种失败 | 错误分类 `timeout / parse / cli_error / exception`，进库也进 `qq_remote_task_errors_total{kind}` |
| 号被盗会被无限刷 | 额度：每小时任务数上限 + 每日 token 上限，超限直接拒绝 |
| 数据只增不减 | 30 天保留策略（任务/事件/已送达发件箱），定期清理 + VACUUM |
| 守护只看进程在不在 | 改为探测 3001 监听 + 桥的 ESTABLISHED 连接；连续 3 次异常才重启 |
| Redis 单点挂了队列就停 | 主从 + 三哨兵：**4.6 秒选主、5.6 秒恢复写入**，数据完整 |
| agent 任务仍在本体全权限跑 | `agent_sandbox: "wsl"`：提示词写文件、`wsl -e bash -lc` 调起（需 WSL 内装 Linux 版 CLI） |
| 不知道跑久了会不会漏内存 | 10 分钟常压：内存 40→47MB 后平稳、队列不堆积、零失败（`docs/benchmarks.md` §6） |

## 快速开始

```powershell
# 1) 管理员注册开机自启（计划任务 QQRemote）
powershell -ExecutionPolicy Bypass -File install-task.ps1

# 2) 启动 NapCat + 桥
.\start-all.bat

# 3) 登录 QQ：http://127.0.0.1:6099/webui  → 密码登录
# 4) 手机加机器人号为好友，发一句话即完成绑定
```

出门前：

```powershell
powershell -ExecutionPolicy Bypass -File 自检.ps1
```

## 常用命令（QQ 里发）

`/状态` `/任务 [n]` `/停止` `/取消 <ID>` `/清空` `/确认` `/取消` `/新会话` `/日志 [n]` `/全文` `/cd <路径>` `/心跳`，以及 `!<PowerShell 命令>` 直接执行。

## 指标与看板

```
curl http://127.0.0.1:3099/metrics
qq_remote_tasks_total{state="succeeded"} 12
qq_remote_task_duration_seconds_bucket{le="30"} 11
qq_remote_queue_depth 0
qq_remote_tokens_total{direction="in"} 205459
```

接 Prometheus + Grafana（本机已装，`tools/monitor/start-monitoring.ps1` 一键起）：

![Grafana 看板](docs/grafana-dashboard.png)

面板包含：队列深度、执行中、任务总数、token 用量、任务速率（按状态）、p95 耗时、队列曲线、token 速率。
Prometheus 每 5 秒抓一次 `/metrics`；Grafana 用 provisioning 文件自动装配数据源与仪表盘，不用手工点。

## 测试

```powershell
pip install pytest
python -m pytest tests -q          # 42 个单测
python tests\mock_test5.py         # 端到端：假 OneBot 服务端 + 真桥
```

端到端测试用 `QQ_REMOTE_CONFIG` / `QQ_REMOTE_STATE` / `QQ_REMOTE_DB` 指向临时文件，
监听 3002 端口，不会碰正在运行的真机 3001。

## 安全模型（务必读）

桥以 `--dangerously-skip-permissions` 运行，**等于本人坐在电脑前的完整权限**。
唯一防线是 `owner_uin` 白名单 + 危险操作确认 + 全量审计。
`guard.py` 是**人工确认**，不是沙箱；真要隔离得上 Docker / WSL / 受限令牌。

## 文档

- `CLAUDE.md` —— 运行手册（拓扑、诊断配方、改代码后如何生效）
- `ARCHITECTURE.md` —— 架构与决策记录（ADR）
- `progress.md` —— 逐次改动与踩坑史
- `使用说明.md` —— 面向使用者的操作指南

## 说明

- 本项目是个人自用工具，文档里的路径按 `C:\Users\<user>\qq-remote` 组织。
- 仓库内不含任何 token / QQ 号 / 密码；`bridge/config.json` 已在 `.gitignore` 中，
  首次使用请从 `bridge/config.example.json` 复制并填入自己的值。
- NapCat 属于第三方项目，不在本仓库内（`napcat/` 已忽略），需自行下载。
