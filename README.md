# Edge Proxy Kit

[![自动验证](https://github.com/lwystian/edge-proxy-kit/actions/workflows/test.yml/badge.svg)](https://github.com/lwystian/edge-proxy-kit/actions/workflows/test.yml)

轻量的 Caddy 反向代理管理工具，适用于网站、API、管理后台等 HTTP 服务。一个官方 Caddy 容器，一个中文交互入口。后端可以是 Docker 容器、宿主机上的服务，或其他服务器。

核心功能：添加和管理网站、自动 HTTPS、可选访问密码、可选路径分流，以及日常维护。新建网站不选择应用类型，也不需要了解后端项目的配置格式。

## 开始使用

需要 Linux、Docker Engine、Compose 插件、Bash、Python 3.8+ 和 Git，以 root 或 sudo 运行。不限定发行版及安装目录。

```bash
git clone --depth 1 --no-tags https://github.com/lwystian/edge-proxy-kit.git
cd edge-proxy-kit
./deploy.sh
```

首次选择**新建入口**或**接管已有 Caddy**。新建时启动官方 `caddy:2.11.4-alpine`，使用 TCP 80/443，证书持久化到 `data/`，配置状态保存到 `config/`。发现端口冲突会停止并提示。

接管已有独立入口：

```bash
./deploy.sh adopt --root /你的入口项目目录
```

接管要求项目使用 `compose.yaml`，将项目内 `Caddyfile` 绑定到容器 `/etc/caddy/Caddyfile`。原手写站点、静态文件挂载、证书、Compose 和容器保持原样；不自动改写原站点。不符合布局时停止接管。

## 添加网站：域名 → 后端

```bash
./deploy.sh add
```

```text
网站域名（默认 HTTPS）：app.example.com
后端地址（IP:端口/域名:端口，输入 d 选择容器）：192.0.2.10:8080
配置已验证并平滑加载。
```

后端地址支持：

| 后端 | 填写示例 |
| --- | --- |
| 可达的服务器地址 | `192.0.2.10:8080` |
| 其他域名 | `https://origin.example.com` |
| 已在共同网络中的 Docker 服务 | `http://web:8000` |
| 交互选择 Docker 容器 | 输入 `d`，再选择容器和内部服务端口 |

没有写协议时，后端默认 HTTP。后端 URL 不含路径或凭据。容器列表仅在输入 `d` 时展开；选择后工具会检查网络，填写完成后按需把 Caddy 接入该容器的已有网络并持久化。输入过程中可用 Ctrl+C 退出；实际应用前自动备份和验证，失败自动回退。

网站默认 HTTPS。用户自行解析域名，并使公网 TCP 80/443 到达本入口，Caddy 才能自动申请和续期公开证书。不需要 DNS 服务商 API 或额外 Certbot。签发异步进行，保存后请通过浏览器检查。内网只需 HTTP 时，网站输入 `http://app.example.com` 或 `http://192.0.2.10`。

## 修改网站

```bash
./deploy.sh edit
```

只选择需要修改的一项：**后端、域名、路径分流、访问密码、HTTPS/HTTP**。只有一个网站时自动选中；修改一项不会重新询问其他配置。没有变化时不备份、不加载。

压缩默认开启，不额外限制请求体大小。旧版本已经保存的白名单、上传限制、压缩设置及路由继续保留，不在日常向导中增加问题。

访问密码默认关闭，启用时隐藏输入且只输入一次，仅保存 bcrypt 哈希。密码不要求复杂度，最长 72 个 UTF-8 字节，不能含换行或 NUL；编辑时回车保留原密码。Basic Auth 需要 HTTPS。

有多个后端才需要路径分流，也可直接执行：

```bash
./deploy.sh route
```

例如 `/api` 转发到另一个 HTTP 服务。默认保留原请求路径；后端要求从根路径访问时，再选择去掉前缀。已有规则沿用原来的前缀处理方式。支持单独新增、修改和移除。

WebSocket 自动转发。API 密钥、登录和跨域策略仍由后端应用负责。Basic Auth 和 Bearer 使用同一个 `Authorization` 头；需要 Bearer 的分流路径可关闭额外访问密码，由应用自身认证。工具不自动配置具体应用的前端 API 地址。

安装、接管、保存网站、网络接入、更新、启停、恢复和卸载均不再二次确认。选择操作并填写必要参数后直接执行，变更前备份、格式校验和失败回退自动完成。

## 日常命令

直接执行 `./deploy.sh` 打开菜单，或者使用：

| 命令 | 作用 |
| --- | --- |
| `add` / `edit` / `list` | 新增、修改、查看网站 |
| `enable` / `disable` / `remove` | 启用、停用、移除网站规则 |
| `status` / `doctor` | 查看状态、诊断配置和 DNS |
| `logs` / `logs -f` | 查看日志、持续跟踪 |
| `update` | 自动更新项目文件和入口镜像 |
| `start` / `stop` / `restart` / `reload` | 启动、停止、重启、验证后平滑加载 |
| `backup` / `restore` | 配置备份、校验并恢复网站规则 |
| `help` | 完整命令说明 |

例如更新：

```bash
./deploy.sh update
```

正常更新只显示检查和更新结果，不显示提交号、分支或历史记录。项目文件有本地改动或更新冲突时停止，不强制覆盖。镜像默认保留已配置版本；切换版本可用 `./deploy.sh update --image caddy:版本号-alpine`。只更新镜像可加 `--image-only`。镜像更新可能短暂中断入口，失败会尝试按原本机镜像 ID 恢复，不重启业务容器。

## 数据与边界

首次新版备份成功时，会清理同级旧目录 `部署目录名-backups/` 中可识别的本项目配置备份，并移除空目录；无法识别、损坏、其他项目的文件及符号链接保持原样。备份跟随实际部署目录，例如管理工具与入口分开存放时，备份放在入口目录，不放在工具仓库目录。

- 只管理本入口。原手写站点不自动转换；重复域名和手动修改的托管区域会被保护。
- 每次配置变更前自动备份并验证，加载失败回退。备份位于入口部署目录内的 `backups/`，只保留最近一份完整配置备份；新备份写完并校验后才删除旧份，失败保留上一份。目录权限 700，文件权限 600。
- `backup` **仅备份配置**，不包含证书、静态资源或业务数据。迁移服务器时须另外备份 `data/config` 或原有证书 volumes。`restore` 校验哈希并自动备份当前状态，不自动回退网络、端口或证书。
- 停用、移除站点仅修改规则。`uninstall` 自动备份后只移除入口容器，保留配置、证书、网络和后端应用；不执行 `down -v` 或全局清理。
- Caddy 在容器中，`127.0.0.1` 指向 Caddy 自身。宿主机服务需使用容器可达的地址；仅绑定宿主机回环端口的服务不能直接用宿主机公网 IP 代替。Docker 端口候选仅来自声明或映射，不保证 HTTP 服务已监听。
- 手动输入 Docker 服务名时，须已有共同网络；也可用 `networks` / `connect-network` 查看并接入已有可信网络。工具生成的 `compose.edge-kit.yaml` 应随原 Compose 一起加载，日后统一使用工具管理。
- 不自动安装依赖、修改防火墙或配置 CDN。支持 HTTP/HTTPS 反代，不管理任意 TCP/UDP 协议。复杂需求可保留独立手写 Caddy 站点，避免将所有 Caddy 功能堆进交互菜单。

运行配置、域名、密码哈希和证书保存在本地，不提交 Git。原始日志可能包含请求信息，分享前请检查。

## 开发与测试

实现只有 `deploy.sh` 和 `edge.py`，Python 仅使用标准库，无额外管理后台、数据库或常驻进程。

```bash
bash -n deploy.sh
python3 tests/test_edge.py
sudo python3 tests/integration.py --run
```

容器测试仅在独立测试环境执行，会创建临时项目，使用测试内部 CA，不申请真实公开证书。已验证和未验证范围见 [测试报告](TEST_REPORT.md)。

本工具非 Caddy 官方项目。依据：[自动 HTTPS](https://caddyserver.com/docs/automatic-https)、[反向代理与 WebSocket](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)、[Basic Auth](https://caddyserver.com/docs/caddyfile/directives/basic_auth)。
