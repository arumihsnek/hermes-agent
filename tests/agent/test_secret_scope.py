"""Tests for the profile-scoped credential primitive (Workstream A / Phase 2)."""
import logging
import os
import subprocess
import sys

import pytest

from agent import secret_scope as ss


@pytest.fixture(autouse=True)
def _reset_multiplex():
    """Ensure each test starts and ends with multiplexing off (it's a global)."""
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


class TestMultiplexInactiveBackwardCompat:
    """Default deployment: get_secret transparently reads os.environ."""

    def test_reads_environ(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert ss.get_secret("ANTHROPIC_API_KEY") == "sk-test"

    def test_missing_returns_default(self, monkeypatch):
        monkeypatch.delenv("NOPE_KEY", raising=False)
        assert ss.get_secret("NOPE_KEY") is None
        assert ss.get_secret("NOPE_KEY", "fallback") == "fallback"

    def test_no_raise_without_scope(self, monkeypatch):
        monkeypatch.delenv("SOME_KEY", raising=False)
        # multiplex off => unscoped read is fine, returns default
        assert ss.get_secret("SOME_KEY") is None


class TestMultiplexActiveFailClosed:
    """Multiplex on: an unscoped secret read raises instead of leaking."""

    def test_unscoped_read_raises(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leaky")
        ss.set_multiplex_active(True)
        with pytest.raises(ss.UnscopedSecretError):
            ss.get_secret("ANTHROPIC_API_KEY")


    def test_scoped_missing_key_returns_default_not_environ(self, monkeypatch):
        # Even though the value exists in os.environ, a scope is authoritative:
        # an absent scope key must NOT fall through to the (cross-profile) env.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-other-profile")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"ANTHROPIC_API_KEY": "sk-mine"})
        try:
            assert ss.get_secret("OPENAI_API_KEY") is None
            assert ss.get_secret("OPENAI_API_KEY", "d") == "d"
        finally:
            ss.reset_secret_scope(token)




class TestScopedSingleProfile:
    """Multiplex OFF with a scope installed: the scope is an overlay, not a
    blindfold. The cron scheduler installs a ``<home>/.env`` scope around every
    job unconditionally, and single-profile deployments legitimately supply
    credentials via the process environment only (systemd ``Environment=``,
    ``pass-cli run`` / ``op run`` wrappers) — those must keep resolving."""

    def test_scope_hit_wins_over_environ(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-environ")
        token = ss.set_secret_scope({"ANTHROPIC_API_KEY": "sk-from-env-file"})
        try:
            assert ss.get_secret("ANTHROPIC_API_KEY") == "sk-from-env-file"
        finally:
            ss.reset_secret_scope(token)


    def test_scope_miss_absent_everywhere_returns_default(self, monkeypatch):
        monkeypatch.delenv("NOPE_KEY", raising=False)
        token = ss.set_secret_scope({})
        try:
            assert ss.get_secret("NOPE_KEY") is None
            assert ss.get_secret("NOPE_KEY", "d") == "d"
        finally:
            ss.reset_secret_scope(token)

    def test_multiplex_on_still_authoritative(self, monkeypatch):
        # The fallthrough is strictly multiplex-off behavior: turning
        # multiplexing on must restore scope-authoritative semantics.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-other-profile")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({})
        try:
            assert ss.get_secret("OPENAI_API_KEY") is None
        finally:
            ss.reset_secret_scope(token)


class TestScopeIsolation:
    """Two scopes never see each other's secrets."""

    def test_nested_scopes_restore(self):
        ss.set_multiplex_active(True)
        t1 = ss.set_secret_scope({"K": "a"})
        try:
            assert ss.get_secret("K") == "a"
            t2 = ss.set_secret_scope({"K": "b"})
            try:
                assert ss.get_secret("K") == "b"
            finally:
                ss.reset_secret_scope(t2)
            assert ss.get_secret("K") == "a"
        finally:
            ss.reset_secret_scope(t1)


class TestEnvFileParsing:
    """load_env_file parses without mutating os.environ."""




    def test_build_profile_secret_scope(self, tmp_path):
        (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-profile\n")
        assert ss.build_profile_secret_scope(tmp_path) == {
            "ANTHROPIC_API_KEY": "sk-profile"
        }

    def test_build_profile_secret_scope_includes_home_external_secrets(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / ".env").write_text("XIAOMI_API_KEY=placeholder\n")
        from hermes_cli import env_loader

        home_key = str(tmp_path.resolve())
        monkeypatch.setitem(
            env_loader._SECRET_SOURCE_VALUES_BY_HOME,
            home_key,
            {"XIAOMI_API_KEY": "sk-from-bitwarden"},
        )

        assert ss.build_profile_secret_scope(tmp_path) == {
            "XIAOMI_API_KEY": "sk-from-bitwarden"
        }

    def test_build_profile_secret_scope_ignores_other_home_external_secrets(
        self, tmp_path, monkeypatch
    ):
        profile = tmp_path / "profile"
        other = tmp_path / "other"
        profile.mkdir()
        other.mkdir()
        from hermes_cli import env_loader

        monkeypatch.setitem(
            env_loader._SECRET_SOURCE_VALUES_BY_HOME,
            str(other.resolve()),
            {"XIAOMI_API_KEY": "sk-other-profile"},
        )

        assert ss.build_profile_secret_scope(profile) == {}

    def test_build_profile_secret_scope_includes_managed_environment(
        self, tmp_path, monkeypatch
    ):
        """Multiplexed profiles inherit the central managed provider env."""
        root = tmp_path / "root"
        profile = root / "profiles" / "worker"
        managed = tmp_path / "managed"
        profile.mkdir(parents=True)
        managed.mkdir()
        (managed / ".env").write_text(
            "OPENCODE_ZEN_API_KEY=central-zen\n"
            "OPENCODE_GO_API_KEY=central-go\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        monkeypatch.setenv("HERMES_MANAGED_PROFILES", "worker")
        monkeypatch.setenv("HERMES_HOME", str(root))
        from hermes_cli import managed_scope
        from hermes_cli import env_loader

        managed_scope.invalidate_managed_cache()
        env_loader.reset_secret_source_cache()
        env_loader.load_hermes_dotenv(hermes_home=profile)

        assert ss.build_profile_secret_scope(profile) == {
            "OPENCODE_ZEN_API_KEY": "central-zen",
            "OPENCODE_GO_API_KEY": "central-go",
        }

    def test_managed_scope_uses_profile_path_when_spawn_sets_profile_home(
        self, tmp_path, monkeypatch
    ):
        """The official Kanban spawn path sets HERMES_HOME to the profile dir."""
        root = tmp_path / "root"
        profile = root / "profiles" / "worker"
        managed = tmp_path / "managed"
        profile.mkdir(parents=True)
        managed.mkdir()
        (managed / ".env").write_text(
            "OPENCODE_ZEN_API_KEY=central-zen\n", encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(profile))
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        monkeypatch.setenv("HERMES_MANAGED_PROFILES", "worker")
        from hermes_cli import managed_scope

        managed_scope.invalidate_managed_cache()

        assert ss.build_profile_secret_scope(profile) == {
            "OPENCODE_ZEN_API_KEY": "central-zen"
        }

    def test_managed_scope_requires_authorized_profile_and_provider_name(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "root"
        profile = root / "profiles" / "worker"
        managed = tmp_path / "managed"
        managed.mkdir()
        profile.mkdir(parents=True)
        (managed / ".env").write_text(
            "OPENCODE_ZEN_API_KEY=central-zen\nUNRELATED_SECRET=must-not-enter\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        monkeypatch.setenv("HERMES_MANAGED_PROFILES", "worker")
        monkeypatch.setenv("HERMES_HOME", str(root))
        from hermes_cli import managed_scope

        managed_scope.invalidate_managed_cache()

        assert ss.build_profile_secret_scope(profile) == {
            "OPENCODE_ZEN_API_KEY": "central-zen"
        }
        reviewer = root / "profiles" / "reviewer"
        assert ss.build_profile_secret_scope(reviewer) == {}

    def test_managed_scope_precedence_is_deterministic(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        profile = root / "profiles" / "worker"
        managed = tmp_path / "managed"
        profile.mkdir(parents=True)
        managed.mkdir()
        (profile / ".env").write_text(
            "OPENCODE_ZEN_API_KEY=profile\n", encoding="utf-8"
        )
        (managed / ".env").write_text(
            "OPENCODE_ZEN_API_KEY=managed\n", encoding="utf-8"
        )
        monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "process")
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        monkeypatch.setenv("HERMES_MANAGED_PROFILES", "worker")
        monkeypatch.setenv("HERMES_HOME", str(root))
        from hermes_cli import env_loader, managed_scope

        managed_scope.invalidate_managed_cache()
        env_loader.reset_secret_source_cache()
        env_loader.load_hermes_dotenv(hermes_home=profile)

        scope = ss.build_profile_secret_scope(profile)
        assert scope["OPENCODE_ZEN_API_KEY"] == "managed"

    def test_managed_scope_does_not_copy_or_log_secret_values(
        self, tmp_path, monkeypatch, caplog
    ):
        root = tmp_path / "root"
        profile = root / "profiles" / "worker"
        managed = tmp_path / "managed"
        managed.mkdir()
        profile.mkdir(parents=True)
        (managed / ".env").write_text(
            "OPENCODE_ZEN_API_KEY=central-zen\n", encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        monkeypatch.setenv("HERMES_MANAGED_PROFILES", "worker")
        monkeypatch.setenv("HERMES_HOME", str(root))
        from hermes_cli import managed_scope

        managed_scope.invalidate_managed_cache()
        ss.build_profile_secret_scope(profile)

        assert not (profile / ".env").exists()
        assert "central-zen" not in caplog.text

    def test_invalid_managed_dir_fails_closed(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        profile = root / "profiles" / "worker"
        managed = tmp_path / "missing-managed"
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        monkeypatch.setenv("HERMES_MANAGED_PROFILES", "worker")
        monkeypatch.setenv("HERMES_HOME", str(root))
        from hermes_cli import managed_scope

        managed_scope.invalidate_managed_cache()

        assert ss.build_profile_secret_scope(profile) == {}

    def test_managed_provider_secret_is_removed_from_unrelated_child_env(
        self, monkeypatch
    ):
        from tools.environments.local import build_subprocess_env

        monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "synthetic-provider-value")

        assert "OPENCODE_ZEN_API_KEY" not in build_subprocess_env()

    def test_managed_cache_refreshes_and_invalidates(self, tmp_path, monkeypatch):
        from hermes_cli import managed_scope

        managed = tmp_path / "managed"
        managed.mkdir()
        env_path = managed / ".env"
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        managed_scope.invalidate_managed_cache()

        env_path.write_text("OPENCODE_ZEN_API_KEY=first\n", encoding="utf-8")
        assert managed_scope.load_managed_env()["OPENCODE_ZEN_API_KEY"] == "first"

        env_path.write_text("OPENCODE_ZEN_API_KEY=second\n", encoding="utf-8")
        stat = env_path.stat()
        os.utime(env_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        assert managed_scope.load_managed_env()["OPENCODE_ZEN_API_KEY"] == "second"

        env_path.write_text("OPENCODE_ZEN_API_KEY=third\n", encoding="utf-8")
        managed_scope.invalidate_managed_cache()
        assert managed_scope.load_managed_env()["OPENCODE_ZEN_API_KEY"] == "third"

    def test_managed_parse_exception_does_not_log_exception_value(
        self, tmp_path, monkeypatch, caplog
    ):
        from hermes_cli import managed_scope

        managed = tmp_path / "managed"
        managed.mkdir()
        (managed / ".env").write_text("synthetic\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        managed_scope.invalidate_managed_cache()

        def raise_with_value(_file):
            raise RuntimeError("synthetic-provider-value")

        monkeypatch.setattr(managed_scope, "_parse_env", raise_with_value)
        with caplog.at_level(logging.WARNING):
            assert managed_scope.load_managed_env() == {}
        assert "synthetic-provider-value" not in caplog.text

    def test_scope_loader_exception_does_not_disclose_value(
        self, tmp_path, monkeypatch, caplog
    ):
        from hermes_cli import managed_scope

        root = tmp_path / "root"
        profile = root / "profiles" / "worker"
        profile.mkdir(parents=True)
        monkeypatch.setenv("HERMES_MANAGED_PROFILES", "worker")
        monkeypatch.setenv("HERMES_HOME", str(root))

        def raise_with_value():
            raise RuntimeError("synthetic-provider-value")

        monkeypatch.setattr(managed_scope, "load_managed_env", raise_with_value)
        with caplog.at_level(logging.WARNING):
            assert ss.build_profile_secret_scope(profile) == {}
        assert "synthetic-provider-value" not in caplog.text

    def test_isolated_profile_scope_and_child_sanitization_end_to_end(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "root"
        profile = root / "profiles" / "worker"
        managed = tmp_path / "managed"
        profile.mkdir(parents=True)
        managed.mkdir()
        (managed / ".env").write_text(
            "OPENCODE_ZEN_API_KEY=synthetic-provider-value\n", encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        monkeypatch.setenv("HERMES_MANAGED_PROFILES", "worker")
        monkeypatch.setenv("HERMES_HOME", str(root))
        from hermes_cli import env_loader, managed_scope
        from tools.environments.local import build_subprocess_env

        managed_scope.invalidate_managed_cache()
        env_loader.reset_secret_source_cache()
        env_loader.load_hermes_dotenv(hermes_home=profile)
        scope = ss.build_profile_secret_scope(profile)
        assert scope["OPENCODE_ZEN_API_KEY"] == "synthetic-provider-value"

        token = ss.set_secret_scope(scope)
        ss.set_multiplex_active(True)
        try:
            assert ss.get_secret("OPENCODE_ZEN_API_KEY") == "synthetic-provider-value"
            child_env = build_subprocess_env()
            assert "OPENCODE_ZEN_API_KEY" not in child_env
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import os; print('present' if os.getenv('OPENCODE_ZEN_API_KEY') else 'absent')",
                ],
                env=child_env,
                capture_output=True,
                text=True,
                check=True,
            )
            assert result.stdout.strip() == "absent"
        finally:
            ss.reset_secret_scope(token)

        assert not (profile / ".env").exists()
        assert not list(profile.rglob("*.json"))
        assert not list(profile.rglob("*.pickle"))


class TestApiServerListenerGlobals:
    """API_SERVER listener settings are deployment config (#69379), not
    profile secrets: the scoped runner reload must keep seeing container env
    (Docker compose ``environment:`` block). API_SERVER_KEY IS a credential
    and stays profile-scoped."""

    LISTENER_VARS = (
        "API_SERVER_ENABLED",
        "API_SERVER_HOST",
        "API_SERVER_PORT",
        "API_SERVER_CORS_ORIGINS",
    )

    def test_listener_vars_read_environ_even_when_scoped_multiplex(self, monkeypatch):
        for name in self.LISTENER_VARS:
            monkeypatch.setenv(name, f"container-{name.lower()}")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"TELEGRAM_BOT_TOKEN": "scoped"})
        try:
            for name in self.LISTENER_VARS:
                assert ss.get_secret(name) == f"container-{name.lower()}"
        finally:
            ss.reset_secret_scope(token)

    def test_api_server_key_stays_profile_scoped(self, monkeypatch):
        monkeypatch.setenv("API_SERVER_KEY", "default-profile-key-0123456789abcdef")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"OTHER": "x"})
        try:
            # A scoped miss must NOT borrow the (potentially cross-profile)
            # environ value: API_SERVER_KEY is a credential.
            assert ss.get_secret("API_SERVER_KEY") is None
        finally:
            ss.reset_secret_scope(token)
        assert not ss._is_global_env("API_SERVER_KEY")
