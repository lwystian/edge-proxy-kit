# 测试报告

开发日期：2026-10-29。未连接或修改用户服务器。

本地 Windows 已执行 Bash 语法检查、Python 编译及 28 项逻辑测试：27 项通过，1 项 Linux 脚本执行测试明确跳过。

覆盖输入验证、路径分流、认证分离、私有备份、原站点保护、原 inode 保持、加载失败回退、损坏备份拒绝、同名项目保护、密码回车保留、卸载范围，以及本地临时 Git 仓库中的安全快进、私有文件保护、本地改动和文件冲突拒绝。

## Linux 与真实容器：已通过

- 验证代码：`da1b9f99b4623c6488eae34d7087ed09389c2573`。
- [GitHub Actions 完整成功记录](https://github.com/lwystian/edge-proxy-kit/actions/runs/40851981925)。
- 独立 Ubuntu 24.04 x86_64 临时 runner，28 项逻辑测试全部通过，Bash 及 Python 语法检查通过。
- 实际使用官方 Caddy `2.11.4-alpine` 和一个临时 Python HTTP 后端；仅临时测试项目创建网络、容器和证书。
- 实际执行全新入口安装，发布标准 TCP 80/443，验证目录、启动及健康检查。
- 接管现有测试入口后，原 Caddyfile 和 Caddy 容器 ID 保持不变。
- Docker 网络接入即时生效，覆盖文件经真实 Compose 验证；更新重建后网络仍保留，后端容器未重启。
- 真实 HTTPS 使用测试专属 Caddy 内部 CA，验证证书链与主机名；不将其视为公开受信任证书。
- 验证 Basic 正确/错误密码、未认证面板及 `/config.js` 拒绝、Mihomo/Control API 的 Bearer 转发与路径处理、WebSocket 101。
- 实际 IP 白名单同时限制面板及 API；原网站仍可访问。
- 实际备份、加载失败自动回退、恢复、镜像更新及卸载；原证书文件不变，卸载后配置、证书和后端容器仍在。

该测试使用普通回显后端验证 MetaCubeXD 路由和请求头；没有在此仓库的 CI 启动真实 MetaCubeXD、执行其网页登录或业务操作。模板路径设计另核对了上游 v1.273.1 的 HTTP prefix、WebSocket endpoint 和控制接口实现。

Windows 本机未启动 Docker Engine，未在本机重复执行容器测试；`tests/integration.py` 不带 `--run` 只输出 SKIP，不计为通过。

## 尚未执行的验证

公开 ACME 签发及真实续期、用户 DNS/CDN/防火墙、真实业务登录及写入、其他发行版及 arm64 容器运行均需部署者验证。证书和业务数据不在配置备份范围。
