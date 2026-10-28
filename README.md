# py-raspberry-qq-agent

面向 QQ 私聊和群聊的树莓娘 Agent，使用 NapCat / OneBot 11 和 OpenAI 兼容模型接口。
聊天、定时动作和报告生成分别执行，报告不需要指定 QQ 收件人。

## 运行

需要 Python 3.12、uv、可用的模型接口及 NapCat WebSocket 服务。

```bash
uv sync --all-groups
cp config.example.yaml config.yaml
cp .env.example .env
uv run --env-file .env py-raspberry-qq-agent --config config.yaml config-check
uv run --env-file .env py-raspberry-qq-agent --config config.yaml
```

所有行为参数从 YAML 读取；使用 `${VAR}` 或 `${VAR:-fallback}` 引用环境变量。
`config.yaml` 和 `.env` 不提交到仓库。

## Docker 运行

准备好 `config.yaml` 和 `.env` 后，在项目目录执行：

```bash
docker build -t py-raspberry-qq-agent .
mkdir -p data logs knowledge notebooks
docker run -d --name qq-agent --restart unless-stopped \
  --env-file .env \
  -v "$PWD/config.yaml:/app/config.yaml:ro" \
  -v "$PWD/data:/app/data" \
  -v "$PWD/logs:/app/logs" \
  -v "$PWD/knowledge:/app/knowledge:ro" \
  -v "$PWD/notebooks:/app/notebooks" \
  py-raspberry-qq-agent
```

镜像使用 Python 3.12，通过 `uv.lock` 安装运行依赖，并保留 uv 供 MCP 子进程使用。
配置、密钥和本地数据不打入镜像；上述挂载对应示例配置中的默认目录。
将 `.env` 中的 `NAPCAT_WS_URL` 设置为容器可访问的 NapCat 地址，
容器内的 `127.0.0.1` 指向容器自身。Linux 上连接宿主机服务时，也可以添加 `--network host`。

查看运行日志：`docker logs -f qq-agent`。

## 执行方式

- 保存收到的消息历史。私聊和群内 @机器人消息经过允许列表、权限和重要性判断。
- 管理员跳过分类模型；其他消息只有达到重要性阈值才进入内存任务队列。
- 聊天 Agent 每次处理一个任务，外部动作通过工具完成。普通模型文本不会自动发送到 QQ。
- 任务由程序取出和结束，不向模型提供添加、删除 TODO 的工具。失败记日志，不自动重试；重启丢失待处理任务。
- QQ 发送使用有界内存队列，串行发送并保留随机间隔。失败记日志，不补发。
- 定时群摘要直接读取历史、生成 Markdown 并保存，可显式配置 MCP 上传工具；不经过聊天 Agent。
- 每个聊天任务使用独立的模型上下文，需要更多信息时调用历史工具；不同任务不共享模型对话缓存。

这是一套尽力而为的自动化，不维护持久化工作队列、投递记录或补偿流程。

## 身份与模型边界

业务代码、配置和本地数据直接使用 QQ 号和群号。不再使用联系人加密、加密密钥或密文映射。
`ModelIds` 只在模型输入和内置 QQ 工具边界把结构化号码转成不透明引用。
模型工具返回的引用在程序内还原，未见过的引用和类型不匹配的引用会被拒绝。

这些引用只是进程内映射，不是用户身份主键，也不提供落盘保密保护。
消息正文、昵称和备注是普通文本，不承诺检测其中人为填写的号码。
外部 MCP 工具保留各自的参数语义，不套用 QQ 字段转换。

联系人备注与管理员授权分开存储，模型只能编辑备注，不能修改授权。

```bash
uv run --env-file .env py-raspberry-qq-agent --config config.yaml admin add --qq 123456789
uv run --env-file .env py-raspberry-qq-agent --config config.yaml admin remove --qq 123456789
uv run --env-file .env py-raspberry-qq-agent --config config.yaml admin list
uv run --env-file .env py-raspberry-qq-agent --config config.yaml profile set --qq 123456789 --name 张三 --notes 技术组同事
uv run --env-file .env py-raspberry-qq-agent --config config.yaml profile get --qq 123456789
uv run --env-file .env py-raspberry-qq-agent --config config.yaml profile list --search 技术组
uv run --env-file .env py-raspberry-qq-agent --config config.yaml profile remove --qq 123456789
```

CLI 和本地文件会显示真实号码。手动编辑资料或通过 CLI 修改时应先停止 Agent，避免内存缓存覆盖文件。
`security.admin_qq_ids` 可指定启动时授予管理员权限的号码。

## 聊天允许列表

```yaml
message_gate:
  allowlist:
    enabled: true
    private_qq_ids: [123456789]
    group_ids: [987654321]
```

启用后只允许指定聊天触发 Agent 和接收模型发起的 QQ 消息；管理员也受限制。
模型创建的定时任务执行时同样受限制。任务分配、到期提醒等程序自动通知不受此列表限制，
收件人由业务记录和 Keycloak QQ 绑定确定；仍使用同一发送队列限速和投递。
未 @机器人的群消息不触发模型。历史、好友列表和群列表查询不受此列表限制。

好友和群工具支持 `page` / `page_size` 分页。联系人备注支持非好友，不用于建立外部账号绑定关系。

## 定时任务与报告

聊天 Agent 的 `manage_scheduled_tasks` 支持 once、interval、daily、weekly、monthly、yearly。
时间使用 `app.timezone`。目标聊天可为空，定时配置保存在 `schedules.json`；触发后的执行队列不落盘。

群摘要示例：

```yaml
group_digest:
  enabled: true
  groups: [987654321]
  interval_minutes: 60
  initial_lookback_minutes: 60
  min_messages: 3
  prompt: 整理技术结论、争议和后续事项。
  upload_tool: "" # 指定如 mcp__upload__upload_markdown；为空则只保存本地
```

文件保存在 `notebooks/group-digests/`。历史窗口使用 `[start, end)`，游标只保存在内存。
每次启动重新按 `initial_lookback_minutes` 选取起点。每个窗口推进一次，生成或上传失败不补做。
历史查询最多读取 `history.max_fetch` 条，超出部分不包含在摘要中。

`reports.summarize` 返回 Markdown；`reports.publish` 保存文件，再按明确配置调用上传工具。
后台代码可以直接复用这两个函数，无须构造聊天任务。

## 知识库、记事本与 MCP

知识库支持列文件、按行读取和字面搜索；记事本支持新建、替换、追加和删除。
路径限制在配置目录内，拒绝绝对路径和 `..` 越界。不引入 RAG 或向量数据库。

MCP 支持 stdio 和 Streamable HTTP，可通过 `allowed_tools` 限制工具。
仓库附带 Markdown 上传桥接器，读取 `mcp.upload`，向 HTTP 接收端发送：

```json
{"title": "标题", "markdown": "# 正文", "metadata": {}}
```

模型工具名带服务前缀，如 `mcp__upload__upload_markdown`。
思考模式按供应商要求配置在 `llm.main.extra_body`；不将模型思考写入 QQ。

## 本地数据

| 文件 | 用途 |
| --- | --- |
| `roles.json` | 按 QQ 号维护管理员授权 |
| `profiles.json` | 按 QQ 号保存昵称和备注 |
| `schedules.json` | 用户创建的定时配置 |
| `messages.jsonl` | 消息历史 |

即时任务、发送队列和群摘要游标只存在于内存。不要公开本地数据、知识库和记事本。
此版本不兼容旧的加密数据文件，不提供迁移或旧参数兼容层。

## 验证

```bash
uv run pytest -q
uv run python -m compileall -q src
```

测试覆盖消息分流、允许列表、模型引用边界、工具调用、任务失败后继续处理、发送间隔、定时配置、报告输出及 MCP 上传。

## 看板到期提醒

`guild.enabled` 启用看板集成，通过 PostgreSQL 直接读取 `guild-go` 的节点和直接负责人。
数据库连接强制只读。Keycloak 使用独立服务账号，读取 `subject` 对应用户的 `qq_number` 属性；
该属性由管理员维护，Bot 不修改用户资料。禁用、删除或未绑定 QQ 的账号跳过并记录原因。
用户资料缓存 5 分钟，查不到的用户缓存 30 秒。

`guild.poll_seconds` 默认 60，`guild.due_reminder.advance_hours` 默认 24。
每轮查询截止时间减去提前量落在 `[上次成功查询时间, 本次数据库时间)` 的未完成任务，
仅通知当前直接负责人。不会反复扫描整个“未来一天”范围，不额外保存去重集合。
首次启动只建立当前时间游标，停机期间不补发。查询失败保留游标；身份解析或发送失败只记日志。
已经到期的任务不提醒。窗口内才新建、但提醒触发时间已经过去的任务也不补发。
通知仍受 QQ 发送允许列表限制，绑定 QQ 并不保证 NapCat 能向该账号私聊。

需要向非好友发送临时会话消息时，可启用：

```yaml
napcat:
  allow_temp_session: true
  cache_ttl_seconds: 7200
```

该开关默认关闭。启用后，好友继续使用普通私聊；非好友先从有效的群成员快照中查找，
未命中时逐群获取尚未缓存或已过期的完整成员列表，找到后停止扫描。
一次查询缓存全群成员，同轮其他收件人可复用快照；仅缓存群号和成员 QQ 号。
好友集合、机器人所在群集合和成员快照统一使用 `cache_ttl_seconds`（默认 7200 秒）。
TTL 从每次成功加载完成时固定计时，成员快照按群独立过期，命中不续期；没有后台扫描任务。
重连、机器人账号变化或关闭时清空内存缓存；群列表刷新时清理已退出群的快照。
发送前仍单独核实对方的群成员身份及用户信息，再通过一个共同群发起临时会话。
无法确认共同群或用户信息时停止发送；不会轮流向多个群尝试发送或自动补发失败消息。
QQ 仍可能拒绝临时会话，日志会记录发送路径、来源群及 NapCat 返回的具体错误。

## 看板周报

周报统计上一个完整自然周：UTC+8 的周一 00:00 至下周一 00:00，右边界不包含。
汇总全看板的周内新增、完成、评论，以及生成时的当前未完成/逾期状态。
计数由代码生成，模型负责整理文字。使用现有时间字段，不推断已经删除的内容或丢失的修改历史；
任务重新打开后，原来的完成记录也无法从当前表恢复。当前负责人和当前状态均不声称是历史快照。

`guild.weekly_report.schedule` 默认为空，不会擅自选择生成时间。
填写如 `{kind: weekly, weekdays: [mon], time: "09:00"}` 后才会自动运行。
`directory` 是记事本内的输出目录，文件按统计周的起始日期命名，重复生成会覆盖同一文件。
`upload_tool` 可指定已有的 MCP Markdown 上传工具；以后也可以直接扩展 `reports.publish`。

可以单独生成上一完整自然周，无须连接或启动 QQ：

```bash
uv run --env-file .env py-raspberry-qq-agent --config config.yaml guild-report
uv run --env-file .env py-raspberry-qq-agent --config config.yaml guild-report --at 2026-10-26T09:00:00+08:00
```

命令需要启用并配置 `guild`。自动周报生成/上传失败只记日志，等待下次计划，不持久化进度或补做。

## 成员分配通知（可选）

`guild.assignments.enabled` 独立控制该功能，处理模块为 `assignments.py`，不依赖到期提醒或周报。
它有独立的内存窗口游标，读取 `node_members.created_at`，向新增直接负责人/成员或角色变更后的用户发私聊。
角色变化时仅重建对应关系，原样保存成员保持原记录及创建时间。包括创建节点时的直接分配及默认复制的直接成员，不按继承权限扩散通知。
Bot 不维护成员快照、事件表或去重存储。两次查询间被移除的关系、被覆盖的中间角色变化，以及跨窗口才提交的事务可能漏报；这是接受的尽力而为行为。

## 配置 Keycloak

`scripts/configure_keycloak.py` 默认只预览计划。它添加管理员可维护的单值 `qq_number` 属性，
创建独立 `qq-bot` client，启用服务账号并授予用户只读角色及相应 client scope。
不向普通登录 token 添加 QQ mapper，不修改任何现有用户的 QQ 绑定，也不覆盖已有同名 client。

```bash
uv run --env-file ../guild-go/.env --env-file ../keycloak.env python scripts/configure_keycloak.py
# 审核输出后，再追加 --apply 执行。
```

`keycloak.env` 提供临时管理员的 `user`、`password`；`guild-go/.env` 提供 `OIDC_ISSUER`。
服务账号凭据只写入 `data/keycloak-bot.env`，文件权限 0600。可用多个 `--env-file` 同时加载 Bot 自身配置和该文件。
QQ 号由管理员在 Keycloak 用户属性中填写，Bot 本地联系人资料不能修改绑定。
Keycloak 默认的用户只读管理角色能读取 realm 用户资料，并不是字段级授权；Bot 运行时只读取所需用户。

PostgreSQL 测试使用已经初始化 `guild-go` 表结构 的独立测试库：

```bash
TEST_GUILD_DATABASE_URL='postgres://.../guild_automation_test' uv run pytest -q
```

Python 集成测试只创建并清理自己的测试记录；Go 集成测试会清空测试库，不要让它们同时运行。
