# 配置参考

- [命令行参数](#命令行参数)
- [系统提示词](#系统提示词)
- [面板密码](#面板密码)
- [API Key 与出口绑定](#api-key-与出口绑定)
- [模型闸门](#模型闸门)
- [限额护栏](#限额护栏)
- [环境变量](#环境变量)

运行时配置都保存在 `accounts/settings.json` 里，大部分可以在看板「设置」页修改，重启后仍然生效。

## 命令行参数

```
python qoder_proxy.py [--host HOST] [--port PORT] [--lan] [--api-key KEY]
                      [--accounts-dir DIR] [--usage-dir DIR]
                      [--panel-password PWD] [--system-prompt TEXT] [--pid-file FILE]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--host` | `$HOST` 或 `127.0.0.1` | 监听地址 |
| `--port` | `$PORT` 或 `8790` | 监听端口 |
| `--lan` | 关 | 监听 `0.0.0.0`，强制要求 API Key（未指定时自动生成） |
| `--api-key` | `$API_KEY` / `$QD_PROXY_KEY` | 启动级 API Key。看板里一旦配置了 Key，这个参数就不再生效 |
| `--accounts-dir` | `$ACCOUNTS_DIR` 或 `./accounts` | 账号与设置目录 |
| `--usage-dir` | `$QD_PROXY_USAGE_DIR` 或 `./usage` | 用量流水目录 |
| `--panel-password` | — | 启动时设置面板密码 |
| `--system-prompt` | — | 请求没有提供系统提示词时，使用此参数的内容 |
| `--pid-file` | — | 把进程 PID 写入该文件，退出时删除（`stop-qoder-proxy.bat` 依赖它） |

## 系统提示词

非空的客户端系统提示词优先，其次是非空启动参数 `--system-prompt`；空字符串不覆盖下一级。两者都没有时，网关默认发送 Qoder 模板自带的提示词。显式传入 `--system-prompt "You are a helpful assistant."` 时，这段文字作为自定义值发送。

在「设置 → 系统提示词」勾选「清空 Qoder 默认提示词」并保存。没有自定义提示词的新请求将省略 Qoder 的默认提示词。Kimi 调用工具时仍会收到网关的工具协议指令。

## 面板密码

- 打开看板需要输入面板密码，默认是 `admin`。它和 API Key 互相独立
- 密码以 PBKDF2-SHA256 摘要保存，不存明文
- 登录会话有效期 7 天，网关重启后依然有效；修改密码会让所有已登录的浏览器下线
- 面板还在用默认密码时，看板不会显示明文 API Key。请先修改密码

## API Key 与出口绑定

在看板「设置」页管理，可以同时配置多个 Key：

- 每个 Key 有名称，可以单独启用 / 停用 / 删除，删除后立即失效
- 每个 Key 可以绑定出口：
  - **国际版**：走 `api3.qoder.sh`，连不上时依次切换到 `api2` / `api1`
  - **国内版**：按选中账号的实际入口走公网 `gateway.qoder.com.cn` 或企业实例专属网关；Key 绑定区域不覆盖账号的 VPC 选择
  - **不绑定**：跟随看板顶部的全局出口
- 请求的模型和 Key 绑定的出口不匹配时，直接返回 400，给出说明

**鉴权规则：**

- 只要有任何一个启用中的 Key（或启动时指定了 `--api-key`），`/v1/*` 就必须带 Key
- 否则不需要 Key，适用于本机单用户场景
- Key 可以通过以下任一方式传入：
  - `Authorization: Bearer <key>`
  - `x-api-key: <key>`（Anthropic 客户端使用）
  - 查询参数 `?key=<key>`

## 模型闸门

看板中没有这项设置的入口，需要直接编辑 `accounts/settings.json`，或者调用 `POST /settings/save`：

| 字段 | 作用 |
|---|---|
| `banned_models` | 全局禁止的模型 |
| `api_keys[].models` | 这个 Key 只能使用的模型，留空表示不限制 |

两个字段都支持 fnmatch 通配符（如 `qwen*`），不区分大小写。被拒绝的请求在本地直接返回，不会发到上游。三个入口（Chat / Responses / Anthropic）共用同一套判断。

### 未知模型与模型身份审计（Issue #163）

环境变量 `QD_UNKNOWN_MODEL=pass|auto|error` 控制目录中尚未识别的模型名：

| 值 | 行为 |
|---|---|
| `pass`（默认） | 保持旧版透传，兼容 BYOK / 自定义模型名；上游仍可能自行回退到 auto |
| `auto` | 将未知名称明确转换为上游 `auto` |
| `error` | 本地返回 HTTP 400，在付费请求发出前拒绝未知名称 |

判断使用冻结别名、官方 key、动态目录 key / 显示名 / 展示 ID，以及本地与磁盘目录缓存，不只检查冻结别名表。请求路径不会为判断模型额外联网；目录尚未刷新时，严格模式可能拒绝实际存在的新模型或自定义 BYOK 名称。缺省模型仍解析为 `auto`。非法配置值返回 400，不会静默改用其他策略。`pass` / `auto` 的未知模型 WARN 日志全进程每分钟最多一条，模型名最多记录 96 字符。

成功用量流水分别记录：

- `requested_model`：客户端请求名。
- `upstream_model_key`：实际提交给上游的模型 key（有请求审计上下文时）。
- `resolved_model`：上游 chunk 明确回报的模型；未回报为 `null`，不会用请求名或提交 key 推测。
- 旧 `model` 字段仍是请求侧分组 / 限额键，保持历史统计兼容，不表示已验证的实际模型。

Responses API 的响应对象与生命周期事件包含 `requested_model` / `resolved_model`，其 `model` 使用上游回报值。最初 `response.created` / `response.in_progress` 还没有上游数据时，`model` 为空字符串、`resolved_model` 为 `null`；发现实际模型后补发更新后的 `response.in_progress`，最终 `response.completed` 使用最新回报值。若回放开始前已捕获实际模型，created 也带该值。非流式响应同样保留真实模型。上游回报 `auto` 仅表示上游报告了 auto，不证明其背后物理模型或计费归属。

## 限额护栏

看板「设置 → 事实性限额护栏」。账号触碰阈值后会暂停接单，请求自动切到其他账号，不用等上游返回 402 / 429。

| 项 | 单位 | 作用 |
|---|---|---|
| `reserve_credits` | 积分点数 | 账号余额 ≤ 该值时暂停 |
| `daily_token_limit` | token | 账号当日累计 token 达到该值时暂停 |
| `daily_credit_limit` | 积分点数 | 账号当日累计消耗积分达到该值时暂停（免费模型不计入） |
| `model_daily_token_limit` | token | 账号对某个模型的当日 token 达到该值时，只暂停这个模型 |
| `expiring_window_days` | 天 | 余额将在 N 天内到期的账号优先派发。这是派发偏好，不会拦截请求 |

- 每项都可以分别设置「全局」「国际版」「国内版」三层。区域一层留空表示继承全局，填 0 表示关闭
- 默认全部关闭
- Credits 是积分点数，实测账号余额在 0 到 1100 之间。不要按 token 的量级去填，否则永远不会触发
- 因余额不足被暂停的账号，会在 UTC+8 每天 10:00（官方重置时间）恢复；如果余额提前恢复，也会提前解除暂停

## 环境变量

### 前缀会话亲和（可选：内存 / Redis）

相同出站消息前缀优先复用已有的 Qoder 上游 `session_id`。默认关闭。
单进程可直接设置 `QD_SESSION_CACHE=memory`，不需要 Redis 或第三方 Python 包。
多进程共享或网关重启后需要复用映射时，可选 Redis 后端。

这里保存的是 **前缀摘要 → 上游 session UUID** 的路由索引，不是模型 KV Cache，
也不保存对话正文、上游访问 token 或 API Key 明文。实际推理缓存由 Qoder 上游控制；
稳定 session 不保证每次缓存命中，前缀变化、上游淘汰和路由变化仍会影响命中率。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QD_SESSION_CACHE` | `off`（配置 Redis URL 时为 `redis`） | `off` / `memory` / `redis`；显式 `off` 优先 |
| `QD_SESSION_CACHE_TTL` | `3600` | 映射空闲有效期，秒，范围 60–86400；命中后续期 |
| `QD_SESSION_CACHE_MAX_ENTRIES` | `4096` | 内存后端最多保留的前缀映射条数（不是会话数），范围 1–100000，LRU 淘汰 |
| `QD_SESSION_REDIS_URL` | 空 | `redis://[:password@]host:6379/0` 或 `rediss://...`；通过环境注入凭证 |
| `QD_SESSION_REDIS_TIMEOUT` | `0.4` | Redis 每次连接/读写的超时，秒 |
| `QD_SESSION_REDIS_TTL` | `3600` | Redis 初版配置别名；`QD_SESSION_CACHE_TTL` 优先 |

按实际出站消息计算累计 HMAC，原子选择最长匹配前缀（内存锁 / Redis Lua）。前缀至少
包含第一条非空 user 消息，不会仅凭共同 system 合并会话。映射按 API Key 标识、上游
账号、区域、模型、工具和参数隔离；客户端提供 `X-Conversation-Id` 等会话 ID 时也隔离
不同会话。未提供 ID 时，同一 API Key 下完全相同的初始对话前缀可共享 session；
需要独立对话时应显式传会话 ID。账号故障切换后重新查找该账号自己的映射。

内存后端重启清空，过期/淘汰后重新生成 UUID；最多保留指定条数。
Redis 后端仅保存 HMAC 键与随机 UUID 值（共享部署需使用相同面板会话签名密钥），
可配持久化；只支持单机或代理入口，不跟随 Cluster 的 MOVED/ASK 重定向。
Redis 不可达、配置错误或缓存值非法时，回退到新的随机 session，可能冷启动但不阻断推理。
日志不打印连接地址或异常详情。普通推理用量记录包含 `session_affinity`（`memory-prefix` /
`redis-prefix` / `random`）和 `upstream_session_id`，用于区分路由复用与实际 token 命中。

可选 Redis Compose 源码构建方案：

    docker compose -f docker-compose.yml -f docker-compose.redis.yml up -d --build

Redis 仅开放 Compose 内部网络，数据由专用命名卷保存。
设置 `QD_SESSION_CACHE=off` 并重启网关即可恢复随机 session 行为。

### 基础

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8790` | 监听地址和端口 |
| `API_KEY` / `QD_PROXY_KEY` | — | 启动级 API Key |
| `ACCOUNTS_DIR` | `./accounts` | 账号目录 |
| `QD_PROXY_USAGE_DIR` | `./usage` | 用量目录 |
| `QD_PROXY_DEFAULT_REALM` | `cn` | 首次启动时的全局出口（`cn` / `intl`）。之后在看板切换的结果会保存下来 |
| `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` | — | 访问上游时使用的代理，见[出口代理](deployment.md#出口代理) |
| `QD_MAX_PAYLOAD_BYTES` | `52428800` | 客户端请求体大小上限（50 MB） |
| `QD_MAX_BODY_MB` | `29` | 发往上游的请求体（编码后）上限，单位 MiB。超过时本地直接返回 413，不发请求、不影响账号。上游网关对单个请求体有约 30MB 的硬限制 |

### 路由与会话

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QD_AUTO_REALM` | `1` | 请求没有指定区域时，按模型归属和账号可用性自动选区，见[双区路由](models.md#双区路由) |
| `QD_AFFINITY_BY_PREFIX` | `1` | 同一段对话固定使用同一个账号，以命中上游的前缀缓存 |
| `QD_AFFINITY_MAX_MSGS` | `0` | 对话超过 N 条消息后不再固定账号，`0` 表示不限制 |
| `QD_AFFINITY_DEBUG` | `0` | 打印会话亲和的调试日志 |
| `QD_INTL_GATEWAY` | `https://api3.qoder.sh` | 国际版推理主机。连接失败时依次切换到 `api2` / `api1` |
| `QD_CN_VPC_ENDPOINT` | — | 国内版默认 VPC 实例名或专属域（如 `TARGET` / `TARGET.vpc.qoder.com.cn`）。账号显式实例 / 显式公网、凭证域推断优先于该默认；未设且无其它来源时走公网。按账号推导 openapi / gateway / website，VPC 不回退公网。仅 CN 生效，见[企业 VPC 专属域](accounts.md#企业-vpc-专属域cn) |

### 会话缓存保温（可选）

上游对 prompt 前缀的缓存有闲置过期行为（实测 ~5 分钟、命中续期，见配套 issue）。会话闲置超过该窗口后，下一发请求全量重写：面板表现 0% 命中、全价积分。保温线程会在会话闲置到阈值时，用内存里登记的上一请求 payload 原样重发一次 `stream=false, max_tokens=1` 的最小请求——前缀逐字节相同即命中缓存并续期，成本 ≈ 一次缓存读（124k ≈ 10.7 credits），远低于全量重写（≈137）。

默认关闭。开启前请先了解两点：① 保温请求是真实的上游调用，持续闲置会持续消耗小额积分；② 上游对「同会话高频重复请求」的风控容忍度未知，建议从小间隔起步，观察 `/health` 的 `risk` 遥测（queue / rate / rejected）再调整。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QD_CACHE_KEEPALIVE` | `0` | 总开关，`1` 开启（不开不存任何数据，行为与改动前一致） |
| `QD_CACHE_KEEPALIVE_INTERVAL` | `240` | 会话闲置多少秒后保温一次，须小于上游 TTL（~5 分钟） |
| `QD_CACHE_KEEPALIVE_MAX_AGE` | `1800` | 会话闲置超过该秒数后不再保温（LRU 淘汰） |
| `QD_CACHE_KEEPALIVE_MAX_CONVS` | `2` | 内存里最多登记几个会话的 payload，超出 LRU 淘汰 |

保温请求照常记账，`key_id=keepalive`，面板「最近请求」里可见、可审计；只存 payload 本身，不落盘。

### 上游容错

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QD_QUEUE_MAX_ROUNDS` | `6` | 上游排队（10605 / RESOURCE_LIMIT）时最多等待重试几轮 |
| `QD_QUEUE_WAIT_MAX_SECS` | `180` | 单轮最多等待的秒数 |
| `QD_QUEUE_WAIT_BUDGET_SECS` | `600` | 排队总共最多等待的秒数 |
| `QD_QUEUE_WAIT_JITTER` | `2` | 每次排队等待额外加上 0 到 N 秒的随机抖动，避免多个客户端同时唤醒 |
| `QD_QUEUE_FAST_MODELS` | （空） | 按模型强制 fast 排队：逗号/分号分隔的公开模型名或 modelKey（大小写不敏感，支持 `*` 通配）。命中的模型遇 10605 排队立即 503 + Retry-After 交给客户端退避，不占流式等待预算；未列出的模型照常走 `QD_QUEUE_WAIT_MODE` 全局档。典型用途：限免/赠品模型交给网关层退避，付费模型维持流内排队 |
| `QD_BAD_ARGS_MAX_RETRIES` | `1` | 工具调用回声坏参数（InvalidToolArguments / #72 EmptyToolEcho）时同账号自动重发的次数上限。回声是**采样随机**的模型内容问题，重试一轮大概率好；仅在**尚未向客户端发出任何字节**时才会重发（已吐出正文的混合轮绝不重发，客户端不会看到两段矛盾内容）。`0` 恢复旧的「永不重发」语义。启动时读取 |
| `QD_SSE_HEARTBEAT` | `5` | 等待首字期间发送 SSE 心跳的间隔（秒），`0` 表示关闭 |

### 流量守卫

限制单个账号的并发与请求节奏，避免把账号打到并发过高、触发排队或风控。用途和边界见[团队与企业账号](accounts.md#团队与企业账号)。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QD_TRAFFIC_GUARD` | 未设置 | `on`：对所有账号启用守卫；`off`：全部关闭（包括团队账号）；未设置时由 `QD_TEAM_GUARD` 决定 |
| `QD_TEAM_GUARD` | `auto` | `auto` 只对识别为团队 / 企业的账号启用；`on` 对所有账号启用；`off` 关闭 |
| `QD_TEAM_MAX_CONCURRENCY` | `1` | 每个账号同时进行的请求数上限，超出的请求排队等待 |
| `QD_TEAM_MIN_GAP_MS` | `400` | 同一账号两次请求启动之间的最小间隔（毫秒）。需要等待时另加 0–120 毫秒随机抖动 |
| `QD_TEAM_WAIT_SECS` | `30` | 拿不到并发名额时最多等待的秒数，超时后放行并打印 WARN，不会卡死 |
| `QD_TEAM_MAX_CLIENTS` | `3` | 一小时内同一账号来自超过 N 个不同客户端 IP 时打印 WARN，提示可能被多人共用 |

### 请求改写

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QD_STRUCTURED_TOOL_HISTORY` | `auto` | 工具调用历史以结构化形式发给上游。`auto` 在 CN / INTL 的 Qwen / GLM / DeepSeek / Kimi 上启用；也可设为 `on` / `off` |
| `QD_TOOL_RESULT_KEEP` | `2000` | 截短历史工具结果时，头尾各保留的字符数；`0` / `off` 表示不截短 |
| `QD_TOOL_RESULT_SCOPE` | `tail` | 截短范围：`tail` 只保留最后 N 条完整、`all` 全部截短、`turn` 是旧行为（会破坏前缀缓存） |
| `QD_TOOL_RESULT_KEEP_TAIL` | `1` | `tail` 模式下保留完整的最后几条 |
| `QD_TOOLCHOICE_NONE_DROPS_TOOLS` | `0` | 设为 `1` 时，`tool_choice="none"` 的请求会连同 `tools` 一起删掉（旧行为） |
| `QD_KEEP_TOPLEVEL_IMAGES` | `0` | 设为 `1` 时，图片除了放在消息内容里，还会在请求体顶层再放两份（旧行为，仅供排查。会让多图请求的体积变成 3 倍） |

### Anthropic 入口

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QD_ANTHROPIC_MSG_SYSTEM` | `inline` | `messages` 里的 system 消息如何处理：`inline` 原位改为 user 消息（保持缓存前缀稳定）；`merge` 合并到顶层 system（旧行为）；`drop` 丢弃 |
| `QD_ANTHROPIC_THINKING` | 请求驱动 | `empty` 表示有思考内容时输出带空 `signature` 的兼容块，`off` 表示隐藏该块；两者均不改变上游思考档位。未设置时按有效请求意图决定；未指定或显式关闭则为 `off`。支持在面板设置或按 API Key 单独覆盖 |

#### QD_ANTHROPIC_THINKING 安全启用判定标准

能否安全启用 `empty` 取决于**下游链路是否存在签名校验方**：
- **直连 Anthropic 官方 API / 会校验 signature 的网关**：不可用，空签名会被直接拒绝。
- **第三方 OpenAI 兼容反代（本项目的常见部署形态，如网关 → Qoder 上游 Qwen/Kimi/DeepSeek）**：**可以安全启用**。原因：
  1. 上游返回的是 OpenAI 格式的 `reasoning_content`，无 signature 概念；
  2. 网关入站处理消息时丢弃历史 thinking block（`elif bt == "thinking": continue`），客户端回传的空签名不会流入上游；
  3. 中转层（如 NewAPI）仅转发 chunk 字节流，不校验 signature。
- **粒度覆盖优先级**：
  1. **按 API Key 覆盖**：在 API Key 配置中设置 `"anthropic_thinking": "empty" | "off"`；
  2. **面板全局设置**：在设置页面或 API 中切换 `anthropic_thinking`；
  3. **进程级环境变量**：`QD_ANTHROPIC_THINKING=empty|off`（作为默认兜底）；
  4. **请求驱动**：以上都没设置时，`thinking.type=enabled/adaptive`、有效档位或正整数思考预算请求输出 `empty`；`thinking.type=disabled` 和有效档位 `none` 不请求输出；
  5. 其余请求默认为 `off`。

### 账号与设备身份

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QD_CREDITS_USAGE_INTERVAL` | `300` | 后台刷新「最近用过的账号」余额的间隔（秒） |
| `QD_NATIVE_IDENTITY` | `1` | 签到 / 活动链路调用官方 UMID 组件获取真实机器身份 |
| `QD_UMID_DIR` | — | 提取的 UMID 组件所在的目录（默认 `<repo>/umid`） |
| `QD_RUNTIME_INFO_ENV` | 国内版 `0` / 国际版 `3` | 传给 `runtime-info` 组件的环境码，与官方桌面端一致（改变后会失效旧环境缓存） |
| `QD_MACHINE_IDENTITY_CACHE` | `auto` | 机器身份落盘缓存，设为 `off` 关闭 |
| `QD_MACHINE_IDENTITY_CACHE_TTL` | `0` | 缓存有效期（秒），到期后换新身份；`0` 表示永不过期 |
| `QD_MACHINE_IDENTITY_RESET` | — | 设为 `1` 时，启动后清空缓存并换新身份 |
| `QD_MACHINE_IDENTITY_VOTE` | `1` | 首次采集身份时多次采样取多数，设为 `0` 时只采一次 |
| `QD_DESKTOP_VERSION` | `0.4.8` | 覆盖签到请求里上报的桌面客户端版本号 |
| `QD_COSY_VERSION` | `1.1.75` | 覆盖推理 / 模型请求里上报的 CLI 版本号（`cosy-version` 请求头与签名 payload） |
| `QD_DEVICE_ID_LEGACY` | `0` | 设为 `1` 时，推理用的设备码保持 v1.5.9 之前的 32 位 hex 形态（默认是 UUIDv4 形态）。从旧版本升级、不想让上游把存量账号看成换了设备时使用 |
| `QD_COSY_OS_HEADERS` | `1` | 请求携带 `cosy-machineos`（SSE 另带 `cosy-machinehostname`）请求头，`0` 表示关闭 |
| `QD_COSY_MACHINE_OS` | 按运行平台 | 覆盖 `cosy-machineos` 的取值；默认 Windows 上为 `x86_64_win32`、其他平台为 `x86_64_linux`，设为 `auto` 时按系统与架构自动识别（`{x86_64,aarch64}_{win32,darwin,linux}`） |

### 启动脚本与调试

| 变量 | 说明 |
|---|---|
| `QD_PROXY_PYTHON` | `.sh` 启动脚本使用的 Python 解释器 |
| `QD_NO_PAUSE=1` | `.bat` 启动 / 停止脚本结束时不暂停 |
| `QD_TASKS_DEBUG=1` | 把任务中心每个子调用的耗时打印到 stderr |


## 联网工具与搜索后端（`QD_WEB_TOOLS`）

客户端在请求里声明的内置工具 `web_search` / `web_fetch`，当前推理协议不能直接代跑；
开启本功能后由**网关代跑工具**：把声明改写成同名 function，模型调用时由所选后端搜索/抓页面，再把结果回灌给模型继续生成。默认最多 3 轮工具调用，随后保留一轮最终回复；后端默认仍在本地执行。

| 开关 | 说明 |
|---|---|
| 看板「设置 → 联网工具」 | 总开关、后端及外部服务配置保存在 `accounts/settings.json`；点击「保存并应用」后，新请求立即生效 |
| `QD_WEB_TOOLS=on/off` | 环境变量优先于面板开关 |
| `QD_WEB_TOOLS_ROUNDS` | 代跑轮次上限，默认 3 |
| `QD_WEB_TOOLS_BACKEND=local/qoder/external` | 覆盖搜索后端。网页抓取选择「沿用搜索设置」时也会受影响。未设置时以面板为准，默认 `local` |

网页抓取可以独立选择「网关直接抓取」「Qoder 官方抓取」或「自定义 HTTP JSON 抓取」。默认是「沿用搜索设置」，旧配置继续按下表执行。

| 选择 | 搜索 | 网页抓取 |
|---|---|---|
| 原项目内置（`local`） | DuckDuckGo → Bing → Wikipedia 引擎链 | 网关直接抓取 |
| Qoder 官方（`qoder`） | 当前推理账号的官方搜索服务 | 当前推理账号的官方抓取服务 |
| 外部搜索服务（`external`） | 面板配置的本地或云端 HTTP 服务 | 网关直接抓取 |

配置在每次 API 请求开始时读取一次；面板切换不会把正在进行的多轮调用混到不同后端。普通应用 function 不被接管，也没有跨后端自动回退。

本地后端安全边界：只允许 http/https；拒绝带凭据的 URL、localhost、私网与链路本地地址；
重定向逐跳复检；响应体 4MB / 正文 20000 字上限。目标 DNS 无论是否使用代理都需通过公网地址检查；
本地工具的代理需显式设置 `QD_WEB_TOOLS_PROXY`，不会自动继承 `HTTPS_PROXY`。

代价：每代跑一轮都会多消耗一次账号额度；默认关闭。

### 外部搜索服务

在「设置 → 联网工具」选择「外部搜索服务」，选择服务类型，填写完整搜索接口地址和所需密钥，然后「保存并应用」。同一后端既可连接 Docker 内网服务，也可连接公网搜索 API。

支持以下预设，接口地址也可修改以使用兼容网关：

| 服务类型 | 默认地址 | 认证 |
|---|---|---|
| SearXNG | 自行填写，例如 `http://searxng:8080/search` | 通常无需密钥；需启用 JSON 输出 |
| Tavily | `https://api.tavily.com/search` | Bearer API Key |
| Brave Search | `https://api.search.brave.com/res/v1/web/search` | `X-Subscription-Token` |
| Serper | `https://google.serper.dev/search` | `X-API-KEY` |
| 自定义 HTTP JSON | 自行填写 | 可选认证请求头、前缀及专用密钥字段 |

在「请求与结果映射」中，可调整 GET/POST、查询参数名、结果数量参数名、固定 JSON 参数，以及结果列表、标题、链接、摘要的字段路径。例如某服务返回 `{"data":{"items":[{"name":"标题","link":"https://...","summary":"摘要"}]}}`，可设列表路径为 `data.items`、标题字段为 `name`、链接字段为 `link`、摘要字段为 `summary`。结果列表在根数组时路径留空；路径支持点分隔和数组数字下标，不执行表达式。GET 用 URL 查询参数，POST 用 JSON 请求体。

各服务配置和密钥独立保存，切换不会共用另一家服务的密钥。面板 API 只返回专用密钥是否已设置；同一来源（协议、主机和端口）下留空可保留旧密钥；来源变更时，留空会清除旧密钥。也可勾选「清除当前服务已保存的密钥」主动删除。密钥使用专用字段，不放在固定参数或 URL 中。配置保存在 `web_tools_backend`、`web_tools_external_provider` 和 `web_tools_external_profiles`。

外部服务地址只能由面板管理员配置，模型不能选择服务地址、请求头或密钥。这里允许管理员指定的本地/私网搜索服务；模型请求的网页及搜索结果链接仍须经过公网限制。外部 API 不跟随重定向，不自动重试，不把认证转发给其他地址。超时 15 秒、JSON 响应最多 2 MiB。容器内 `localhost` 指容器自身；跨容器接入需共享 Docker 网络并使用服务名。

预设协议参考：[SearXNG](https://docs.searxng.org/dev/search_api.html)、[Tavily](https://docs.tavily.com/documentation/api-reference/endpoint/search)、[Brave Search](https://api-dashboard.search.brave.com/api-reference/web/search/get)、[Serper](https://serper.dev/)。服务的可用性、密钥和用量规则由对应服务决定。

### 自定义网页抓取

在「网页抓取后端」选择「自定义 HTTP JSON 抓取」，填写**受信任的管理员配置服务**的完整接口地址，例如 `http://fetcher:8080/fetch`。默认以 JSON POST 发送 `{"url":"https://example.com/"}`，并读取 `text` 正文、可选的 `title` 和必填的 `final_url`。GET 使用查询参数。字段路径支持点分隔，例如 `data.markdown`。

抓取密钥和搜索密钥分开保存。抓取服务地址的协议、主机或端口变更时，必须重新提供密钥，否则清除旧密钥。密钥使用专用字段，不放在固定参数或 URL 中。

网关会检查提交的目标和服务报告的实际最终网址；缺少有效最终网址时不会接受正文。**这不控制远端连接**：抓取服务必须自行在连接时和每次重定向时只允许公网目标，并以出站网络策略阻止对私网、环回和链路本地地址的访问。不要接入无法保证这些约束的服务。网关不会跟随抓取 API 本身的重定向；JSON 响应最多 2 MiB，正文最多 20000 字。

### 可选的 Qoder 官方搜索 / 网页抓取（国内版 / 国际版）

```bash
QD_WEB_TOOLS=on QD_WEB_TOOLS_BACKEND=qoder python qoder_proxy.py
```

也可直接通过面板选择「Qoder 官方搜索」，把网页抓取设为「沿用搜索设置」并启用总开关。Docker Compose 的环境变量方式适合需要强制固定后端的部署。需要包含新增工具模块的源码或镜像；仅设置旧镜像的环境变量不会新增功能。

- 复用**当前这轮模型推理实际选中的账号**及其 COSY 签名。不会另选账号、跨区借用凭据，也不需要额外搜索 API Key、Node 或官方 CLI 进程。
- 支持国内版和国际版公网账号，按当前推理账号的区域自动选择端点：国内版使用账号的 `gateway`，国际版使用 `https://center.qoder.sh`。国际版的 `QD_INTL_GATEWAY` 只覆盖推理地址，不改变官方工具地址；两个区域不互相借用账号或自动回退。
- 企业 VPC 工具尚未验证，显式报错。已有登录过期或官方服务拒绝请求时也返回工具错误，不会静默切换本地搜索。
- 查询和抓取 URL 会发送给 Qoder 官方服务；连接沿用账号模块的 `HTTP_PROXY` / `HTTPS_PROXY` 策略。
- 抓取前执行本地 URL / DNS 公网检查，正文最多返回 20000 字。服务端返回的最终重定向 URL 也会检查；**远端中间重定向无法由网关逐跳审计**，不能套用本地后端的逐跳连接保证。
- 官方搜索返回的是 Qoder 工具服务结果，不等于模型服务商原生搜索，也不保证比本地引擎结果更好。额外搜索 / 抓取是否收费、按何规则计费，以官方账号规则为准；网关没有得到独立工具用量字段。
- 实测一个用户报告无可用模型额度的国际版账号仍能搜索和抓取；这不能证明工具免费，也不绕过模型推理的额度要求。网关仍使用实际选中的推理账号执行工具，不另选“搜索专用”账号。
- 本功能只代跑客户端声明的内置 `web_search` / `web_fetch`。WebUI、Hermes 等自行执行的普通 function 工具仍由客户端处理，不会因设置这个变量自动改用官方搜索。

Responses 客户端仅需声明 `web_search`，即可搜索和打开网页：

```json
{
  "model": "YOUR_MODEL",
  "input": "需要联网处理的用户问题",
  "tools": [{"type": "web_search"}],
  "tool_choice": "auto",
  "max_tool_calls": 6,
  "include": ["web_search_call.action.sources"],
  "stream": true
}
```

网关不追加新闻核查或回答风格的系统提示词；工具说明只描述能力和返回字段。何时搜索、查询词、是否阅读全文、回答语言与格式由应用提示词及模型决定。`tool_choice:none` 会禁止网关工具；`auto` 不保证模型必定搜索。保留应用的 `instructions` 及显式并行设置。项目原有的缺省系统提示词规则仍然适用，应用可按现有方式提供自己的指令。

Responses 返回 `web_search_call`（搜索或 `open_page`）、实际工具进度事件和已观测来源的 `url_citation`。`include` 可要求搜索来源列表。工具进度实时发送，最终正文仍先聚合再输出，并非逐 token 透传。引用只表示链接来自本次工具结果，不代表正文事实已经核实。

`max_tool_calls` 默认 8、最高 32；与管理员配置的轮次上限同时生效。重复的相同工具调用可在单次 API 请求、同一账号内复用结果。`web_search.filters.allowed_domains` / `blocked_domains` 在网关后处理，不依赖上游是否执行过滤；也约束抓取的输入和返回最终 URL，不能阻止远端已经发生的中间跳转。

此适配覆盖常用无状态文本搜索流程。云端会话续接、后台任务、仅缓存搜索及图像搜索未实现，相应请求显式拒绝；定位和搜索上下文大小未映射到官方接口。普通应用 function 调用仍交给应用执行；上游若在同一批混合返回托管与应用工具，目前会报错。存在应用工具且客户端未指定并行设置时，网关默认串行以减少这种情况。

协议与验证范围见[官方工具协议记录](official-web-tools.md)。没有环境覆盖时，在面板选择「原项目内置」并保存即可切回原实现。

### 结构化工具历史（`QD_STRUCTURED_TOOL_HISTORY` / `_REALMS`）

网关把 assistant 的 tool_calls 写回历史有两种形态：

- **结构化**：`tool_calls` 字段直传（上游原生支持时最干净）；
- **文本化**：改写成 `[assistant 请求调用工具]` + JSON 数组塞进 `content` 里。

文本化是兼容性兜底，但长会话里模型会**照抄这段脚手架**，回读链认不出来就只能 fail-closed（`502 … marker_only` / `unrecovered_falsified`）。

| 变量 | 取值 | 说明 |
|---|---|---|
| `QD_STRUCTURED_TOOL_HISTORY` | `auto`（默认）/ `on` / `off` | `auto` = CN / INTL 的白名单模型（Qwen / GLM / DeepSeek / Kimi）用结构化 |
| `QD_STRUCTURED_TOOL_HISTORY_REALMS` | 例如 `cn,intl` | 覆盖 `auto` 生效的区域集合 |

CN / INTL 默认均启用上述白名单；需要缩小范围时设置 `QD_STRUCTURED_TOOL_HISTORY_REALMS=cn`，关闭原生历史时设置 `QD_STRUCTURED_TOOL_HISTORY=off`。工具 ID 不齐备仍回退文本化。

### Kimi 工具参数 JSON（#153）

Kimi 的展示名（如 `Kimi-K3.1-Pro`）和内部 key（`kmodel` / `kmodel_latest`）采用同一模型判断。默认保留 `assistant.tool_calls` 与 `role:tool`，避免长工具链反复展示双层 JSON 文本信封，诱导模型照抄。assistant 工具消息的 `content` 保持空字符串而非 `null`。

存在工具声明且 `tool_choice` 不是 `none` 时，在现有系统提示末尾追加固定的工具协议约束：原生调用通道、参数 JSON 对象、正确转义、遵守原 schema、完整闭合、长写入按工具能力拆分。该段提示不随步数变化，不覆盖客户端提示，不修改 schema、推理强度或采样参数；普通回答不强制调用工具。

这属于生成端预防，不保证上游永远输出合法 JSON。回读与参数校验仍保留：错误参数不会通过猜测补齐后执行，也不会因该错误自动重发收费请求。现有历史中已包含的文本信封不会被猜测转换；建议升级后新开对话验证长链。离线回归覆盖请求构造与协议出口，实际模型生成改善需真实环境观察。

## 性能与看板缓存（v1.8.0）

统计概览与详细统计使用 10 秒固定 TTL 的有界结果缓存；日志发生追加、截断或替换时立即失效，相同冷查询并发合并。最近请求的行数计数按文件指纹复用，推理不走此缓存。`/health.analytics_cache` 暴露命中/计算/合并等待数量，不包含凭据。

日用量缓存不再因持续轮询延长过期时间。基准与适用边界见 [v1.8.0 性能说明](performance-v1.8.0.md)。

## 图片预处理与转换超时（`QD_IMAGE_TRANSCODE`）

`Image conversion timeout` 表示 provider 的图片转换阶段超时，与模型首字超时、网关 Key 失效不同。网关不会把图片删成文字来伪装成功。

- Chat / Responses / Anthropic 桥接后，在统一出站构造中处理内联图片。
- `QD_IMAGE_TRANSCODE=auto`（默认）：大于等于 256 KiB 的 PNG/BMP/TIFF 优先归一；不缩分辨率，不移除任何图片。非透明图片可改为 quality=95、4:4:4 JPEG，且必须比原图至少小 20% 才替换；透明图保留无损 PNG。需要逐像素输入时设 `QD_IMAGE_TRANSCODE=off`。
- 小图、JPEG、动画、带 EXIF/ICC/色彩或 DPI 元数据的图、16 位图、解码失败的图片原样保留；远程 URL 不下载、不改写。
- `/health` 的 `image_preflight.codec_available` 可确认当前运行解释器是否具备编解码组件；`enabled` 表示预处理是否开启。
- 归一结果只放内存 LRU（最多 32 条、32 MiB），相同图片跨轮与重试使用相同字节，不落盘，不把 base64/URL/凭据写进日志。
- 官方 Docker 镜像包含 Pillow；本机部署可在**运行网关的同一 Python 解释器**安装：

```bash
python -m pip install "Pillow>=11.3,<14"
```

未安装 Pillow 时核心代理仍正常运行，图片原样透传；有大图时日志以 `codec_missing` 提醒。安装后重启网关。

转换超时仅在尚未输出时允许一次自动重试（受现有重试开关限制）；耗尽后保留 HTTP 504，返回 `upstream_image_conversion_timeout`，保留上游 request_id，不冷却账号、不解绑会话、不对全池重复转换。该修复减少转换压力与重试放大，不承诺消除上游服务本身的间歇超时。

## 工具协议残片的收尾策略（`QD_TOOL_ECHO_MODE`）

有些模型（实测 `Qwen3.8-Flash`）会把工具调用**当成正文**吐出来（DSML 标签 / `[assistant 请求调用工具]` 标记 + 半截参数），随后流结束。网关识别到这类残片时默认 **fail-closed**：不再把残片当正文补发，流式响应以 `response.failed`（Responses）或错误帧收尾。

问题在于：部分客户端（Codex）收到 failed 后会**静默结束对话**，用户看到的是「话说到一半突然没了、没有任何提示」。如果你遇到这种中断、且日志里有 `unrecovered_dsml` / `marker_only` / `truncated_arguments` 这类字样，可以切到 lenient：

```
QD_TOOL_ECHO_MODE=lenient
```

| 取值 | 行为 |
|---|---|
| `strict`（默认） | 以失败收尾（`response.failed`），客户端应看到错误并重试 |
| `lenient` | **按正常完成收尾**并保留已产出的正文，本轮算完成、可直接继续；日志仍打 WARN、`最近请求` 里有一行 `lenient_tool_echo` 记录 |

lenient 是**折中**：它把「整轮丢失」换成「这一轮没有工具调用、下一轮模型会重新发起」，代价是客户端不知道残片被丢弃了。上游修好输出形态后建议切回 strict。
