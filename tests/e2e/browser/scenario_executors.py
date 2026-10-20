import re
import subprocess
import sys
import time

import httpx
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness
from tests.e2e.helpers import (
    PROJECT_ROOT,
    isolated_process_env,
    isolated_xdg_env,
    write_yaml_config,
)
from workgate.executor.profile import ExecutorProfileStore
from workgate.persistence import FileStateStore


def run_executor_admin(harness: BrowserHarness) -> None:
    page = harness.page

    started = httpx.post(
        f"{harness.base_url}/executor/v1/pair/start",
        json={
            "requested_name": "browser-executor",
            "metadata": {
                "hostname": "browser-laptop",
                "platform": "linux",
                "build": "browser-e2e",
            },
        },
        timeout=5,
    )
    assert started.status_code == 200
    pair = started.json()
    assert pair["verification_uri"] == f"{harness.base_url}/pair"

    page.goto(pair["verification_uri"], wait_until="domcontentloaded")
    expect(page.locator("#connection-state")).to_have_text("Connected")
    expect(page.locator("#page-title")).to_have_text("Executors")
    expect(page.locator("#executors-panel")).to_be_visible()

    page.locator("#executor-pair-open").click()
    expect(page.locator("#executor-pair-dialog")).to_be_visible()
    assert (
        page.locator("#executor-pair-dialog").evaluate(
            "el => getComputedStyle(el, '::backdrop').backgroundColor"
        )
        == "rgba(17, 24, 39, 0.45)"
    )
    page.locator("#executor-pair-code").fill(pair["user_code"].lower())
    page.locator("#executor-pair-form").get_by_role(
        "button", name="Inspect request"
    ).click()

    expect(page.locator("#executor-pair-review")).to_be_visible()
    expect(page.locator("#executor-pair-requested-name")).to_have_text(
        "browser-executor"
    )
    expect(page.locator("#executor-pair-hostname")).to_have_text(
        "browser-laptop"
    )
    expect(page.locator("#executor-pair-platform")).to_have_text("linux")
    expect(page.locator("#executor-pair-build")).to_have_text("browser-e2e")
    expect(page.locator("#executor-pair-existing")).to_have_text("None")
    page.locator("#executor-pair-name").fill("browser-approved")
    page.locator("#executor-pair-approve").click()

    expect(page.locator("#executor-pair-dialog")).to_be_hidden()
    expect(page.locator("#executor-list")).to_contain_text("browser-approved")
    page.locator("#executor-list").get_by_text(
        "browser-approved", exact=True
    ).click()
    expect(page.locator("#executor-detail-status")).to_have_text("offline")
    expect(page.locator(".executor-row-selected")).to_have_css(
        "background-color", "rgb(238, 236, 255)"
    )
    executor_id = page.locator("#executor-detail-id").inner_text()
    assert executor_id.startswith("exec_")

    page.locator("#executor-rename-open").click()
    expect(page.locator("#executor-rename-dialog")).to_be_visible()
    page.locator("#executor-rename-name").fill("browser-renamed")
    page.locator("#executor-rename-form").get_by_role(
        "button", name="Rename"
    ).click()
    expect(page.locator("#executor-rename-dialog")).to_be_hidden()
    expect(page.locator("#executor-list")).to_contain_text("browser-renamed")
    for view in ("dashboard", "file", "terminal"):
        option = page.locator(f'#{view}-executor option[value="{executor_id}"]')
        expect(option).to_contain_text("browser-renamed")

    page.locator("#executor-revoke-open").click()
    expect(page.locator("#executor-revoke-dialog")).to_be_visible()
    expect(page.locator("#executor-revoke-form .danger-action")).to_have_css(
        "color", "rgb(161, 63, 73)"
    )
    expect(page.locator("#executor-revoke-name")).to_have_text(
        "browser-renamed"
    )
    page.locator("#executor-revoke-form").get_by_role(
        "button", name="Revoke executor"
    ).click()
    expect(page.locator("#executor-revoke-dialog")).to_be_hidden()
    expect(page.locator("#executor-detail-status")).to_have_text("revoked")
    expect(page.locator("#executor-revoked")).to_have_text("1")
    for view in ("dashboard", "file", "terminal"):
        expect(
            page.locator(f'#{view}-executor option[value="{executor_id}"]')
        ).to_have_count(0)


def run_browser_approved_cli_pairing(harness: BrowserHarness) -> None:
    """Complete real CLI pairing through the owner's browser OAuth session."""
    harness.login()  # Real Chromium OAuth authorization-code + PKCE + UI session.
    page = harness.page  # login restores a fresh page after the OAuth callback.
    expect(page.locator("#connection-state")).to_have_text("Connected")

    for approve in (True, False):
        name = "browser-cli-approved" if approve else "browser-cli-denied"
        root = harness.root / name
        root.mkdir()
        state_dir = root / "state"
        config = write_yaml_config(
            root / "executor.yaml", {"state_dir": str(state_dir)}
        )
        stdout_path = harness.artifacts / f"{name}.stdout.log"
        stderr_path = harness.artifacts / f"{name}.stderr.log"
        env = isolated_process_env(isolated_xdg_env(root / "xdg"))
        with (
            stdout_path.open("w", encoding="utf-8") as stdout,
            stderr_path.open("w", encoding="utf-8") as stderr,
        ):
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "workgate.main",
                    "executor",
                    "connect",
                    harness.base_url,
                    "--name",
                    name,
                    "--config",
                    str(config),
                ],
                cwd=PROJECT_ROOT,
                env=env,
                stdout=stdout,
                stderr=stderr,
                text=True,
            )
        try:
            deadline = time.monotonic() + 35
            code = None
            while time.monotonic() < deadline:
                assert process.poll() is None, (
                    f"real Executor CLI exited before browser approval: {process.returncode}"
                )
                match = re.search(
                    r"(?m)^Code: (\S+)$",
                    stdout_path.read_text(encoding="utf-8"),
                )
                if match:
                    code = match.group(1)
                    break
                time.sleep(0.1)
            assert code, "real Executor CLI did not print pairing user code"

            page.goto(f"{harness.base_url}/pair", wait_until="domcontentloaded")
            expect(page.locator("#connection-state")).to_have_text("Connected")
            page.locator("#executor-pair-open").click()
            page.locator("#executor-pair-code").fill(code.lower())
            page.locator("#executor-pair-form").get_by_role(
                "button", name="Inspect request"
            ).click()
            expect(page.locator("#executor-pair-review")).to_be_visible()
            expect(page.locator("#executor-pair-requested-name")).to_have_text(
                name
            )
            if approve:
                page.locator("#executor-pair-name").fill(name)
                page.locator("#executor-pair-approve").click()
            else:
                page.locator("#executor-pair-deny").click()
            expect(page.locator("#executor-pair-dialog")).to_be_hidden()

            assert process.wait(timeout=35) == (0 if approve else 1), (
                f"real Executor CLI pairing exit: {process.returncode}"
            )
            profile = ExecutorProfileStore(
                FileStateStore(lambda state_dir=state_dir: state_dir)
            ).load()
            if approve:
                assert (
                    profile is not None
                    and profile.control_url == harness.base_url
                )
                expect(page.locator("#executor-list")).to_contain_text(name)
                page.locator("#executor-list").get_by_text(
                    name, exact=True
                ).click()
                expect(page.locator("#executor-detail-id")).to_have_text(
                    profile.executor_id
                )
            else:
                assert profile is None, (
                    "browser-denied CLI pairing persisted credentials"
                )
                assert "pairing_denied" in stderr_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                expect(page.locator("#executor-list")).not_to_contain_text(name)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    harness.assert_clean_browser()
