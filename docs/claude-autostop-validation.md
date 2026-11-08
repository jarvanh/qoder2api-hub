# Claude Code 自动停止：诊断与验证

验证时间：2026-11-06 17:39:37 +08:00。修复基线：`a858922`。

## 已确认的故障

- 部署前容器镜像 `qoder2api-hub:fix-a858922` 的 `qoder_proxy.py` 和 `qoder_anthropic.py` 均与仓库一致，排除旧镜像未更新。
- 用户提供的会话在北京时间 17:10:04 返回“第二条命令：”后停止；同一时刻容器记录 `dropped truncated leaked tool-call echo (180 chars) — issue #9`，Claude 收到的却是 `stop_reason=end_turn`。
- 17:15:48，回复把 `[Bash 工具调用]` 和参数写成正文，未产生真实工具调用，同样以 `end_turn` 结束。
- 原提交只修改工具参数 JSON 的对象归一化及历史双重编码，未修改这些流式成功/失败分支。

## 代码问题与修复

1. `_leaked_partial_droppable` 已确认是工具回显截断，但三处调用方又要求 `_marker_only_echo` 才报错。只要数组已开始写，就绕过错误分支，调用被丢弃后仍正常结束。现已修复流式 Chat/Anthropic、非流式聚合和 Responses 三个出口；截断调用明确失败，不再发送成功收尾。
2. `_structured_realms` 原默认仅 `cn`，当前实际使用 `intl`，因此日志持续显示 `structured=False`。工具历史变成正文信封会给模型提供伪协议样本。默认范围现为 `cn,intl`，保留模型白名单和工具 ID 检查，以及显式关闭/回退开关。GLM-5.3 已通过真实国际区接口验证。
3. 新增已声明工具的 `[名称 工具调用]` 正文守卫。无法确认的伪调用不会被猜测执行，也不会伪装成任务成功；流式在发送成功 finish 前返回错误。代码围栏示例和未声明的名称保持普通文本行为。
4. 旧测试有明确断言“截断后 finish_reason 仍为 stop”，实际锁定了本次故障。已同步失败语义，保留正文、泄漏、分片和完整工具调用的原有检查。

Anthropic 官方说明：[停止原因](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)、[流式事件与工具参数](https://platform.claude.com/docs/en/build-with-claude/streaming)。工具调用需要结构化 `tool_use`，工具输入最终必须是对象；普通正文中的命令不能触发工具执行。

## 验证结果

- 修改前：124 个套件全部通过，3391 项检查，证明旧测试漏掉了该失败语义。
- 新增回归用例在旧源码上：19 项中 13 项失败；修复后 19 项全部通过。覆盖真实 HTTP Anthropic 路由、1 字符/7 字符/整帧切分、截断数组、伪 Bash、完整双重编码参数及非调用正文。
- 最终全量：125 个套件全部通过，3411 项检查；命令 `python3 tests/run_all.py --logs /tmp/qoder-autostop-final`。
- 最终镜像内：7 个 Anthropic 相关套件全部通过，185 项检查，测试容器关闭外网。
- 真正的 Claude CLI 2.1.312 + GLM-5.3：新会话连续 3 次工具调用、新会话连续 8 次工具调用、用户原会话副本续聊 2 次工具调用，全部自动完成。
- 上线后通过用户实际 HTTPS 反代地址再完成 3 次连续调用；第二条 `ls` 返回目录不存在错误后仍自动完成第三条命令和总结。
- 共 16 次真实工具调用，输入全部为对象，工具结果 ID 全部正确配对。原会话续聊使用 `--fork-session --no-session-persistence`。

实测主要覆盖 GLM-5.3；不能据此保证所有上游模型的生成行为。上游继续返回不可恢复的工具文本时，会收到明确错误，而非误报任务完成。

## 部署结果与回滚

- 当前镜像：`qoder2api-hub:fix-autostop-c0ebd63cc286`，容器健康状态 `healthy`。
- 修复镜像基于已核对的 `fix-a858922`，仅替换 `/app/qoder_proxy.py`；依赖和其它源码沿用原镜像。
- `qoder_proxy.py` SHA-256：`c0ebd63cc2864add28513c0dc4560c9eaf9889486fdb165f7748d6d6eb893289`，已与运行容器逐字节核对。
- 仅切换当前 qoder2api 服务的镜像引用；账号和用量仍使用原挂载目录。
- 原镜像 `qoder2api-hub:fix-a858922` 保留。Compose 原文件备份在 `/tmp/qoder-autostop-work/docker-compose.before.yml`，权限 0600。
- 回滚可将原备份还原到部署 Compose 文件后，运行 `docker-compose --project-name qoder2api-hub -f /panel/1panel/apps/local/qoder2api-hub/qoder2api-hub/docker-compose.yml up -d --no-deps --pull never --wait qoder2api-hub`。
- 真实 CLI 输出与汇总在 `/tmp/qoder-autostop-work/claude-*.jsonl`、`validation-summary.json`；全量测试日志在 `/tmp/qoder-autostop-final/`。
