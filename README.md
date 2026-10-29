# Edge Proxy Kit

[![自动验证](https://github.com/lwystian/edge-proxy-kit/actions/workflows/test.yml/badge.svg)](https://github.com/lwystian/edge-proxy-kit/actions/workflows/test.yml)

一个官方 Caddy 容器，加一个中文交互脚本。新增网站、HTTPS、路径分流、访问认证和 Docker 网络接入，不必每次手写配置。没有管理后台、数据库、额外常驻服务或自建镜像。

支持新建独立入口，也支持接管现有 Caddy Compose。项目不是 Caddy 官方项目。适用具备 Docker Engine、Compose 插件、Bash、Python 3.8+、Git 的 Linux amd64/arm64 系统，不限定发行版和安装目录。脚本只检查依赖，不自动安装 Docker 或修改防火墙。

## 开始使用

在自己选择的独立目录中克隆，以 root 或 sudo 运行：

```bash
git clone https://github.com/lwystian/edge-proxy-kit.git
cd edge-proxy-kit
./deploy.sh
```

首次提供两个选项：

1. **新建入口**：在选定的现有空项目目录或仓库目录生成 Compose，启动一个官方 `caddy:2.11.4-alpine` 容器，发布 TCP 80/443。已有程序占用端口时会停止，不停止其他容器。新安装的证书及 ACME 账户存放在 `data/`，配置状态在 `config/`。
2. **接管已有入口**：选择现有项目目录及 Caddy 服务名，验证后建立管理记录。原 Caddyfile、Compose、证书挂载、静态资源挂载和容器保持不变。原来手写的站点继续生效，不自动转换或覆盖。

例如已有独立入口位于 `/你选择的路径/edge-proxy`：

```bash
./deploy.sh adopt --root /你选择的路径/edge-proxy
```

接管只支持项目中的 `compose.yaml`，以及 `./Caddyfile` 文件绑定到容器 `/etc/caddy/Caddyfile` 的标准布局。Compose 服务名默认 `caddy`，可输入其他名称。使用多个其他 Compose 文件、Docker Swarm、自定义 Caddy 配置位置等布局会停止接管，不省略原配置继续执行。

工具记住所管理的目录。以后在 **Git 克隆的工具目录** 执行 `./deploy.sh` 打开菜单，或使用下面的子命令。运行配置可与工具代码分开存放。

## 新增网站会询问什么

执行 `./deploy.sh add`：

| 参数 | 默认及行为 |
| --- | --- |
| 站点类型 | 普通反代；可选 MetaCubeXD 面板与 API 同域分流 |
| 网站域名 | 必填，例如 `app.example.com`；用户自行配置 DNS |
| 自动 HTTPS | 默认启用；可选择 HTTP，用于局域网等场景 |
| 后端地址 | 必填，例如 `http://app-web:8000`，使用容器内部端口 |
| 网页访问密码 | 默认关闭；启用后隐藏输入密码，只保存 bcrypt 哈希 |
| 高级选项 | 默认跳过；可设置路径分流、IP 白名单、压缩和上传大小 |
| 配置预览 | 隐藏密码哈希，确认后才备份、验证、加载 |

密码不要求复杂度，bcrypt 最长支持 72 个 UTF-8 字节，不能包含换行或 NUL。留空可生成密码，只在当前终端显示一次，后续不保存明文。用户名支持字母、数字、`_ . @ -`。

高级设置支持多个路径前缀，选择保留或去掉前缀。例如 `/api/orders` 去掉 `/api` 后转发为 `/orders`。匹配同时覆盖 `/api` 和 `/api/*`，不会误匹配 `/apix`。较长的路径优先。每条路径可选择是否要求 Basic Auth；关闭时必须由后端提供所需认证。

WebSocket 由 Caddy 自动转发，不需要添加 Upgrade 头或安装插件。HTTPS 后端默认验证其证书，不提供跳过证书校验开关。跨域需求应由后端配置；优先使用同域路径分流，工具不会添加 `Access-Control-Allow-Origin: *`。

Basic Auth 占用 `Authorization` 请求头，不能和同一条请求的 Bearer 认证叠加。后端需要 Bearer 时，可为其 API 设置独立分流路径并关闭该路径的 Basic Auth。

## HTTPS 与证书

网站输入完整域名且选择 HTTPS 后，由 Caddy 自动申请、续期受支持的公开证书。用户自行配置域名解析，确保公网 TCP 80/443 能到达本入口，CAA、IPv6 记录和云防火墙也必须正确。不使用第三方 DNS API、DNS Token 或专用插件，不支持通配符自动签发。

保存成功只表示配置已加载，**证书签发是异步过程**，需再用浏览器及日志验证。HTTP 不加密，所以工具不允许在 HTTP 模式启用 Basic Auth。新安装不发布 Caddy 管理 API 2019，也不默认开放 UDP 443；现有项目接管时保持原端口设置。

证书数据是持久化数据，`remove`、`uninstall`、代码更新不会删除它。新安装保留 `data/` 和 `config/`；接管部署沿用原证书目录或 volume。不要删除证书来测试续期，也不要让两个 Caddy 同时写同一个证书存储。

## Docker 网络

容器中的 `127.0.0.1` 指向 Caddy 容器自身。后端端口只绑定宿主机回环地址时，不能直接把这个地址填入 Caddy。

```bash
./deploy.sh networks
./deploy.sh connect-network
```

第二个命令询问一个**已存在的用户自定义 bridge 网络**。确认后仅把 Caddy 接入该网络，保存到 `compose.edge-kit.yaml`；不创建网络，不修改后端 Compose，不重启后端容器，也不需要立刻重建 Caddy。此后通过后端容器名或唯一网络别名访问，避免固定容器 IP。

接入前请评估网络内的服务是否可信。工具保留 Caddy 原有全部网络。以后统一使用本工具管理入口，它会同时加载原 Compose 和覆盖文件；如果手动只加载原 Compose，新增网络可能在容器重建时丢失。

## MetaCubeXD 示例

先把 Caddy 接入 Mihomo 所在网络，再执行 `./deploy.sh add`，选择类型 2，输入自己的域名、面板容器地址和 API 容器地址。例如：

```text
面板后端：http://mihomo-server:9090
API 后端：http://mihomo-server:9091
```

工具生成三类路由：

- `/mihomo` 和 `/mihomo/*`：去掉前缀后转发内核 API，保留其 Bearer 密钥及 WebSocket。
- `/api/control` 和 `/api/control/*`：转发面板控制接口，由上游 `CONTROL_TOKEN` 认证。
- 其他路径：转发面板，可加 Basic Auth，保护 HTML、静态资源和 `/config.js`。

面板里将后端地址设为 `https://你的域名/mihomo`，填写 Mihomo API Secret。工具不读取、生成或关闭这两个 API 的密钥。上游原生控制接口会暴露于该域名；应使用足够强的原有密钥，必要时再设置整个站点的 IP 白名单。Basic Auth 保护面板资源，不能代替 API 自身认证。

## 统一命令

```bash
./deploy.sh                         # 中文菜单
./deploy.sh help
./deploy.sh install
./deploy.sh adopt
./deploy.sh add
./deploy.sh edit
./deploy.sh list
./deploy.sh enable
./deploy.sh disable
./deploy.sh remove
./deploy.sh networks
./deploy.sh connect-network
./deploy.sh status
./deploy.sh doctor
./deploy.sh logs
./deploy.sh logs -f
./deploy.sh reload
./deploy.sh start
./deploy.sh stop
./deploy.sh restart
./deploy.sh update
./deploy.sh backup
./deploy.sh restore
./deploy.sh uninstall
```

`edit/enable/disable/remove` 仅管理由工具创建的站点。手写站点单独保留；工具发现同域名或原配置中的通配符覆盖该域名时会拒绝新增，避免覆盖业务。停用/移除只修改规则，后端容器和证书数据不删除。

`reload` 先验证后平滑加载，不重建容器；已有 WebSocket 可能重连。`stop/restart/uninstall` 会影响此入口的全部网站，需要明确确认。`start` 可拉取本机缺少的镜像，适用于恢复首次拉取失败的安装。

`update` 自动安全拉取 Git 代码，再由新版脚本备份配置并更新入口镜像；本地修改、分叉或文件冲突时停止，不强制覆盖。默认保留已配置的镜像版本，不跳转到 `latest`。指定官方新版本可用：

```bash
./deploy.sh update --image caddy:版本号-alpine
./deploy.sh update --image-only
```

镜像更新可能短暂中断入口；失败会尝试用更新前的本机镜像 ID 恢复，并把覆盖文件固定到该 ID，下次请显式指定正确的 `--image`。其他业务容器不参加更新。代码更新会保留原 Git 历史；镜像回退不回退已拉取的工具代码。

`doctor` 检查 Compose、容器归属、Caddyfile 挂载、配置语法、托管记录和域名解析。它不能证明公网证书已经签发、后端登录有效或业务写入正常。IP 白名单使用直连客户端地址；入口前有 CDN/其他代理时，需自行配置可信代理策略，工具不盲目信任 `X-Forwarded-For`。

## 备份与保护范围

每次网站配置变更前，备份到项目同级的 `项目目录名-backups/edge-时间戳/`。包括 Caddyfile、Compose、工具覆盖文件、存在的 `.env`、管理配置及站点记录，附每个文件的 SHA256。目录权限 700，文件权限 600。

`backup` 是**配置备份**，不包含证书存储、静态资源或业务数据库。异机迁移时，应另外安全备份原有 `data/config` 目录或对应 Docker volumes，以及后端应用自己的数据。工具不会暗中停服务复制业务数据，也不声称配置备份等于整机备份。

`restore` 先检查清单、哈希及项目身份，再确认、备份当前状态，恢复网站规则与站点记录。Compose 副本仅供参考，不自动回退端口、网络、镜像或证书，避免破坏当前服务关系。加载失败会把原 Caddyfile 和记录写回并尝试重新加载；回退加载也失败会明确报告，保留备份。

工具用原 inode 写入单文件绑定的 Caddyfile，避免宿主机替换文件后容器仍读取旧配置。不会使用 `down -v`、全局 Docker 清理、删除数据目录或修改数据库。接管旧迁移项目后，不要继续执行旧的一次性迁移/回退脚本。

运行日志、原始 Docker 日志和私有诊断可能含域名、请求及哈希；请只在本机查看。代码仓库不包含真实域名、证书、密码、IP、订阅或现有服务器配置。

## 文件与测试

```text
deploy.sh                  唯一入口
edge.py                    交互、配置生成和管理逻辑；仅 Python 标准库
tests/test_edge.py         逻辑及失败保护测试
tests/integration.py       独立 Docker 项目中的真实 Caddy 验证
.github/workflows/test.yml 自动测试
TEST_REPORT.md             已验证和未验证的范围
```

部署目录中的 `Caddyfile`、`compose.yaml`、`compose.edge-kit.yaml`、`.edge-kit/`、`data/`、`config/` 及工具目录的 `settings.json` 均属于本地运行数据，不提交 Git。

```bash
python3 tests/test_edge.py
python3 tests/integration.py          # 显示 SKIP
sudo python3 tests/integration.py --run
```

最后一个命令仅在测试环境运行，会创建临时 Docker 网络、后端和 Caddy，使用临时内部 CA 验证 TLS，不申请真实公开证书，不连接现有业务服务器。

官方依据：[Caddy CLI](https://caddyserver.com/docs/command-line)、[反向代理及 WebSocket](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)、[Basic Auth](https://caddyserver.com/docs/caddyfile/directives/basic_auth)、[自动 HTTPS](https://caddyserver.com/docs/automatic-https)。
