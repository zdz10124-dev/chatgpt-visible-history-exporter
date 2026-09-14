# ChatGPT 本地聊天数据库：AI 接口说明

数据库位置：`C:\tools\ChatGPT可见聊天全量导出器\chatgpt_history.sqlite3`

推荐 AI 优先使用 CLI；需要程序间通信时使用 HTTP API。

## 1. 查看状态

```powershell
python C:\tools\ChatGPT可见聊天全量导出器\chatdb.py status
```

## 2. 查询消息

```powershell
python C:\tools\ChatGPT可见聊天全量导出器\chatdb.py query [筛选参数]
```

支持：

- `--after 2026-08-01T00:00:00+08:00`
- `--before 2026-08-24T00:00:00+08:00`
- `--role user|assistant`
- `--conversation-id <会话ID>`
- `--project <项目ID或项目名片段>`
- `--title <会话标题关键词>`
- `--keyword <正文关键词>`
- `--archived true|false`
- `--consumer knowledge_base`
- `--unread-for knowledge_base`
- `--unread --consumer knowledge_base`
- `--limit 200`
- `--offset 0`
- `--order asc|desc`

示例：读取 2026-08-01 之后所有尚未被知识库处理的消息：

```powershell
python C:\tools\ChatGPT可见聊天全量导出器\chatdb.py query --after "2026-08-01T00:00:00+08:00" --unread-for knowledge_base --limit 500
```

示例：查询“榴莲”相关聊天：

```powershell
python C:\tools\ChatGPT可见聊天全量导出器\chatdb.py query --keyword "榴莲" --limit 500
```

## 3. 标记已处理

只有 AI 真正处理完消息后才调用：

```powershell
python C:\tools\ChatGPT可见聊天全量导出器\chatdb.py mark --consumer knowledge_base msg_id_1 msg_id_2 msg_id_3
```

不同 consumer 独立记录，例如：

- `knowledge_base`：个人知识库整理
- `daily_summary`：每日总结
- `project_tracker`：项目进度整理

因此“某条聊天被知识库读过”不会影响“每日总结是否读过”。

## 4. 推荐的 AI 工作流

1. `query --unread-for knowledge_base` 查询尚未处理的消息。
2. AI 整理、抽取并写入个人知识库。
3. 仅对成功处理的 `message id` 调用 `mark`。
4. 再次查询，直到没有未读结果。

不要在查询瞬间自动标记，否则 AI 中途失败会造成消息被误认为已经处理。

## 5. HTTP API

启动：

```powershell
C:\tools\ChatGPT可见聊天全量导出器\start_chatdb.bat
```

服务：`http://127.0.0.1:17890`

### GET /api/status

数据库状态。

### GET /api/messages

参数与 CLI 基本一致：

`after,before,role,conversation_id,project,title,keyword,archived,consumer,unread,unread_for,limit,offset,order`

例如：

`GET /api/messages?after=2026-08-01T00:00:00%2B08:00&unread_for=knowledge_base&limit=500`

### GET /api/conversations

参数：

`after,before,title,project,limit,offset`

### POST /api/consumers/{consumer}/mark

请求体：

```json
{
  "message_ids": ["msg_id_1", "msg_id_2"]
}
```

### POST /api/sync/batch

浏览器扩展内部同步接口，普通 AI 不需要调用。

## 6. 数据语义

数据库仅保存：

- 用户可见输入
- GPT 最终可见输出
- 会话标题、时间、归档状态、Project 信息

不会保存：

- system prompt
- 思维链 / reasoning / thoughts
- 工具调用和工具返回
- assistant 发往内部 recipient 的消息
- Cookie、密码、access token

每条消息有稳定 `message id`，读取状态按 `(consumer, message_id)` 保存，因此可以精确判断“哪个 AI 工作流已经处理过哪条消息”。
