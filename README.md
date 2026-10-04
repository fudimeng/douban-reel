# 豆瓣映单 · Douban Reel

定时读取豆瓣影视「想看」列表，自动向 Seerr 提交电影和电视剧下载请求。支持 Docker 部署和中文 Web UI，无需填写 TMDB Token。

## 功能

- 电影、电视剧可分别开启，默认同步最近 30 天的条目。
- 默认每天同步一次，支持手动预览、同步和停止任务。
- 从 Cookie 自动识别豆瓣账号，每次同步检查并刷新有效 Cookie，失效时可通过 Bark 提醒。
- 已请求或已有媒体的条目自动跳过，失败条目下次重试。
- 运行中可修改配置，新配置从下一次任务生效。
- 保存同步记录，支持手动指定 TMDB ID。

## Docker 部署

需要 Docker Compose v2 和已配置好下载服务的 Seerr。

```bash
git clone https://github.com/fudimeng/douban-reel.git
cd douban-reel
python3 scripts/init_env.py
docker compose up -d --build
```

打开 [http://localhost:8787](http://localhost:8787)。用户名默认 `admin`，生成的密码保存在 `.env` 中。没有 Python 时，复制 `.env.example` 为 `.env`，设置至少 12 位的 `ADMIN_PASSWORD` 后启动。

默认仅监听本机。需要局域网访问时，在 `.env` 中设置 `BIND_ADDRESS=0.0.0.0`，运行 `docker compose up -d`，通过 `http://服务器IP:8787` 访问。端口可通过 `PORT` 修改。

### 免密码部署

在 `.env` 中设置 `AUTH_ENABLED=false`，然后运行 `docker compose up -d`。此时 `ADMIN_PASSWORD` 可留空；默认开启认证。

免密码模式适合受限局域网或已有认证的反向代理，任何能访问页面的人都能管理配置和发起同步。

## 首次配置

1. 粘贴豆瓣 Cookie，点击「验证并保存」，账号自动识别。
2. 填写 Seerr 地址和 API Key，点击顶部「保存配置」。API Key 位于 Seerr 设置 → 通用。
3. 点击「预览同步」，确认匹配结果后开启定时同步，或点击「立即同步」。

获取 Cookie：浏览器登录豆瓣电影，按 F12 → Network，刷新页面，在页面请求的 Request Headers 中复制完整 Cookie。也支持浏览器导出的 Cookie JSON。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| 电影 / 电视剧 | 都开启 | 可分别关闭 |
| 历史范围 | 30 天 | 按豆瓣想看标记日期，包含今天 |
| 同步间隔 | 1440 分钟 | 每天一次，可调整 |
| 随机间隔上限 | 15 秒 | 豆瓣请求间隔在 2 秒至上限之间随机 |
| 无明确季号时 | 第一季 | 可改为当前全部普通季 |
| 定时同步 | 关闭 | 开启并保存后开始计时 |
| Bark | 未配置 | 可选，填写完整推送地址 |

Seerr 地址需能从容器内访问：同一 Docker 网络可用 `http://seerr:5055`，访问宿主机可用 `http://host.docker.internal:5055`，也可填写局域网地址。不要使用 `localhost` 指代其他服务。

## 同步规则

- 通过 IMDb ID 精确匹配媒体。无法匹配的条目可手动指定 TMDB ID。
- 电视剧有明确季号时请求对应季，不请求特别篇。
- 电影或季已在 Seerr 请求、处理中或已有媒体时跳过；成功条目会保留去重记录，删除 Seerr 请求不会自动重新下载。
- Cookie 失效后需重新导入；同一份 Cookie 成功提醒一次，重新导入后恢复提醒。
- 停止任务不会撤销已提交的请求。「已提交」表示请求已交给 Seerr，实际下载由其配置的服务处理。

## 更新与备份

```bash
git pull
docker compose up -d --build
```

配置和记录保存在 `sync-data` 数据卷中。备份前停止容器，并备份整个数据卷，包括数据库和加密密钥。`docker compose down` 保留数据，`docker compose down -v` 会删除数据。

## 让 Agent 安装并配置

复制以下 Prompt，填写目标机器即可：

```text
请安装并配置开源项目「豆瓣映单 / Douban Reel」。

- 仓库：https://github.com/fudimeng/douban-reel.git
- 目标机器：<主机名或 SSH 别名；已在目标机器则直接操作>
- 域名：<可选；不填则使用局域网地址>
- 登录认证：开启（需要免密码时改为关闭，设置 AUTH_ENABLED=false）
- 开启定时同步：否（需要自动下载时改为是）

请阅读 README，检查目标机器的 Docker Compose、可用端口和已有部署，
选择安装目录，在指定机器上构建并部署。已有部署先备份，保留数据和设置。

优先从目标机器现有服务的配置中读取 Seerr API Key、豆瓣 Cookie 和可选 Bark 地址，
不修改原服务。无法找到时再询问私密文件位置；豆瓣账号从 Cookie 自动识别。
不要在聊天、日志、命令参数或 Git 中暴露凭据，.env 仅部署用户可读。

其余使用默认值：电影和电视剧都开启，最近 30 天，每天一次，
随机间隔上限 15 秒，无明确季号时只请求第一季。
可使用 Web UI 或下方管理 API 配置，Cookie 必须经过验证接口保存。

如提供域名，配置 DNS 和 HTTPS 反向代理，保留已有站点。
验证容器健康、页面访问、Seerr 连接，并执行一次预览同步检查匹配结果。
预览通过后，按上述选项决定是否开启定时同步。
不要为测试提交实际下载请求或发送 Bark 通知。

最后报告访问地址、部署版本、验证结果、定时任务状态和凭据文件位置，
并提供更新与备份方法，不报告凭据内容。
```

### 管理 API

API 与网页使用相同的认证设置。写操作需请求头 `X-Requested-With: douban-reel`，JSON 请求需 `Content-Type: application/json`。

| 操作 | 方法与路径 |
| --- | --- |
| 健康检查 | `GET /healthz` |
| 读取配置 | `GET /api/config` |
| 保存配置 | `PUT /api/config` |
| 验证并保存 Cookie | `POST /api/cookie`，请求体 `{"cookie":"<完整 Cookie>"}` |
| 测试 Seerr | `POST /api/test/seerr` |
| 预览同步 | `POST /api/sync?preview=true` |
| 实际同步 | `POST /api/sync?preview=false` |
| 查询任务与记录 | `GET /api/status` |
| 停止任务 | `POST /api/stop` |

保存配置示例（敏感值从私密文件读取）：

```json
{
  "seerr_url": "http://host.docker.internal:5055",
  "seerr_api_key": "<API Key>",
  "movies": true,
  "tv": true,
  "history_days": 30,
  "interval_minutes": 1440,
  "request_delay": 15,
  "tv_seasons": "first",
  "enabled": false
}
```

可选字段 `bark_url` 用于保存推送地址。API Key 和 Bark 地址留空时保留已有值，Cookie 通过独立接口导入。预览完成后检查对应运行记录的状态和错误信息。

## 开发

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

本地运行需设置至少 12 位的 `ADMIN_PASSWORD`：

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8787
```

## 许可证

[MIT License](LICENSE)。提交问题或代码时，请移除真实 Cookie、API Key、推送地址和密码。
