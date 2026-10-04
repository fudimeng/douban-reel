# 豆瓣 → Seerr

独立运行的中文 Web 工具：定时读取豆瓣影视 **想看** 列表，通过 IMDb ID 精确匹配 TMDB，提交 Seerr 电影或电视剧请求，由 Seerr 对接 Radarr / Sonarr。

## 功能

- 电影、电视剧分别开启；默认历史范围为最近 **30 天**，按天调整。
- 默认每 360 分钟同步，可设置 15–10080 分钟；默认不启用自动同步。
- 中文响应式 Web UI：配置、连接测试、手动预览、立即同步、停止任务、运行记录、条目状态与手动 TMDB 映射。
- Cookie 采用 [fudimeng/jellyfin-plugin-douban-sync](https://github.com/fudimeng/jellyfin-plugin-douban-sync) 的录入流程：粘贴完整 Cookie，检查 `dbcl2` / `ck`，服务端验证后加密保存，不回显，支持删除。该项目仅作为流程参考，没有复制其源码。
- Cookie、Seerr API Key 和 TMDB Token 使用 Fernet 加密保存；管理界面使用 HTTP Basic 登录。
- SQLite 持久化配置、同步状态、映射和运行记录；任务互斥、跨进程数据目录锁、失败项下次重试。
- 豆瓣默认请求间隔 5 秒，遇到安全验证、限流或异常页面停止本轮，不尝试绕过验证。

## Docker 部署

在项目目录运行：

```bash
python3 scripts/init_env.py
docker compose up -d --build
```

打开 **http://localhost:8787**，浏览器会提示登录。用户名默认 `admin`，自动生成的密码在本地 `.env` 文件中。初始化脚本不覆盖已有 `.env`，也不会把密码输出到终端日志。

没有 Python 的 NAS 可复制 `.env.example` 为 `.env`，自行修改 `ADMIN_PASSWORD`（至少 12 个字符），然后运行 Docker Compose。

默认仅监听 `127.0.0.1`。需要局域网访问时，将 `.env` 中 `BIND_ADDRESS` 改为 NAS 的局域网 IP 或 `0.0.0.0`，再次执行 `docker compose up -d`，通过 `http://NAS-IP:8787` 访问。远程访问请放在 HTTPS 反向代理后，避免明文传输登录凭据。应用部署在域名根路径。

```bash
docker compose ps
docker compose logs --tail=100
docker compose down
```

Docker 数据保存在命名卷 `sync-data`。正常停止容器不会删除数据；不要使用 `docker compose down -v`，除非确实想删除配置和同步历史。镜像以 UID 10001 非 root 用户运行。

## 首次配置

1. 输入豆瓣用户 ID 或个人主页 URL。这里指个人主页 `/people/xxx/` 中的 `xxx`，不是昵称。
2. 浏览器登录豆瓣电影，按 F12 → Network，刷新页面，选中发往 `movie.douban.com` 的页面请求，从 Request Headers 复制完整 `Cookie`。
3. 在 UI 中粘贴 Cookie，点击“验证并保存 Cookie”。至少包含 `dbcl2` 和 `ck`。也支持 Cookie JSON 数组，非豆瓣域名的 Cookie 会过滤掉。原 Cookie 验证失败时不会覆盖已保存的 Cookie。
4. 填入 Seerr 基础地址及 API Key（Seerr 设置 → 通用）。不要填写网页的 `/requests` 路径。
5. 填入 [TMDB API 设置](https://www.themoviedb.org/settings/api)里的 **API Read Access Token**（长令牌，不是 32 位 API Key；无需添加 `Bearer ` 前缀）。
6. 调整电影/电视剧开关、历史天数、同步间隔等，保存配置，再测试连接。
7. 先“预览同步”，查看匹配结果；确认后“立即同步”，或启用定时同步并保存。

Seerr 需要事先配置好默认 Radarr / Sonarr 服务、质量配置和根目录。工具提交普通清晰度请求（非 4K）。请求的自动批准、后续搜索和实际下载由 Seerr、Radarr / Sonarr 和下载客户端决定。“已提交”不代表已下载。

### 容器中的 Seerr 地址

- 同一 Docker 网络：例如 `http://seerr:5055`，将本工具加入 Seerr 所在网络即可。
- 访问宿主机：`http://host.docker.internal:5055`（Compose 已配置 host-gateway）。
- 访问其他设备：`http://192.168.1.100:5055`。
- 不要用 `localhost:5055` 指代其他容器；容器中的 localhost 是本工具自身。

## 同步规则

**日期范围**：以 Asia/Shanghai 的当前自然日为基准，包含今天在内的最近 N 天。例如 10 月 4 日、30 天，对应 9 月 5 日起的“想看”标记。按豆瓣标记日期，不按上映日期。每轮重新扫描该窗口，扩大天数即可补同步更早条目；日期缺失时记录失败，不猜测。

**定时任务**：保存配置、进程启动或真实同步结束后，从当前时刻重新计算间隔。进程重启不会立即自动补跑，可用“立即同步”。预览不重置定时器；开启定时同步后，预览结束仍可能按原计划执行真实同步。关闭定时开关不影响手动同步。运行中修改配置会被拒绝，请先停止任务或等待完成。

**电影匹配**：读取豆瓣详情页的 IMDb ID，调用 TMDB `/find/{imdb_id}`，要求对应媒体类型唯一匹配；无 IMDb ID、类型不一致或多结果时，记录待处理，可在 UI 中手动指定 TMDB ID。不会按模糊标题自动提交。

**电视剧匹配**：识别豆瓣标题的“第 N 季”及 `Season N`，仅请求该季。无明确季号时，默认只请求第一季，可设置为当前全部普通季。排除第 0 季；目标季不存在时留待处理。不同数据源的分季方式可能不同，请先预览或使用手动映射。IMDb 仅匹配到单集的结果不会推断为整季。

**重复检查**：提交前读取 Seerr 详情，排除已经请求、处理中或已有媒体的电影/季；同一目标 Seerr 和豆瓣用户的成功条目也会本地去重。重启保留去重记录，切换 Seerr 或豆瓣用户会使用独立记录。成功条目之后不再请求，因此“全部普通季”仅指首次同步时的季，未来新增季需要在豆瓣另标记对应季；删除 Seerr 请求不会自动触发重新下载。

**失败重试**：失败条目在下一次手动/定时同步中重新处理，但仍需位于当前历史窗口。Seerr 写入超时时不立即重复 POST；下一轮先读取 Seerr 状态再决定是否提交。网络中断与服务端最终状态之间不能保证严格 exactly-once，Seerr 自身检查作为最后一道去重措施。

**停止**：当前 HTTP 请求最多等待 30 秒，完成后退出；已提交的请求不会撤销。网页遇到豆瓣验证时，需在同出口网络浏览器中完成验证再重新导入 Cookie。

## 数据与备份

`/data/sync.db` 保存 SQLite 数据，`/data/secret.key` 保存本机加密密钥。备份前停止容器，一并备份整个数据卷（包括可能存在的 SQLite WAL 文件）。不要只保存数据库而丢失密钥。该加密用于避免数据库中直接出现明文凭据，不能防御同时取得数据卷与密钥的访问者。

当前版本面向单管理员、单豆瓣账号、单 Seerr 实例；只运行一个服务副本、一个 Uvicorn worker。一个数据目录只允许一个进程运行。界面显示最近 20 次运行与最近 200 个条目，累计计数包含所有条目。

## 本地开发与验证

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

本地启动前设置 `ADMIN_PASSWORD`（至少 12 位），然后：

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8787
```

测试使用模拟外部接口，覆盖日期边界、分页、媒体开关、预览、重复请求、分季、失败重试、Cookie 加密/验证、认证与 CSRF。真实豆瓣登录和 Seerr 下载链路需要使用你自己的凭据联调。豆瓣 HTML 接口并非稳定公共 API，页面变化可能需要更新解析器。

协议依据：[Seerr API 定义](https://github.com/seerr-team/seerr/blob/develop/seerr-api.yml)、[TMDB Find By ID](https://developer.themoviedb.org/reference/find-by-id)。

## 开源与贡献

本项目采用 [MIT License](LICENSE)。欢迎提交问题与改进；提交代码前请运行上述测试。

仓库只包含源码、测试、部署模板与文档。`.env.example` 不包含密码，实际 `.env`、运行数据库、加密密钥、日志、截图及备份均被忽略。测试中的 Cookie、密码和 Token 是用于模拟接口的虚构值。

提交 Issue、PR 或日志前，请移除真实豆瓣 Cookie、Seerr API Key、TMDB Token 与管理员密码。不要上传数据卷或含登录信息的截图。
