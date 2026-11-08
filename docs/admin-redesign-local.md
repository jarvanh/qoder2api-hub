# 后台重构（本地待验收，未发布）

版本仍为 1.8.0；本次不 commit / push / tag / release / 发布镜像。

## 导航与界面

- 总览：健康概览、跨区域账号横向对比、原出口与调度、运行概览。
- 模型：原模型能力/价格/上下文表独立管理，原客户端协议不变。
- 路由编排：有序规则，按模型通配符和 Key ID 匹配，指定区域、账号组、账号与 round-robin / fill-first。数值优先级越小越先匹配；同优先级保留保存顺序。
- 账号/统计/日志/智能体/设置沿用统一浅色蓝绿及暗色主题；保留表格和原管理动作。动画尊重 reduced-motion。
- 记住登录仅持久化签名会话令牌，不保存面板密码；未选时使用 sessionStorage，退出清空两种存储。会话沿用服务端7天期限，改密码会吊销。
- 设置中的 API 示例折叠展示 Base URL、cURL、Chat Completions、Responses 与 Anthropic Messages，一键复制占位符，不读取明文 Key。

## 账号组与 Key 范围

账号 group 默认空字符串（未分组）。支持导入指定组、单账号改组、筛选与组启停/刷新凭证/测试/签到领积分。组任务后台串行处理，最多2个活跃任务，相同进行中操作去重；结果按账号返回。补积分只调用原官方签到/活动流程，不伪造本地余额。

Key account_ids 与 account_groups 取并集，两个都空为不限；路由账号/组范围与 Key 范围取交集。明确空交集直接拒绝，不回退全池；已绑定会话同样受范围限制。模型通配符不改变真实模型ID或签名字节，恢复重试仍受相同范围控制。

管理接口均要求 X-Panel-Token：

- GET /admin/groups
- POST /admin/accounts/group {uids,group}
- POST /admin/groups/action {group,action}，支持 enable/disable/refresh/test/checkin/credits
- GET /admin/jobs?id=JOB_ID
- GET /admin/routing
- POST /admin/routing/save {rules}
- POST /admin/routing/preview {model,key_id?,realm?}

路由独立存储 accounts/admin/routing.json，避免被账号文件扫描误当凭据。预览不推进调度游标、不访问上游、不回显凭据。

## 日志目录

默认统一为 logs/：gateway.log、gateway.err.log、usage/usage.jsonl、usage/usage-summary.json、usage/activity.jsonl，以及测试报告 logs/tests/。

QD_LOG_DIR 可覆盖目录；QD_PROXY_USAGE_DIR 或 --usage-dir 显式值继续优先。首次启动从旧 usage/保守复制流水，原件不删除；目标已有数据不覆盖。Windows启动器直接重定向，终端/容器运行则保留控制台输出并写日志。compose挂载./logs，旧./usage只读供迁移。

## 参考与适配

参考 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 的路由策略与 [官方管理中心](https://github.com/router-for-me/Cli-Proxy-API-Management-Center) 的状态/管理/日志分区设计。未复制其v8管理接口、供应商OAuth执行器或虚构配额；横向对比适配为本项目账号，后端使用现有CN/INTL池和官方积分数据。
