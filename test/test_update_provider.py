"""Tests for kiro_crew.platform.update_provider."""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.platform.governance import UpdatePins
from kiro_crew.platform.update_provider import (
    CommandProvider,
    UpdateCheckResult,
    UpdateProvider,
    _current_platform_key,
    _kill_and_reap,
    _shell_exec_args,
    _trusted_path_env,
    resolve_provider,
)


class TestUpdateCheckResult:
    def test_defaults(self) -> None:
        r = UpdateCheckResult()
        assert r.available is False
        assert r.remote_version == ""
        assert r.error == ""

    def test_available(self) -> None:
        r = UpdateCheckResult(available=True, remote_version="1.2.3")
        assert r.available is True
        assert r.remote_version == "1.2.3"

    def test_error(self) -> None:
        r = UpdateCheckResult(error="network timeout")
        assert r.available is False
        assert r.error == "network timeout"

    def test_frozen(self) -> None:
        r = UpdateCheckResult()
        with pytest.raises(Exception):
            r.available = True  # type: ignore[misc]


class TestProtocol:
    def test_command_is_provider(self) -> None:
        assert isinstance(CommandProvider(), UpdateProvider)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the command provider lane is POSIX-only by design: _shell_exec_args "
    "refuses on Windows because the child's command lookup cannot be made "
    "trustworthy there (no trusted PATH to substitute, and cmd.exe searches CWD)",
)
class TestCommandProvider:
    @pytest.mark.asyncio
    async def test_check_no_command(self) -> None:
        p = CommandProvider(check_command="", apply_command="echo ok")
        result = await p.check()
        assert result.error == "no check_command configured"

    @pytest.mark.asyncio
    async def test_check_returns_version(self) -> None:
        p = CommandProvider(check_command="echo 2.0.0", apply_command="echo ok")
        result = await p.check()
        assert result.available is True
        assert result.remote_version == "2.0.0"

    @pytest.mark.asyncio
    async def test_check_no_update(self) -> None:
        p = CommandProvider(check_command="exit 1", apply_command="echo ok")
        result = await p.check()
        assert result.available is False
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_check_empty_version_is_error(self) -> None:
        # An exit-0 check that prints NO version is a broken command, not an
        # available update: returning available=True with an empty version would
        # make apply() run and restart to the SAME version forever. The provider
        # fails the check (error set, available False) instead.
        p = CommandProvider(check_command="true", apply_command="echo ok")
        result = await p.check()
        assert result.available is False
        assert result.error != ""

    @pytest.mark.asyncio
    async def test_apply_success(self) -> None:
        p = CommandProvider(check_command="echo 2.0.0", apply_command="echo done")
        success = await p.apply()
        assert success is True

    @pytest.mark.asyncio
    async def test_apply_failure(self) -> None:
        p = CommandProvider(check_command="echo 2.0.0", apply_command="exit 1")
        success = await p.apply()
        assert success is False

    @pytest.mark.asyncio
    async def test_apply_no_command(self) -> None:
        p = CommandProvider(check_command="echo 2.0.0", apply_command="")
        success = await p.apply()
        assert success is False

    @pytest.mark.asyncio
    async def test_check_version_truncated(self) -> None:
        # Long output is truncated to 128 chars
        long_version = "x" * 200
        p = CommandProvider(check_command=f"echo {long_version}", apply_command="echo ok")
        result = await p.check()
        assert result.available is True
        assert len(result.remote_version) <= 128


class TestResolveProvider:
    """The seam is policy-only and selection is by PRESENCE of commands.

    resolve_provider() returns a CommandProvider carrying the policy's commands
    when the active pins define a check_command or apply_command, and None
    otherwise (the ungoverned default — the gateway keeps its built-in update
    behaviour). There is no mechanism enum and no config/env path into the seam.
    """

    def test_no_commands_returns_none(self) -> None:
        """Empty pins (no commands) → None."""
        with patch(
            "kiro_crew.platform.governance.active_update_pins",
            return_value=UpdatePins(),
        ):
            assert resolve_provider() is None

    def test_commands_present_creates_command_provider(self) -> None:
        """Policy pins carrying commands → CommandProvider with them."""
        with patch(
            "kiro_crew.platform.governance.active_update_pins",
            return_value=UpdatePins(
                check_command="/opt/check.sh",
                apply_command="/opt/apply.sh",
            ),
        ):
            provider = resolve_provider()
            assert isinstance(provider, CommandProvider)
            assert provider.check_command == "/opt/check.sh"
            assert provider.apply_command == "/opt/apply.sh"

    def test_check_command_only_creates_provider(self) -> None:
        """Presence of just a check_command is enough to select the provider."""
        with patch(
            "kiro_crew.platform.governance.active_update_pins",
            return_value=UpdatePins(check_command="/opt/check.sh"),
        ):
            provider = resolve_provider()
            assert isinstance(provider, CommandProvider)
            assert provider.check_command == "/opt/check.sh"
            assert provider.apply_command == ""

    def test_apply_command_only_creates_provider(self) -> None:
        """Presence of just an apply_command is enough to select the provider."""
        with patch(
            "kiro_crew.platform.governance.active_update_pins",
            return_value=UpdatePins(apply_command="/opt/apply.sh"),
        ):
            provider = resolve_provider()
            assert isinstance(provider, CommandProvider)
            assert provider.check_command == ""
            assert provider.apply_command == "/opt/apply.sh"

    def test_platform_only_policy_is_still_a_provider(self) -> None:
        """A policy may define commands ONLY per platform. Returning None there
        would silently fall through to the built-in updater and bypass the
        administrator-selected package manager."""
        key = _current_platform_key()
        pins = UpdatePins(
            platform_commands={key: {"check_command": "c", "apply_command": "a"}}
        )
        with patch(
            "kiro_crew.platform.governance.active_update_pins", return_value=pins
        ):
            provider = resolve_provider()
        assert isinstance(provider, CommandProvider)
        assert provider._resolve_command("check_command") == "c"

    def test_platform_only_policy_for_another_platform_still_provider(self) -> None:
        """Presence is policy-wide, not host-specific: a policy naming only other
        platforms must NOT fall through to the built-in updater. The provider is
        returned and refuses on this host instead."""
        pins = UpdatePins(
            platform_commands={"some-other-platform": {"apply_command": "a"}}
        )
        with patch(
            "kiro_crew.platform.governance.active_update_pins", return_value=pins
        ):
            provider = resolve_provider()
        assert isinstance(provider, CommandProvider)
        assert provider._resolve_command("check_command") == ""

    def test_empty_platform_entry_is_not_presence(self) -> None:
        """A platform key carrying no commands is not a configured provider."""
        pins = UpdatePins(platform_commands={"linux-x86_64": {}})
        with patch(
            "kiro_crew.platform.governance.active_update_pins", return_value=pins
        ):
            assert resolve_provider() is None

    def test_platform_commands_passed_through(self) -> None:
        """policy platform_commands are carried onto the CommandProvider, and the
        resolved provider picks the right one for the current platform."""
        current_key = _current_platform_key()
        with patch(
            "kiro_crew.platform.governance.active_update_pins",
            return_value=UpdatePins(
                check_command="/opt/check.sh",
                apply_command="/opt/apply.sh",
                platform_commands={
                    current_key: {"apply_command": "/opt/apply-native.sh"},
                },
            ),
        ):
            provider = resolve_provider()
            assert isinstance(provider, CommandProvider)
            assert provider.platform_commands == {
                current_key: {"apply_command": "/opt/apply-native.sh"},
            }
            # _resolve_command uses the platform override for apply, default for check
            assert provider._resolve_command("apply_command") == "/opt/apply-native.sh"
            assert provider._resolve_command("check_command") == "/opt/check.sh"

    def test_reading_pins_fails_returns_none(self) -> None:
        """If reading the policy pins raises, resolve_provider fails closed to
        None (the gateway keeps its built-in behaviour)."""
        with patch(
            "kiro_crew.platform.governance.active_update_pins",
            side_effect=RuntimeError("policy unreadable"),
        ):
            assert resolve_provider() is None


class TestPlatformHelpers:
    """Test _current_platform_key and _shell_exec_args."""

    def test_platform_key_format(self) -> None:
        key = _current_platform_key()
        parts = key.split("-")
        assert len(parts) == 2
        assert parts[0] == sys.platform

    def test_platform_key_normalized_machine(self) -> None:
        with patch("platform.machine", return_value="x86_64"):
            key = _current_platform_key()
            assert key.endswith("-x86_64")

    def test_platform_key_amd64_normalized(self) -> None:
        with patch("platform.machine", return_value="AMD64"):
            key = _current_platform_key()
            assert key.endswith("-x86_64")

    def test_platform_key_aarch64_normalized(self) -> None:
        with patch("platform.machine", return_value="aarch64"):
            key = _current_platform_key()
            assert key.endswith("-arm64")

    def test_platform_key_arm64(self) -> None:
        with patch("platform.machine", return_value="arm64"):
            key = _current_platform_key()
            assert key.endswith("-arm64")

    def test_platform_key_unknown_passthrough(self) -> None:
        with patch("platform.machine", return_value="riscv64"):
            key = _current_platform_key()
            assert key.endswith("-riscv64")

    def test_shell_exec_args_posix(self) -> None:
        # Shell is resolved via trusted_system_bin; patch it to a known path so
        # the test does not depend on the host's actual /bin/sh location.
        with patch.object(sys, "platform", "linux"):
            with patch(
                "kiro_crew.platform.update_provider.trusted_system_bin",
                return_value="/bin/sh",
            ):
                args = _shell_exec_args("my-updater check")
                assert args == ["/bin/sh", "-c", "my-updater check"]

    def test_shell_exec_args_posix_fallback(self) -> None:
        # When the trusted lookup misses, fail CLOSED (return None) rather than
        # falling back to a bare name — the bare name is the agent-writable-PATH
        # hole this resolution exists to close.
        with patch.object(sys, "platform", "linux"):
            with patch(
                "kiro_crew.platform.update_provider.trusted_system_bin",
                return_value=None,
            ):
                assert _shell_exec_args("my-updater check") is None

    def test_shell_exec_args_windows_refused(self) -> None:
        # Windows is refused outright: there is no trusted PATH to substitute
        # there and cmd.exe resolves a bare command word from the CWD first, so
        # the child's lookup stays agent-influenceable. Fail closed.
        with patch.object(sys, "platform", "win32"):
            with patch(
                "kiro_crew.platform.update_provider.trusted_system_bin",
                return_value="C:\\Windows\\System32\\cmd.exe",
            ):
                assert _shell_exec_args("my-updater check") is None


class TestCommandProviderPlatformOverrides:
    """Test platform_commands resolution in CommandProvider."""

    @pytest.mark.asyncio
    async def test_platform_override_apply(self) -> None:
        """Platform-specific apply_command overrides the default."""
        current_key = _current_platform_key()
        p = CommandProvider(
            check_command="echo 2.0.0",
            apply_command="echo default-apply",
            platform_commands={
                current_key: {"apply_command": "echo platform-apply"},
            },
        )
        # The resolved command uses the platform override
        assert p._resolve_command("apply_command") == "echo platform-apply"

    @pytest.mark.asyncio
    async def test_platform_override_check(self) -> None:
        """Platform-specific check_command overrides the default."""
        current_key = _current_platform_key()
        p = CommandProvider(
            check_command="echo default-check",
            apply_command="echo apply",
            platform_commands={
                current_key: {"check_command": "echo platform-check"},
            },
        )
        assert p._resolve_command("check_command") == "echo platform-check"

    def test_no_override_falls_back_to_default(self) -> None:
        """When platform key doesn't match, default command is used."""
        p = CommandProvider(
            check_command="echo default",
            apply_command="echo apply-default",
            platform_commands={
                "fake-platform-key": {"apply_command": "echo other"},
            },
        )
        assert p._resolve_command("apply_command") == "echo apply-default"
        assert p._resolve_command("check_command") == "echo default"

    def test_partial_override_only_overrides_specified_field(self) -> None:
        """Only the field specified in the override is replaced."""
        current_key = _current_platform_key()
        p = CommandProvider(
            check_command="echo default-check",
            apply_command="echo default-apply",
            platform_commands={
                current_key: {"apply_command": "echo platform-apply"},
            },
        )
        # check_command falls back to default since not overridden
        assert p._resolve_command("check_command") == "echo default-check"
        assert p._resolve_command("apply_command") == "echo platform-apply"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="runs a real command through the shell; the command lane is "
        "POSIX-only by design (see _shell_exec_args)",
    )
    @pytest.mark.asyncio
    async def test_platform_override_actually_runs(self) -> None:
        """Integration: platform override is actually executed."""
        current_key = _current_platform_key()
        p = CommandProvider(
            check_command="echo default-version",
            apply_command="echo default-apply",
            platform_commands={
                current_key: {"check_command": "echo 3.0.0-platform"},
            },
        )
        result = await p.check()
        assert result.available is True
        assert result.remote_version == "3.0.0-platform"


class TestUpdatePinsCommandFields:
    """UpdatePins.from_dict parsing of command fields (governance)."""

    def test_from_dict_command_fields(self) -> None:
        pins = UpdatePins.from_dict(
            {
                "check_command": "/opt/check.sh",
                "apply_command": "/opt/apply.sh",
            }
        )
        assert pins.check_command == "/opt/check.sh"
        assert pins.apply_command == "/opt/apply.sh"
        assert pins.platform_commands == {}

    def test_from_dict_command_defaults_empty(self) -> None:
        pins = UpdatePins.from_dict({})
        assert pins.check_command == ""
        assert pins.apply_command == ""
        assert pins.platform_commands == {}

    def test_from_dict_platform_commands(self) -> None:
        pins = UpdatePins.from_dict(
            {
                "check_command": "/opt/check.sh",
                "apply_command": "/opt/apply.sh",
                "platform_commands": {
                    "linux-x86_64": {
                        "check_command": "/opt/check-x64.sh",
                        "apply_command": "/opt/apply-x64.sh",
                    },
                    "darwin-arm64": {"apply_command": "/opt/apply-arm64.sh"},
                },
            }
        )
        assert pins.platform_commands == {
            "linux-x86_64": {
                "check_command": "/opt/check-x64.sh",
                "apply_command": "/opt/apply-x64.sh",
            },
            "darwin-arm64": {"apply_command": "/opt/apply-arm64.sh"},
        }

    def test_from_dict_command_non_string_raises(self) -> None:
        from kiro_crew.platform.governance import PlatformCompositionError

        with pytest.raises(PlatformCompositionError, match="must be a string"):
            UpdatePins.from_dict({"check_command": 123})

    def test_from_dict_platform_commands_not_mapping_raises(self) -> None:
        from kiro_crew.platform.governance import PlatformCompositionError

        with pytest.raises(PlatformCompositionError, match="must be a mapping"):
            UpdatePins.from_dict({"platform_commands": "nope"})

    def test_from_dict_platform_commands_unknown_key_raises(self) -> None:
        from kiro_crew.platform.governance import PlatformCompositionError

        with pytest.raises(PlatformCompositionError, match="unknown key"):
            UpdatePins.from_dict(
                {
                    "platform_commands": {
                        "linux-x86_64": {"bogus_command": "/opt/x.sh"},
                    },
                }
            )

    def test_from_dict_platform_commands_non_string_value_raises(self) -> None:
        from kiro_crew.platform.governance import PlatformCompositionError

        with pytest.raises(PlatformCompositionError, match="must be a string"):
            UpdatePins.from_dict(
                {
                    "platform_commands": {
                        "linux-x86_64": {"apply_command": 42},
                    },
                }
            )


# ---------------------------------------------------------------------------
# Helpers for mocking asyncio subprocesses
# ---------------------------------------------------------------------------


def _fake_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> "MagicMock":
    """Build a mock subprocess whose communicate() is awaitable."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    return proc


class TestCommandProviderNoShellAndTimeout:
    """CommandProvider fail-closed shell + timeout + stderr redaction."""

    @pytest.mark.asyncio
    async def test_check_no_trusted_shell(self) -> None:
        p = CommandProvider(check_command="echo hi", apply_command="echo ok")
        with patch(
            "kiro_crew.platform.update_provider._shell_exec_args",
            return_value=None,
        ):
            result = await p.check()
        assert result.error == "no trusted shell found"

    @pytest.mark.asyncio
    async def test_apply_no_trusted_shell(self) -> None:
        p = CommandProvider(check_command="echo hi", apply_command="echo ok")
        with patch(
            "kiro_crew.platform.update_provider._shell_exec_args",
            return_value=None,
        ):
            assert await p.apply() is False

    @pytest.mark.asyncio
    async def test_check_timeout_kills_proc(self) -> None:
        p = CommandProvider(check_command="sleep 100", apply_command="echo ok")
        proc = _fake_proc(returncode=0)
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "sleep 100"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("asyncio.wait_for", AsyncMock(side_effect=asyncio.TimeoutError())),
        ):
            result = await p.check()
        assert result.error == "check_command timed out"
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_file_not_found(self) -> None:
        p = CommandProvider(check_command="echo hi", apply_command="echo ok")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "echo hi"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(side_effect=FileNotFoundError()),
            ),
            patch.object(sys, "platform", "linux"),
        ):
            result = await p.check()
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_apply_timeout_kills_proc(self) -> None:
        p = CommandProvider(check_command="echo hi", apply_command="sleep 100")
        proc = _fake_proc(returncode=0)
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "sleep 100"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("asyncio.wait_for", AsyncMock(side_effect=asyncio.TimeoutError())),
        ):
            assert await p.apply() is False
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_file_not_found(self) -> None:
        p = CommandProvider(check_command="echo hi", apply_command="echo ok")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "echo ok"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(side_effect=FileNotFoundError()),
            ),
            patch.object(sys, "platform", "linux"),
        ):
            assert await p.apply() is False

    @pytest.mark.asyncio
    async def test_apply_failure_redacts_stderr(self) -> None:
        import types

        p = CommandProvider(check_command="echo hi", apply_command="fail")
        proc = _fake_proc(returncode=1, stderr=b"token=secret123 failed")
        sec = types.ModuleType("kiro_crew.security")
        sec.redact_credentials = MagicMock(side_effect=lambda t: (t, 0))  # type: ignore[attr-defined]
        sec.redact_exfiltration_urls = MagicMock(side_effect=lambda t: (t, 0))  # type: ignore[attr-defined]
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "fail"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch.dict(sys.modules, {"kiro_crew.security": sec}),
        ):
            assert await p.apply() is False
        sec.redact_credentials.assert_called_once()
        sec.redact_exfiltration_urls.assert_called_once()


class TestCancellationKillsUpdaterChild:
    """A gateway shutdown cancels the update task. The updater child must be
    killed and reaped, not left mutating the installation after we are gone.

    Every test here stubs BOTH ``_shell_exec_args`` AND ``trusted_system_path``
    so the command lane runs identically on every host (POSIX and Windows
    runners): the seams are what make it platform-dependent, so neutralising
    both keeps these tests about the cancellation/reap semantics only.
    """

    @staticmethod
    def _proc():
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.kill = MagicMock()
        return proc

    @pytest.mark.asyncio
    async def test_command_apply_cancelled_kills_child(self) -> None:
        proc = self._proc()
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "a"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("asyncio.wait_for", AsyncMock(side_effect=asyncio.CancelledError())),
        ):
            with pytest.raises(asyncio.CancelledError):
                await p.apply()
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_command_check_cancelled_kills_child(self) -> None:
        proc = self._proc()
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "c"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("asyncio.wait_for", AsyncMock(side_effect=asyncio.CancelledError())),
        ):
            with pytest.raises(asyncio.CancelledError):
                await p.check()
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_runs_outside_any_writable_checkout(self) -> None:
        """A relative command word must not resolve in the gateway's cwd."""
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        spawn = AsyncMock(return_value=proc)
        p = CommandProvider(check_command="c", apply_command="./update.sh")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "./update.sh"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            assert await p.apply() is True
        assert spawn.await_args.kwargs["cwd"] == "/"

    @pytest.mark.asyncio
    async def test_kill_and_reap_kills_the_whole_tree(self) -> None:
        """An update command is a shell pipeline, so killing only the direct
        child leaves its members running and can leave communicate() waiting on
        pipes those survivors hold."""
        proc = MagicMock()
        proc.pid = 4242
        proc.kill = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        with patch(
            "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()
        ) as tree:
            await _kill_and_reap(proc)
        tree.assert_awaited_once()
        assert tree.await_args.args[0] == 4242

    @pytest.mark.asyncio
    async def test_kill_and_reap_bounds_the_reap(self) -> None:
        """A descendant ignoring the signal must not turn cleanup into a hang."""
        proc = MagicMock()
        proc.pid = 1
        proc.kill = MagicMock()

        async def _never_returns():
            await asyncio.sleep(3600)

        proc.communicate = _never_returns
        with patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()):
            await asyncio.wait_for(_kill_and_reap(proc), timeout=30)

    @pytest.mark.asyncio
    async def test_kill_and_reap_tolerates_dead_child(self) -> None:
        """Reaping is best-effort: a child that already exited must not raise."""
        proc = MagicMock()
        proc.kill = MagicMock(side_effect=ProcessLookupError())
        proc.communicate = AsyncMock(side_effect=RuntimeError("already reaped"))
        await _kill_and_reap(proc)

    @pytest.mark.asyncio
    async def test_cancel_during_spawn_propagates_cancellation(self) -> None:
        """Cancellation landing INSIDE create_subprocess_exec leaves no child and
        no bound name: the handler must re-raise CancelledError, not trip over an
        unbound local and replace it with UnboundLocalError."""
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "a"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(side_effect=asyncio.CancelledError()),
            ),
        ):
            with pytest.raises(asyncio.CancelledError):
                await p.apply()

    @pytest.mark.asyncio
    async def test_check_cancel_during_spawn_propagates_cancellation(self) -> None:
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "c"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(side_effect=asyncio.CancelledError()),
            ),
        ):
            with pytest.raises(asyncio.CancelledError):
                await p.check()


class TestCommandProviderTrustedPath:
    """The child must not resolve the operator's command words through a PATH
    that can lead with an agent-writable directory."""

    def test_trusted_path_env_replaces_path_only(self) -> None:
        with (
            patch.dict(os.environ, {"PATH": "/home/u/.local/bin:/usr/bin", "LANG": "C"}),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
        ):
            env = _trusted_path_env()
        assert env is not None
        assert env["PATH"] == "/usr/bin:/bin"
        # Everything else survives, so a package manager keeps its proxy/locale.
        assert env["LANG"] == "C"

    def test_trusted_path_env_fails_closed_when_unavailable(self) -> None:
        # No trusted PATH to substitute: refuse rather than pass the inherited
        # (agent-influenceable) PATH through to the child.
        with (
            patch.dict(os.environ, {"PATH": "/orig"}),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value=None,
            ),
        ):
            assert _trusted_path_env() is None

    @pytest.mark.asyncio
    async def test_apply_refuses_without_trusted_path(self) -> None:
        spawn = AsyncMock()
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "a"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "kiro_crew.platform.update_provider._trusted_path_env",
                return_value=None,
            ),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            assert await p.apply() is False
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_check_refuses_without_trusted_path(self) -> None:
        spawn = AsyncMock()
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "c"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "kiro_crew.platform.update_provider._trusted_path_env",
                return_value=None,
            ),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            assert (await p.check()).error
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_passes_trusted_env(self) -> None:
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        spawn = AsyncMock(return_value=proc)
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "a"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "kiro_crew.platform.update_provider._trusted_path_env",
                return_value={"PATH": "/usr/bin:/bin"},
            ),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            assert await p.apply() is True
        assert spawn.await_args.kwargs["env"] == {"PATH": "/usr/bin:/bin"}

    @pytest.mark.asyncio
    async def test_check_passes_trusted_env(self) -> None:
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"9.9.9", b""))
        spawn = AsyncMock(return_value=proc)
        p = CommandProvider(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.update_provider._shell_exec_args",
                return_value=["/bin/sh", "-c", "c"],
            ),
            patch(
                "kiro_crew.platform.update_provider.trusted_system_path",
                return_value="/usr/bin:/bin",
            ),
            patch(
                "kiro_crew.platform.update_provider._trusted_path_env",
                return_value={"PATH": "/usr/bin:/bin"},
            ),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            assert (await p.check()).available is True
        assert spawn.await_args.kwargs["env"] == {"PATH": "/usr/bin:/bin"}


class TestManualEntryPointsHonourPolicy:
    """`POST /api/update` and `kirocrew update` must not run the built-in
    git/CDN mechanism on a host whose policy selects its own updater. An
    authenticated operator clicking Update is who they say they are, not proof
    that this host may update by git."""

    @pytest.mark.asyncio
    async def test_apply_policy_update_returns_none_without_provider(self) -> None:
        from kiro_crew.platform.governance import UpdatePins
        from kiro_crew.platform.update_provider import apply_policy_update

        with patch(
            "kiro_crew.platform.governance.active_update_pins",
            return_value=UpdatePins(),
        ):
            assert await apply_policy_update() is None

    @pytest.mark.asyncio
    async def test_apply_policy_update_delegates_when_configured(self) -> None:
        from kiro_crew.platform.governance import UpdatePins
        from kiro_crew.platform.update_provider import apply_policy_update

        pins = UpdatePins(check_command="c", apply_command="a")
        with (
            patch(
                "kiro_crew.platform.governance.active_update_pins", return_value=pins
            ),
            patch.object(CommandProvider, "apply", AsyncMock(return_value=True)) as ap,
        ):
            assert await apply_policy_update() is True
        ap.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_provider_failure_is_reported_not_swallowed(self) -> None:
        """False must reach the caller so it can refuse to fall back."""
        from kiro_crew.platform.governance import UpdatePins
        from kiro_crew.platform.update_provider import apply_policy_update

        pins = UpdatePins(apply_command="a")
        with (
            patch(
                "kiro_crew.platform.governance.active_update_pins", return_value=pins
            ),
            patch.object(CommandProvider, "apply", AsyncMock(return_value=False)),
        ):
            assert await apply_policy_update() is False


class TestWheelUpdateCommandPropagatesDownloadFailure:
    """`curl … | sh` reports sh's status, and a shell handed empty input exits 0,
    so a CDN failure looked like a successful update: version unchanged, gateway
    restarted, check still saw an update, unattended path looped."""

    def test_command_is_not_a_bare_pipe(self) -> None:
        from kiro_crew.platform.update_layout import wheel_update_command

        with patch(
            "kiro_crew.platform.update_layout.cdn_bases",
            return_value=("https://f", "https://a"),
        ):
            cmd = wheel_update_command("stable")
        assert "| sh" not in cmd, "piping hides the download's exit status"
        assert "set -e" in cmd
        assert "mktemp" in cmd and "trap" in cmd, "temp installer must be cleaned up"

    def test_download_failure_is_non_zero(self) -> None:
        """Executed for real: an unresolvable host must fail the command."""
        import subprocess

        from kiro_crew.platform.update_layout import wheel_update_command

        with patch(
            "kiro_crew.platform.update_layout.cdn_bases",
            return_value=("https://f", "https://invalid.invalid.invalid"),
        ):
            cmd = wheel_update_command("stable")
        shell = _shell_exec_args(cmd)
        assert shell is not None
        assert subprocess.run(shell, capture_output=True).returncode != 0
