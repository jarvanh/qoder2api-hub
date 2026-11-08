# 官方管理中心视觉移植（本地待确认）

参考来源：router-for-me/Cli-Proxy-API-Management-Center，提交953fb24。官方项目是CLIProxyAPI的独立管理前端，本项目仍使用Qoder自己的Python API，不接其v8管理接口。

本轮直接编译/适配的源文件：

- src/features/dashboard/DashboardPage.tsx 和 dashboard.module.scss：双栏状态Hero、浮动请求卡、四项指标、流量区、横比列表、健康/运行配置、快捷入口。
- LiveWire / curve：真实数据Catmull-Rom曲线、渐变面积、1.3秒描边与末端呼吸光点；无数据使用安静基线。
- Meter：连续6px圆角轨道、宽度更新360ms。
- hooks/motion.ts：24px上浮、450ms、70ms级差（封顶360ms）、900ms数字计数。
- PageTransition：28px / 360ms进入、按压160ms和悬停200ms强减速曲线。
- 登录 dotMatrixField.ts：直接转译原点阵采样、噪声和雷达揭示算法；30FPS，隐藏/退出停绘，reduced-motion静态显示。
- RoutingPage/MemberCard/SaveBar：优先级轨道、账号成员选择卡、侧边决策预览、浮动保存/放弃，加入上下移动操作。
- themes.scss：暖纸浅色与暖灰深色主题，统一所有管理页面。

布局保留用户要求的顶部模型独立导航；原出口与调度仍放总览。原供应商横比改为本地账号横比，按同一6小时窗口比较请求、成功率和趋势。

数据来自面板鉴权 GET /admin/overview：36个10分钟桶，成功/失败/账号归属守恒；无样本成功率为null。未归属历史记录单独注明。接口不发上游模型发现请求；只读本地目录和流水，有界10秒聚合缓存。

官方原组件已独立运行用于画面对照；本机Qoder预览使用临时假账号与合成日志，合成数据不写正式账号或usage。

MIT署名与许可保留在 docs/third-party/CPAMC-LICENSE.txt。

没有提交、推送、tag、Release或镜像发布。版本仍1.8.0。
