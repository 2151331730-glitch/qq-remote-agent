# QQ 远程任务 · 进度与决策记录

## 2026-10-24 建立

### 目标
上课期间电脑常驻后台，手机 QQ 发任务 → 本机 claude 执行 → 结果回 QQ。
用户选择：一次性号 <BOT_QQ> 当机器人；权限全开（要"像在电脑上操作"）。

### 已确定的技术选型与理由

1. **NapCatQQ v4.18.41（NapCat.Shell.zip）** 作为 QQ 协议层
   - 现有 QQ NT 9.9.36 已装于 `D:\Program Files\Tencent\QQNT`，Shell 版复用该客户端，不需要往 QQ 目录塞插件。
   - 不用 go-cqhttp（2023 年起停更）、不用 Lagrange（要自己装 .NET 运行时）。
   - 不用 dsh-im 插件：其 issue #434 记录了 DSH 0.2.1-rc.2 桌面版建会话崩溃（本机正好是 0.2.1-rc.2）。

2. **自写 bridge.py 而不是用现成机器人框架**
   - AstrBot/NoneBot 是为"多人群聊机器人"设计的，本项目只需要"单人私聊执行命令"，自写 300 行更可控。
   - 直接调 `claude.exe`（原生二进制，避免 .cmd 在 subprocess 里的引号问题）。

3. **事件驱动长连接，不做定时轮询**
   - NapCat 的 OneBot 11 WebSocket 反向推送，消息到即触发，比轮询省电省延迟。

4. **串行任务队列（上限 5）**
   - 多个 claude 同时改同一目录会互相踩，排队更稳。

5. **全开权限 + 白名单 + 全量日志**
   - 用户要求"像在电脑上操作"，上课时无法点确认，只能 `--dangerously-skip-permissions`。
   - 兜底：只有绑定 QQ 能发；WS 只监听 127.0.0.1 且带 token；每条命令落日志。

### 已完成
- [x] 目录 `C:\Users\win11\qq-remote\`
- [x] NapCat 解压 + `onebot11_<BOT_QQ>.json`（WS 127.0.0.1:3001，token）
- [x] `bridge/bridge.py`（命令、队列、分段、心跳、超时、断线重连）
- [x] `bridge/config.json`、`start-all.ps1`（守护重启）、`install-task.ps1`
- [x] 电源：交流电永不休眠（原本就是 0）；合盖不睡眠；无线网卡最高性能
- [x] `claude -p` 实测可用（7 秒返回）
- [x] bridge 断线重连实测（NapCat 未启动时每 5 秒重试，不崩）
- [x] `tests/mock_test.py`：假 OneBot 端到端通过（绑定 / `/心跳` / `/状态` / `!命令` / 普通任务 / 结果回传）
- [x] `tests/mock_test3.py`：`--resume` 上下文续接通过（第二条任务答出第一条设的 7）
- [x] `tests/mock_test2.py`：`/停止` 能取消运行中的 claude 并回提示
- [x] `自检.ps1` 可用（QQ/端口/桥/计划任务/电源/日志一屏看完）

### 踩到的坑（改脚本前必读）
- **`.ps1` 必须存成 UTF-8 with BOM**。PowerShell 5.1 读无 BOM 的 UTF-8 会按 GBK 解析，
  中文注释里的引号被吃掉，报 `Array index expression is missing`。三个脚本已全部转 BOM，
  以后用编辑器改完要确认 BOM 还在。
- OneBot 客户端调 `send_private_msg` 等回执是**串行阻塞**的，事件分发必须 `create_task`，
  否则慢发送会把收消息也堵住（已修）。
- 绑定主人的那条消息原本会 return，导致第一个任务被吞（已改成绑定后继续执行）。

### 2026-10-24 16:10 真机登录成功（方式与原计划不同）
- 二维码扫不了 → 改用 WebUI 的**「密码登录」**标签，账号 <BOT_QQ> + 密码直接登进去了，
  没有触发短信/设备验证。
- WebUI token：见 `napcat\config\webui.json`（**不写入文档，也不要打印**）。
- 结果：`get_login_info` 返回 `{"user_id":<BOT_QQ>,"nickname":"<BOT_NICK>"}`；
  OneBot WS 3001 已监听；bridge 16:10:45 自动连上；`send_private_msg` 给自己返回 ok/0。
- 好友列表目前只有它自己 —— 等用户大号加好友后才能端到端测。

### 2026-10-24 16:10-16:30 收尾与两个真 bug
- **好友打通**：用户大号（<OWNER>）加好友成功，桥自动同意并绑定；
  实测「在吗」→ 8 秒返回正常回复。
- **Bug 1（严重）**：`serve()` 只 `await events.get()`，NapCat 重启导致连接断开后事件队列
  永远等不到，桥卡死不重连。改成 `asyncio.wait({get_task, reader})` 谁先完成先处理；
  `worker()` 提到 `main()` 全局只起一次，避免断线把正在跑的任务一起取消。
  用 `tests/mock_test4.py`（3002 端口 + 临时配置）验证：断开 → 立刻重连。
- **Bug 2**：好友申请被 NapCat 推三次，桥批三次。加了 60 秒内按 flag 去重。
- **WebUI 收紧**：host 改 127.0.0.1、`autoLoginAccount=<BOT_QQ>`；**重启后自动登录成功**
  （WebUI 显示「QQ登录成功」，3001 自动起来，桥自动连上），不需要再扫码或输密码。
- **踩坑**：`Set-Content -Encoding UTF8` 给 `webui.json` 写了 BOM，NapCat 解析失败退回默认，
  表现为 token 失效 + 自动登录不生效。已用 Python 重写为无 BOM。
- **新增脚本**：`apply-webui.ps1`（改 WebUI 配置，必须停机时改）、`restart-bridge.ps1`
  （只重启桥，带输出日志到 `logs/restart.txt`）。
- **测试隔离**：桥支持 `QQ_REMOTE_CONFIG` / `QQ_REMOTE_STATE` 环境变量，
  测试用 3002 端口和临时 state，不干扰真机 3001。

### 待验证
- [ ] NapCat 能否在 QQ 9.9.36 上注入成功（版本差异风险）
- [ ] 一次性号能否在手机上登录（需要短信）
- [ ] 扫码登录 NapCat
- [ ] 大号添加机器人号是否被风控拦截
- [ ] 端到端：发一句话 → 执行 → 回结果
- [ ] `install-task.ps1` 以管理员注册计划任务

### 已知风险
- 第三方协议违反腾讯用户协议，号可能被封（用一次性号对冲）。
- 合盖运行散热差，长时间高负载有降频/过热风险。
- claude 每条消息带完整系统提示（约 2.8 万 token），DeepSeek 侧实际计费远低于 CLI 报的 $0.14，但量大仍要留意。

### Resume point（下次接着说）
从「第一次配置」第 1 步开始：手机登录一次性号 → 管理员启动 start-all → WebUI 扫码。
文档：`C:\Users\win11\qq-remote\使用说明.md`

## 2026-10-24 16:35 换头像实测
- 用 `set_qq_avatar`（NapCat OneBot 扩展 API）把机器人号头像换成动漫女生。
- 图片：`tmp\avatar1.png`（480x480，来自 https://www.loliapi.com/acg/pp/ 随机图，WebP 转 PNG）。
- 验证：`https://q1.qlogo.cn/g?b=qq&nk=<bot>&s=640` 拉回的头像与新图一致，retcode=0。
- 结论：NapCat 的 set_qq_avatar 可用，本地路径直接传即可（PNG）。
- 16:40 用户嫌第一张不好看，重换为 Q 版兔耳帽小女孩（tmp\a5.png），qlogo 像素比对完全一致。

### 2026-10-24 16:37 心跳与打断保护
- 心跳间隔 180 → **60 秒**（任务执行期间每 60 秒报一次「还在执行」）。
- 事故：重启桥时把正在跑的「我要洛克希头像」一起杀了，claude 子进程变成孤儿继续下载图片、结果却回不去。
  - 已手动补做：把 `tmp\roxy_anilist.png`（洛克希）通过 set_qq_avatar 设上，并用 QQ 头像 CDN 复核确认生效。
- 修复：
  - `restart-bridge.ps1` 现在**先杀桥派生的 claude 子进程**再杀桥，不留孤儿。
  - 桥把当前任务写进 `state.json` 的 `current_task`；重启后连上时若发现非空，
    主动发一条「上次重启时有个任务被打断：<内容>，需要的话重发一次」。
- 教训：任何重启都要先确认队列和运行中任务；生产环境不要在用户发任务时重启。

## 2026-10-24 16:45 代码审核（QQ 远程任务）
发现并修复：
1. serve() 里先 send(PENDING_NOTICE) 再起 reader → 提醒必然 15 秒超时发不出去；改为先起 reader。
2. resume 解析失败时无条件开新会话重跑 → 任务可能执行两遍；只在报错明确是"会话不存在"时重试。
3. /全文 对不足 3000 字的结果回"（无输出）"→ 改为"已经完整发过了"。
4. 断线时 pending 调用干等 15 秒；reader 结束时立即让它们抛 ConnectionError。
5. ws.send 抛异常时 pending 条目泄漏；已包进 try/finally。
6. /停止 取消任务后没清 STATE["current_task"] → 重启后误报"上次任务被打断"；已清。
7. state.json 非原子写 → 临时文件 + os.replace。
8. config.json 的 owner_uin 由 0 固定为当前主人号（state 丢失后别人第一条私聊会被当成主人，这是最危险的一条）。
验证：py_compile 通过；tests/mock_test5.py 全流程通过（心跳/!命令/任务/状态，8 秒完成）。
生效方式：本次改的是运行中进程的旧代码，安排 tools/apply-bridge-restart.ps1 在本任务结果发出后重启 bridge，由守护脚本自动拉起。

遗留风险（未改）：
- 全权限执行、owner 固定后仍有单点风险；WS 仅本地 + token。
- 消息用 create_task 并发处理，连发时顺序极端情况可能乱。
- 监督脚本只看进程存在，不检查 NapCat 是否成功登录。
- 无 git 版本控制；start-all.ps1 里给自动变量 $args 赋值（无害）。

注意：16:35-16:43 期间 bridge.py 被另一个会话改过（SEEN_FLAGS 去重、serve 重连重写、PENDING_NOTICE），本次审核在其基础上继续，改动已合并验证。
- 16:50 换成《无职转生》洛克希（洛琪希·米格路迪亚）头像：Bangumi 角色立绘 46465（642x1229）裁头部+女巫帽，600x600 上采样；qlogo 比对一致。素材 tmp\cand_B.png。

## 2026-10-24 17:10 创新性升级 v1（QQ 任务「那就做」）
按上一轮给的三条建议先做前两条的半成品 + 一条完整的：
1. **危险操作确认**（guard.py）：任务文本命中 删除/清空/格式化/卸载/关机 等词，或 ! 命令命中 rm -rf / del /s / format / diskpart / shutdown / Remove-Item -Recurse / reg delete / bcdedit / net user / takeown / icacls / schtasks /delete / taskkill /f，先拦下并要求 /确认（5 分钟有效），/取消 放弃。确认后仍是全权限，属于人工闸门不是沙箱。
2. **审计 + 看板 + 网页入口**（panel.py）：logs/audit.jsonl 记录 task_start/task_end/shell_start/shell_end/confirm_*；127.0.0.1:3099 看板显示状态、排队、花费、最近事件；网页可提交任务（需 X-Panel-Token，token 在 config.json，不打印）。审计文件 5MB 自动轮转。
3. **统一入口的雏形**：QQ 与网页共用同一个队列和同一个 owner 白名单；飞书等其它入口未接（需要应用凭据）。
其它：新增 restart-bridge.ps1（唯一重启入口，先杀 claude 子进程再杀 bridge，验证进程启动时间晚于文件修改时间）；tools/apply-bridge-restart.py（等本任务结果发完再重启）。
测试：tests/mock_test6.py 全部通过（危险任务拦截、/取消、/确认后执行、无 token 403、网页提交、审计事件）；mock_test5 回归通过。

### 2026-10-24 18:05-18:12 工程化升级（v2）

目标：把"能用的个人脚本"变成"面试能讲的系统"。三根支柱 + 基建。

**1. 持久化任务队列（store.py）**
- SQLite WAL；状态机 queued→running→succeeded/failed/cancelled/interrupted
- `dedup_key=qq:<message_id>` 唯一索引 → 至少一次投递、恰好一次执行
- 崩溃/重启：启动时 `recover_interrupted()`，把残留 running 标成 interrupted 并主动通知
- 轻量迁移：老库缺 `tokens_*` 列时自动 ALTER TABLE

**2. 并发与背压**
- worker pool（`workers`，默认 1）+ 按 cwd 加 asyncio.Lock：同目录串行、不同目录可并行
- 多 worker 时禁用 `--resume`，避免共享会话互相污染
- 队列深度 ≥ max_queue 直接拒绝；新增 `/任务`、`/取消 <ID>`

**3. 可观测性**
- metrics.py：自实现 Prometheus 指标（counter/gauge/histogram，累积桶语义）
- panel.py：p50/p95、24h 成功率、近 7 天柱状图、`/metrics` 端点、任务表带 token
- 事件表 events + audit.jsonl 双落盘

**4. 真实 token 计量与成本熔断**
- 实测发现：`--resume` 会话会让单次输入涨到 205k token（只回三个字）
- 记录 tokens_in/out（含缓存命中），`cost_usd` 标注为 CLI 估算值
- `session_token_limit`（默认 120k）：超限自动开新会话；重启时也丢弃超限旧会话
- 效果：205k → 29k（7 倍），单任务 8.4 秒

**5. 工程基建**
- git 仓库（3 个提交）+ .gitignore（napcat/、logs/、config.json、db 全部排除）
- 44 个 pytest 单测（store/guard/metrics/panel，含迁移测试）
- GitHub Actions CI（Windows，3.11/3.13 矩阵）
- README（能力表 + 快速开始）、ARCHITECTURE（7 条 ADR + 已知限制）

**踩坑**
- `git add -A` 把 napcat 整个目录和 webui.json（含 token）提交了 → 删库重建，补 .gitignore
- 测试里硬编码 token → 抽 `tests/_cfg.py` 统一从 config 读
- 端点 `/api/tasks` 语义从"审计事件"改成"任务列表"，旧测试要改用 `/api/events`
- `!` 命令的 PowerShell 输出偶发 GBK 乱码 → 命令前置 `[Console]::OutputEncoding=[Text.Encoding]::UTF8`
- claude.exe 自更新时偶发 WinError 216 → 起进程失败重试一次

### 2026-10-24 18:14-18:36 四件套：压测 / Redis / 沙箱 / Grafana

**1. 压测（docs/benchmarks.md）**
- 假 CLI（tests/stub_claude.py）+ 可替换命令（config.claude_cmd），压测零 token 成本
- 40 任务：1 worker 同目录 2.83/s；4 worker 同目录 2.88/s（锁生效，故意不加速）；
  4 worker 跨目录 **10.15/s（3.6 倍）**
- 优化：claim 时直接跳过已锁目录（`claim_next(exclude_cwds=...)`），跨目录吞吐 5.16 → 10.15

**2. Redis 后端（store_redis.py）**
- 同 6 个方法：HASH 存任务、ZSET 做队列、SET 记 running、Lua 原子 claim
- 演示（tests/demo_multiworker.py）：两个 bridge 进程共享队列，40 条 20/20 分摊，4.94s，8.09/s，零重复
- 坑：redis-py 8 默认 RESP3（HELLO 3），Redis 5.0 不认 → `protocol=2`

**3. 沙箱（sandbox.py）**
- `!!` 前缀命令走沙箱：Docker（--network none --rm，只挂 cwd）→ WSL（Ubuntu）→ PowerShell 兜底
- 实测 WSL 后端：`!!` 命令在 Linux 里跑，`/mnt/c` 可见
- 诚实边界写进 ADR-009：agent 任务仍是全权限，沙箱只缩小"命令级破坏半径"

**4. Grafana**
- winget 找不到包 → 直连 dl.grafana.com（Pigeon 代理，473MB）+ GitHub 下 Prometheus（108MB）
- Grafana 13 首启要从 catalog 装 datasource 插件，不挂代理会失败（Plugin not registered）
  → 用 HTTP(S)_PROXY 起进程后正常
- provisioning 自动装配数据源与 8 面板仪表盘；截图存 docs/grafana-dashboard.png

### 2026-10-24 19:00-19:05 可靠性七件套

1. **发件箱**（store/outbox）：结果先落库再发，重连补发；实测断线不丢
2. **守护健康探测**：不再只看进程，改为 3001 监听 + 桥的 ESTABLISHED 连接，连续 3 次异常才动作
3. **错误分类**：timeout / parse / cli_error / exception / spawn，入 `error_kind` 列 + `qq_remote_task_errors_total{kind}`（补上了之前只声明不打点的死指标）
4. **保留策略**：30 天清理 + VACUUM；审计轮转从"覆盖 .1"改成带时间戳文件名
5. **额度**：每小时任务数 + 每日 token，超限直接拒绝（防号被盗刷）
6. **流式进度**：`--output-format stream-json`，工具调用转成 `🔧 PowerShell: …`，默认 15 秒一条；实测发件箱里能看到
7. **单实例锁**：bind 127.0.0.1:30991，第二实例直接退出

**事故**：我自己用 `start-all.ps1 -Once` 做验证时，非管理员读不到管理员进程的命令行，
被误判为"桥没跑"并起了第二个实例（两个进程共享 state.json）。已修：判断改为看 TCP 连接，
并加单实例锁兜底。这条也说明守护脚本必须在管理员权限下跑（计划任务里是，手工验证要注意）。

**测试**：70 个（新增 outbox/额度/清理/流式/错误分类）。
