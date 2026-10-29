#!/usr/bin/env python3
"""纯逻辑和失败保护测试，不连接生产环境。"""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import edge

HASH = "$2a$14$" + "A" * 53


def site(**extra):
    value = {"domain": "panel.example.test", "mode": "https", "enabled": True,
             "upstream": "http://app_web:8000", "routes": [], "allow": [], "compress": True, "body_mb": 0}
    value.update(extra)
    return value


class EdgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "入口 项目"
        self.root.mkdir()
        edge.private(self.root / "Caddyfile", "# existing\nlegacy.test {\n respond old\n}\n")
        edge.write_json(self.root / "compose.yaml", {"name": "edge-test", "services": {"caddy": {"image": edge.IMAGE}}})
        self.settings = {"schema": 1, "project": "edge-test", "service": "caddy"}
        edge.write_json(self.root / ".edge-kit/settings.json", self.settings)
        edge.write_json(self.root / edge.CATALOG, [])
        self.manager = edge.Manager(self.root)

    def test_domain_idna_and_injection_rejection(self):
        self.assertEqual(edge.domain("EXAMPLE.COM."), "example.com")
        self.assertTrue(edge.domain("例子.中国").startswith("xn--"))
        for invalid in ("example.com:443", "https://example.com", "a.test { respond bad }", "a\nb", "*.test", "a..test"):
            with self.subTest(value=invalid), self.assertRaises(edge.Error):
                edge.domain(invalid)

    def test_upstream_preserves_protocol_and_accepts_docker_names_and_ipv6(self):
        for value in ("http://my_app:8000", "https://api.example.com", "h2c://grpc:8080", "http://[2001:db8::1]:8080"):
            self.assertEqual(edge.upstream(value), value)
        self.assertEqual(edge.upstream("app:8000"), "http://app:8000")

    def test_upstream_rejects_localhost_credentials_paths_and_injection(self):
        for value in ("http://127.0.0.1:9090", "localhost:9090", "http://[::1]:8080", "http://a:65536", "http://user:pass@app", "http://app/api", "app:80\nrespond bad", "app:80{bad}", "ftp://app"):
            with self.subTest(value=value), self.assertRaises(edge.Error):
                edge.upstream(value)

    def test_routes_auth_and_body_render_without_disabling_tls_verification(self):
        value = site(auth={"user": "admin", "hash": HASH}, body_mb=16, allow=["192.0.2.0/24", "2001:db8::/32"],
                     routes=[{"prefix": "/api", "upstream": "https://api.example.test", "strip": True, "auth": False}])
        text = edge.render_site(value)
        self.assertIn("max_size 16MiB", text)
        self.assertIn("@path0 path /api /api/*", text)
        self.assertIn("uri strip_prefix /api", text)
        self.assertIn("remote_ip 192.0.2.0/24 2001:db8::/32", text)
        self.assertEqual(text.count("basic_auth"), 1)
        self.assertNotIn("tls_insecure_skip_verify", text)
        self.assertNotIn("Authorization", text)

    def test_mihomo_bearer_routes_do_not_inherit_basic_auth(self):
        value = site(mihomo=True, auth={"user": "admin", "hash": HASH},
                     routes=[{"prefix": "/mihomo", "upstream": "http://mihomo:9091", "strip": True, "auth": False}])
        text = edge.render_site(value)
        self.assertEqual(text.count("basic_auth"), 1)
        self.assertIn("@control path /api/control /api/control/*", text)
        value["routes"].append({"prefix": "/api", "upstream": "app:8000"})
        with self.assertRaises(edge.Error):
            edge.render_site(value)

    def test_invalid_paths_whitelist_auth_and_body_rejected(self):
        for value in (site(allow=["all"]), site(mode="http", auth={"user": "admin", "hash": HASH}),
                      site(auth={"user": "admin", "hash": "plaintext"}), site(body_mb=-1),
                      site(routes=[{"prefix": "/../secret", "upstream": "app:80"}])):
            with self.assertRaises(edge.Error):
                edge.render_site(value)

    def test_no_duplicate_sites_or_routes(self):
        with self.assertRaises(edge.Error):
            edge.managed([site(), site()])
        with self.assertRaises(edge.Error):
            edge.render_site(site(routes=[{"prefix": "/api", "upstream": "app:80"}] * 2))

    def test_disabled_site_renders_no_rules_but_keeps_record(self):
        self.assertEqual(edge.render_site(site(enabled=False)), "")

    def test_marker_corruption_refuses_edit(self):
        for text in (edge.END, edge.BEGIN, edge.BEGIN + edge.END + "extra", edge.BEGIN * 2 + edge.END):
            with self.assertRaises(edge.Error):
                edge.split_config(text)

    def test_apply_preserves_base_and_file_inode(self):
        before = (self.root / "Caddyfile").read_text(encoding="utf-8")
        inode = (self.root / "Caddyfile").stat().st_ino
        with mock.patch.object(self.manager, "current", side_effect=lambda: (self.root / "Caddyfile").read_text(encoding="utf-8")), \
             mock.patch.object(self.manager, "adapt", return_value={}), mock.patch.object(self.manager, "exec"):
            self.manager.apply([site()])
        self.assertTrue((self.root / "Caddyfile").read_text(encoding="utf-8").startswith(before))
        self.assertEqual((self.root / "Caddyfile").stat().st_ino, inode)
        self.assertEqual(self.manager.sites()[0]["domain"], "panel.example.test")

    def test_reload_failure_restores_original_file_and_sites(self):
        before = (self.root / "Caddyfile").read_bytes()
        with mock.patch.object(self.manager, "current", side_effect=lambda: (self.root / "Caddyfile").read_text(encoding="utf-8")), \
             mock.patch.object(self.manager, "adapt", return_value={}), \
             mock.patch.object(self.manager, "exec", side_effect=[edge.Error("failed reload"), "ok"]):
            with self.assertRaisesRegex(edge.Error, "已恢复"):
                self.manager.apply([site()])
        self.assertEqual((self.root / "Caddyfile").read_bytes(), before)
        self.assertEqual(self.manager.sites(), [])

    def test_validation_failure_does_not_write_configuration(self):
        before = (self.root / "Caddyfile").read_bytes()
        with mock.patch.object(self.manager, "current", return_value=before.decode()), \
             mock.patch.object(self.manager, "adapt", side_effect=[{}, edge.Error("invalid")]), \
             mock.patch.object(self.manager, "backup") as backup:
            with self.assertRaises(edge.Error):
                self.manager.apply([site()])
            backup.assert_not_called()
        self.assertEqual((self.root / "Caddyfile").read_bytes(), before)

    def test_existing_unmanaged_domain_and_wildcard_are_protected(self):
        for existing in ("panel.example.test", "*.example.test"):
            with mock.patch.object(self.manager, "current", return_value="legacy.test {}\n"), \
                 mock.patch.object(self.manager, "adapt", return_value={"match": [{"host": [existing]}]}):
                with self.assertRaisesRegex(edge.Error, "原配置"):
                    self.manager.apply([site()])

    def test_hand_modified_managed_block_is_not_overwritten(self):
        text = edge.BEGIN + "manual change\n" + edge.END
        with mock.patch.object(self.manager, "current", return_value=text):
            with self.assertRaisesRegex(edge.Error, "手动修改"):
                self.manager.apply([site()])

    def test_backup_manifest_and_permissions(self):
        directory = self.manager.backup()
        value = edge.read_json(directory / "manifest.json")
        self.assertEqual(set(value["files"]), {"Caddyfile", "compose.yaml", ".edge-kit/settings.json", edge.CATALOG})
        if os.name == "posix":
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual((directory / "Caddyfile").stat().st_mode & 0o777, 0o600)

    def test_corrupt_backup_is_rejected_before_apply(self):
        directory = self.manager.backup()
        (directory / "Caddyfile").write_text("corrupted", encoding="utf-8")
        with mock.patch.object(edge, "ask", return_value=str(directory)), mock.patch.object(self.manager, "apply") as apply:
            with self.assertRaisesRegex(edge.Error, "SHA256"):
                edge.restore(self.manager)
            apply.assert_not_called()

    def test_password_is_passed_by_stdin_and_not_command_arguments(self):
        with mock.patch.object(edge.getpass, "getpass", side_effect=["1", "1"]), mock.patch.object(self.manager, "exec", return_value=HASH) as execute:
            self.assertEqual(self.manager.hash_password(), HASH)
            execute.assert_called_once_with("caddy", "hash-password", data="1\n")

    def test_help_has_no_docker_or_privilege_requirements(self):
        with mock.patch.object(sys, "argv", ["deploy.sh", "help"]), mock.patch.object(edge, "run") as execute, contextlib.redirect_stdout(io.StringIO()) as output:
            edge.main()
            execute.assert_not_called()
        self.assertIn("connect-network", output.getvalue())

    def test_uninstall_only_removes_own_service_and_keeps_data(self):
        (self.root / "data").mkdir()
        edge.private(self.root / "data/cert-marker", "keep")
        with mock.patch.object(edge, "yes", return_value=True), mock.patch.object(self.manager, "container"), mock.patch.object(self.manager, "compose") as compose:
            edge.dispatch(self.manager, type("Args", (), {"command": "uninstall"})())
        self.assertEqual(compose.call_args_list, [mock.call("stop", "caddy"), mock.call("rm", "-f", "caddy")])
        self.assertEqual((self.root / "data/cert-marker").read_text(), "keep")

    def test_image_update_failure_uses_previous_image_id(self):
        with mock.patch.object(edge, "yes", return_value=True), mock.patch.object(self.manager, "current", return_value=""), \
             mock.patch.object(self.manager, "adapt"), mock.patch.object(self.manager, "container", return_value={"Image": "sha256:" + "a" * 64}), \
             mock.patch.object(self.manager, "compose", side_effect=["", edge.Error("pull failed"), ""]):
            with self.assertRaisesRegex(edge.Error, "原镜像 ID"):
                edge.update_image(self.manager)
        self.assertEqual(edge.read_json(self.root / edge.OVERLAY)["services"]["caddy"]["image"], "sha256:" + "a" * 64)

    @unittest.skipUnless(os.name == "posix", "Linux CI 验证脚本执行")
    def test_shell_entry_forwards_arguments_from_path_with_spaces(self):
        entry = self.root / "deploy.sh"
        edge.private(entry, (ROOT / "deploy.sh").read_bytes())
        os.chmod(entry, 0o700)
        edge.private(self.root / "edge.py", "import sys; print(repr(sys.argv[1:]))\n")
        result = subprocess.run([str(entry), "help"], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "['help']")


if __name__ == "__main__":
    unittest.main(verbosity=2)
