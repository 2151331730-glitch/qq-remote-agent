# qq-remote · 运行手册

> Claude Code 在本目录（或子目录）启动时自动读取本文件。
> 目的：不用每次重新推进程树、不用再问"这个窗口能不能关"、改代码前先看「改代码后怎么生效」。
> 详细历史与决策理由见 `progress.md`；给用户的说明见 `使用说明.md`。

## 一句话

手机 QQ 私聊 → NapCat 挂在本机 QQ NT 上 → 本地 WebSocket → `bridge.py` → `claude.exe`（全权限）→ 结果分段发回 QQ。

## 拓扑

```
计划任务 QQRemote（登录触发，管理员）
└─ powershell start-all.ps1        ← 守护循环，每 30 秒检查
   ├─ QQ 没跑 → cmd /c napcat\launcher.bat → NapCatWinBootMain.exe 注入 QQ NT 9.9.36
   │     ├─ OneBot11 WS   ws://127.0.0.1:3001   （配置 napcat\config\onebot11_*.json）
   │     └─ WebUI         http://127.0.0.1:6099 （token 在 napcat\config\webui.json，勿打印）
   └─ bridge 没跑 → python bridge\bridge.py（隐藏窗口，管理员权限）
         ├─ 看板   http://127.0.0.1:3099      （bridge\panel.py）
         └─ 每个任务派生一个 claude.exe（--dangerously-skip-permissions）
```

## 进程 / 端口

| 东西 | 怎么看 |
|---|---|
| 计划任务 | `Get-ScheduledTask QQRemote \| Get-ScheduledTaskInfo` |
| bridge | `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` 里 CommandLine 含 `bridge\bridge.py` |
| 桥是否连上 NapCat | `Get-NetTCPConnection -State Established -RemotePort 3001`（推荐，因为管理员进程的命令行非管理员读不到） |
| 远程 claude | `Get-CimInstance Win32_Process -Filter "Name='claude.exe'"`，看 ParentProcessId 是不是 bridge |
| 守护日志 | `logs\supervisor.log`（每次拉起都会记一行） |

**注意**：`start-all.ps1` 用 `-WindowStyle Hidden` 拉起 bridge；NapCat 用 `-WindowStyle Minimized`。任务栏里能看到的那唯一一个 cmd 窗口是 NapCat 的启动器，不是 bridge。

## 文件

| 路径 | 作用 |
|---|---|
| `start-all.ps1` | 守护循环：QQ / bridge 缺了就拉起 |
| `install-task.ps1` | 注册计划任务 QQRemote（管理员跑一次） |
| `restart-bridge.ps1` | **唯一正确的重启入口**：停任务 → 先杀 claude 子进程 → 杀 bridge → 重启任务；日志写 `logs\restart.txt` |
| `自检.ps1` | 出门前六项体检 + 最近 6 行日志 |
| `bridge\bridge.py` | 桥本体（asyncio，单客户端，串行队列≤5，超时 45 min，心跳 60 s） |
| `bridge\store.py` | **任务状态机 + 幂等 + 恢复**（SQLite WAL）：queued→running→succeeded/failed/cancelled/interrupted |
| `bridge\metrics.py` | 计数/仪表/直方图，输出 Prometheus 文本 |
| `bridge\guard.py` | 危险词/危险命令识别 → 要求 `/确认`；审计写 `logs\audit.jsonl` |
| `bridge\panel.py` | 3099 看板 + `/api/*` + `/metrics` |
| `bridge\tasks.db` | 任务与事件表（**不入库**，运行时生成） |
| `bridge\config.example.json` | 配置模板；真正的 `config.json` 含 token，**不入库** |
| `bridge\config.json` | 机器人号 / 主人号 / WS 地址 / token / claude 路径 / 工作目录 |
| `bridge\state.json` | `owner_uin` / `session_id` / `cwd` / `current_task` / `cost_total`（原子写） |
| `logs\bridge.log` | 主日志（收到/任务开始/任务结束/心跳） |
| `logs\last-result.txt` | 最后一次完整结果 |
| `logs\audit.jsonl` | 每条任务、命令、确认动作 |

## 改代码后怎么生效（唯一正确姿势）

bridge 是**常驻进程**，改完 `bridge.py` 不重启 = 改动无效，这是最容易犯的错。

1. **先看有没有任务在飞**：`state.json` 的 `current_task` 非空，或 `logs\bridge.log` 最后一条是「任务开始」没有对应的「任务结束」。
2. **有任务在飞就别重启**。等它结束，或用 `tools\apply-bridge-restart.ps1` 那种"等结果发完再重启"的方式。
3. 重启：`powershell -ExecutionPolicy Bypass -File C:\Users\win11\qq-remote\restart-bridge.ps1`
4. **验证新代码真的生效**，不是"进程还活着"：
   ```powershell
   $p = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*bridge\bridge.py*' }
   $p.CreationDate; (Get-Item C:\Users\win11\qq-remote\bridge\bridge.py).LastWriteTime
   ```
   **进程启动时间必须晚于文件修改时间**。
5. 再发一条 `/心跳`，确认端到端通。

**不要这么做**：
- ❌ 直接关那个黑框窗口（会杀掉当前任务，claude 子进程可能变孤儿：任务还在跑，结果回不去）
- ❌ `taskkill /IM python.exe /F`（会误杀别的 Python 程序，比如 DailyWallpaper）
- ❌ 只杀 bridge 不杀它的 claude 子进程（`restart-bridge.ps1` 特意先杀子进程就是为这个）
- ❌ 在用户刚发任务时重启

## 编码坑（两个方向相反，别记混）

| 文件 | 必须的编码 | 写错的后果 |
|---|---|---|
| 所有 `.ps1` | **UTF-8 with BOM** | PS 5.1 按 GBK 读中文注释，报 `Array index expression is missing` |
| `napcat\config\webui.json` | **UTF-8 无 BOM** | NapCat 解析失败，**静默**退回默认配置：token 失效、自动登录失效 |

⚠️ 现存地雷：`apply-webui.ps1` 用的是 `Set-Content -Encoding UTF8`，在 PowerShell 5.1 下会写 BOM，正好踩第二个坑。用 PowerShell 7 跑，或改用 Python 写。

## 诊断配方

| 现象 | 先查 |
|---|---|
| 手机发消息没反应 | `自检.ps1` → QQ 是否登录（WebUI）→ `logs\bridge.log` 有没有「收到」 |
| 一直回「未授权」 | `state.json` 的 `owner_uin` 不对；删掉该字段让它重新绑定 |
| 回「连接失败」 | 3001 没监听 = NapCat 没登录 |
| 任务卡住 | 手机发 `/停止`，再 `/新会话` |
| 重启电脑后不跑 | 计划任务是否注册、是否 `RunLevel Highest` |
| 改了代码没效果 | 见上节：进程启动时间 vs 文件修改时间 |

## 任务队列语义（改队列前必读）

- 状态机：`queued → running → succeeded | failed | cancelled | interrupted`，只有 `queued` 能被 `/取消 <ID>`。
- 幂等：`dedup_key = qq:<message_id>` 有唯一索引，重复投递直接丢弃（`store.enqueue` 返回 `dup=True`）。
- 崩溃恢复：启动时 `recover_interrupted()` 把残留 `running` 标成 `interrupted`，并在连上 QQ 后主动通知。
- 并发：`workers` 配置默认 1；多个 worker 时按 `cwd` 加 `asyncio.Lock`，同目录串行；**多 worker 会禁用 `--resume`**，避免共享同一会话。
- 背压：`STORE.queue_depth() >= max_queue` 直接拒绝，不无限堆积。
- 后端选择：`store_backend: "sqlite" | "redis"`；Redis 支持直连（`redis_url`）或哨兵（`redis_sentinel: ["127.0.0.1:26380", ...]` + `redis_master_name`）。
- agent 沙箱：`agent_sandbox: "none" | "wsl"`，配 `agent_sandbox_cmd`（WSL 内的 CLI 路径）；启动时会探测命令是否存在。
- 成本：每任务记录 `tokens_in/tokens_out`（含缓存命中）；单次输入超过 `session_token_limit`（默认 120k）自动清空 `session_id`，重启时也会丢弃超限旧会话。`cost_usd` 是 CLI 按 Claude 价目估的，不等于 DeepSeek 实际账单。

## 开发约定

- **测试要隔离**：用环境变量 `QQ_REMOTE_CONFIG` / `QQ_REMOTE_STATE` / `QQ_REMOTE_DB` 指向临时文件 + 3002 端口，别碰真机 3001。
- 单测：`python -m pytest tests -q`（store / guard / metrics / panel 共 42 个）。
- 端到端：`python tests\mock_test5.py`（假 OneBot 服务端 + 真桥）。
- 改完至少 `python -m py_compile bridge\bridge.py`；有对应 mock 测试就跑一遍。
- **token 一律从 config 读，不写进代码/文档**；`bridge/config.json` 已被 .gitignore 排除。
- `state.json` 必须原子写（临时文件 + `os.replace`）。
- **token、密码、QQ 密码一律不打印**，需要时从文件读。
- 新增危险能力先过 `guard.py` 的判定，别绕过。

## 安全边界

bridge 以 `--dangerously-skip-permissions` 跑，**等于本人坐在电脑前的完整权限**。唯一防线是「只有 `owner_uin` 那个号能下指令」。所以：不要把这个号给别人；不要放开群消息；所有命令都进 `audit.jsonl`。

已知遗留风险：守护只看进程是否存在，不检查 NapCat 是否真的登录成功；`create_task` 并发处理消息，极端情况下顺序可能乱；无 git 版本控制。
