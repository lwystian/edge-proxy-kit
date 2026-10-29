#!/usr/bin/env python3
"""纯逻辑和失败保护测试，不连接生产环境。"""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
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

    def test_old_application_record_remains_byte_compatible(self):
        value = site(mihomo=True, auth={"user": "admin", "hash": HASH},
                     routes=[{"prefix": "/mihomo", "upstream": "http://mihomo:9091", "strip": True, "auth": False}])
        text = edge.render_site(value)
        self.assertEqual(text.count("basic_auth"), 1)
        self.assertIn("@control path /api/control /api/control/*", text)
        ordinary = copy.deepcopy(value)
        ordinary.pop("mihomo")
        self.assertNotIn("@control", edge.render_site(ordinary))
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

    def test_backup_retains_one_snapshot_in_deployment_and_removes_known_legacy_backups(self):
        first = self.manager.backup()
        legacy = self.root.parent / (self.root.name + "-backups")
        legacy.mkdir()
        shutil.copytree(first, legacy / first.name)
        latest = self.manager.backup()
        self.assertEqual(latest.parent, self.root / "backups")
        self.assertFalse(first.exists())
        self.assertFalse(legacy.exists())
        self.assertEqual(list(latest.parent.iterdir()), [latest])

    def test_failed_backup_preserves_last_completed_snapshot(self):
        previous = self.manager.backup()
        before = (previous / "manifest.json").read_bytes()
        with mock.patch.object(edge, "private", side_effect=edge.Error("test disk failure")):
            with self.assertRaises(edge.Error):
                self.manager.backup()
        self.assertEqual((previous / "manifest.json").read_bytes(), before)
        self.assertEqual(list(previous.parent.iterdir()), [previous])

    def test_backup_rotation_preserves_unknown_files_and_foreign_project(self):
        first = self.manager.backup()
        owner = edge.read_json(first / ".edge-kit/settings.json")
        owner["project"] = "another-project"
        edge.write_json(first / ".edge-kit/settings.json", owner)
        edge.private(first.parent / "notes.txt", "keep")
        self.manager.backup()
        self.assertTrue(first.is_dir())
        self.assertEqual((first.parent / "notes.txt").read_text(), "keep")

    def test_corrupt_backup_is_rejected_before_apply(self):
        directory = self.manager.backup()
        (directory / "Caddyfile").write_text("corrupted", encoding="utf-8")
        with mock.patch.object(edge, "ask", return_value=str(directory)), mock.patch.object(self.manager, "apply") as apply:
            with self.assertRaisesRegex(edge.Error, "SHA256"):
                edge.restore(self.manager)
            apply.assert_not_called()

    def test_password_is_passed_by_stdin_and_not_command_arguments(self):
        with mock.patch.object(edge.getpass, "getpass", return_value="1") as password, mock.patch.object(self.manager, "exec", return_value=HASH) as execute:
            self.assertEqual(self.manager.hash_password(), HASH)
            execute.assert_called_once_with("caddy", "hash-password", data="1\n")
            password.assert_called_once()

    def test_blank_edit_password_keeps_hash_without_running_caddy(self):
        with mock.patch.object(edge.getpass, "getpass", return_value=""), mock.patch.object(self.manager, "exec") as execute:
            self.assertEqual(self.manager.hash_password(HASH), HASH)
            execute.assert_not_called()

    def backend_container(self, shared=False):
        return {"Id": "app-id", "Name": "/real-app-1", "State": {"Running": True},
                "Config": {"ExposedPorts": {"8080/tcp": {}, "9090/tcp": {}, "53/udp": {}}},
                "NetworkSettings": {"Ports": {"9090/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9090"}], "8080/tcp": None},
                                    "Networks": {"shared" if shared else "backend-net": {}}}}

    def test_port_candidates_prioritize_published_tcp_and_exclude_udp(self):
        self.assertEqual(edge.container_ports(self.backend_container()), [9090, 8080])

    def test_backend_selection_uses_real_container_name_and_stages_network_only(self):
        pending = set()
        with mock.patch.object(self.manager, "containers", return_value=[self.backend_container()]), \
             mock.patch.object(self.manager, "container", return_value={"Id": "proxy", "NetworkSettings": {"Networks": {"shared": {}}}}), \
             mock.patch.object(edge, "ask", side_effect=["d", "1", "9090"]), mock.patch.object(self.manager, "connect_network") as connect:
            self.assertEqual(self.manager.backend(pending=pending), "http://real-app-1:9090")
            connect.assert_not_called()
        self.assertEqual(pending, {"backend-net"})

    def test_backend_shared_network_does_not_schedule_extra_connections(self):
        pending = set()
        with mock.patch.object(self.manager, "containers", return_value=[self.backend_container(shared=True)]), \
             mock.patch.object(self.manager, "container", return_value={"NetworkSettings": {"Networks": {"shared": {}}}}), \
             mock.patch.object(edge, "ask", side_effect=["d", "1", "9090"]):
            self.manager.backend(pending=pending)
        self.assertEqual(pending, set())

    def test_manual_address_does_not_trigger_extra_confirmation_or_container_scan(self):
        with mock.patch.object(self.manager, "containers", side_effect=AssertionError("unnecessary scan")), mock.patch.object(edge, "ask", return_value="http://198.51.100.10:9090") as ask, mock.patch.object(edge, "yes", side_effect=AssertionError("unexpected confirmation")):
            self.assertEqual(self.manager.backend(), "http://198.51.100.10:9090")
        self.assertEqual(ask.call_count, 1)

    def test_simple_add_has_no_application_preset_or_extra_confirmation(self):
        before = (self.root / "Caddyfile").read_bytes()
        with mock.patch.object(edge, "ask", return_value="new.example.test") as ask, \
             mock.patch.object(edge, "yes", side_effect=AssertionError("unexpected confirmation")) as confirm, \
             mock.patch.object(self.manager, "backend", return_value="http://app:8000"), \
             mock.patch.object(self.manager, "apply_with_networks") as apply, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.manager.add()
            ask.assert_called_once_with("网站域名（默认 HTTPS）", None)
            confirm.assert_not_called()
            apply.assert_called_once()
        self.assertNotIn("MetaCubeXD", output.getvalue())
        self.assertNotIn("reverse_proxy", output.getvalue())
        self.assertEqual((self.root / "Caddyfile").read_bytes(), before)

    def test_compact_menu_keeps_common_commands_and_groups_advanced_actions(self):
        for answers, expected in ((["1"], "add"), (["2", "4"], "disable"), (["6", "2"], "containers"), (["5"], "update"), (["0"], None)):
            with mock.patch.object(edge, "ask", side_effect=answers), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(edge.menu(), expected)
                self.assertNotIn("MetaCubeXD", output.getvalue())

    def test_route_only_changes_route_and_does_not_ask_other_site_options(self):
        original = site(auth={"user": "admin", "hash": HASH}, allow=["192.0.2.0/24"], body_mb=8, compress=False)
        edge.write_json(self.root / edge.CATALOG, [original])
        with mock.patch.object(edge, "ask", return_value="/api") as ask, \
             mock.patch.object(edge, "yes", side_effect=[True, False, True]), \
             mock.patch.object(self.manager, "backend", return_value="https://api.example.test"), \
             mock.patch.object(self.manager, "hash_password") as password, \
             mock.patch.object(self.manager, "apply_with_networks") as apply:
            self.manager.route()
            ask.assert_called_once_with("路径前缀，例如 /api", None)
            password.assert_not_called()
        updated = apply.call_args[0][0][0]
        self.assertEqual(updated["routes"], [{"prefix": "/api", "upstream": "https://api.example.test", "strip": True, "auth": False}])
        updated["routes"] = []
        self.assertEqual(updated, original)

    def test_route_edit_preserves_other_routes_and_authentication(self):
        old = {"prefix": "/api", "upstream": "http://api:8000", "strip": True, "auth": False}
        other = {"prefix": "/other", "upstream": "http://other:8001", "strip": False, "auth": True}
        value = site(auth={"user": "admin", "hash": HASH}, routes=[old, copy.deepcopy(other)])
        with mock.patch.object(edge, "ask", side_effect=["2", "1", "/api"]), \
             mock.patch.object(edge, "yes", side_effect=[True, False]), \
             mock.patch.object(self.manager, "backend", return_value="http://new-api:9000"):
            self.assertTrue(self.manager.route_form(value, set()))
        self.assertEqual(value["routes"][0]["upstream"], "http://new-api:9000")
        self.assertFalse(value["routes"][0]["auth"])
        self.assertEqual(value["routes"][1], other)

    def test_edit_password_preserves_other_saved_options(self):
        value = site(auth={"user": "admin", "hash": HASH}, allow=["192.0.2.0/24"])
        original = copy.deepcopy(value)
        edge.write_json(self.root / edge.CATALOG, [value])
        with mock.patch.object(edge, "ask", side_effect=["4", "new-user"]), mock.patch.object(edge, "yes", return_value=True), mock.patch.object(self.manager, "hash_password", return_value=HASH), mock.patch.object(self.manager, "backend") as backend, mock.patch.object(self.manager, "apply_with_networks") as apply:
            self.manager.edit()
            backend.assert_not_called()
        original["auth"]["user"] = "new-user"
        self.assertEqual(apply.call_args[0][0], [original])

    def test_interrupting_route_input_does_not_apply_or_replace_saved_catalog(self):
        edge.write_json(self.root / edge.CATALOG, [site()])
        before = (self.root / edge.CATALOG).read_bytes()
        with mock.patch.object(edge, "ask", side_effect=KeyboardInterrupt), mock.patch.object(self.manager, "apply_with_networks") as apply:
            with self.assertRaises(KeyboardInterrupt):
                self.manager.route()
            apply.assert_not_called()
        self.assertEqual((self.root / edge.CATALOG).read_bytes(), before)

    def test_edit_only_changes_chosen_field_and_unchanged_edit_does_not_reload(self):
        edge.write_json(self.root / edge.CATALOG, [site()])
        with mock.patch.object(edge, "ask", return_value="1"), \
             mock.patch.object(self.manager, "backend", return_value=site()["upstream"]), \
             mock.patch.object(self.manager, "hash_password") as password, \
             mock.patch.object(self.manager, "apply_with_networks") as apply:
            self.manager.edit()
            password.assert_not_called()
            apply.assert_not_called()

    def test_backend_accepts_direct_remote_address_without_selecting_manual_mode(self):
        with mock.patch.object(self.manager, "containers", return_value=[]), mock.patch.object(edge, "ask", return_value="https://remote.example.test:8443") as ask, mock.patch.object(self.manager, "list_containers") as listing:
            self.assertEqual(self.manager.backend(), "https://remote.example.test:8443")
            listing.assert_not_called()
        self.assertEqual(ask.call_count, 1)

    def test_site_address_defaults_to_https_and_allows_explicit_http(self):
        self.assertEqual(edge.site_address("app.example.com"), ("app.example.com", "https"))
        self.assertEqual(edge.site_address("http://192.0.2.10"), ("192.0.2.10", "http"))
        self.assertEqual(edge.site_address("https://APP.EXAMPLE.COM/"), ("app.example.com", "https"))
        self.assertEqual(edge.site_address("new.example.com", "http"), ("new.example.com", "http"))
        for value in ("http://app.example.com:8080", "http://user:pass@app.example.com", "https://app.example.com/path", "ftp://app.example.com"):
            with self.subTest(value=value), self.assertRaises(edge.Error):
                edge.site_address(value)

    def test_new_site_contains_only_generic_fields_and_two_inputs(self):
        with mock.patch.object(edge, "ask", side_effect=["app.example.com", "198.51.100.10:8080"]) as ask, mock.patch.object(edge, "yes", side_effect=AssertionError("unexpected confirmation")) as confirm, mock.patch.object(self.manager, "containers", return_value=[]), mock.patch.object(self.manager, "apply_with_networks") as apply:
            self.manager.add()
        self.assertEqual(ask.call_count, 2)
        confirm.assert_not_called()
        self.assertEqual(apply.call_args[0], ([{"domain": "app.example.com", "mode": "https", "enabled": True,
                                              "upstream": "http://198.51.100.10:8080", "routes": []}], set()))

    def test_new_routes_keep_original_path_by_default_and_use_existing_site_auth(self):
        value = site(auth={"user": "admin", "hash": HASH})
        with mock.patch.object(edge, "ask", return_value="/api"), mock.patch.object(self.manager, "backend", return_value="http://api:8000"), mock.patch.object(edge, "yes", side_effect=lambda prompt, default=False: default) as confirm:
            self.manager.route_form(value, set())
        self.assertEqual(value["routes"], [{"prefix": "/api", "upstream": "http://api:8000", "strip": False, "auth": True}])
        self.assertEqual(confirm.call_args_list, [mock.call("转发时去掉此前缀？", False), mock.call("此路径也要求网页登录密码？", True)])

    def test_network_change_is_rolled_back_if_site_apply_fails(self):
        original = {"services": {"caddy": {"image": edge.IMAGE}}}
        edge.write_json(self.root / edge.OVERLAY, original)
        proxy = {"Id": "proxy", "NetworkSettings": {"Networks": {"shared": {}}}}
        joined = {"Id": "proxy", "NetworkSettings": {"Networks": {"shared": {}, "new-network": {}}}}
        with mock.patch.object(self.manager, "container", side_effect=[proxy, joined]), \
             mock.patch.object(self.manager, "connect_network"), \
             mock.patch.object(self.manager, "apply", side_effect=edge.Error("failed")), mock.patch.object(edge, "run") as execute:
            with self.assertRaises(edge.Error):
                self.manager.apply_with_networks([site()], {"new-network"})
            execute.assert_called_once_with(["docker", "network", "disconnect", "new-network", "proxy"])
        self.assertEqual(edge.read_json(self.root / edge.OVERLAY), original)

    def test_fresh_install_refuses_an_existing_compose_project(self):
        destination = self.root.parent / "fresh"
        destination.mkdir()
        with mock.patch.object(edge, "ask", return_value="edge-test"), mock.patch.object(edge, "run", return_value="existing-container"):
            with self.assertRaisesRegex(edge.Error, "同名 Compose"):
                edge.setup(destination)
        self.assertEqual(list(destination.iterdir()), [])

    def test_help_has_no_docker_or_privilege_requirements(self):
        with mock.patch.object(sys, "argv", ["deploy.sh", "help"]), mock.patch.object(edge, "run") as execute, contextlib.redirect_stdout(io.StringIO()) as output:
            edge.main()
            execute.assert_not_called()
        self.assertIn("connect-network", output.getvalue())

    def test_uninstall_only_removes_own_service_and_keeps_data(self):
        (self.root / "data").mkdir()
        edge.private(self.root / "data/cert-marker", "keep")
        with mock.patch.object(edge, "yes", side_effect=AssertionError("unexpected confirmation")), mock.patch.object(self.manager, "container"), mock.patch.object(self.manager, "compose") as compose:
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


@unittest.skipUnless(shutil.which("git"), "自动更新测试需要 Git")
class GitUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.seed, self.remote, self.root = base / "seed", base / "remote.git", base / "project"
        self.seed.mkdir()
        self.git(self.seed, "init", "-b", "main")
        self.git(self.seed, "config", "user.name", "Edge Test")
        self.git(self.seed, "config", "user.email", "test@example.invalid")
        for name in ("edge.py", "deploy.sh", "README.md", ".gitignore", ".gitattributes"):
            shutil.copy2(ROOT / name, self.seed / name)
        self.commit("initial")
        self.git(self.seed, "clone", "--bare", str(self.seed), str(self.remote))
        self.git(self.seed, "remote", "add", "origin", str(self.remote))
        self.git(self.seed, "push", "-u", "origin", "main")
        self.git(self.seed, "clone", "-b", "main", str(self.remote), str(self.root))

    def git(self, directory, *args):
        return edge.run(["git", "-c", "core.fsmonitor=false", "-c", "safe.directory=" + directory.as_posix()] + list(args), cwd=directory)

    def commit(self, message):
        self.git(self.seed, "add", "-A")
        self.git(self.seed, "commit", "-m", message)

    def publish(self):
        (self.seed / "README.md").write_text("new docs\n", encoding="utf-8")
        self.commit("remote change")
        self.git(self.seed, "push")

    def test_code_fast_forward_preserves_private_files(self):
        edge.private(self.root / "Caddyfile", "private site")
        edge.private(self.root / "data/cert-marker", "private cert")
        self.publish()
        with mock.patch.object(edge, "SOURCE", self.root):
            self.assertTrue(edge.sync_code())
        self.assertEqual((self.root / "README.md").read_text(), "new docs\n")
        self.assertEqual((self.root / "Caddyfile").read_text(), "private site")
        self.assertEqual((self.root / "data/cert-marker").read_text(), "private cert")

    def test_unchanged_code_is_not_reexecuted(self):
        with mock.patch.object(edge, "SOURCE", self.root):
            self.assertFalse(edge.sync_code())

    def test_dirty_code_is_not_overwritten(self):
        self.publish()
        (self.root / "README.md").write_text("my local changes", encoding="utf-8")
        with mock.patch.object(edge, "SOURCE", self.root), self.assertRaisesRegex(edge.Error, "本地修改"):
            edge.sync_code()
        self.assertEqual((self.root / "README.md").read_text(), "my local changes")

    def test_remote_private_path_is_rejected(self):
        (self.seed / "Caddyfile").write_text("bad runtime config", encoding="utf-8")
        self.git(self.seed, "add", "-f", "Caddyfile")
        self.git(self.seed, "commit", "-m", "unsafe runtime config")
        self.git(self.seed, "push")
        edge.private(self.root / "Caddyfile", "keep me")
        with mock.patch.object(edge, "SOURCE", self.root), self.assertRaisesRegex(edge.Error, "私有运行配置"):
            edge.sync_code()
        self.assertEqual((self.root / "Caddyfile").read_text(), "keep me")

    def test_ignored_file_collision_is_not_overwritten(self):
        (self.seed / "private.log").write_text("remote file", encoding="utf-8")
        self.git(self.seed, "add", "-f", "private.log")
        self.git(self.seed, "commit", "-m", "ignored filename collision")
        self.git(self.seed, "push")
        edge.private(self.root / "private.log", "private local file")
        with mock.patch.object(edge, "SOURCE", self.root), self.assertRaises(edge.Error):
            edge.sync_code()
        self.assertEqual((self.root / "private.log").read_text(), "private local file")


if __name__ == "__main__":
    unittest.main(verbosity=2)
