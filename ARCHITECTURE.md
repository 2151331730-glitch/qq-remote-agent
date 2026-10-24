# 架构与决策记录

> 这份文件回答两个问题：**它是怎么搭起来的**，以及**为什么这么搭**。
> 每条决策都带背景、取舍和"如果重来会怎么选"，便于面试时讲清楚。

## 1. 组件

| 组件 | 职责 | 关键实现 |
|---|---|---|
| NapCat Shell 4.18 | 注入 QQ NT，把 QQ 消息转成 OneBot11 协议 | 外部项目，不改源码；WS 只监听本机 |
| `bridge.py` | 事件循环、WS 客户端、worker pool、命令解析 | asyncio，单事件循环，无阻塞调用 |
| `store.py` | 任务状态机 + 幂等 + 恢复 | SQLite WAL，`UPDATE ... RETURNING` 原子取队 |
| `guard.py` | 危险词/危险命令识别、审计 | 正则 + 关键词，宁多问一句 |
| `panel.py` | 本机看板、HTTP 入口、Prometheus 指标 | 标准库 `ThreadingHTTPServer`，零依赖 |
| `metrics.py` | 计数/仪表/直方图 | 自实现，输出 Prometheus 文本格式 |
| `restart-bridge.ps1` | 安全重启 | 拒绝在任务执行中重启；校验新进程真的加载了新代码 |
| `start-all.ps1` | 守护 NapCat + bridge | 计划任务登录触发，30 秒巡检 |

## 2. 数据流

```
QQ 消息 (private, owner)
  → on_message
    ├─ 以 / 开头   → handle_command（不发任务）
    ├─ 以 ! 开头   → submit_shell → guard 判定 → 直接跑 PowerShell
    └─ 其它         → submit_task
                        ├─ 队列深度 ≥ max_queue → 拒绝（背压）
                        ├─ 危险词命中 → 挂起待确认，5 分钟 TTL
                        └─ STORE.enqueue(dedup_key="qq:<message_id>")
                                → WAKE.set() 唤醒 worker
worker:
  claim_next() → 按 cwd 拿 asyncio.Lock → run_claude()
    → 成功/失败/取消 → STORE.finish() + METRICS + audit + 回 QQ
```

## 3. 任务状态机

```
queued ──claim──▶ running ──▶ succeeded
   │                 ├──────▶ failed
   │                 ├──────▶ cancelled   （/停止）
   ├──cancel────────▶ cancelled           （/取消 <ID>）
   └──进程崩溃────────▶ interrupted        （启动时 recover）
```

- 只有 `queued` 可以被取消；`running` 的取消走 asyncio cancel + `taskkill /T`。
- `interrupted` 是"上次进程死了但任务没结束"，重启后主动通知主人。

## 4. 决策记录（ADR）

### ADR-001 队列用 SQLite，不用 Redis
**背景**：单机、单用户、峰值每秒个位数任务。
**取舍**：Redis 要额外进程和运维，且断电后 `AOF everysec` 仍可能丢一秒；SQLite 单文件、WAL 下读写并发足够、可直接用 SQL 做统计（p50/p95 段）。
**代价**：不能多机共享队列。真要多副本时，把 `store.py` 换成 Redis/Postgres 即可，接口只有 6 个方法。
**重来会怎么选**：一样。规模不到 1k QPS 不要上分布式队列。

### ADR-002 QQ 接入用 NapCat，不用官方机器人
**背景**：官方 QQ 机器人需要实名认证 + 沙箱审核，1-2 天起步；NapCat 当场可用。
**取舍**：NapCat 走第三方协议，违反腾讯用户协议，有封号风险 → 用一次性号对冲。
**代价**：必须保持 QQ 客户端运行；QQ 版本升级可能破坏注入。

### ADR-003 全权限执行 + 危险操作确认
**背景**：使用场景是"人在上课，无法点确认"，任何需要交互的权限模型都会卡死任务。
**取舍**：`--dangerously-skip-permissions` + owner 白名单 + 危险词确认 + 审计。
**明确不吹**：这是**人工确认**，不是沙箱。确认之后仍是全权限。真隔离需要 Docker/WSL/受限令牌。

### ADR-004 事件驱动，不做定时轮询
WS 长连接推送，消息到达即触发。轮询的代价是延迟（平均间隔一半）和空转。
断线由 `asyncio.wait({events.get(), reader})` 检测并重连——早期版本只 `await events.get()`，连接断了会永久卡死。

### ADR-005 幂等键 = QQ message_id
QQ 断线重连、NapCat 重投都会造成重复投递。`dedup_key` 唯一索引保证"至少一次投递 → 恰好一次执行"。
**代价**：如果用户真的想重复执行同一条消息，需要换一条（message_id 不同）。

### ADR-006 worker pool + 按 cwd 加锁
claude 在同一目录并发会互相踩文件；不同目录没有冲突。
默认 `workers=1`（保守），需要并行时改配置。多 worker 时**禁用 --resume**，避免两个任务共享同一会话上下文。

### ADR-007 自实现指标，不引 prometheus_client
只有 4 个指标，标准库足够；但严格按 Prometheus 命名约定（`_total`、`_seconds`、histogram 累积桶），换成官方库时采集端不用改。

## 5. 已知限制

- 单机单实例；`state.json` 与 SQLite 都是本地文件。
- 守护只看进程是否存在，不检查 NapCat 是否真的登录成功（需要额外探测 OneBot `get_login_info`）。
- 消息按到达顺序并发处理（`create_task`），极端情况下命令与任务可能交错。
- 无沙箱：见 ADR-003。
- Windows 特有：`.ps1` 必须 UTF-8 BOM；NapCat 的 `webui.json` 必须 UTF-8 无 BOM（方向相反）。

## 6. 可观测性

| 类型 | 位置 |
|---|---|
| 人类日志 | `logs/bridge.log`（按天轮转，保留 14 天） |
| 审计 | `logs/audit.jsonl`（任务/命令/确认，5MB 轮转） |
| 结构化状态 | `bridge/tasks.db` 的 `tasks` / `events` 表 |
| 指标 | `GET :3099/metrics`（Prometheus 文本） |
| 看板 | `http://127.0.0.1:3099/`（p50/p95、成功率、近 7 天） |
