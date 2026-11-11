# Podman Quadlet

三个用户级容器均只加入 `message-gateway` 网络，无需 Traefik。网关向宿主机发布
`127.0.0.1:8081`；NapCat 只发布 WebUI `127.0.0.1:6099`。
Agent 不发布端口，通过网络内 DNS 访问两个服务。

- Agent 入站、历史、联系人：`ws://napcat:3001`。
- Agent 出站：`http://message-gateway:8081`，账号 `qq`。
- 网关到 NapCat：`http://napcat:3002`。

准备 `~/napcat/config`、`~/napcat/QQ`、`~/napcat/.env.napcat`，以及网关 `.env` 和
`accounts.json`。NapCat 对应 QQ 账号的 OneBot 配置需启用 `0.0.0.0:3001` WebSocket
服务和 `0.0.0.0:3002` HTTP 服务。网关账号的 `access_token` 与 HTTP 服务的 token 一致。
Agent 的 `NAPCAT_ACCESS_TOKEN` 对应 WebSocket 服务的 token。

Agent 原配置和数据放在 `~/miss-raspberry-agent/`；在 `config.yaml` 中加入主 README
所示的 `message_gateway` 配置。在 `.env.gateway` 中设置：

```dotenv
MESSAGE_GATEWAY_URL=http://message-gateway:8081
MESSAGE_GATEWAY_API_KEY=<网关 accounts.json 中该账号的 client_token>
MESSAGE_GATEWAY_ACCOUNT=qq
```

设置 `message_gateway.enabled: true`，保护环境文件权限。将 Quadlet 复制到
`~/.config/containers/systemd/`，生产使用固定提交的镜像标签，预先拉取或本地构建。

```bash
systemctl --user daemon-reload
systemctl --user start napcat.service message-gateway.service miss-raspberry-agent.service
```

`WantedBy=default.target` 由 Quadlet 生成开机启动关系，无需 `systemctl enable`。
用户需开启 linger 才能在退出登录后继续运行。迁移已有部署时，先停止三个服务，替换
Quadlet，删除旧 `napcat.network` 与 `miss-raspberry-agent.network` 定义，重载后启动；
确认无容器占用后再删除旧 Podman 网络。保留一份原配置以便恢复。

宿主机上的其他服务访问 `http://127.0.0.1:8081`；其他容器可加入 `message-gateway`
网络后访问网络别名。容器中的 `127.0.0.1` 是容器自身，不是宿主机。
