"""划词适配器（DeskMate Selection Adapter）。

把开源划词客户端（OpenAI Translator 等，支持自定义 OpenAI 兼容端点）
接上本地 OpenWorker sidecar 的接入层 —— 类比项目里用到的 OmniRoute/A2A 适配层。

为什么需要它：
- sidecar 端口随机、需 `x-openworker-token` 鉴权、CORS 仅放行本地 webview/localhost；
- 开源扩展无法知道随机端口与 token，也受跨域限制；
- 本适配器监听【固定端口】(默认 18795)、放开 CORS、仅信任 localhost，对外暴露
  OpenAI 兼容的 `/v1/chat/completions`，转发时补上 sidecar token。

动作覆盖（解释/翻译/搜索/总结）：
- 解释/翻译/总结：OpenAI Translator 原生模式直接走 `/v1/chat/completions` 代理；
- 搜索：客户端自定义 prompt 以哨兵前缀 `【联网搜索】` 开头时，适配器先调
  `coworker.web.tool.web_search` 取结果再交给模型综合，无需改动客户端；
- 另提供 `/v1/deskmate/selection` 便捷端点（服务端动作，供自有客户端/脚本调用）。
"""
