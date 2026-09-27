"""Lifecycle-only supervisor for offline standalone control/executor children."""

import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

import httpx
import yaml

from ..app_paths import ensure_private_directory
from ..config.settings import Settings
from ..persistence import StateLayout
from ..protocol.standalone import (
    STANDALONE_BOOTSTRAP_ENV,
    STANDALONE_CONTROL_CHILD_ENV,
    STANDALONE_CONTROL_READY_HEADER,
    STANDALONE_CONTROL_READY_NONCE_ENV,
    STANDALONE_CONTROL_URL_ENV,
    STANDALONE_EXECUTOR_CHILD_ENV,
    STANDALONE_EXECUTOR_CONFIG_DIR_ENV,
    STANDALONE_EXECUTOR_NAME_ENV,
    STANDALONE_EXECUTOR_OWNER_ACTION_FILE_ENV,
    STANDALONE_EXECUTOR_RUNTIME_DIR_ENV,
)
from ..utils.private_files import atomic_write_private_text, private_file_lock
from .config import StandaloneChildConfig, resolve_standalone_child_config

_CHILD_POLL_INTERVAL_S = 0.2
_CHILD_STOP_TIMEOUT_S = 5.0
_BOOTSTRAP_WAIT_TIMEOUT_S = 10.0
_STANDALONE_PIN_FILE = "oauth-admin-pin"


@dataclass(frozen=True, slots=True)
class PreparedStandalone:
    """Private child configuration and protected local bootstrap paths."""

    child_config: StandaloneChildConfig
    control_config_path: Path
    executor_config_path: Path
    bootstrap_path: Path
    executor_profile_path: Path
    executor_owner_action_path: Path
    executor_agent_config_dir: Path
    oauth_admin_pin_path: Path
    generated_oauth_admin_pin: bool
    control_ready_nonce: str


def _private_yaml(path: Path, value: Mapping[str, object]) -> None:
    text = yaml.safe_dump(dict(value), sort_keys=True, allow_unicode=True)
    atomic_write_private_text(path, text)


def prepare_standalone(
    settings: Settings,
    *,
    child_config: StandaloneChildConfig | None = None,
) -> PreparedStandalone:
    """Resolve separate child authority and private launcher material."""
    resolved = child_config or resolve_standalone_child_config(settings)
    runtime_dir = ensure_private_directory(
        settings.runtime_dir / "standalone" / resolved.instance_namespace
    )
    executor_agent_config_dir = (
        settings.config_dir
        / "standalone"
        / resolved.instance_namespace
        / "executor"
        / "agent"
    )
    control_path = runtime_dir / "control.yaml"
    executor_path = runtime_dir / "executor.yaml"
    bootstrap_path = runtime_dir / "executor-bootstrap.json"
    profile_path = StateLayout(
        resolved.executor_state_dir
    ).executor_profile_path
    if profile_path.is_file():
        bootstrap_path.unlink(missing_ok=True)

    control_payload = dict(resolved.control)
    _private_yaml(control_path, control_payload)
    _private_yaml(executor_path, resolved.executor)
    return PreparedStandalone(
        child_config=resolved,
        control_config_path=control_path,
        executor_config_path=executor_path,
        bootstrap_path=bootstrap_path,
        executor_profile_path=profile_path,
        executor_owner_action_path=runtime_dir / "executor-owner-action",
        executor_agent_config_dir=executor_agent_config_dir,
        oauth_admin_pin_path=(
            resolved.control_state_dir / _STANDALONE_PIN_FILE
        ),
        generated_oauth_admin_pin=not bool(settings.oauth_admin_pin),
        control_ready_nonce=secrets.token_urlsafe(24),
    )


def cleanup_standalone_runtime_files(prepared: PreparedStandalone) -> None:
    """Remove regenerable child configs and consumed launcher plaintext."""
    prepared.control_config_path.unlink(missing_ok=True)
    prepared.executor_config_path.unlink(missing_ok=True)
    if prepared.executor_profile_path.is_file():
        prepared.bootstrap_path.unlink(missing_ok=True)


def standalone_child_env(
    source: Mapping[str, str] | None = None,
    *,
    bootstrap_path: Path | None = None,
    control_url: str | None = None,
    standalone_control: bool = False,
    standalone_executor: bool = False,
    executor_config_dir: Path | None = None,
    executor_owner_action_path: Path | None = None,
    executor_runtime_dir: Path | None = None,
    control_ready_nonce: str | None = None,
) -> dict[str, str]:
    """Remove ambient settings, then add only the private launcher channel."""
    env = dict(os.environ if source is None else source)
    for name in tuple(env):
        if name.startswith("WORKGATE_"):
            env.pop(name, None)
    env["PYTHONUNBUFFERED"] = "1"
    if bootstrap_path is not None:
        env[STANDALONE_BOOTSTRAP_ENV] = str(bootstrap_path)
        env[STANDALONE_EXECUTOR_NAME_ENV] = "standalone"
    if standalone_control:
        env[STANDALONE_CONTROL_CHILD_ENV] = "1"
    if control_ready_nonce is not None:
        env[STANDALONE_CONTROL_READY_NONCE_ENV] = control_ready_nonce
    if standalone_executor:
        env[STANDALONE_EXECUTOR_CHILD_ENV] = "1"
    if executor_config_dir is not None:
        env[STANDALONE_EXECUTOR_CONFIG_DIR_ENV] = str(executor_config_dir)
    if executor_owner_action_path is not None:
        env[STANDALONE_EXECUTOR_OWNER_ACTION_FILE_ENV] = str(
            executor_owner_action_path
        )
    if executor_runtime_dir is not None:
        env[STANDALONE_EXECUTOR_RUNTIME_DIR_ENV] = str(executor_runtime_dir)
    if control_url is not None:
        env[STANDALONE_CONTROL_URL_ENV] = control_url
    return env


def workgate_child_argv(*arguments: str) -> list[str]:
    """Invoke this Workgate installation from source or a frozen executable."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *arguments]
    return [sys.executable, "-m", "workgate.main", *arguments]


ProcessFactory = Callable[..., subprocess.Popen[Any]]
ControlReadyProbe = Callable[[str, str], bool]
StartedCallback = Callable[[], None]


def _control_is_listening(control_url: str, ready_nonce: str) -> bool:
    try:
        response = httpx.get(
            f"{control_url}/healthz",
            timeout=0.2,
            follow_redirects=False,
            trust_env=False,
        )
    except httpx.HTTPError:
        return False
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("ok") is True
        and response.headers.get(STANDALONE_CONTROL_READY_HEADER) == ready_nonce
    )


class StandaloneSupervisor:
    """Keep independent control and executor OS processes alive together."""

    def __init__(
        self,
        prepared: PreparedStandalone,
        *,
        process_factory: ProcessFactory = subprocess.Popen,
        control_ready_probe: ControlReadyProbe = _control_is_listening,
        poll_interval_s: float = _CHILD_POLL_INTERVAL_S,
        bootstrap_wait_timeout_s: float = _BOOTSTRAP_WAIT_TIMEOUT_S,
    ) -> None:
        self.prepared = prepared
        self._process_factory = process_factory
        self._control_ready_probe = control_ready_probe
        self._poll_interval_s = max(0.01, poll_interval_s)
        self._bootstrap_wait_timeout_s = max(0.1, bootstrap_wait_timeout_s)
        self._children: dict[str, subprocess.Popen[Any]] = {}
        self._blocked_roles: set[str] = set()
        self._stop = threading.Event()

    @property
    def children(self) -> Mapping[str, subprocess.Popen[Any]]:
        return dict(self._children)

    def _bootstrap_needed(self) -> bool:
        return not self.prepared.executor_profile_path.is_file()

    def _argv(self, role: str) -> list[str]:
        if role == "control":
            return workgate_child_argv(
                "control",
                "--config",
                str(self.prepared.control_config_path),
            )
        if role == "executor":
            return workgate_child_argv(
                "executor",
                "run",
                "--config",
                str(self.prepared.executor_config_path),
            )
        raise ValueError(f"unknown standalone child role: {role}")

    def _spawn(self, role: str) -> subprocess.Popen[Any]:
        if role == "executor":
            self.prepared.executor_owner_action_path.unlink(missing_ok=True)
        bootstrap_path = (
            self.prepared.bootstrap_path if self._bootstrap_needed() else None
        )
        control_url = (
            self.prepared.child_config.control_url
            if role == "executor"
            else None
        )
        process = self._process_factory(
            self._argv(role),
            env=standalone_child_env(
                bootstrap_path=bootstrap_path,
                control_url=control_url,
                standalone_control=role == "control",
                standalone_executor=role == "executor",
                executor_config_dir=(
                    self.prepared.executor_agent_config_dir.parent
                    if role == "executor"
                    else None
                ),
                executor_owner_action_path=(
                    self.prepared.executor_owner_action_path
                    if role == "executor"
                    else None
                ),
                control_ready_nonce=(
                    self.prepared.control_ready_nonce
                    if role == "control"
                    else None
                ),
                executor_runtime_dir=(
                    self.prepared.control_config_path.parent / "executor"
                    if role == "executor"
                    else None
                ),
            ),
        )
        self._children[role] = process
        return process

    def _wait_for_control_ready(self) -> bool:
        deadline = time.monotonic() + self._bootstrap_wait_timeout_s
        while True:
            if self._stop.is_set():
                return False
            control = self._children["control"]
            if control.poll() is not None:
                raise RuntimeError(
                    "standalone control exited before becoming ready"
                )
            bootstrap_ready = (
                not self._bootstrap_needed()
                or self.prepared.bootstrap_path.is_file()
            )
            if (
                bootstrap_ready
                and not self._stop.is_set()
                and self._control_ready_probe(
                    self.prepared.child_config.control_url,
                    self.prepared.control_ready_nonce,
                )
                and control.poll() is None
            ):
                return True
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "timed out waiting for standalone control readiness"
                )
            time.sleep(min(self._poll_interval_s, 0.05))

    def start(self) -> None:
        """Launch separate control and executor processes exactly once."""
        if self._children:
            raise RuntimeError("standalone supervisor is already started")
        if self._stop.is_set():
            return
        self._spawn("control")
        try:
            if not self._wait_for_control_ready() or self._stop.is_set():
                self.shutdown()
                return
            self._spawn("executor")
        except BaseException:
            self.shutdown()
            raise

    def restart_exited_children(self) -> tuple[str, ...]:
        """Restart only children that exited, preserving the surviving role."""
        if self.prepared.executor_profile_path.is_file():
            self.prepared.bootstrap_path.unlink(missing_ok=True)
        restarted: list[str] = []
        for role in ("control", "executor"):
            if role in self._blocked_roles:
                continue
            if role == "executor" and "control" in self._blocked_roles:
                continue
            process = self._children.get(role)
            if process is None or process.poll() is None:
                continue
            if self._stop.is_set():
                continue
            if (
                role == "executor"
                and self.prepared.executor_owner_action_path.is_file()
            ):
                self._blocked_roles.add(role)
                print(
                    "Standalone executor requires owner action and will remain "
                    "offline until standalone is restarted after repair.",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            print(
                f"Standalone {role} exited with code {process.returncode}; "
                "restarting.",
                file=sys.stderr,
                flush=True,
            )
            self._spawn(role)
            if role == "control":
                try:
                    if not self._wait_for_control_ready():
                        self._stop_process(self._children[role])
                        continue
                except RuntimeError as exc:
                    self._blocked_roles.add(role)
                    failed = self._children[role]
                    self._stop_process(failed)
                    print(
                        "Standalone control failed to become ready and will "
                        "remain offline until standalone is restarted after "
                        f"repair: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
            restarted.append(role)
        return tuple(restarted)

    def request_stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _stop_process(process: subprocess.Popen[Any]) -> None:
        if process.poll() is None:
            process.terminate()
        if process.poll() is not None:
            return
        try:
            process.wait(timeout=_CHILD_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_CHILD_STOP_TIMEOUT_S)

    @contextmanager
    def _signal_handlers(self):
        previous: dict[int, Any] = {}

        def stop_handler(_signum: int, _frame: FrameType | None) -> None:
            self.request_stop()

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, stop_handler)
        try:
            yield
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

    def run_forever(self, *, on_started: StartedCallback | None = None) -> None:
        """Run until interrupted while independently restarting exited children."""
        with self._signal_handlers():
            try:
                self.start()
                if self._stop.is_set():
                    return
                if on_started is not None:
                    on_started()
                while not self._stop.wait(self._poll_interval_s):
                    self.restart_exited_children()
            finally:
                self.shutdown()

    def shutdown(self) -> None:
        """Stop both direct children without confusing one role with the other."""
        self._stop.set()
        children = tuple(self._children.values())
        for process in children:
            self._stop_process(process)
        self._children.clear()


class StandaloneAlreadyRunningError(RuntimeError):
    """Raised when another standalone supervisor owns the same state root."""


def run_standalone(settings: Settings) -> None:
    """Launch one protected offline standalone control/executor pair."""
    child_config = resolve_standalone_child_config(settings)
    standalone_root = ensure_private_directory(
        child_config.control_state_dir.parent
    )
    stack = ExitStack()
    try:
        stack.enter_context(
            private_file_lock(standalone_root / "run.lock", timeout_s=0)
        )
    except TimeoutError as exc:
        raise StandaloneAlreadyRunningError(
            "standalone state is already active in another supervisor"
        ) from exc
    with stack:
        prepared = prepare_standalone(settings, child_config=child_config)

        def announce_started() -> None:
            print(
                f"Standalone control: {prepared.child_config.control_url}",
                flush=True,
            )
            print(
                "Standalone executor integration config: "
                f"{prepared.executor_agent_config_dir}",
                flush=True,
            )
            if prepared.generated_oauth_admin_pin:
                print(
                    "Standalone OAuth admin PIN file: "
                    f"{prepared.oauth_admin_pin_path}",
                    flush=True,
                )

        try:
            StandaloneSupervisor(prepared).run_forever(
                on_started=announce_started
            )
        finally:
            cleanup_standalone_runtime_files(prepared)
