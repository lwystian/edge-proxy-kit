#!/usr/bin/env python3
"""显式 --run 才创建临时 Docker 项目；不连接生产服务器。"""
import base64
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import time
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import edge


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    if "--run" not in sys.argv:
        print("SKIP：真实容器测试需显式指定 --run。")
        return
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("请在独立 Linux 测试环境以 root 运行。")
    with tempfile.TemporaryDirectory(prefix="edge-kit-ci-") as directory:
        root = Path(directory) / "deployment"
        source = Path(directory) / "tool"
        root.mkdir()
        source.mkdir()
        project = "edge-ci-" + uuid.uuid4().hex[:10]
        network = project + "-backend"
        app = project + "-app"
        fresh = Path(directory) / "fresh"
        fresh.mkdir()
        with mock.patch.object(edge, "SOURCE", source), mock.patch.object(edge, "ask", return_value=project + "-fresh"), mock.patch.object(edge, "yes", side_effect=[True, False]):
            try:
                edge.setup(fresh)
                new_manager = edge.Manager(fresh)
                assert new_manager.container()["State"]["Running"]
                assert (fresh / "data").is_dir() and (fresh / "config").is_dir()
                new_manager.compose("down")
                print("PASS 全新入口实际安装、标准 80/443 发布、数据目录及健康检查")
            finally:
                edge.run(["docker", "compose", "--project-directory", fresh, "-p", project + "-fresh", "-f", fresh / "compose.yaml", "down"])
        http_port, https_port = free_port(), free_port()
        fixture = '''from http.server import BaseHTTPRequestHandler,HTTPServer
import json,base64,hashlib
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.headers.get('Upgrade','').lower()=='websocket':
   key=base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key']+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
   self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',key);self.end_headers();return
  body=json.dumps({'path':self.path,'auth':self.headers.get('Authorization')}).encode()
  self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*args): pass
HTTPServer(('0.0.0.0',8000),Handler).serve_forever()
'''
        edge.private(root / "fixture.py", fixture)
        original = '{\n local_certs\n}\nlegacy.test {\n respond "legacy-ok"\n}\n'
        edge.private(root / "Caddyfile", original)
        edge.write_json(root / "compose.yaml", {"name": project, "services": {"caddy": {
            "image": edge.IMAGE, "ports": ["127.0.0.1:%s:80" % http_port, "127.0.0.1:%s:443" % https_port],
            "volumes": ["./Caddyfile:/etc/caddy/Caddyfile:ro", "./data:/data", "./config:/config"],
            "healthcheck": {"test": ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:2019/config/"], "interval": "2s", "timeout": "2s", "retries": 20}}}})
        dc = ["docker", "compose", "--project-directory", str(root), "-f", str(root / "compose.yaml")]
        edge.run(["docker", "network", "create", network])
        try:
            edge.run(["docker", "run", "-d", "--name", app, "--network", network, "--mount", "type=bind,src=%s,dst=/fixture.py,readonly" % (root / "fixture.py"), "python:3.12-alpine", "python", "/fixture.py"], timeout=600)
            app_before = json.loads(edge.run(["docker", "inspect", app]))[0]
            edge.run(dc + ["up", "-d", "--wait", "--wait-timeout", "90"], timeout=600)
            with mock.patch.object(edge, "SOURCE", source), mock.patch.object(edge, "ask", return_value="caddy"), mock.patch.object(edge, "yes", return_value=True):
                edge.setup(root, adopt=True)
            manager = edge.Manager(root)
            original_container = manager.container()["Id"]
            assert (root / "Caddyfile").read_text() == original
            with mock.patch.object(edge, "ask", return_value=network), mock.patch.object(edge, "yes", return_value=True):
                manager.connect_network()
            assert manager.container()["Id"] == original_container
            assert network in manager.container()["NetworkSettings"]["Networks"]
            print("PASS 无改动接管，Caddy 网络持久化；未重建入口及后端容器")
            password = "1"
            hashed = manager.exec("caddy", "hash-password", data=password + "\n").strip()
            value = {"domain": "panel.test", "mode": "https", "upstream": "http://%s:8000" % app,
                     "enabled": True, "compress": True, "allow": [], "body_mb": 4, "mihomo": True,
                     "auth": {"user": "admin", "hash": hashed},
                     "routes": [{"prefix": "/mihomo", "upstream": "http://%s:8000" % app, "strip": True, "auth": False}]}
            manager.apply([value])
            assert (root / "Caddyfile").read_text().startswith(original)
            for _ in range(50):
                if (root / "data/caddy/pki/authorities/local/root.crt").is_file():
                    break
                time.sleep(.2)
            context = ssl.create_default_context(cafile=str(root / "data/caddy/pki/authorities/local/root.crt"))
            def request(host, path="/", authorization=None, websocket=False):
                for attempt in range(30):
                    try:
                        connection = http.client.HTTPConnection("127.0.0.1", https_port, timeout=8)
                        connection.sock = context.wrap_socket(socket.create_connection(("127.0.0.1", https_port), timeout=8), server_hostname=host)
                        headers = {"Host": host}
                        if authorization:
                            headers["Authorization"] = authorization
                        if websocket:
                            headers.update({"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": base64.b64encode(b"0123456789abcdef").decode()})
                        connection.request("GET", path, headers=headers)
                        response = connection.getresponse()
                        result = response.status, response.read() if response.status != 101 else b""
                        connection.close()
                        return result
                    except (ssl.SSLError, OSError):
                        if attempt == 29:
                            raise
                        time.sleep(.2)
            auth = "Basic " + base64.b64encode(b"admin:1").decode()
            assert request("legacy.test")[1] == b"legacy-ok"
            assert request("panel.test")[0] == 401
            assert request("panel.test", "/config.js")[0] == 401
            assert request("panel.test", authorization=auth)[0] == 200
            assert request("panel.test", authorization="Basic " + base64.b64encode(b"admin:wrong").decode())[0] == 401
            code, body = request("panel.test", "/mihomo/version", "Bearer API-TEST")
            assert code == 200 and json.loads(body) == {"path": "/version", "auth": "Bearer API-TEST"}
            code, body = request("panel.test", "/api/control/profiles", "Bearer CONTROL-TEST")
            assert code == 200 and json.loads(body)["auth"] == "Bearer CONTROL-TEST"
            assert request("panel.test", "/mihomo/traffic", "Bearer API-TEST", websocket=True)[0] == 101
            print("PASS 真实 HTTPS（临时内部 CA）、Basic 正误密码、面板资源保护、路径分流、Bearer 转发及 WebSocket 101")
            restricted = copy.deepcopy(value)
            restricted["allow"] = ["192.0.2.0/24"]
            manager.apply([restricted])
            assert request("panel.test", authorization=auth)[0] == 403
            assert request("panel.test", "/mihomo/version", "Bearer API-TEST")[0] == 403
            manager.apply([value])
            print("PASS IP 白名单在面板及 API 上真实生效")
            old = (root / "Caddyfile").read_bytes()
            execute = manager.exec
            failed = [False]
            def fail_once(*args, **kwargs):
                if args[:2] == ("caddy", "reload") and not failed[0]:
                    failed[0] = True
                    raise edge.Error("test load failure")
                return execute(*args, **kwargs)
            changed = copy.deepcopy(value)
            changed["body_mb"] = 8
            with mock.patch.object(manager, "exec", side_effect=fail_once):
                try:
                    manager.apply([changed])
                    raise AssertionError("failure was ignored")
                except edge.Error:
                    pass
            assert (root / "Caddyfile").read_bytes() == old
            assert request("legacy.test")[1] == b"legacy-ok"
            snapshot = manager.backup()
            manager.apply([changed])
            with mock.patch.object(edge, "ask", return_value=str(snapshot)), mock.patch.object(edge, "yes", return_value=True):
                edge.restore(manager)
            assert manager.sites()[0]["body_mb"] == 4
            print("PASS 配置备份及恢复、加载失败自动回退、原有网站保持可用")
            ca_before = (root / "data/caddy/pki/authorities/local/root.crt").read_bytes()
            with mock.patch.object(edge, "yes", return_value=True):
                edge.update_image(manager)
            assert network in manager.container()["NetworkSettings"]["Networks"]
            assert request("legacy.test")[1] == b"legacy-ok"
            assert (root / "data/caddy/pki/authorities/local/root.crt").read_bytes() == ca_before
            assert json.loads(edge.run(["docker", "inspect", app]))[0]["State"]["StartedAt"] == app_before["State"]["StartedAt"]
            print("PASS 镜像更新及网络持久化，原证书保持、后端容器未重启")
            with mock.patch.object(edge, "yes", return_value=True):
                edge.dispatch(manager, type("Args", (), {"command": "uninstall"})())
            assert (root / "data/caddy/pki/authorities/local/root.crt").read_bytes() == ca_before
            assert (root / "Caddyfile").exists()
            assert json.loads(edge.run(["docker", "inspect", app]))[0]["State"]["Running"]
            print("PASS 卸载仅移除入口容器，文件、证书及后端容器保留")
        finally:
            edge.run(dc + ["down"], timeout=90)
            edge.run(["docker", "rm", "-f", app])
            edge.run(["docker", "network", "rm", network])


if __name__ == "__main__":
    main()
