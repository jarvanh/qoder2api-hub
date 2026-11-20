# Qoder 官方 Web 工具协议与验证（国内版 / 国际版）

此记录来自 Qoder CLI 和 CLI CN 1.1.89 分发包的静态请求链分析，以及 2026-11-16 使用自有有效国内版、国际版账号的联网验证。Qoder 是闭源项目，可分析分发的压缩 JavaScript 不等于取得源码仓库或服务端实现。这里只记录互操作协议，不携带账号凭据、官方客户端代码或 WASM 二进制。

分析的 JavaScript SHA256：CN 为 `eb9d1d6ab7dbdf00c7f14706419b030747aff4e27339b6f146a99e46ff212196`，国际版为 `b7a2d6d54bf868f7226abc759c10c6361ea4ab04ddd4c0615eb90ccef3524377`。国际版从[官方 npm 发行包](https://registry.npmjs.org/@qoder-ai/qodercli/1.1.89)提取，完整 tarball 已核对 npm SHA512 integrity；CN 对应[同版本发行包](https://registry.npmjs.org/@qodercn-ai/qoderclicn/1.1.89)。沿工具声明 → 调用处理 → 服务对象 → HTTP transport 阅读调用链，观察参数、认证、响应解析与客户端后处理；未绕过认证。

## 请求链

官方客户端的 `webSearch.search` 请求依次经过登录检查、Bearer、WASM 签名及 HTTP transport。两版客户端的搜索、抓取实现及端点表逐字相同，通过区域标志选择主机：

| 区域 | WebSearch / WebFetch 的公网端点 |
|---|---|
| 国内版（CN） | `https://gateway.qoder.com.cn`，沿用当前账号的 gateway 配置 |
| 国际版（INTL） | `https://center.qoder.sh`，与 `api1/api2/api3.qoder.sh` 推理服务分开 |

国际版当前使用已验证的默认 center 地址，不进行官方客户端的动态 center 选址；`QD_INTL_GATEWAY` 不覆盖工具地址。不能仅删除区域限制后继续复用国际版推理主机。

| 工具 | 方法与路径 | 内层参数 |
|---|---|---|
| WebSearch | `POST /algo/api/v1/webSearch/oneSearch?Encode=1` | `query`、`timeRange: NoLimit`、`contents: {mainText:false, markdownText:false, summary:false}` |
| WebFetch | `POST /algo/api/v1/webFetch?Encode=1` | `url`、`maxAge:0`、`timeout:14000`、`formats:[markdown]` |

客户端另外存在 `unifiedSearch` 分支，由 Lingma / Aliyun 集成模式选择，并非国内版 / 国际版开关；本实现使用两个区域均已验证的 Qoder 公网 `oneSearch` 路径。

内层参数先转为 JSON 字符串，再放入外层：

```json
{"payload":"<内层 JSON 字符串>","encodeVersion":"1"}
```

整个外层 JSON 必须经现有 `qoder_sign.qoder_encode` 编码。使用 `SESSIONS.get(account).headers` 对**实际发送的编码后 body**及路径生成 COSY 签名，`sse=False`、`accept=application/json`。签名路径按既有实现去掉 `/algo` 前缀。

只对未编码 JSON 签名并发送会得到 HTTP 500；编码 body 后，同一有效账号和现有签名器可调用成功。无需执行官方 WASM 或把 CLI agent 包装成 API。

## 响应形状

搜索成功响应为包含 `requestId`、`pageItems` 的 JSON；条目包含 `title`、`link`、`snippet`、`publishedTime`、`hostname` 等字段。本实现映射为本地工具已有的 `title/url/snippet`，保留发布时间；去重并限制到 5 条，摘要最多 1000 字符。

抓取成功响应为包含 `requestId`、`results` 的 JSON。单个结果包含 `statusCode`、`markdown`、`metadata` 等字段。按官方解析器偏好依次读取 `markdown`、`text`、`html`、`rawHtml`，返回正文最多 20000 字，保留截断标记、最终 URL 以及至多 10 个公开页面链接。

HTTP 错误、业务 `errorCode`、结构异常和空正文都不能伪装成成功。对外只返回明确但经过收敛的错误，不回显原始服务端 body、Token 或异常内容。没有自动重试工具请求，也没有静默跨后端回退。

网关的签名 POST 只发送到选定的工具端点。该端点返回任意重定向时，本次工具调用报错。抓取网页时目标站点的跳转仍由远端工具服务处理。

## 已验证与未验证

- 使用公网 CN 账号，`oneSearch` 实际返回了技术查询的搜索结果；并非只验证 HTTP 200。
- 使用同一签名链，`webFetch` 实际返回 Python 官方 asyncio 文档的 Markdown，原始正文 53347 字符。
- 官方 CLI 的 `createSearchResult` 会在客户端再次检查域名，精确主机或子域匹配；对 `allowed_domains:[docs.python.org]` 的上游单次实测仍返回其他域名。因此本实现也在网关过滤，不能仅依赖请求参数。应用级过滤同时约束抓取的输入及返回最终地址；模型只能进一步缩小范围。
- 官方客户端 `formatResults` 包含标题、链接、摘要、发布时间、最多 10 条结果和强制来源链接提醒。本实现复用数据结构，默认最多 5 条，不照搬强制提醒。其他集成的搜索分支不视为当前账号可用能力。客户端证据不能证明服务端搜索源或排序算法。
- 官方搜索当次结果包含第三方 SEO 页面，不能据此承诺搜索质量或事实准确性。
- 两个区域均已验证搜索和网页抓取。企业 VPC、额外工具收费规则、权限变更与所有站点抓取兼容性未验证。
- 搜索 / 抓取与大模型推理是分开的工具请求；Responses 事件由网关生成，不会增加模型厂商云端 Agent 状态。

## 验证方法

离线测试：

```bash
python tests/run_all.py --jobs 2
```

离线套件验证编码与实际 body 签名一致、当前账号传递、无额外账号选择、错误不泄露凭据、不静默回退、URL 检查、内容裁剪，以及真实网关工具处理器的多轮回灌、最后一轮回复、事件序号 / ID、引用、用量汇总、调用预算、应用指令保留和应用 function 交还。

手动联网诊断（显式执行才出网，可能产生官方工具用量）：

```bash
python scripts/probe_official_web.py --accounts-dir /path/to/accounts --query 'Python asyncio TaskGroup'
python scripts/probe_official_web.py --accounts-dir /path/to/accounts --url https://docs.python.org/3/library/asyncio-task.html
python scripts/probe_official_web.py --accounts-dir /path/to/accounts --realm intl --query 'Python asyncio TaskGroup'
```

可用 `--realm cn` / `--realm intl` 筛选账号；筛选后仍有多个账号时必须通过 `--account UID` 明确选择。脚本仅读取现有登录，不刷新、不保存或打印 Token；输出仅含账号区域、成功状态、条目数或正文字符数，不向模型发起推理请求。

## 面向通用应用的协议适配

- 保留应用的 `instructions`、`tool_choice` 及显式并行设置；不注入新闻核实、指定语言、来源或回答格式的系统提示词。
- 声明 `web_search` 同时获得打开网页能力。Responses 输出 `web_search_call`，动作区分 `search/open_page`；普通 function 交还应用执行。
- 流式请求在推理前发送 `response.created/in_progress`，实际调用工具时发送生命周期事件。事件序号递增，工具 ID 保持一致，失败不伪装成完成。
- `include:[web_search_call.action.sources]` 返回搜索来源列表。回答引用本次观测过的链接时生成 `url_citation`，不为未观测链接编造标注。
- 工具预算用尽后保留一次模型回复，汇总各轮模型 token 与用量细节。调用结果仅在本次请求、同一账号中复用。
- 进度实时发送，最终正文仍聚合后输出。未实现云端会话续接、后台任务、仅缓存搜索和图像搜索；定位及搜索上下文大小未映射。同批混合托管工具与应用 function 目前显式报错。

来源标注只说明链接出现在工具结果中，不证明每个断言都由链接支持。搜索摘要不能等同于阅读全文，最终回答质量仍取决于模型规划和来源质量。

## 验证记录与限制（2026-11-16）

离线验证覆盖签名、账号作用域、失败处理、域名过滤、调用预算、事件序号、来源标注、用量汇总和应用指令保留。后端切换测试另外验证保存原子性、进行中请求的配置快照、服务间密钥隔离、掩码回显、自定义 HTTP 映射和拒绝转发重定向凭据。跨平台测试矩阵由项目现有 CI 执行。

使用自有公网 CN 账号的真实请求已验证：

- 搜索、页面抓取及模型多轮回灌可完整执行；一次自然文档提问由模型自主抓取 Python TaskGroup 官方文档，最终返回来源标注。
- Responses 在实际工具执行期间输出进度；测试中也出现过后续模型流失败，网关以 `response.failed` 结束，不以 HTTP 200 判定成功。
- 新闻查询可能多次搜索但不打开原文，也可能在最终回答中缺少引用；协议适配不保证研究质量。

使用相同的 3 组查询和 2 个 URL，官方后端比现有 DuckDuckGo → Bing → Wikipedia 链更相关，并改善了一次纽约时报中文页面抓取超时；已部署 SearXNG 对技术文档的来源质量仍更好。每项只测一次，这些观测不构成性能基准或长期可用性保证。

外站及论文检索的抽样结果：

- 搜索可返回 Reuters、英文 Wikipedia 等外站，但针对 X 的查询没有得到有效站内结果，页面抓取也未取得可读内容。不能仅据此区分索引、反爬或网络限制。
- 12 组学术查询中，增加官方站点限定改善了来源域名，但不保证命中具体论文。已知 NeurIPS、CVPR、JMLR 页面可读；部分 OpenReview、AAAI 页面返回验证页面；IEEE、ACM 及部分 arXiv 抓取失败。
- 限定域名和准确标题不能消除索引缺失、排序、反爬及返回条目上限的影响。当前实现尚未自动识别所有反爬验证页面。
- Tavily、Brave、Serper 使用本地 HTTP 协议桩验证，尚未使用真实服务密钥联测。SearXNG、原内置后端和官方 CN 后端均已取得实际搜索结果。

上述外站及论文抽样使用 CN 账号。国际版后续单次探测另有以下结果，不应将一方的覆盖范围套用到另一方：

- Python TaskGroup 搜索返回 5 条结果，官方文档排第一；该页面抓取成功，正文裁剪至 20000 字符。
- Reuters 站内搜索返回 5 条 Reuters 域名链接；X/NVIDIA 站内搜索返回主页及具体推文链接，但 X 主页抓取仍无可读正文。这不证明具体新闻断言正确，也不保证任意外站均可访问。
- 测试账号由用户报告无可用模型额度，但工具调用仍成功；未核对独立工具账单，不能认定永久免费。国际版测试只调用独立工具，未验证无额度情况下的模型推理或完整模型多轮回灌。

## 工具边界与后端选择

官方抓取由 Qoder 服务器执行。网关在提交 URL 前检查公网解析，接收最终重定向 URL 后再次检查；无法控制远端解析、连接钉扎或中间重定向。最终地址检查不能撤销远端已发生的抓取。

面板支持原项目内置、Qoder 官方、外部搜索服务三种模式。外部服务包括 SearXNG、Tavily、Brave、Serper 及自定义 HTTP JSON，配置和密钥按服务分别持久化。每个请求冻结一次配置，环境变量覆盖会显示并锁定相应控件；切换失败不会静默回退到另一后端。

外部搜索端点由管理员配置，可以指向内网服务；模型仅能提供查询，不能修改目标端点或认证。外部模式的页面抓取继续使用网关原有公网 URL 防护。

启用、代理策略及恢复原后端的方法见[联网工具与搜索后端](configuration.md#联网工具与搜索后端qd_web_tools)。
