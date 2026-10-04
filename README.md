# 豆瓣映单 · Douban Reel

独立运行的中文 Web 工具：定时读取豆瓣影视 **想看** 列表，将 IMDb ID 交给 Seerr 精确匹配媒体，提交电影或电视剧请求，由 Seerr 对接 Radarr / Sonarr。无需另外申请或填写 TMDB Token。

## 功能

- 电影、电视剧分别开启；默认历史范围为最近 **30 天**，按天调整。
- 默认每天同步一次（1440 分钟），可设置 15–10080 分钟；默认不启用自动同步。
- 中文响应式 Web UI：配置、连接测试、手动预览、立即同步、停止任务、运行记录、条目状态与手动 TMDB 映射。
- Cookie 采用 [fudimeng/jellyfin-plugin-douban-sync](https://github.com/fudimeng/jellyfin-plugin-douban-sync) 的录入流程：粘贴完整 Cookie，检查 `dbcl2` / `ck`，服务端验证后加密保存，不回显，支持删除。该项目仅作为流程参考，没有复制其源码。
- 每次同步（含预览）先校验登录，更新豆瓣响应中的 Cookie 与登录页提供的 `ck` 并加密保存。豆瓣未返回新值时沿用有效值；失效的登录凭据不能靠刷新恢复，需重新导入。
- Cookie 失效或需要登录验证时可通过 Bark 通知。同一版 Cookie 成功提醒一次，重新导入后重新启用提醒；去重状态跨重启保留，发送失败在下一次失效检查重试。普通网络故障、限流和页面解析错误不触发失效通知。
- Cookie、Seerr API Key、Bark 推送地址使用 Fernet 加密保存；管理界面使用 HTTP Basic 登录。Bark 地址留空保留，可测试推送和删除；支持自建 HTTP(S) 服务。
- SQLite 持久化配置、同步状态、映射和运行记录；任务互斥、跨进程数据目录锁、失败项下次重试。
- 豆瓣请求间隔随机为 2 秒至配置上限，默认上限 15 秒，可设置 2–60 秒。首个请求立即发送；网络请求已耗时会从等待时间中扣除。同步、Cookie 验证和跳转共用限速，等待可随任务停止中断。遇到安全验证、限流或异常页面停止本轮，不尝试绕过验证。
- 运行中也能保存配置、验证/更新 Cookie 和设置映射；当前任务使用启动时的配置，新配置从下一次任务生效。

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
5. 调整电影/电视剧开关、历史天数、同步间隔等，保存配置，再测试连接。
6. 先“预览同步”，查看匹配结果；确认后“立即同步”，或启用定时同步并保存。

Seerr 请求接口使用 TMDB ID，因此仍需要将豆瓣条目匹配到 TMDB 媒体。但 Seerr 自身提供 `imdb:tt…` 精确查询，这一步由 Seerr 完成，本工具只需 Seerr 地址和 API Key。

Seerr 需要事先配置好默认 Radarr / Sonarr 服务、质量配置和根目录。工具提交普通清晰度请求（非 4K）。请求的自动批准、后续搜索和实际下载由 Seerr、Radarr / Sonarr 和下载客户端决定。“已提交”不代表已下载。

### 容器中的 Seerr 地址

- 同一 Docker 网络：例如 `http://seerr:5055`，将本工具加入 Seerr 所在网络即可。
- 访问宿主机：`http://host.docker.internal:5055`（Compose 已配置 host-gateway）。
- 访问其他设备：`http://192.168.1.100:5055`。
- 不要用 `localhost:5055` 指代其他容器；容器中的 localhost 是本工具自身。

## 同步规则

**日期范围**：以 Asia/Shanghai 的当前自然日为基准，包含今天在内的最近 N 天。例如 10 月 4 日、30 天，对应 9 月 5 日起的“想看”标记。按豆瓣标记日期，不按上映日期。每轮重新扫描该窗口，扩大天数即可补同步更早条目；日期缺失时记录失败，不猜测。

**定时任务**：保存配置、进程启动或真实同步结束后，从当前时刻重新计算间隔。进程重启不会立即自动补跑，可用“立即同步”。预览不重置定时器；开启定时同步后，预览结束仍可能按原计划执行真实同步。关闭定时开关不影响手动同步。

**运行中修改配置**：允许保存媒体开关、天数、间隔、服务连接信息、Cookie 和映射。当前任务继续使用启动时的配置和映射，避免一批条目发送到不同服务器或使用不同账号；新配置从下一次任务生效。保存会立即按新设置重新计算定时器。关闭定时同步或删除 Cookie 会取消后续自动任务，但不会中断当前任务；需要立即退出时点击“停止任务”。Cookie 验证结束后仅更新对应凭据，不会覆盖验证期间修改的同步规则。验证和同步共用豆瓣请求限速。

**登录刷新与通知**：自动刷新只合并豆瓣返回的登录凭据，不执行自动登录，也不修改收藏。任务刷新时不会覆盖运行期间手动导入或删除的新 Cookie，也不会重置失效通知去重。通知只包含固定的重新录入提示，不包含 Cookie、设备密钥、账号 ID 或上游响应。Bark 推送地址录入与去重规则参考 Jellyfin 豆瓣同步插件，未复制其源码。

**电影匹配**：读取豆瓣详情页的 IMDb ID，调用 Seerr `/api/v1/search?query=imdb:tt…`，要求对应媒体类型唯一匹配，再核对详情页的 `externalIds.imdbId`；无 IMDb ID、类型不一致、多结果或 IMDb ID 不一致时，记录待处理，可在 UI 中手动指定 TMDB ID。不会按模糊标题自动提交。Seerr 自身需要能够访问 TMDB。

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

测试使用模拟外部接口，覆盖日期边界、分页、媒体开关、预览、重复请求、分季、失败重试、Seerr IMDb 查询与身份核对、Cookie 加密/验证、运行中更新配置和凭据、认证与 CSRF。真实豆瓣登录和 Seerr 下载链路需要使用你自己的凭据联调。豆瓣 HTML 接口并非稳定公共 API，页面变化可能需要更新解析器。

协议依据：[Seerr API 定义](https://github.com/seerr-team/seerr/blob/develop/seerr-api.yml)、[Seerr IMDb 查询实现](https://github.com/seerr-team/seerr/blob/develop/server/lib/search.ts)。

## 开源与贡献

本项目采用 [MIT License](LICENSE)。欢迎提交问题与改进；提交代码前请运行上述测试。

仓库只包含源码、测试、部署模板与文档。`.env.example` 不包含密码，实际 `.env`、运行数据库、加密密钥、日志、截图及备份均被忽略。测试中的 Cookie、密码和 Token 是用于模拟接口的虚构值。

提交 Issue、PR 或日志前，请移除真实豆瓣 Cookie、Seerr API Key 与管理员密码。不要上传数据卷或含登录信息的截图。
