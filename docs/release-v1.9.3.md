# v1.9.3 — 支持 chunked 请求体与多粒度 thinking block 配置

## 新特性与修复

### 1. 支持 Transfer-Encoding: chunked 请求体（合并 PR #148）
- **问题根因**：部分客户端（如 Claude Code / undici）在传输大载荷（如长消息、大尺寸图像或复杂工具上下文）时使用 `Transfer-Encoding: chunked` 发送 `/v1/messages`。旧版本只按 `Content-Length` 读取，导致请求体被判定为空对象并误报 400 `messages is required`，未消费的 chunk 帧还会在 keep-alive 连接上被误当作下一条伪 HTTP 请求。
- **解决方案**：
  - 新增 `_read_chunked_body()`，逐 chunk 解析分块大小与数据，消费可选 trailer 与 CRLF 终态，超限时安全关闭连接；
  - `_read_payload()` 增加 `Transfer-Encoding: chunked` 识别分支；
  - 彻底修复 Claude Code 在长上下文和大请求体场景下的 400 失败问题。

### 2. Anthropic thinking block 判据补充与多粒度覆盖（Issue #146）
- **安全启用判定依据**：
  - 在 `docs/configuration.md` 与 `docs/clients.md` 明确何时可安全启用 `empty` 模式：在反代上游为第三方 OpenAI 兼容接口（如 Qoder 上游模型 Qwen/Kimi/DeepSeek）且网关入站丢弃历史 thinking block、中转层仅透传字节流的拓扑下，可安全启用思考过程回传。
- **多粒度覆盖链**：
  - 优先级：`Key 维度覆盖` > `面板/配置全局覆盖` > `环境变量 QD_ANTHROPIC_THINKING` > `默认 off`；
  - 在 API Key 规范化清洗中持久化保存每 Key 的 `anthropic_thinking` 字段；
  - 提供 `/settings/anthropic-thinking` 管理端点，支持在面板设置热更新，无需重启容器；
  - `chat_to_messages` 与 `stream_anthropic_events` 支持按请求上下文动态选用 thinking 输出策略。

## 验证

- 本地全量回归测试：141 个套件，3829 项检查全部通过（exit 0）；
- 新增 `tests/_test_issue146_anthropic_thinking.py` 22 项覆盖存取、Key 粒度、覆盖优先级链及流式事件；
- CI 跨平台自动化测试（Ubuntu Python 3.9/3.12、Windows Python 3.12）通过。

## 升级方式

### 容器部署
```bash
docker pull ghcr.io/shuishuipingan/qoder2api-hub:1.9.3
```
或拉取 `:1.9` / `:latest` 标签并重启容器。

### 源码部署
拉取 `main` 分支最新代码并重启服务即可：
```bash
git pull origin main
```
