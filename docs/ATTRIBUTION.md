# 项目继承与来源

## 直接参考：nsy_chat_live

- 仓库：[huangwg2529/nsy_chat_live](https://github.com/huangwg2529/nsy_chat_live)
- 固定版本：[69c6d519cb0b394f839296d2af51f5e954078b81](https://github.com/huangwg2529/nsy_chat_live/tree/69c6d519cb0b394f839296d2af51f5e954078b81)
- 原说明：[固定版本 README](https://github.com/huangwg2529/nsy_chat_live/blob/69c6d519cb0b394f839296d2af51f5e954078b81/README.md)

| RepArchive 文件 | 来源或参考范围 |
| --- | --- |
| `src/protocols/http.proto` | 基础令牌、账号与聊天消息的 protobuf 字段和编号；整理为本项目所需的最小消息集合 |
| `src/protocols/external.proto` | 个人资料、推与关注列表的基础字段和编号 |
| `src/export_replive.py` | 聊天分页、令牌刷新和媒体备份流程的参考；运行实现为 Python |
| `src/sns_login.py` | 上游 `login/google.go`、`login/twitter.go` 的 Google / X 登录流程和应用公开标识参考；本项目以 Python 实现相应 PKCE、状态校验和 transport |
| `src/callback_bridge.py` | 与上述登录流程配合的本机协议接收实现；macOS、Windows、Linux 接收程序由本项目生成 |

协议文件以字段编号兼容既有服务为目的，未包含上游全部协议或功能。RepArchive 未移植上游直播监控、ffmpeg 录制和完整 Go 服务。

## 相关参考：Replive+

- 仓库：[monamona0917/replive-plus](https://github.com/monamona0917/replive-plus)
- 固定版本：[36702a9f03386f464fbe23bfd81b0f7a62c10481](https://github.com/monamona0917/replive-plus/tree/36702a9f03386f464fbe23bfd81b0f7a62c10481)
- 参考用途：社区工具的相关实现与功能结构。

其 [固定版本 README](https://github.com/monamona0917/replive-plus/blob/36702a9f03386f464fbe23bfd81b0f7a62c10481/README.md) 明确标注以 [nsy_chat_live](https://github.com/huangwg2529/nsy_chat_live) 和 [Chilfish/replive-oyu](https://github.com/Chilfish/replive-oyu) 为基础进行重构扩展。replive-oyu 因此是这条社区实现链路的间接来源；本仓库没有直接收录其前端源码，也没有把 Replive+ 的消息发送、已读状态和 Prime Chat 等能力声明为 RepArchive 已支持功能。

## App 协议模型与本项目新增实现

- `src/protocols/phone_auth.proto`：Replive Android 4.8.1 Wire 模型中的 SMS 认证字段编号。
- `src/protocols/subscription_content.proto`：订阅、提问卡片、回答视频及列表分页的相关协议字段。
- `src/protocols/sns_auth.proto`：Replive iOS 4.7.3 的 SNS 认证相关字段，并结合上游登录请求进行互操作整理。
- `src/phone_login.py`：已有账号短信登录、号码规范化、频率处理和错误信息。
- `src/webui.py`、`src/web/`：统一控制台、任务与会话管理。
- `src/archive_site.py`、`src/viewer/`：可读导出结构、独立日文浏览页面和资源校验。

本仓库不分发原 App 的安装包、反编译源码、人物图片或订阅媒体。浏览页面的静态结构、样式、程序与 RepArchive Logo 位于本项目源码中；人物资源来自使用者有权访问并自行导出的数据。

## 依赖

依赖版本见 `requirements.txt`；protobuf、grpcio-tools、phonenumbers、tzdata 及其传递依赖遵循各自包所附的许可。此处列出来源不改变依赖的版权或许可证。

## 许可证与原教程

参考版本未提供明确 LICENSE 的情况已记录在 [NOTICE.md](../NOTICE.md)。不能以仓库可公开访问为依据，替第三方内容声明新的许可证。

原教程：[飞书使用说明](https://my.feishu.cn/wiki/PXe9wkiksifZR9kpVoucsKs1nQe)。本项目的实际操作以 [README.md](../README.md) 和 [USAGE.md](USAGE.md) 为准。
