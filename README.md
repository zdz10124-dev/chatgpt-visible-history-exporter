# ChatGPT 可见聊天全量导出器

路径：`C:\tools\ChatGPT可见聊天全量导出器`

作用：尽可能全量读取当前已登录 ChatGPT 网页账号的聊天，只保留：

- 用户输入
- GPT 对用户可见的最终回复

自动过滤：system、tool、内部 recipient、reasoning/thoughts/reasoning_recap、常见隐藏消息，并只沿当前 `current_node` 导出当前可见分支。

同时尝试读取普通会话、已归档会话、Projects 内会话。

## 安装

1. Edge 打开 `edge://extensions/`；Chrome 打开 `chrome://extensions/`。
2. 开启“开发人员模式”。
3. 点击“加载解压缩的扩展”。
4. 选择 `C:\tools\ChatGPT可见聊天全量导出器`。
5. 打开或刷新 `https://chatgpt.com/`。
6. 页面右下角点击“导出聊天”。
7. 浏览器会下载 JSON 和 Markdown 两份文件。

## 安全

扩展只发 GET 请求，不删除、不修改、不归档任何聊天；不会把 Cookie、密码或 accessToken 写入磁盘。accessToken 仅在当前页面内临时使用。

## 注意

这里调用的是 ChatGPT 网页自身使用的内部 backend-api，并非公开稳定 API。以后网页接口字段或路径变化时，可能需要同步更新本工具。
