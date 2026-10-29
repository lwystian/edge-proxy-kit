#!/usr/bin/env python3
"""官方 Caddy Compose 的中文交互入口；不管理后端应用或删除数据卷。"""
import argparse
import contextlib
import copy
import datetime
import fnmatch
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse

SOURCE = Path(__file__).resolve().parent
IMAGE = "caddy:2.11.4-alpine"
BEGIN = "# BEGIN EDGE-PROXY-KIT\n"
END = "# END EDGE-PROXY-KIT\n"
OVERLAY = "compose.edge-kit.yaml"
CATALOG = ".edge-kit/sites.json"


class Error(Exception):
    pass


def run(args, data=None, cwd=None, timeout=120):
    try:
        result = subprocess.run([str(a) for a in args], input=data, cwd=cwd, timeout=timeout,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        raise Error("命令无法执行或超时：" + str(args[0]))
    if result.returncode:
        diagnostic = SOURCE / ".edge-kit/last-error.log"
        with contextlib.suppress(Exception):
            if not diagnostic.parent.is_symlink():
                private(diagnostic, result.stdout + "\n" + result.stderr)
        raise Error("命令失败：%s，退出码 %s；本机私有诊断：%s" % (args[0], result.returncode, diagnostic))
    return result.stdout


def tty():
    if not sys.stdin.isatty():
        raise Error("交互操作需要终端，不要通过管道传入密码。")


def ask(message, default=None):
    tty()
    return input(message + (" [%s]" % default if default is not None else "") + "：").strip() or default


def yes(message, default=False):
    value = ask(message + (" [Y/n]" if default else " [y/N]"))
    return default if not value else value.lower() in ("y", "yes", "是")


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def private(path, content):
    path = Path(path)
    if path.is_symlink():
        raise Error("拒绝覆盖符号链接：" + path.name)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".edge-tmp-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.chmod(name, 0o600)
            stream.write(content.encode("utf-8") if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, value):
    private(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path):
    if Path(path).is_symlink():
        raise Error("配置文件不能是符号链接。")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def inplace(path, content):
    # Caddyfile 是单文件绑定，不能原子替换 inode，否则容器可能仍读取旧文件。
    descriptor = os.open(str(path), os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "wb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise Error("Caddyfile 必须是普通文件。")
        stream.write(content.encode("utf-8"))
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


@contextlib.contextmanager
def lock(root):
    import fcntl
    directory = root / ".edge-kit"
    if directory.is_symlink():
        raise Error("管理目录不能是符号链接。")
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    descriptor = os.open(str(directory / "lock"), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error("另一个管理操作正在执行，请稍后重试。")
        yield
    finally:
        os.close(descriptor)


def domain(value):
    try:
        value = value.strip().rstrip(".").encode("idna").decode("ascii").lower()
    except (AttributeError, UnicodeError):
        raise Error("请输入有效域名。")
    if len(value) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value):
        raise Error("域名不能包含协议、端口、路径、空格或通配符。")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in value.split(".")):
        raise Error("域名格式无效。")
    return value


def upstream(value):
    if not isinstance(value, str) or any(c.isspace() or c in '{}"\\' for c in value):
        raise Error("后端地址包含无效字符。")
    value = value if "://" in value else "http://" + value
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ("http", "https", "h2c") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError()
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        host = parsed.hostname
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", host):
                raise ValueError()
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            raise Error("容器里的本机地址指向 Caddy 自身。请填写同网络的容器名和容器端口，或可达的服务器地址。")
    except ValueError:
        raise Error("后端格式为 http://容器名:端口 或 https://后端域名:端口，不含路径或凭据。")
    return value.rstrip("/")


def prefix(value):
    if not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9_/-]+", value) or "//" in value or ".." in value:
        raise Error("路径前缀例如 /api，不能包含通配符、查询参数或 ..。")
    return value.rstrip("/")


def validate_site(site):
    if site.get("domain") != domain(site.get("domain")) or site.get("mode") not in ("https", "http"):
        raise Error("站点域名或访问模式无效。")
    if site["mode"] == "https":
        try:
            ipaddress.ip_address(site["domain"])
        except ValueError:
            pass
        else:
            raise Error("自动公开 HTTPS 请使用已解析的域名；局域网/IP 访问可选 HTTP。")
        if "." not in site["domain"]:
            raise Error("自动公开 HTTPS 需要完整域名。")
    upstream(site.get("upstream"))
    routes = site.get("routes", [])
    seen = set()
    for route in routes:
        path = prefix(route.get("prefix"))
        if path in seen:
            raise Error("路径前缀重复。")
        seen.add(path)
        upstream(route.get("upstream"))
        if site.get("mihomo") and (path == "/api" or path.startswith("/api/control")):
            raise Error("MetaCubeXD 模式的 /api/control 为保留控制接口，不能另加分流规则。")
    for cidr in site.get("allow", []):
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise Error("IP 白名单应为 IPv4、IPv6 或 CIDR。")
    auth = site.get("auth")
    if auth and (site["mode"] != "https" or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", auth.get("user", ""))
                 or not re.fullmatch(r"\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}", auth.get("hash", ""))):
        raise Error("访问认证需要 HTTPS、有效用户名及 bcrypt 哈希。")
    if not isinstance(site.get("body_mb", 0), int) or not 0 <= site.get("body_mb", 0) <= 10240:
        raise Error("上传限制范围为 0–10240 MiB，0 表示不额外限制。")


def render_site(site):
    validate_site(site)
    if not site.get("enabled", True):
        return ""
    address = ("http://" if site["mode"] == "http" else "") + site["domain"]
    lines = [address + " {"]
    if site.get("compress", True):
        lines += ["\tencode zstd gzip"]
    lines += ['\theader X-Content-Type-Options "nosniff"', "\theader -Server"]
    if site.get("allow"):
        lines += ["\t@blocked not remote_ip " + " ".join(site["allow"]), '\trespond @blocked "Forbidden" 403']
    if site.get("body_mb"):
        lines += ["\trequest_body {", "\t\tmax_size %sMiB" % site["body_mb"], "\t}"]

    def handle(target, matcher="", strip=None, authentication=True):
        result = ["\thandle" + (" " + matcher if matcher else "") + " {"]
        if strip:
            result += ["\t\turi strip_prefix " + strip]
        if site.get("auth") and authentication:
            result += ["\t\tbasic_auth {", "\t\t\t%s %s" % (site["auth"]["user"], site["auth"]["hash"]), "\t\t}"]
        result += ["\t\treverse_proxy " + target + " {", "\t\t\theader_up X-Real-IP {remote_host}",
                   "\t\t\ttransport http {", "\t\t\t\tdial_timeout 5s", "\t\t\t}", "\t\t}", "\t}"]
        return result

    for index, route in enumerate(sorted(site.get("routes", []), key=lambda r: -len(r["prefix"]))):
        path = route["prefix"]
        lines += ["\t@path%s path %s %s/*" % (index, path, path)]
        lines += handle(route["upstream"], "@path%s" % index,
                        path if route.get("strip", True) else None, route.get("auth", True))
    if site.get("mihomo"):
        lines += ["\t@control path /api/control /api/control/*"]
        lines += handle(site["upstream"], "@control", authentication=False)
    lines += handle(site["upstream"])
    # route 保留顺序：IP 拒绝必须发生在 handle/reverse_proxy 之前。
    return lines[0] + "\n\troute {\n" + "\n".join("\t" + line for line in lines[1:]) + "\n\t}\n}\n"


def split_config(text):
    if BEGIN not in text:
        if END in text:
            raise Error("托管标记损坏，请先检查 Caddyfile。")
        return text, ""
    if text.count(BEGIN) != 1 or text.count(END) != 1 or not text.endswith(END):
        raise Error("托管标记重复、损坏或位置不正确，未修改配置。")
    base, tail = text.split(BEGIN)
    return base, tail[:-len(END)]


def managed(sites):
    names = [s["domain"] for s in sites]
    if len(names) != len(set(names)):
        raise Error("站点域名重复。")
    return "\n".join(render_site(s) for s in sites)


def hosts(config):
    result = set()
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "host" and isinstance(value, list):
                    result.update(x.lower() for x in value if isinstance(x, str))
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(config)
    return result


class Manager:
    def __init__(self, root, settings=None):
        self.root = Path(root).resolve()
        self.settings = settings or read_json(self.root / ".edge-kit/settings.json")
        if self.settings.get("schema") != 1 or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", self.settings.get("project", "")):
            raise Error("管理配置无效。")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.settings.get("service", "")):
            raise Error("Caddy 服务名无效。")

    def command(self, *args):
        values = ["docker", "compose", "--project-directory", str(self.root), "-p", self.settings["project"],
                  "-f", str(self.root / "compose.yaml")]
        if (self.root / OVERLAY).is_file():
            values += ["-f", str(self.root / OVERLAY)]
        return values + list(args)

    def compose(self, *args, timeout=120):
        return run(self.command(*args), cwd=self.root, timeout=timeout)

    def container(self):
        ids = self.compose("ps", "-a", "-q", self.settings["service"]).split()
        if len(ids) != 1:
            raise Error("需要唯一的 Caddy 容器。尚未启动可运行 ./deploy.sh start。")
        obj = json.loads(run(["docker", "inspect", ids[0]]))[0]
        labels = obj["Config"].get("Labels", {})
        if labels.get("com.docker.compose.project") != self.settings["project"] or labels.get("com.docker.compose.service") != self.settings["service"]:
            raise Error("容器归属不符，拒绝操作。")
        files = labels.get("com.docker.compose.project.config_files", "").split(",")
        if any(Path(name).resolve() not in (self.root / "compose.yaml", self.root / OVERLAY) for name in files):
            raise Error("已有容器使用了其他 Compose 文件；请先审查完整配置，本工具不擅自省略。")
        mounts = [m for m in obj["Mounts"] if m["Destination"] == "/etc/caddy/Caddyfile"]
        if len(mounts) != 1 or mounts[0]["Type"] != "bind" or Path(mounts[0]["Source"]).resolve() != self.root / "Caddyfile":
            raise Error("只支持当前项目 Caddyfile 绑定到 /etc/caddy/Caddyfile 的官方布局，未修改容器。")
        return obj

    def exec(self, *args, data=None):
        container = self.container()
        if not container["State"]["Running"]:
            raise Error("Caddy 未运行，请先启动。")
        return run(["docker", "exec", "-i", "-w", "/etc/caddy", container["Id"]] + list(args), data=data)

    def adapt(self, text, validate=True):
        arguments = ["caddy", "adapt", "--config", "-", "--adapter", "caddyfile"]
        if validate:
            arguments += ["--validate"]
        return json.loads(self.exec(*arguments, data=text))

    def current(self):
        path = self.root / "Caddyfile"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise Error("Caddyfile 不存在、过大或是符号链接。")
        text = path.read_text(encoding="utf-8")
        if self.exec("cat", "/etc/caddy/Caddyfile") != text:
            raise Error("容器绑定了旧配置文件，请先审查并重建本项目 Caddy 容器；未自动重建。")
        return text

    def sites(self):
        path = self.root / CATALOG
        return read_json(path) if path.exists() else []

    def backup(self):
        parent = self.root.parent / (self.root.name + "-backups")
        if parent.is_symlink():
            raise Error("备份目录不能是符号链接。")
        parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
        directory = parent / ("edge-" + stamp())
        directory.mkdir(mode=0o700)
        hashes = {}
        for name in ("Caddyfile", "compose.yaml", OVERLAY, ".env", ".edge-kit/settings.json", CATALOG):
            source = self.root / name
            if source.is_symlink():
                raise Error("备份拒绝符号链接。")
            if source.is_file():
                content = source.read_bytes()
                private(directory / name, content)
                hashes[name] = hashlib.sha256(content).hexdigest()
        write_json(directory / "manifest.json", {"schema": 1, "files": hashes})
        print("配置备份：" + str(directory))
        return directory

    def apply(self, sites, base=None):
        old = self.current()
        original, block = split_config(old)
        if block != managed(self.sites()):
            raise Error("托管区域存在手动修改，未覆盖。请先审查差异。")
        original = original if base is None else base
        existing_hosts = hosts(self.adapt(original))
        for site in sites:
            if any(fnmatch.fnmatchcase(site["domain"], h) for h in existing_hosts):
                raise Error("域名已被原配置管理，未覆盖原站点：" + site["domain"])
        # 第一次接管时仅在原配置后添加换行和有边界的托管区域。
        if original and not original.endswith("\n"):
            original += "\n"
        candidate = original + BEGIN + managed(sites) + END
        self.adapt(candidate)
        snapshot = self.backup()
        previous = self.sites()
        try:
            inplace(self.root / "Caddyfile", candidate)
            self.current()
            self.exec("caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile")
            write_json(self.root / CATALOG, sites)
        except Exception:
            inplace(self.root / "Caddyfile", old)
            write_json(self.root / CATALOG, previous)
            try:
                self.exec("caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile")
            except Exception:
                raise Error("加载及回退加载均失败，原配置已写回。请检查入口状态；备份：" + str(snapshot))
            raise Error("配置加载失败，已恢复原配置及站点记录；备份：" + str(snapshot))
        print("配置已验证并平滑加载。HTTPS 证书申请异步进行，请用 doctor 和浏览器检查。")
        print("已有 WebSocket 连接可能随配置加载重连；后端应用、证书卷均未重建。")

    def hash_password(self, current_hash=None):
        while True:
            first = getpass.getpass("访问密码（留空%s）：" % ("保留现有密码" if current_hash else "生成随机密码"))
            if not first:
                if current_hash:
                    return current_hash
                first = secrets.token_urlsafe(18)
                print("生成的访问密码（仅显示这一次，请妥善保存）：" + first)
                break
            if "\0" in first or "\n" in first or "\r" in first or len(first.encode()) > 72:
                print("bcrypt 密码须为 1–72 UTF-8 字节，不能含换行或 NUL；不限制复杂度。")
            elif first == getpass.getpass("再次输入访问密码："):
                break
            else:
                print("两次输入不同，请重试。")
        hashed = self.exec("caddy", "hash-password", data=first + "\n").strip()
        return hashed

    def backend(self, default=None):
        print("后端示例：http://app-web:8000；使用容器内部端口，不能填写 Caddy 的 127.0.0.1。")
        return upstream(ask("后端地址", default))

    def add(self, editing=None):
        sites = self.sites()
        previous = next((s for s in sites if s["domain"] == editing), None)
        if editing and previous is None:
            raise Error("只能交互编辑本工具创建的站点；原有手写站点保持原样。")
        print("普通反代选 1；MetaCubeXD 面板与 API 同域分流选 2。WebSocket 自动支持。")
        preset = ask("站点类型（1=普通，2=MetaCubeXD）", "2" if previous and previous.get("mihomo") else "1")
        if preset not in ("1", "2"):
            raise Error("请选择 1 或 2。")
        site = copy.deepcopy(previous) if previous else {"enabled": True, "compress": True, "allow": [], "body_mb": 0}
        site["domain"] = domain(ask("网站域名", editing))
        site["mode"] = "https" if yes("启用自动 HTTPS（域名须解析且公网 80/443 可达）？", site.get("mode", "https") == "https") else "http"
        print("请先使用 networks / connect-network，让 Caddy 加入后端所在的可信 Docker 网络。")
        site["upstream"] = self.backend(site.get("upstream"))
        if site.get("mihomo", False) != (preset == "2"):
            site["routes"] = []
        site["mihomo"] = preset == "2"
        site.setdefault("routes", [])
        if site["mihomo"]:
            old_api = next((r["upstream"] for r in site["routes"] if r["prefix"] == "/mihomo"), None)
            panel_host = urllib.parse.urlsplit(site["upstream"]).hostname
            api = upstream(ask("Mihomo API 地址", old_api or "http://%s:9091" % ("[" + panel_host + "]" if ":" in panel_host else panel_host)))
            site["routes"] = [r for r in site["routes"] if r["prefix"] != "/mihomo"] + [{"prefix": "/mihomo", "upstream": api, "strip": True, "auth": False}]
            print("/mihomo/ 转发内核 API；/api/control/ 使用 CONTROL_TOKEN。两个 API 均依赖原有 Bearer 密钥，不叠加 Basic Auth。")
            print("面板后端地址填写 https://你的域名/mihomo（HTTP 模式改用 http）。")
        if yes("启用网页登录密码？", bool(site.get("auth"))):
            site["auth"] = {"user": ask("访问用户名", (site.get("auth") or {}).get("user", "admin")), "hash": self.hash_password((site.get("auth") or {}).get("hash"))}
            if not site["mihomo"]:
                print("Basic Auth 占用 Authorization；后端若也用 Bearer，请为 API 配置独立且免 Basic 的路径。")
        else:
            site.pop("auth", None)
        if yes("修改高级选项（路径分流、IP 白名单、压缩、上传限制）？"):
            allow = ask("允许的 IP/CIDR，逗号分隔，- 清空；回车保留", ",".join(site.get("allow", []))) or ""
            site["allow"] = ("" if allow == "-" else allow).replace(" ", "").split(",")
            site["allow"] = [x for x in site["allow"] if x]
            site["compress"] = yes("启用压缩？", site.get("compress", True))
            site["body_mb"] = int(ask("请求体上限 MiB，0 为不额外限制", str(site.get("body_mb", 0))))
            if site["routes"] and yes("重新配置已有路径分流？（保留 MetaCubeXD 必需的 /mihomo 路由）"):
                site["routes"] = [r for r in site["routes"] if site["mihomo"] and r["prefix"] == "/mihomo"]
            while yes("增加一个路径分流？"):
                path = prefix(ask("路径前缀，例如 /api"))
                site["routes"].append({"prefix": path, "upstream": self.backend(), "strip": yes("转发时去掉此前缀？", True),
                                       "auth": yes("此路径也要求网页登录密码？", True) if site.get("auth") else False})
        validate_site(site)
        if any(s["domain"] == site["domain"] and s["domain"] != editing for s in sites):
            raise Error("此域名已存在。")
        print("\n待应用配置（密码哈希已隐藏）：\n" + re.sub(r"\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}", "<密码哈希>", render_site(site)))
        if yes("确认备份并应用以上配置？", True):
            self.apply([s for s in sites if s["domain"] != editing] + [site])

    def toggle(self, action):
        sites = self.sites()
        selected = ask("站点域名（留空取消）")
        if not selected:
            return
        name = domain(selected)
        site = next((s for s in sites if s["domain"] == name), None)
        if site is None:
            raise Error("此站点不在托管列表中。")
        if not yes("确认%s %s？证书与后端数据均保留" % (action, name)):
            return
        if action == "remove":
            sites.remove(site)
        else:
            site["enabled"] = action == "enable"
        self.apply(sites)

    def list_sites(self):
        for site in self.sites():
            print("%s %s %s → %s" % ("启用" if site.get("enabled", True) else "停用", site["mode"], site["domain"], site["upstream"]))
        base, _ = split_config(self.current())
        print("原有配置管理的域名（不自动改写）：" + ", ".join(sorted(hosts(self.adapt(base, False)))))

    def networks(self):
        obj = self.container()
        print("Caddy 已接入：" + ", ".join(obj["NetworkSettings"]["Networks"]))
        print("已有 Docker 网络：\n" + run(["docker", "network", "ls", "--format", "{{.Name}}\t{{.Driver}}"]))

    def connect_network(self):
        self.networks()
        name = ask("要接入的已有网络名（留空取消）")
        if not name:
            return
        info = json.loads(run(["docker", "network", "inspect", name]))[0]
        if info["Name"] != name or info["Driver"] != "bridge" or name == "bridge":
            raise Error("请选择已有的用户自定义 bridge 网络。")
        obj = self.container()
        key = "edge_" + hashlib.sha256(name.encode()).hexdigest()[:12]
        overlay = read_json(self.root / OVERLAY) if (self.root / OVERLAY).exists() else {"services": {}, "networks": {}}
        service = overlay.setdefault("services", {}).setdefault(self.settings["service"], {})
        before = json.loads(self.compose("config", "--format", "json"))
        # 显式保留服务原先的所有网络；原 compose 文件及其他服务不改写。
        networks = dict(before["services"][self.settings["service"]].get("networks", {"default": {}}))
        networks[key] = {}
        service["networks"] = networks
        overlay.setdefault("networks", {})[key] = {"external": True, "name": name}
        print("仅将 Caddy 接入 %s，并写入 %s 持久化。不会修改或重启后端容器。" % (name, OVERLAY))
        print("此网络内的容器将能与 Caddy 直接通信；之后请统一使用本工具管理入口。")
        if not yes("确认接入？"):
            return
        self.backup()
        old = (self.root / OVERLAY).read_bytes() if (self.root / OVERLAY).exists() else None
        connected = False
        try:
            write_json(self.root / OVERLAY, overlay)
            after = json.loads(self.compose("config", "--format", "json"))
            attached = after["services"][self.settings["service"]]["networks"][key]
            defined = after["networks"][key]
            if attached not in (None, {}) or defined.get("name") != name or defined.get("external") is not True or any(k not in ("name", "external", "ipam") for k in defined) or defined.get("ipam"):
                raise Error("新增网络的实际 Compose 定义不符合预期。")
            expected = copy.deepcopy(before)
            expected["services"][self.settings["service"]]["networks"][key] = attached
            expected["networks"][key] = defined
            if after != expected:
                raise Error("Compose 出现预期以外的变化，已停止。")
            if name not in obj["NetworkSettings"]["Networks"]:
                run(["docker", "network", "connect", name, obj["Id"]])
                connected = True
        except Exception:
            if old is None:
                (self.root / OVERLAY).unlink(missing_ok=True)
            else:
                private(self.root / OVERLAY, old)
            if connected:
                run(["docker", "network", "disconnect", name, obj["Id"]])
            raise
        print("网络已接入并持久化，没有重建容器。")

    def doctor(self):
        self.compose("config", "--quiet")
        text = self.current()
        self.adapt(text)
        obj = self.container()
        base, block = split_config(text)
        if block != managed(self.sites()):
            raise Error("托管区域与站点记录不一致。")
        print("容器：%s；状态：%s；健康：%s" % (obj["Name"].lstrip("/"), obj["State"]["Status"], obj["State"].get("Health", {}).get("Status", "未定义")))
        print("Compose、数据挂载、容器归属、配置语法及站点记录检查通过。")
        for site in self.sites():
            if site.get("enabled", True):
                try:
                    addresses = sorted({x[4][0] for x in socket.getaddrinfo(site["domain"], None)})
                    print("DNS %s → %s" % (site["domain"], ", ".join(addresses)))
                except OSError:
                    print("待处理：域名无法解析：" + site["domain"])
        print("证书实际签发、公网可达、后端登录及业务功能仍需浏览器验证。IP 白名单使用直连客户端地址，置于 CDN 后需另行配置可信代理。")


def setup(root, adopt=False):
    root = Path(root).expanduser().resolve()
    if not root.is_dir() or root == root.parent or root in (Path("/opt"), Path("/etc"), Path("/"), Path.home()):
        raise Error("请选择已存在的独立项目目录，不要直接使用系统目录或主目录。")
    if (root / ".edge-kit/settings.json").exists():
        raise Error("此目录已初始化，请直接运行菜单或 status。")
    run(["docker", "version", "--format", "{{.Server.Version}}"])
    run(["docker", "compose", "version"])
    if adopt:
        if not (root / "compose.yaml").is_file() or not (root / "Caddyfile").is_file():
            raise Error("接管需要现有 compose.yaml 和 Caddyfile。")
        config = json.loads(run(["docker", "compose", "--project-directory", root, "-f", root / "compose.yaml", "config", "--format", "json"]))
        project, service = config["name"], ask("已有 Caddy 的 Compose 服务名", "caddy")
        settings = {"schema": 1, "project": project, "service": service, "deployment_id": secrets.token_hex(16)}
        manager = Manager(root, settings)
        manager.adapt(manager.current())
        if BEGIN in manager.current() or (root / OVERLAY).exists():
            raise Error("已有托管标记或同名覆盖配置，拒绝覆盖。")
        print("仅建立管理记录，原 Caddyfile、Compose、证书和容器保持原样。")
        if not yes("确认接管现有 Caddy？"):
            return
    else:
        if any((root / name).exists() for name in ("Caddyfile", "compose.yaml", OVERLAY, "data", "config")):
            raise Error("目录已有运行配置或数据，请选择 adopt 接管，不能按新安装覆盖。")
        project = ask("Compose 项目名", "edge-proxy")
        settings = {"schema": 1, "project": project, "service": "caddy", "deployment_id": secrets.token_hex(16)}
        manager = Manager(root, settings)
        if run(["docker", "ps", "-a", "--filter", "label=com.docker.compose.project=" + project, "-q"]).strip():
            raise Error("已有同名 Compose 项目，不能作为新安装重建。请接管原项目或换一个项目名。")
        for port in (80, 443):
            try:
                with socket.socket() as listener:
                    listener.bind(("0.0.0.0", port))
            except OSError:
                raise Error("宿主机端口 %s 已占用，不能安装另一个入口；现有 Caddy 可选择 adopt。" % port)
        print("将新建一个官方 Caddy 容器，发布 TCP 80/443，证书保存在项目 data/；不修改防火墙或 DNS。")
        if not yes("确认安装？", True):
            return
        for directory in ("data", "config"):
            (root / directory).mkdir(mode=0o700)
        private(root / "Caddyfile", "# 由 Edge Proxy Kit 管理；使用 ./deploy.sh 添加站点\n")
        write_json(root / "compose.yaml", {"name": project, "services": {"caddy": {
            "image": IMAGE, "restart": "unless-stopped", "ports": ["80:80/tcp", "443:443/tcp"],
            "volumes": ["./Caddyfile:/etc/caddy/Caddyfile:ro", "./data:/data", "./config:/config"],
            "healthcheck": {"test": ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:2019/config/"],
                            "interval": "10s", "timeout": "3s", "retries": 6}}}})
    with lock(root):
        write_json(root / ".edge-kit/settings.json", settings)
        write_json(root / CATALOG, [])
        manager.backup()
        if not adopt:
            manager.compose("config", "--quiet")
            manager.compose("pull", manager.settings["service"], timeout=900)
            manager.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "90", manager.settings["service"], timeout=150)
        manager.doctor()
    write_json(SOURCE / "settings.json", {"root": str(root)})
    print("管理已就绪，执行 ./deploy.sh add 添加网站，或直接执行 ./deploy.sh 打开菜单。")
    if not adopt and yes("现在新增第一个网站？", True):
        with lock(root):
            manager.add()


HELP = """Edge Proxy Kit — 轻量中文交互反向代理
用法：./deploy.sh [命令]；--root 指定已有入口项目目录
  （无命令）           中文菜单；首次选择新装或接管
  install / adopt      新建入口 / 接管已有 Caddy Compose
  add / edit           交互新增 / 修改托管站点
  list                 查看托管站点及原配置域名
  enable / disable     启用 / 停用站点
  remove               移除站点规则；保留证书及后端数据
  networks             列出现有 Docker 网络
  connect-network      交互接入已有网络并持久化
  status / doctor      状态 / 配置及 DNS 诊断
  logs [-f]            本机查看入口原始日志，分享前须检查敏感内容
  reload               验证并平滑加载现有配置
  start / stop / restart 仅操作本项目 Caddy
  update [--image 版本] 自动拉取工具代码并更新入口镜像
  update --image-only  跳过 Git，仅更新镜像
  backup               备份配置和站点记录（不包含证书/业务数据）
  restore              校验后恢复网站规则，当前状态先备份
  uninstall            备份并移除 Caddy 容器；保留文件、证书和网络
请只接入可信网络。DNS、公网防火墙、CDN 与业务后端由用户管理。"""


def restore(manager):
    selected = ask("配置备份目录完整路径（留空取消）")
    if not selected:
        return
    directory = Path(selected).expanduser().resolve()
    manifest = read_json(directory / "manifest.json")
    allowed = {"Caddyfile", "compose.yaml", OVERLAY, ".env", ".edge-kit/settings.json", CATALOG}
    if manifest.get("schema") != 1 or not {"Caddyfile", CATALOG}.issubset(manifest.get("files", {})):
        raise Error("备份清单不完整。")
    for name, expected in manifest["files"].items():
        if name not in allowed or (directory / name).is_symlink() or hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise Error("备份内容或 SHA256 校验失败，未覆盖。")
    old_settings = read_json(directory / ".edge-kit/settings.json")
    if old_settings != manager.settings:
        raise Error("备份不属于当前项目，拒绝恢复。")
    print("仅恢复网站规则和站点记录；不覆盖 Compose、网络、证书或业务数据。")
    if yes("确认先备份当前配置，再恢复？"):
        base, block = split_config((directory / "Caddyfile").read_text(encoding="utf-8"))
        sites = read_json(directory / CATALOG)
        if block != managed(sites):
            raise Error("备份的托管区域与记录不一致。")
        manager.apply(sites, base=base)


def sync_code():
    if not (SOURCE / ".git").exists():
        raise Error("自动代码更新需要在 Git 克隆目录运行。仅更新镜像可用 update --image-only。")
    def git(*args):
        return run(["git", "-c", "safe.directory=" + SOURCE.as_posix()] + list(args), cwd=SOURCE).strip()
    if git("status", "--porcelain", "--untracked-files=no"):
        raise Error("工具代码有本地修改，未覆盖。请先审查 Git 差异。")
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD")
    remote = git("for-each-ref", "--format=%(upstream:remotename)", "refs/heads/" + branch)
    if not remote or remote == "." or remote.startswith("-"):
        raise Error("当前分支需要配置远程 upstream。")
    git("fetch", "--quiet", "--", remote)
    before, after = git("rev-parse", "HEAD"), git("rev-parse", "@{upstream}^{commit}")
    if before == after:
        return False
    if git("merge-base", before, after) != before:
        raise Error("分支不能快进，未强制合并。")
    names = git("ls-tree", "-r", "--name-only", "-z", after).split("\0")
    if not {"edge.py", "deploy.sh", "README.md"}.issubset(names):
        raise Error("远程代码缺少必要文件。")
    for name in names:
        if name in ("Caddyfile", "compose.yaml", OVERLAY, "settings.json", ".env") or name.split("/")[0] in ("data", "config", ".edge-kit"):
            raise Error("远程版本包含私有运行配置，拒绝自动更新。")
    compile(git("show", after + ":edge.py"), "edge.py", "exec")
    git("merge", "--ff-only", "--no-overwrite-ignore", after)
    print("工具代码已快进更新；旧提交仍可通过 Git 历史查看。", flush=True)
    return True


def dispatch(manager, args):
    action = args.command
    if action == "add":
        manager.add()
    elif action == "edit":
        manager.list_sites()
        selected = ask("编辑的托管站点域名（留空取消）")
        if selected:
            manager.add(domain(selected))
    elif action in ("enable", "disable", "remove"):
        manager.toggle(action)
    elif action == "list":
        manager.list_sites()
    elif action == "networks":
        manager.networks()
    elif action == "connect-network":
        manager.connect_network()
    elif action == "doctor":
        manager.doctor()
    elif action == "status":
        print(manager.compose("ps", "-a"))
        print("配置目录：" + str(manager.root))
    elif action == "backup":
        manager.backup()
    elif action == "restore":
        restore(manager)
    elif action == "reload":
        manager.adapt(manager.current())
        manager.exec("caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile")
    elif action == "logs":
        print("日志可能包含域名和请求信息；请勿分享未经检查的原始日志。")
        values = manager.command("logs", "--tail", "100") + (["-f"] if args.follow else []) + [manager.settings["service"]]
        subprocess.run(values, check=True)
    elif action in ("start", "stop", "restart", "update", "uninstall"):
        service = manager.settings["service"]
        if action in ("stop", "restart", "uninstall"):
            manager.container()
        if action in ("stop", "restart", "uninstall") and not yes("%s 将中断本入口的所有站点，确认继续？" % action):
            return
        if action == "uninstall":
            manager.backup()
            manager.compose("stop", service)
            manager.compose("rm", "-f", service)
            print("仅移除入口容器，配置、证书、Docker 网络和业务应用均保留。")
            return
        if action == "stop":
            manager.compose("stop", service)
            return
        if action == "update":
            update_image(manager, args.image)
            return
        if action == "restart":
            manager.compose("restart", service)
        if action == "start" and manager.compose("ps", "-a", "-q", service).strip():
            manager.container()
        manager.compose("up", "-d", "--no-deps", "--no-build", "--pull", "missing" if action == "start" else "never", "--wait", "--wait-timeout", "90", service, timeout=150)
        manager.doctor()
    else:
        raise Error("未知命令，请执行 ./deploy.sh help。")


def update_image(manager, image=None):
    manager.adapt(manager.current())
    obj = manager.container()
    old_overlay = (manager.root / OVERLAY).read_bytes() if (manager.root / OVERLAY).exists() else None
    overlay = json.loads(old_overlay) if old_overlay else {"services": {}}
    service = manager.settings["service"]
    if image and not re.fullmatch(r"(?:docker.io/library/)?caddy:\d+\.\d+\.\d+-alpine(?:@sha256:[a-f0-9]{64})?", image):
        raise Error("请使用官方 Caddy 明确版本，例如 caddy:2.11.4-alpine，可附 sha256。")
    print("入口镜像更新可能短暂中断所有站点，其他业务容器不会重启。")
    if not yes("确认先备份配置再更新镜像？", True):
        return
    manager.backup()
    try:
        if image:
            overlay.setdefault("services", {}).setdefault(service, {})["image"] = image
            write_json(manager.root / OVERLAY, overlay)
        manager.compose("config", "--quiet")
        manager.compose("pull", service, timeout=900)
        manager.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "90", service, timeout=150)
        manager.doctor()
    except Exception:
        # 精确使用更新前本机镜像 ID，避免标签已被拉取覆盖导致回退仍运行新镜像。
        rollback = json.loads(old_overlay) if old_overlay else {"services": {}}
        rollback.setdefault("services", {}).setdefault(service, {})["image"] = obj["Image"]
        write_json(manager.root / OVERLAY, rollback)
        manager.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "90", service, timeout=150)
        raise Error("镜像更新失败，已按原镜像 ID 恢复；覆盖配置暂时固定原镜像，下次更新请显式指定 --image。")
    print("入口镜像及配置检查完成。证书、后端应用及业务数据保留。")


def main():
    parser = argparse.ArgumentParser(description="Edge Proxy Kit 中文交互反向代理")
    parser.add_argument("command", nargs="?", default=None)
    parser.add_argument("--root")
    parser.add_argument("--image")
    parser.add_argument("--image-only", action="store_true")
    parser.add_argument("-f", "--follow", action="store_true")
    args = parser.parse_args()
    if args.command in ("help",):
        print(HELP)
        return
    if sys.platform != "linux" or os.geteuid() != 0:
        raise Error("请在 Linux amd64/arm64 服务器使用 root 或 sudo 运行；help 不需要权限。")
    if args.command == "update" and not args.image_only:
        with lock(SOURCE):
            changed = sync_code()
        if changed:
            values = [sys.executable, str(SOURCE / "edge.py"), "update", "--image-only"]
            if args.root:
                values += ["--root", args.root]
            if args.image:
                values += ["--image", args.image]
            sys.stdout.flush()
            os.execv(sys.executable, values)
    pointer = read_json(SOURCE / "settings.json") if (SOURCE / "settings.json").exists() else {}
    root = Path(args.root or pointer.get("root", SOURCE)).resolve()
    if args.command in ("install", "adopt"):
        if args.command == "adopt" and not args.root:
            root = Path(ask("现有 Caddy Compose 目录", "/opt/edge-proxy" if Path("/opt/edge-proxy").is_dir() else str(root))).expanduser().resolve()
        setup(root, args.command == "adopt")
        return
    if not (root / ".edge-kit/settings.json").exists():
        if args.command == "update":
            print("工具代码检查完成；尚未部署，请运行 ./deploy.sh。")
            return
        if args.command is not None:
            raise Error("尚未部署或接管，请先运行 ./deploy.sh。")
        print("1. 新建独立 Caddy 入口\n2. 接管已有 Caddy Compose（保留现有配置和证书）")
        choice = ask("选择", "1")
        if choice not in ("1", "2"):
            raise Error("请选择 1 或 2。")
        root = Path(ask("项目目录", "/opt/edge-proxy" if choice == "2" and Path("/opt/edge-proxy").is_dir() else str(root))).expanduser().resolve()
        setup(root, choice == "2")
        return
    manager = Manager(root)
    if args.command is None:
        print("1 新增网站  2 修改网站  3 网站列表  4 停用网站  5 启用网站\n6 接入 Docker 网络  7 诊断  8 状态  9 备份  10 恢复\n11 移除网站  12 启动  13 停止  14 重启  15 更新  16 日志\n17 网络列表  18 平滑加载  19 卸载入口  20 帮助  0 退出")
        selected = ask("选择操作", "0")
        actions = {"1": "add", "2": "edit", "3": "list", "4": "disable", "5": "enable", "6": "connect-network", "7": "doctor", "8": "status", "9": "backup", "10": "restore", "11": "remove", "12": "start", "13": "stop", "14": "restart", "15": "update", "16": "logs", "17": "networks", "18": "reload", "19": "uninstall", "20": "help"}
        if selected == "0":
            return
        args.command = actions.get(selected)
        if args.command is None:
            raise Error("无效选择。")
        if args.command == "help":
            print(HELP)
            return
        if args.command == "update" and not args.image_only:
            sys.stdout.flush()
            os.execv(sys.executable, [sys.executable, str(SOURCE / "edge.py"), "update", "--root", str(root)])
    with lock(root):
        dispatch(manager, args)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n已取消；已有配置和数据保留。", file=sys.stderr)
        sys.exit(1)
    except Error as error:
        print("错误：" + str(error), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print("错误：操作失败（%s），请检查权限、配置和备份；未输出可能含敏感信息的原始异常。" % type(error).__name__, file=sys.stderr)
        sys.exit(1)
