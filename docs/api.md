# 接口参考

## 鉴权方式

| 标记 | 含义 |
|---|---|
| 公开 | 不需要任何凭证 |
| Key | 需要 API Key（`Authorization: Bearer` 或 `x-api-key`）。仅回环监听且未配置 Key 时可直接访问。已登录的面板会话同样可以访问 |
| 面板 | 需要面板会话：先调用 `POST /panel/login` 拿到 token，之后在请求头 `X-Panel-Token` 中带上 |

## 推理

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/v1/chat/completions` | Key | OpenAI Chat Completions |
| POST | `/v1/responses` | Key | OpenAI Responses API |
| POST | `/v1/messages` | Key | Anthropic Messages |
| POST | `/v1/messages/count_tokens` | Key | Token 数估算（本地计算，不请求上游） |
| GET | `/v1/models` | Key | 模型清单；`?merge_realms=1` 同时返回两个区域的模型 |
| GET | `/models/refresh`（或 `/v1/models/refresh`） | Key | 立即向上游重新获取模型清单，并更新磁盘缓存。区域的选择规则与推理请求相同 |

- 指定区域：使用 `X-Realm: cn|intl` 请求头，或 `?realm=cn|intl` 查询参数；普通推理 JSON 中的 `realm` 不参与路由。Key 的区域绑定优先于请求头/查询参数，需核对实际响应区域。`/diag/queue` 的 JSON `realm` 是该诊断接口显式支持的例外，不应推广到推理接口。
- 实际走的区域会写进响应头 `X-Qoder-Realm`；如果发生了自动切换，切换原因写进 `X-Qoder-Realm-Reason`
- Chat Completions 请求已知目录模型时，如果上游仅回报通用标签 `auto`，响应 `model` 回显请求名。上游明确回报的其他模型名不会改写，未知/BYOK 和自动路由模型也不会改写；用量中的 `upstream_model_key` 和 `resolved_model` 始终独立记录。
- Chat Completions 与 Responses 支持命名思考档位和显式关闭（例如 `enable_thinking: false`）。仅指定启用时沿用模型默认值；Token 预算不猜测成档位。最近请求的 `reasoning_intent` 区分未传、命名档位、关闭、默认启用、预算未映射和未识别参数；实际消耗的 `reasoning_tokens` 与请求意图是不同指标。

## 额度构成与端点自动探测

不同区域的额度接口不是一套：CN 走 `openapi.qoder.com.cn/api/v2/quota/usage`；INTL 上这个路径常常 404/403，需要用别的端点。网关内置**候选矩阵**（主机 × 路径），首次取额度失败时自动探测并把命中的组合记到 `accounts/credits-source.json`，之后优先使用。

```bash
curl -s -X POST http://127.0.0.1:8790/credits/probe \\
  -H 'Content-Type: application/json' -H "X-Panel-Token: $TOKEN" \\
  -d '{"limit": 3}'
```

返回每个候选的 `status` 与命中的 `fields`；命中的那个会被记住。面板「用量洞察 → 额度构成」也有「自动探测」按钮。

### 构成口径

面板把额度拆成四类（按上游返回的容器名归一，认不出就不显示、不臆造）：

| 列 | 来源字段 |
|---|---|
| 套餐内 | `userQuota` / `planQuota` / `subscriptionQuota` |
| 资源包 | `addOnQuota` / `resources` / `packages[]` |
| SOTA | `sotaCredits` / `sota` / `sotaQuota` |
| 共享池 | `orgResourcePackage` / `sharedQuota` / `teamQuota`（组织级，汇总去重） |

每类都带 `remain` / `used` / `size`，有 `expiresAt` 就显示「最近到期」。

## 排队诊断（`POST /diag/queue`，面板鉴权）

issue #158 报告两个区域、所有账号持续 `10605` / `p4` / `serviceAvailable:true`，原因未确认。排队信号不证明模型容量不足，也不证明账号、凭证、套餐或网络无问题。`p4` / `p3` 是未文档化的标签，不映射为风险等级；`serviceAvailable:true` 不代表本次请求已获准推理。

诊断会实际发出**一次上游探测**（不是只读健康检查）。它使用 `aggregate_stream()` 消费真实 SSE，包括 HTTP 200 流内的错误信封；不会只读一段响应就判成功，空流或仅握手的流不算成功，必须有有效的上游成功终止帧；也不在一次调用中循环等待、轮换账号或进行 HTTP/传输重试探测。不要高频轮询此接口。

```bash
curl -sS -X POST 'http://HOST/diag/queue' \
  -H 'Content-Type: application/json' -H 'X-Panel-Token: TOKEN' \
  -d '{"uid":"TARGET","model":"TARGET","realm":"cn"}'
```

请求参数：`model` 为待核对模型（省略为 `auto`），`uid` 可指定待核对账号；`realm` 在此诊断处理器中显式支持 JSON 传入。一次探测只代表实际使用账号/端点/模型的本次结果，不能代表全部账号、另一区域或持续状态。

| 字段 | 含义与边界 |
|---|---|
| `kind` | `ok` / `queued` / `exhausted` / `daily` / `error` 等诊断分类；`ok` 不是“账号永远不受限制” |
| `code` | 诊断记录的状态码；不要仅凭 HTTP 200 判断流内没有排队，也不要把它与上游业务码 10605 混同 |
| `queue_type` | 接受 `p0` 至 `p9` 的队列标签，其余/缺失为 `unknown`；未公开定义风险/优先级语义 |
| `service_available` | 上游可用性布尔标志；可以与 `kind=queued` 同时存在 |
| `retry_after` / `queue_count` | 解析的建议等待秒数 / 队列计数；仅接受 0–86400 的整数，未提供或格式无效时为空，不补造 |
| `reason` | `unknown`：本地解析不能确认根因 |
| `identity_source` | `native` / `derived` / `unknown`，本地身份来源，不证明上游如何判定风险 |
| `advice` | 有限等待、受控对照与支持确认的建议，不是根因认定 |
| `detail` | 排队结果为空字符串；不公开原始队列文本 |
| `elapsed_ms` | 本次探测耗时 |

遇到排队，遵守 `retryAfterSeconds` / HTTP `Retry-After`，并设有限次数和总预算；到限停止，不无限重试。普通推理默认 `QD_QUEUE_WAIT_MODE=visible`；`fast` 快速返回带 `Retry-After` 的 503，不是 429，也不修复排队。流已建立时可能通过流内事件报告排队/超时。

持续现象需要**同账号、同时间窗口、同区域、同明确模型**对照官方客户端与网关，保持输入/出口等变量一致、顺序执行并记录起止时间。共同排队或仅网关排队都不足以单独确认容量或策略原因；将脱敏结构化结果交官方支持确认标签含义、账号/套餐资格和服务状态。不要通过换身份/账号/出口绕过，也没有住宅 IP 必定恢复的保证。完整操作见 [持续排队排查手册](<risk-control-playbook.md>)。

## 输出上限（max_tokens）

官方模型清单里没有 `maxOutputTokens`，但上游对超限请求会回 `400 invalid_parameter_error: Range of max_tokens should be [1, 131072]`。网关把这行错误当成**学习信号**：

- 解析出 `N` 后按 `(区域, 模型)` 记住并落盘到 `usage/max-output-limits.json`（7 天 TTL）；
- 之后的请求若 `max_tokens`（含 `/v1/responses` 的 `max_output_tokens`）超过 `N`，**自动压到 `N`** 并打一条 WARN，不再让客户端吃 400；
- `/v1/models` 会补上 `max_output_tokens` / `max_completion_tokens`，客户端可以自己先算。

也就是说：**第一次**超限仍会看到上游那条 400（网关要靠它才知道上限），之后同一模型就不会了。

## 探活与状态

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/ping`、`/healthz`、`/livez`、`/readyz` | 公开 | 返回纯文本 `pong`，不检查账号池 |
| GET | `/health` | 公开 | 版本、当前区域、账号数、风控信号、运行环境自检；带 Key 访问时额外返回当前账号信息 |
| GET | `/realm` | 公开 | 当前全局出口，以及自动选区是否开启 |
| GET | `/credits/summary` | Key | 各账号余额汇总，只返回本地记录的数值，**不请求上游**，适合其他系统轮询 |

`/health` 中用于排查的字段（不含凭证）：

| 字段 | 内容 |
|---|---|
| `risk` | 网关启动以来收到的风控类信号次数：`queue`（排队）、`rate`（429 限流）、`rejected`（403 拒绝）、`version_gate`（430 版本过低），以及最近一次信号的详情 |
| `queue_observation` | `queue_observation_snapshot()` 返回的安全聚合，排队原因保持 `unknown`；精确字段见下文 |
| `env` | 运行环境自检：`container`（是否在容器中）、`proxy`（是否配置了出口代理）、`accounts_writable`（`accounts/` 是否可写）、`identity_cached`（机器身份是否已缓存）、`clock_skew_s`（与上游的时钟偏差，秒）、`traffic_guard`（流量守卫模式） |

### `queue_observation` 的公开字段与限制

| 字段 | 类型 / 说明 |
|---|---|
| `reason` | 字符串，固定为 `unknown`；不由排队标签推断根因 |
| `persistent` | 布尔值，是否至少有一个作用域满足本地持续判据 |
| `active_scopes` / `persistent_scopes` | 最近一次观察距现在不超过 900 秒的活跃作用域数 / 其中满足持续判据的作用域数 |
| `observations` | 当前活跃作用域内排队样本总数，不是请求总数或历史累计 |
| `queue_types` | `p0` 至 `p9` / `unknown` 的活跃作用域计数映射；不表示风险等级或全局队列人数 |
| `max_observed_span_seconds` | 当前活跃作用域中的最大观察跨度（秒），不是已证明连续不可用的时长 |
| `process_only` | `true`：仅当前进程内存 |
| `max_gap_seconds` | `900`：持续序列允许的最大相邻观察间隔 |
| `scope_limit` | `256`：最多保留的内部作用域条目数 |
| `retention_seconds` | `86400`：内部条目的 24 小时 TTL |
| `hint` | 对观察结果及边界的说明 |

快照仅统计最后一次排队观察距现在不超过 900 秒的活跃作用域；超过此间隔即不再计入活跃/持续聚合，下一次观察重新开始序列，即使内部条目尚未达到 24 小时 TTL。

内部作用域为实际使用的账号 UID + 实际选中的推理网关 URL（含备用主机） + 实际模型键（modelKey）的 SHA256 摘要；公开响应不暴露摘要键、账号、端点、模型、原始队列文本、请求标识、提示词或凭证。

同一作用域至少 **3 次**排队观察，首末跨度 **≥900 秒**，相邻观察最大间隔 **≤900 秒**，才满足持续判据；不同作用域不能合并计数来达到阈值。成功仅在收到上游成功终止帧后重置匹配作用域，HTTP 200 握手不重置，其他账号/端点/模型成功也不清全局。

状态 TTL 为 **24 小时**，最多 **256** 个条目，容量淘汰、过期和进程重启都会丢失观察。它不是跨重启/完整历史数据库，也不能证明两次采样之间始终排队；无样本、采样不足或 `persistent=false` 不能证明恢复。`GET /health` 不发送上游探测，多实例状态不会合并。

现有 `QD_TRAFFIC_GUARD=on` 只是把全部账号纳入**按账号**闸门：默认并发 1、最小间隔 400ms，等待租约默认 30 秒后 fail-open 放行；因此不是严格全局限流器，不保证消除排队或修复上游风险策略。本文不改变任何默认值。

`/credits/summary` 返回的结构：

```json
{
  "by_realm": {"cn": {}, "intl": {}},
  "totals": {"remain": 0, "accounts": 0},
  "accounts": [
    {"uid": "", "nickname": "", "realm": "",
     "credits_remain": 0, "credits_used": 0, "credits_size": 0, "registered_at": 0}
  ],
  "note": ""
}
```

返回的是上一次记录时的余额，不是实时数值。从未记录过余额的账号也会列出，但相关字段为 `null`，并且不计入汇总。

## 看板与管理

以下端点供看板使用，均需要**面板**鉴权，另有说明的除外。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | Web 看板（公开） |
| POST | `/panel/login`、`/panel/logout`、`/panel/password` | 登录、登出、修改面板密码（公开） |
| GET / POST | `/settings`、`/settings/save` | 读取 / 保存设置（API Key、限额、模型闸门等） |
| GET | `/settings/reveal?id=` | 读取某个 Key 的明文（面板还在用默认密码时返回 403） |
| POST | `/realm` | 切换全局出口（仅面板会话可以修改） |
| GET | `/logs`、`/logs/export`；POST `/logs/clear` | 运行日志 |
| GET | `/update/check` | 检查 GitHub 上是否有新版本 |
| POST | `/admin/shutdown` | 关闭网关进程，见下节 |
| GET | `/diag/vm` | 本机虚拟化检测 |

### 关闭网关（`POST /admin/shutdown`）

停止网关进程，等价于 stop 脚本。与 `/update/apply` 同一套安全模型：

| 闸门 | 行为 |
|---|---|
| 面板会话 | 缺少或无效的 `X-Panel-Token` → `401` |
| 本机来源 | 非 loopback 或携带 `Forwarded` / `X-Forwarded-For` / `X-Real-IP` / `CF-Connecting-IP` → `403`；设 `QD_SHUTDOWN_ALLOW_REMOTE=1` 放开 |
| 显式确认 | 请求体必须是 `{"confirm": true}`，**布尔 true**；`1` / `"true"` → `400` |
| 一键禁用 | `QD_SHUTDOWN_DISABLE=1` → `403`，面板按钮同时隐藏 |
| 无监听器 / 正在关闭 | 返回 `409`；后台线程启动失败返回 `503`，可重试 |

```bash
curl -X POST 'http://127.0.0.1:8790/admin/shutdown' \
     -H 'X-Panel-Token: TOKEN' -H 'Content-Type: application/json' \
     -d '{"confirm": true}'
# {"ok":true,"stopping":true,"restart_hint":"..."}
```

由独立线程延迟关闭监听循环，给响应发送留出时间。`HTTPServer.shutdown()`
不能在运行 `serve_forever` 的同一线程调用；`ThreadingHTTPServer` 的请求工作线程
与监听线程不同，并不天然死锁。退出走与
SIGTERM 相同的收尾路径：`server_close()` → atexit（pid 文件清理）。
`GET /settings` 的 `shutdown` 块回显 `can_shutdown` / `blocked_reasons` /
`restart_hint`，面板据此决定是否渲染按钮。关闭后需用启动脚本重新拉起。

### 账号

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/accounts` | 账号列表 |
| POST | `/accounts/login/start`、GET `/accounts/login/poll`、POST `/accounts/login/cancel` | OAuth 设备授权 |
| POST | `/accounts/import/pat` | 导入 PAT |
| POST | `/accounts/import/desktop` | 请求体为 `{}` 时只扫描本机凭证；带 `{"path": ...}` 时只允许导入服务器扫描已发现的有效凭证；带 `{"all": true}` 时导入全部 |
| GET | `/accounts/export` | 仅导出账号元数据；需要面板会话，不提供凭证下载 |
| POST | `/accounts/import` | 导入账号 JSON；包含凭证的备份需在服务器终端用 `--backup-accounts` 生成 |
| POST | `/accounts/refresh`、`/accounts/test` | 刷新 Token、测试账号 |
| POST | `/accounts/set`、`/accounts/set-all`、`/accounts/delete` | 修改、批量修改、删除账号 |
| GET / POST | `/accounts/credits` | 逐个账号**向上游**查询余额（会请求上游，不要用来轮询） |
| POST | `/accounts/checkin` | 每日签到（单个或全部账号）；返回本轮新增总额 `credit_added` 和逐账号 `state`（`claimed` / `already_claimed` / `no_credit` / `failed`）。HTTP 200 只表示批次得到结果，不等于领取到账 |

国内和国际账号都尝试主动领取官方活动列表标记为可领取的积分项；发放由上游活动与账号资格决定，桌面端显示的活动并不保证同一账号在网关收到相同的可领取项。没有可领取项不会猜测到账或给当前账号补签到章。

### 签到、调度与用量

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/tasks` | 签到状态、活动、福利包资格、额度快照；`?realm=` 只看某个区域 |
| POST | `/tasks/run` | 批量签到并领取活动奖励 |
| POST | `/tasks/travel` | 批量领取 Pro 福利包 |
| GET | `/scheduler` | 调度器状态与排程日志 |
| POST | `/scheduler/trigger`、`/scheduler/toggle` | 立即执行一次巡检；开关调度器 |
| GET | `/usage`、`/usage/recent` | 用量汇总、最近请求 |
| GET | `/usage/analytics`、`/usage/by-account`、`/usage/by-key`、`/usage/perf` | 用量透视、性能指标。支持 `?range=day\|week\|month\|all\|custom&since=&until=` |
| GET | `/usage/timeseries` | 用量时间序列 |
| GET | `/usage/daily` | 每日用量，`?days=7\|30&group=model\|realm\|key\|account` |

### 智能体配置

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/agents` | 面板 | 本机客户端检测结果与接入状态 |
| GET | `/agents/models` | 面板 | 用于预填客户端配置的模型表 |
| GET | `/agents/cli` | 公开 | 配置器 CLI 的源码，供远程机器下载 |
| POST | `/agents/apply`、`/agents/restore` | 面板，且只接受本机访问 | 写入客户端配置 / 还原到接入前 |


## 用量洞察（参考 wb 的三项能力）

| 端点 | 说明 | 数据来源 |
|---|---|---|
| `GET /pricing` | 参考价目表（USD / 1M tokens）+ 未定价模型清单 | `qoder_pricing.DEFAULT_PRICES` + `usage/pricing-overrides.json` |
| `GET /accounts/credits/grants` | 积分包 / 获取记录（面额、剩余、已用） | 最近一次积分快照（**不发上游请求**） |
| `GET /accounts/credits/usage` | 官方积分消耗：累计 / 单日峰值 / 近 365 天逐日消耗 | 上游 `credits-summary` + `credits-heatmap`（0.4.3 客户端接口），每账号缓存 5 分钟；`?uid=` / `?realm=` 可过滤 |
| `GET /usage/remaining` | 每账号剩余积分 + 近 24h 消耗 + 预计可用天数 | `usage.jsonl` + 账号积分 |
| `GET /accounts/activity` | 签到 / 领取动作流水（30 天保留） | `usage/activity.jsonl` |

### 费用估算口径

- 输入按「缓存命中 / 未命中」两档、输出单独一档，单位 USD / 1M tokens；
- 只用于「这些 token 放在 OpenRouter 上大概值多少钱」的参考，**不能用于结算**；
- 未收录模型**不计价**（面板显示 `—`，绝不猜数字），可在 `usage/pricing-overrides.json` 覆盖：

```json
{"dfmodel": {"in": 0.28, "cached": 0.028, "out": 0.42},
 "my-custom-model": [0.5, 0.1, 2.0]}
```

### 动作流水

`POST /accounts/checkin` 与「领取全部福利」会往 `usage/activity.jsonl` 追加一行（时间 / 动作 / 账号 / 结果 / 积分 / 说明），
文件超过 5MB 自动轮转为 `.1`，读接口只返回最近 30 天。

