import time
from urllib.parse import urlencode

from playwright.sync_api import Route, expect

from tests.browser.harness import BrowserHarness


def _file_entry(harness: BrowserHarness, path: str):
    return harness.page.locator(f'.file-entry[title="{path}"]')


def _show_task_tab(page, tab: str) -> None:
    button = page.locator(f"#task-tab-{tab}")
    button.click()
    expect(button).to_have_attribute("aria-selected", "true")


def _refresh_task(page) -> None:
    _show_task_tab(page, "plan")
    page.locator("#todo-refresh").click()
    _show_task_tab(page, "progress")


def run_files_todos_audit(harness: BrowserHarness) -> None:
    page = harness.page
    harness.navigate("files")

    expect(_file_entry(harness, "notes.txt")).to_be_visible()
    _file_entry(harness, "notes.txt").click()
    expect(page.locator("#file-preview-body")).to_contain_text(
        "executor browser fixture"
    )
    page.locator("#file-edit").click()
    expect(page.locator("#file-editor-form")).to_be_visible()
    page.locator("#file-editor").fill("edited by Chromium\n")
    page.locator("#file-editor-form").get_by_role(
        "button", name="Save file"
    ).click()
    expect(page.locator("#file-state")).to_contain_text(
        f"Saved {harness.executor_id}:notes.txt"
    )
    assert harness.executor_workspace.joinpath("notes.txt").read_text() == (
        "edited by Chromium\n"
    )

    # A second writer changes the file after the browser editor opened it.
    # The server must reject a stale save without losing either version.
    page.locator("#file-edit").click()
    expect(page.locator("#file-editor-form")).to_be_visible()
    page.locator("#file-editor").fill("unsaved Chromium draft\n")
    harness.executor_workspace.joinpath("notes.txt").write_text(
        "external update\n", encoding="utf-8"
    )
    page.locator("#file-editor-form").get_by_role(
        "button", name="Save file"
    ).click()
    expect(page.locator("#file-state")).to_contain_text(
        "File changed on disk; your edits are preserved."
    )
    expect(page.locator("#file-editor-form")).to_be_visible()
    expect(page.locator("#file-editor")).to_have_value(
        "unsaved Chromium draft\n"
    )
    assert harness.executor_workspace.joinpath("notes.txt").read_text() == (
        "external update\n"
    )

    page.once("dialog", lambda dialog: dialog.dismiss())
    page.locator("#file-editor-reload").click()
    expect(page.locator("#file-editor")).to_have_value(
        "unsaved Chromium draft\n"
    )
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#file-editor-reload").click()
    expect(page.locator("#file-editor")).to_have_value("external update\n")
    page.locator("#file-editor").fill("edited by Chromium\n")
    page.locator("#file-editor-form").get_by_role(
        "button", name="Save file"
    ).click()
    expect(page.locator("#file-state")).to_contain_text(
        f"Saved {harness.executor_id}:notes.txt"
    )
    assert harness.executor_workspace.joinpath("notes.txt").read_text() == (
        "edited by Chromium\n"
    )

    delayed = False

    def delay_stale_preview(route: Route) -> None:
        nonlocal delayed
        if "stale-executor.txt" in route.request.url and not delayed:
            delayed = True
            time.sleep(0.4)
        route.continue_()

    page.route("**/api/ui/files/preview**", delay_stale_preview)
    _file_entry(harness, "stale-executor.txt").click()
    _file_entry(harness, "notes.txt").click()
    expect(page.locator("#file-preview-body")).to_contain_text(
        "edited by Chromium"
    )
    page.wait_for_timeout(600)
    expect(page.locator("#file-preview-body")).not_to_contain_text(
        "stale executor preview"
    )
    page.unroute("**/api/ui/files/preview**", delay_stale_preview)

    # File-manager operations stay in browser controls; no destination prompts.
    page.locator("#file-new-folder").click()
    expect(page.locator("#file-operation-dialog")).to_be_visible()
    page.locator("#file-operation-name").fill("archive")
    page.locator("#file-operation-form button[type=submit]").click()
    expect(_file_entry(harness, "archive")).to_be_visible()
    assert harness.executor_workspace.joinpath("archive").is_dir()

    page.locator("#file-filter").fill("copy-")
    expect(_file_entry(harness, "copy-source.txt")).to_be_visible()
    expect(_file_entry(harness, "notes.txt")).to_have_count(0)
    page.locator("#file-filter").clear()
    page.locator("#file-sort").select_option("size")
    page.locator("#file-sort-direction").click()
    expect(page.locator("#file-sort-direction")).to_have_text("Descending")
    displayed = page.locator('#file-list .file-entry[title$=".txt"]')
    order = [entry.get_attribute("title") or "" for entry in displayed.all()]
    assert order and all(order)
    sizes = [
        harness.executor_workspace.joinpath(name).stat().st_size
        for name in order
    ]
    assert sizes == sorted(sizes, reverse=True)
    page.locator("#file-sort").select_option("name")

    _file_entry(harness, "copy-source.txt").click()
    page.locator("#file-copy").click()
    expect(page.locator("#file-clipboard-state")).to_contain_text("Copy")
    _file_entry(harness, "archive").dblclick()
    expect(page.locator("#file-breadcrumbs")).to_contain_text("archive")
    page.locator("#file-paste").click()
    page.locator("#file-operation-name").fill("copied.txt")
    page.locator("#file-operation-form button[type=submit]").click()
    expect(_file_entry(harness, "archive/copied.txt")).to_be_visible()
    assert (
        harness.executor_workspace.joinpath("archive/copied.txt").read_text()
        == "copy source\n"
    )

    _file_entry(harness, "archive/copied.txt").click()
    page.locator("#file-move").click()
    expect(page.locator("#file-clipboard-state")).to_contain_text("Cut")
    page.locator("#file-breadcrumbs").get_by_role(
        "button", name="Workspace"
    ).click()
    page.locator("#file-paste").click()
    page.locator("#file-operation-name").fill("moved.txt")
    page.locator("#file-operation-form button[type=submit]").click()
    expect(_file_entry(harness, "moved.txt")).to_be_visible()
    assert not harness.executor_workspace.joinpath(
        "archive/copied.txt"
    ).exists()

    page.locator("#file-rename").click()
    page.locator("#file-operation-name").fill("renamed.txt")
    page.locator("#file-operation-form button[type=submit]").click()
    expect(_file_entry(harness, "renamed.txt")).to_be_visible()
    assert (
        harness.executor_workspace.joinpath("renamed.txt").read_text()
        == "copy source\n"
    )

    page.locator("#file-upload-input").set_input_files(
        {
            "name": "binary-upload.bin",
            "mimeType": "application/octet-stream",
            "buffer": b"\x00\x80\xffBROWSER",
        }
    )
    expect(_file_entry(harness, "binary-upload.bin")).to_be_visible()
    assert (
        harness.executor_workspace.joinpath("binary-upload.bin").read_bytes()
        == b"\x00\x80\xffBROWSER"
    )

    # An existing binary file cannot be silently overwritten by upload.
    page.locator("#file-upload-input").set_input_files(
        {
            "name": "binary-upload.bin",
            "mimeType": "application/octet-stream",
            "buffer": b"replacement",
        }
    )
    expect(page.locator("#file-state")).to_contain_text("Upload failed")
    assert harness.executor_workspace.joinpath(
        "binary-upload.bin"
    ).read_bytes() == (b"\x00\x80\xffBROWSER")

    page.locator("#file-new").click()
    page.locator("#file-operation-name").fill("created.txt")
    page.locator("#file-operation-form button[type=submit]").click()
    expect(harness.page.locator("#file-editor-form")).to_be_visible()
    assert harness.executor_workspace.joinpath("created.txt").exists()
    page.locator("#file-editor-cancel").click()

    task = harness.api(
        "POST",
        "/tools/task",
        body={"action": "create"},
    )
    assert task["status"] == 200
    task_id = task["payload"]["task_id"]
    zero_session_task = harness.api(
        "POST",
        "/tools/task",
        body={"action": "create", "label": "zero-session-browser"},
    )
    assert zero_session_task["status"] == 200
    zero_session_task_id = zero_session_task["payload"]["task_id"]

    unattached = harness.api(
        "POST",
        "/tools/session_start",
        body={"workdir": ".", "label": "unattached-browser"},
    )
    assert unattached["status"] == 200
    unattached_session_id = unattached["payload"]["session_id"]

    session = harness.api(
        "POST",
        "/tools/session_start",
        body={"workdir": ".", "task_id": task_id},
    )
    assert session["status"] == 200
    session_id = session["payload"]["session_id"]
    session_shell = harness.api(
        "POST",
        "/tools/bash",
        body={
            "session_id": session_id,
            "command": "bash",
            "pty": True,
            "name": "live-workspace-deep-link",
        },
    )
    assert session_shell["status"] == 200
    assert session_shell["payload"]["mode"] == "pty"
    shell_id = str(session_shell["payload"]["result"]["shell_id"])
    harness.track_terminal(harness.executor_id, shell_id)
    harness.navigate("tasks")
    page.locator("#tasks-refresh").click()
    harness.navigate("overview")
    dashboard_task = page.locator(
        f'#dashboard-task-list [data-task-id="{task_id}"]'
    )
    expect(dashboard_task).to_be_visible()
    dashboard_task.click()
    expect(page.locator("#page-location")).to_have_text("Tasks")
    expect(
        page.locator(f'#task-list .task-entry[data-task-id="{task_id}"]')
    ).to_have_attribute("aria-current", "true")
    page.goto(
        f"{harness.base_url}/ui?task_id={task_id}&session_id={session_id}#tasks",
        wait_until="domcontentloaded",
    )
    expect(page.locator("#connection-state")).to_have_text("Connected")
    page.locator("#tasks-refresh").click()
    expect(
        page.locator(f'#task-list .task-entry[data-task-id="{task_id}"]')
    ).to_have_attribute("aria-current", "true")
    expect(
        page.locator(
            f'#task-list .task-entry[data-task-id="{zero_session_task_id}"]'
        )
    ).to_be_visible()
    expect(
        page.locator(
            f'#task-list .unattached-session-entry[data-session-id="{unattached_session_id}"]'
        )
    ).to_be_visible()
    expect(
        page.locator(
            f'#session-list .session-entry[data-session-id="{session_id}"]'
        )
    ).to_have_attribute("aria-current", "true")

    # Live executor inventory overrides stale retained session availability,
    # disabling termination without reloading or discarding the task workspace.
    def offline_session_executor(route):
        response = route.fetch()
        payload = response.json()
        for executor in payload.get("data", {}).get("executors", []):
            if executor.get("executor_id") == harness.executor_id:
                executor["online"] = False
        route.fulfill(response=response, json=payload)

    page.route("**/api/ui/executors", offline_session_executor)
    expect(page.locator("#session-detail-status")).to_contain_text(
        "executor offline", timeout=12_000
    )
    expect(page.locator("#session-terminate")).to_be_disabled()
    page.unroute("**/api/ui/executors", offline_session_executor)
    expect(page.locator("#session-detail-status")).not_to_contain_text(
        "executor offline", timeout=12_000
    )

    # Task selection never chooses an execution session implicitly.
    page.locator(
        f'#task-list .task-entry[data-task-id="{zero_session_task_id}"]'
    ).click()
    expect(page.locator("#session-list .session-entry")).to_have_count(0)
    expect(page.locator("#session-detail-title")).to_have_text(
        "No session selected"
    )
    page.locator(f'#task-list .task-entry[data-task-id="{task_id}"]').click()
    expect(page.locator("#task-tab-progress")).to_have_attribute(
        "aria-selected", "true"
    )
    expect(page.locator("#task-panel-plan")).to_be_hidden()
    page.locator("#task-tab-progress").focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#task-tab-plan")).to_have_attribute(
        "aria-selected", "true"
    )
    page.keyboard.press("ArrowRight")
    expect(page.locator("#task-tab-sessions")).to_have_attribute(
        "aria-selected", "true"
    )
    expect(
        page.locator(
            f'#session-list .session-entry[data-session-id="{session_id}"]'
        )
    ).to_have_attribute("aria-current", "false")
    expect(page.locator("#session-detail-title")).to_have_text(
        "No session selected"
    )
    _show_task_tab(page, "sessions")
    page.locator(
        f'#session-list .session-entry[data-session-id="{session_id}"]'
    ).click()
    expect(
        page.locator(
            f'#session-list .session-entry[data-session-id="{session_id}"]'
        )
    ).to_have_attribute("aria-current", "true")
    expect(page.locator("#session-audit-filter-form")).to_be_visible()
    audit_top = page.locator("#session-audit-filter-form").bounding_box()
    assert audit_top and audit_top["y"] < 820
    expect(page.locator("#todo-state")).to_contain_text("loaded 0 plan steps")
    _show_task_tab(page, "progress")
    expect(page.locator("#task-status")).to_have_text("active")

    reported = harness.api(
        "POST",
        "/tools/task",
        body={
            "action": "report",
            "task_id": task_id,
            "objective": "Browser durable task",
            "summary": "Visible in the WebUI",
        },
    )
    assert reported["status"] == 200
    _refresh_task(page)
    expect(page.locator("#task-objective")).to_have_text("Browser durable task")
    expect(page.locator("#task-summary")).to_have_text("Visible in the WebUI")

    completed = harness.api(
        "POST",
        "/tools/task",
        body={
            "action": "finish",
            "task_id": task_id,
        },
    )
    assert completed["status"] == 200
    _refresh_task(page)
    expect(page.locator("#task-status")).to_have_text("completed")
    expect(page.locator("#todo-refresh")).to_be_enabled()
    expect(page.locator("#todo-add")).to_be_disabled()

    resumed = harness.api(
        "POST",
        "/tools/task",
        body={
            "action": "resume",
            "task_id": task_id,
        },
    )
    assert resumed["status"] == 200
    _refresh_task(page)
    expect(page.locator("#task-status")).to_have_text("active")
    expect(page.locator("#todo-add")).to_be_enabled()

    task_list_box = page.locator("#task-list").bounding_box()
    task_workspace_box = page.locator(".tasks-workspace").bounding_box()
    assert task_list_box is not None and task_workspace_box is not None
    assert abs(task_list_box["y"] - task_workspace_box["y"]) < 1

    availability_override = "missing_on_executor"

    def project_session_availability(route: Route) -> None:
        response = route.fetch()
        payload = response.json()
        for task_row in payload.get("data", {}).get("tasks", []):
            for row in task_row.get("sessions", []):
                if row.get("session_id") == session_id:
                    row["availability"] = availability_override
        route.fulfill(response=response, json=payload)

    page.route("**/api/ui/tasks", project_session_availability)
    page.locator("#tasks-refresh").click()
    expect(page.locator("#session-detail-status")).to_have_text(
        "Unavailable · missing on executor"
    )
    expect(
        page.locator(
            f'#session-list .session-entry[data-session-id="{session_id}"] .session-entry-meta'
        )
    ).to_contain_text("missing on executor")
    expect(page.locator("#todo-refresh")).to_be_enabled()
    expect(page.locator("#todo-add")).to_be_enabled()
    expect(page.locator("#session-audit-refresh")).to_be_enabled()
    expect(page.locator("#session-terminate")).to_be_enabled()

    availability_override = "executor_offline"
    page.locator("#tasks-refresh").click()
    expect(page.locator("#session-detail-status")).to_have_text(
        "Unavailable · executor offline"
    )
    expect(page.locator("#todo-refresh")).to_be_enabled()
    expect(page.locator("#todo-add")).to_be_enabled()
    expect(page.locator("#session-audit-refresh")).to_be_enabled()
    expect(page.locator("#session-terminate")).to_be_disabled()

    page.unroute("**/api/ui/tasks", project_session_availability)
    page.locator("#tasks-refresh").click()
    expect(page.locator("#session-detail-status")).to_contain_text(
        "Available · "
    )
    expect(page.locator("#todo-add")).to_be_enabled()
    expect(page.locator("#session-audit-refresh")).to_be_enabled()
    expect(page.locator("#session-terminate")).to_be_enabled()

    _show_task_tab(page, "plan")
    page.locator("#todo-add").click()
    row = page.locator("#todo-list .todo-row").last
    row.locator("input").fill("verify browser todos")
    row.locator("select").nth(0).select_option("in_progress")
    row.locator("select").nth(1).select_option("high")

    page.locator("#todo-add").click()
    rows = page.locator("#todo-list .todo-row")
    expect(rows).to_have_count(2)
    second_row = rows.last
    second_row.locator("input").fill("remove browser todo")
    for selector, index in [
        ("input", 0),
        ("select", 0),
        ("select", 1),
        ("button", 0),
    ]:
        first_box = rows.nth(0).locator(selector).nth(index).bounding_box()
        second_box = rows.nth(1).locator(selector).nth(index).bounding_box()
        assert first_box is not None and second_box is not None
        assert abs(first_box["x"] - second_box["x"]) < 1
    second_row.get_by_role("button", name="Remove").click()
    expect(rows).to_have_count(1)

    page.locator("#todo-save").click()
    expect(page.locator("#todo-state")).to_contain_text(f"Saved {task_id}")
    _show_task_tab(page, "progress")
    expect(page.locator("#task-state")).to_contain_text(task_id)
    todos = harness.api("GET", f"/api/ui/todos?task_id={task_id}")
    assert todos["status"] == 200
    assert todos["payload"]["data"]["todos"][0]["content"] == (
        "verify browser todos"
    )

    audited_write = harness.api(
        "POST",
        "/tools/write_file",
        body={
            "session_id": session_id,
            "path": "audit-scope.txt",
            "content": "scope protected\n" + "payload-e2e-" * 2_000,
            "overwrite": True,
        },
    )
    assert audited_write["status"] == 200
    audited_link = harness.api(
        "POST",
        "/tools/file_link/create",
        body={
            "session_id": session_id,
            "path": "audit-scope.txt",
            "ttl_s": 60,
            "filename": None,
        },
    )
    assert audited_link["status"] == 200

    # A Live Workspace deep link restores the semantic task first and the
    # selected execution session only as optional routing context.
    deep_link_query = urlencode(
        {
            "task_id": task_id,
            "session_id": session_id,
            "executor_id": harness.executor_id,
            "workdir": ".",
            "shell_id": shell_id,
        }
    )
    page.goto(
        f"{harness.base_url}/ui?{deep_link_query}#tasks",
        wait_until="domcontentloaded",
    )
    expect(page.locator("#connection-state")).to_have_text("Connected")
    expect(
        page.locator(f'#task-list .task-entry[data-task-id="{task_id}"]')
    ).to_have_attribute("aria-current", "true")
    expect(
        page.locator(
            f'#session-list .session-entry[data-session-id="{session_id}"]'
        )
    ).to_have_attribute("aria-current", "true")

    def hide_deep_link_session(route: Route) -> None:
        response = route.fetch()
        payload = response.json()
        for task_row in payload.get("data", {}).get("tasks", []):
            task_row["sessions"] = [
                row
                for row in task_row.get("sessions", [])
                if row.get("session_id") != session_id
            ]
        route.fulfill(response=response, json=payload)

    page.route("**/api/ui/tasks", hide_deep_link_session)
    page.locator("#tasks-refresh").click()
    expect(page.locator("#session-detail-title")).to_have_text(
        "No session selected"
    )
    expect(
        page.locator('#session-list .session-entry[aria-current="true"]')
    ).to_have_count(0)
    expect(
        page.locator(f'#task-list .task-entry[data-task-id="{task_id}"]')
    ).to_have_attribute("aria-current", "true")
    expect(page.locator("#task-status")).to_have_text("active")
    page.unroute("**/api/ui/tasks", hide_deep_link_session)
    page.locator("#tasks-refresh").click()
    expect(
        page.locator(
            f'#session-list .session-entry[data-session-id="{session_id}"]'
        )
    ).to_have_attribute("aria-current", "false")
    expect(page.locator("#session-detail-title")).to_have_text(
        "No session selected"
    )
    _show_task_tab(page, "sessions")
    page.locator(
        f'#session-list .session-entry[data-session-id="{session_id}"]'
    ).click()
    expect(
        page.locator(
            f'#session-list .session-entry[data-session-id="{session_id}"]'
        )
    ).to_have_attribute("aria-current", "true")

    page.goto(
        f"{harness.base_url}/ui?{deep_link_query}#files",
        wait_until="domcontentloaded",
    )
    expect(page.locator("#connection-state")).to_have_text("Connected")
    expect(page.locator("#file-executor")).to_have_value(harness.executor_id)
    expect(page.locator("#file-path")).to_have_value(".")
    expect(page.locator("#file-state")).to_contain_text("entries")

    page.goto(
        f"{harness.base_url}/ui?{deep_link_query}#audit",
        wait_until="domcontentloaded",
    )
    expect(page.locator("#connection-state")).to_have_text("Connected")
    expect(page.locator("#audit-summary")).to_contain_text(
        f"Session · {session_id}"
    )
    expect(page.locator("#audit-state")).to_contain_text(
        f"Session · {session_id}"
    )

    page.goto(
        f"{harness.base_url}/ui?{deep_link_query}#terminals",
        wait_until="domcontentloaded",
    )
    expect(page.locator("#connection-state")).to_have_text("Connected")
    expect(page.locator("#terminal-executor")).to_have_value(
        harness.executor_id
    )
    expect(
        page.locator(f'#terminal-list .terminal-session[title*="{shell_id}"]')
    ).to_have_attribute("aria-current", "true")
    expect(page.locator("#terminal-state")).to_contain_text("Connected")

    page.goto(
        f"{harness.base_url}/ui?{deep_link_query}#tasks",
        wait_until="domcontentloaded",
    )
    expect(page.locator("#connection-state")).to_have_text("Connected")
    expect(
        page.locator(
            f'#session-list .session-entry[data-session-id="{session_id}"]'
        )
    ).to_have_attribute("aria-current", "true")

    page.locator("#session-audit-operation").select_option("files")
    page.locator("#session-audit-search").fill("write_file")
    page.locator("#session-audit-refresh").click()
    expect(
        page.locator("#session-audit-list .audit-entry").first
    ).to_be_visible()
    page.locator("#session-audit-list .audit-entry").first.click()
    expect(
        page.locator("#session-audit-detail-body .audit-call-panel")
    ).to_have_count(2)
    session_request = page.locator(
        "#session-audit-detail-body .audit-call-panel"
    ).first
    expect(session_request.locator(".audit-tree-branch").first).to_be_visible()
    session_request.get_by_role("button", name="Raw").click()
    expect(session_request.locator(".audit-detail-json")).to_be_visible()
    session_request.get_by_role("button", name="Tree").click()
    expect(session_request.locator(".audit-tree-branch").first).to_be_visible()
    session_request.get_by_role("button", name="Raw").click()
    task_list_box = page.locator("#task-list").bounding_box()
    task_workspace_box = page.locator(".tasks-workspace").bounding_box()
    assert task_list_box is not None and task_workspace_box is not None
    task_list_bottom = task_list_box["y"] + task_list_box["height"]
    task_workspace_bottom = (
        task_workspace_box["y"] + task_workspace_box["height"]
    )
    assert abs(task_list_bottom - task_workspace_bottom) < 1, (
        task_list_box,
        task_workspace_box,
    )
    expect(
        page.locator("#session-audit-detail-body .audit-call-panel").nth(0)
    ).to_contain_text("Call request")
    expect(
        page.locator("#session-audit-detail-body .audit-call-panel").nth(1)
    ).to_contain_text("Call result")
    expect(page.locator("#session-audit-detail-body")).to_contain_text(
        "audit-scope.txt"
    )
    expect(page.locator("#session-audit-detail-body")).to_contain_text(
        "payload-e2e-"
    )
    request_body = page.locator(
        "#session-audit-detail-body .audit-call-panel-body"
    ).nth(0)
    expect(request_body).to_have_attribute("tabindex", "0")
    scroll_extent = request_body.evaluate(
        "element => ({height: element.clientHeight, scrollHeight: element.scrollHeight})"
    )
    assert scroll_extent["scrollHeight"] > scroll_extent["height"]
    request_body.focus()
    expect(request_body).to_be_focused()
    request_body.press("PageDown")
    scroll_deadline = time.monotonic() + 2
    while time.monotonic() < scroll_deadline:
        if request_body.evaluate("element => element.scrollTop") > 0:
            break
        page.wait_for_timeout(50)
    else:
        raise AssertionError(
            "focused audit request body did not scroll on PageDown"
        )
    detail_style = page.locator(
        "#session-audit-detail-body .audit-detail-json"
    ).first.evaluate(
        """element => {
          const style = getComputedStyle(element);
          return {
            minHeight: style.minHeight,
            borderTopWidth: style.borderTopWidth,
            paddingTop: style.paddingTop,
          };
        }"""
    )
    assert detail_style == {
        "minHeight": "0px",
        "borderTopWidth": "0px",
        "paddingTop": "0px",
    }

    page.locator("#session-audit-operation").select_option("")
    page.locator("#session-audit-search").fill("create_file_link")
    page.locator("#session-audit-refresh").click()
    expect(
        page.locator("#session-audit-list .audit-entry").first
    ).to_be_visible()
    page.locator("#session-audit-list .audit-entry").first.click()
    page.locator(
        "#session-audit-detail-body .audit-call-panel"
    ).first.get_by_role("button", name="Raw").click()
    expect(
        page.locator("#session-audit-detail-body .audit-call-panel").nth(0)
    ).to_contain_text('"filename": null')
    expect(
        page.locator("#session-audit-detail-body .audit-call-panel").nth(1)
    ).to_contain_text("related_events")
    expect(
        page.locator("#session-audit-detail-body .audit-call-panel").nth(1)
    ).to_contain_text("download_link_created")

    harness.navigate("audit")
    expect(page).to_have_url(f"{harness.base_url}/ui#audit")
    expect(page.locator("#audit-summary")).to_contain_text("Global")
    page.locator("#audit-operation").select_option("files")
    page.locator("#audit-search").fill("write_file")
    page.locator("#audit-refresh").click()
    expect(page.locator("#audit-list .audit-entry").first).to_be_visible()
    page.locator("#audit-list .audit-entry").first.click()
    expect(page.locator("#audit-detail-body .audit-call-panel")).to_have_count(
        2
    )
    expect(
        page.locator("#audit-detail-body .audit-call-panel").nth(0)
    ).to_contain_text("Call request")
    expect(
        page.locator("#audit-detail-body .audit-call-panel").nth(1)
    ).to_contain_text("Call result")
    expect(page.locator("#audit-detail-body")).to_contain_text(
        "audit-scope.txt"
    )
    result_branches = (
        page.locator("#audit-detail-body .audit-call-panel")
        .nth(1)
        .locator("details.audit-tree-branch")
    )
    expect(result_branches.first).to_be_visible()
    if result_branches.count() > 1:
        result_branches.nth(1).locator("summary").click()
        expect(result_branches.nth(1)).to_have_attribute("open", "")

    empty_write = harness.api(
        "POST",
        "/tools/write_file",
        body={
            "session_id": session_id,
            "path": "empty-audit.txt",
            "content": "",
            "overwrite": True,
        },
    )
    assert empty_write["status"] == 200
    page.locator("#audit-search").fill("write_file")
    page.locator("#audit-refresh").click()
    expect(page.locator("#audit-list .audit-entry").first).to_be_visible()
    page.locator("#audit-list .audit-entry").first.click()
    request_text = page.locator("#audit-detail-body .audit-call-panel").nth(0)
    request_text.get_by_role("button", name="Raw").click()
    expect(request_text).to_contain_text("empty-audit.txt")
    expect(request_text).to_contain_text('"content": ""')

    page.locator("#audit-operation").select_option("")
    page.locator("#audit-search").fill("oauth_client_approved")
    page.locator("#audit-refresh").click()
    expect(page.locator("#audit-list .audit-entry").first).to_be_visible()
    page.locator("#audit-list .audit-entry").first.click()
    expect(
        page.locator("#audit-detail-body .audit-call-panel").nth(1)
    ).to_contain_text("client_id")
    expect(
        page.locator("#audit-detail-body .audit-call-panel").nth(1)
    ).not_to_contain_text("No output recorded")

    full_token = harness.api_token
    assert isinstance(full_token, str) and full_token
    read_only_token = harness.issue_token("audit:read shell:read executor:use")
    harness.set_token(read_only_token)
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#connection-state")).to_have_text("Connected")
    harness.navigate("tasks")
    expect(
        page.locator(f'#task-list .task-entry[data-task-id="{task_id}"]')
    ).to_have_attribute("aria-current", "false")
    page.locator(f'#task-list .task-entry[data-task-id="{task_id}"]').click()
    expect(
        page.locator(f'#task-list .task-entry[data-task-id="{task_id}"]')
    ).to_have_attribute("aria-current", "true")
    expect(page.locator("#session-audit-operation")).to_be_disabled()
    _show_task_tab(page, "sessions")
    page.locator(
        f'#session-list .session-entry[data-session-id="{session_id}"]'
    ).click()
    expect(page.locator("#session-audit-operation")).to_be_enabled()
    page.locator("#session-audit-operation").select_option("files")
    page.locator("#session-audit-search").fill("write_file")
    page.locator("#session-audit-refresh").click()
    expect(
        page.locator("#session-audit-list .audit-entry").first
    ).to_be_visible()
    page.locator("#session-audit-list .audit-entry").first.click()
    expect(page.locator("#session-audit-detail-meta")).to_have_text(
        "Details unavailable"
    )
    expect(page.locator("#session-audit-detail-body")).to_contain_text(
        "shell:write"
    )
    harness.console_errors = [
        line for line in harness.console_errors if "403 (Forbidden)" not in line
    ]

    harness.set_token(full_token)
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#connection-state")).to_have_text("Connected")
    harness.navigate("tasks")
    page.locator(f'#task-list .task-entry[data-task-id="{task_id}"]').click()
    expect(
        page.locator(f'#task-list .task-entry[data-task-id="{task_id}"]')
    ).to_have_attribute("aria-current", "true")
    expect(page.locator("#session-terminate")).to_be_disabled()
    _show_task_tab(page, "sessions")
    page.locator(
        f'#session-list .session-entry[data-session-id="{session_id}"]'
    ).click()
    expect(
        page.locator(
            f'#session-list .session-entry[data-session-id="{session_id}"]'
        )
    ).to_have_attribute("aria-current", "true")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#session-terminate").click()
    expect(page.locator("#session-detail-status")).to_contain_text(
        "Ended · retained history available"
    )
    expect(
        page.locator(f'#task-list .task-entry[data-task-id="{task_id}"]')
    ).to_have_attribute("aria-current", "true")
    expect(
        page.locator(
            f'#session-list .session-entry[data-session-id="{session_id}"]'
        )
    ).to_have_attribute("aria-current", "true")
    blocked = harness.api(
        "POST",
        "/tools/read",
        body={"session_id": session_id, "path": "notes.txt"},
    )
    assert blocked["status"] == 400
    assert blocked["payload"]["error"] == "validation_error"
    assert "is ended" in blocked["payload"]["message"]
    harness.console_errors = [
        line
        for line in harness.console_errors
        if "400 (Bad Request)" not in line
    ]

    # Keep browser scenarios isolated. The ended session remains a valid
    # retained Tasks deep link, but the following terminal scenario should
    # start without inheriting that explicit execution context.
    page.goto(f"{harness.base_url}/ui#overview", wait_until="domcontentloaded")
