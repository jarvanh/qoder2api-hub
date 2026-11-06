# 图片转换超时：现场分析与验证

## 现场结论

用户提供的会话导出中，seq=1108（turn=6、step=7）的失败原文为：

```json
{"code":"provider_error","message":"Image conversion timeout","type":"provider_error","details":"image conversion timeout after 30 seconds"}
```

该轮前面刚通过 read_image 读入一张 1920×1080、780,734 字节的 PNG。随后客户端重试后曾得到正常回复。因此：

- 能确认 provider 的图片转换阶段超时，不是“网关 Key 无效”。
- 不能据此确认图片损坏；也不能从客户端导出推断该次上游完整请求的图片总数（未提供原始 HTTP 请求体）。
- 导出媒体目录有 12 张图，共 7,539,371 字节。长历史会重放图片，预处理可减轻转换压力，但这不是上游 TTL/缓存/故障根因的直接证据。

## 已完成的修改

1. 统一出站图片预处理：Chat / Responses / Anthropic 桥接后处理，不删图、不缩分辨率。
2. 大型不透明 PNG/BMP/TIFF 可以转换为 quality=95、4:4:4 JPEG，体积至少缩减 20% 才使用；透明图保持无损 PNG。
3. 色彩配置、EXIF、DPI、像素比例、文本元数据、动画和高位深图保守保留原始字节；远程 URL 不由本地下载。
4. 仅内存 LRU 缓存（最多 32 项、32 MiB 的留存 URL），不落盘。原始请求对象不变。
5. 图片超时独立错误类型：`upstream_image_conversion_timeout`。HTTP/SSE 共享一次额外重试预算；SSE 图片重开固定原账号，已有输出不重复推理，不假装正常完成。
6. 不冷却/惩罚健康账号、不解绑会话；团队账号重开前释放旧租约，避免等待自己的并发位。
7. 官方镜像复制新模块并包含 Pillow；本机实际运行的内置 Python 已安装 Pillow 13.0.0。
8. `/health.image_preflight` 返回开关、编解码组件是否可用及缓存大小，不返回图片/URL/凭据。

## 复测结果

| 范围 | 原始 | 处理后 |
|---|---:|---:|
| 导出目录 12 张原图合计 | 7,539,371 bytes | 3,152,544 bytes |
| 出错轮次的新预览图 | 780,734 bytes | 559,736 bytes |
| 预览图分辨率 | 1920×1080 | 1920×1080 |

12 张图片数量、像素尺寸不变。部分图片采用高质量有损 JPEG，不能把“尺寸不变”称为“逐像素无损”；需要原始字节可设 `QD_IMAGE_TRANSCODE=off`。

- 新增回归套件：93 条断言全部通过。
- 全量：122 suites / 3296 checks / 0 failed。
- 覆盖原图编解码、透明度/元数据、高位深/APNG、无组件回退、三协议桥接、HTTP/SSE混合预算、团队租约、输出后失败、不伪造成功。
- 本机 8790 网关已重启加载当前修复；健康返回 `enabled=true`、`codec_available=true`。
- 原预览图经运行中的 INTL Sonus 网关，日志显示 `count=1 optimized=1 chars=1041002->746339 codec_missing=0`；该次调用遭遇上游 `10605` 排队，未得到模型成功响应。不得据此宣称上游图片服务已全面恢复。

## 使用

本机当前进程已加载，无需再次安装。其它本机部署用运行网关的 Python 安装后重启：

```bash
python -m pip install "Pillow>=11.3,<14"
```

不改 API Key、不删除图片、不换低配模型。若仍出现 `upstream_image_conversion_timeout`，保留返回的上游 request_id 定位 provider；如果出现 `10605`，则是另一条独立的上游排队问题。

此修复随 v1.8.0 发布，已包含在官方新镜像；本机编解码组件与运行状态另以实际 `/health` 为准。
