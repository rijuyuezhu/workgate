"""Real Chromium tests for the standalone Executor topology page.

Graph layout/evidence are driven by synthetic inventory to exercise 2, 5 and
many Executors without creating fake live Executor services; peer RPC and OAuth
are independently exercised by tests/control/test_peer_links.py.
"""

import copy
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Route, expect

from tests.e2e.browser.harness import BrowserHarness

pytestmark = [pytest.mark.browser, pytest.mark.timeout(180)]


def graph_fixture(harness: BrowserHarness, size: int):
    base = harness.api("GET", "/api/ui/executors")
    assert base["status"] == 200, base
    original = base["payload"]["data"]["executors"]
    assert original
    rows = []
    for i in range(size):
        row = copy.deepcopy(original[0])
        # Keep the primary genuine Executor selected so unrelated management
        # polling does not target a synthetic, unpaired Executor ID.
        row["executor_id"] = (
            original[0]["executor_id"]
            if i == 0
            else f"exec_browser_topology_{i:03d}"
        )
        row["name"] = f"Node {chr(65 + i)}" if i < 26 else f"Node {i + 1}"
        row["online"] = (
            bool(original[0]["online"]) if i == 0 else i != 1 or size == 2
        )
        row["revoked_at"] = None
        rows.append(row)
    measurements = {}
    methods = []

    def inventory(route: Route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "data": {"executors": rows}}),
        )

    def evidence(route: Route):
        request = route.request
        methods.append(request.method)
        if request.method == "GET":
            query = parse_qs(urlsplit(request.url).query)
            src = query["source_executor_id"][0]
            dst = query["destination_executor_id"][0]
            result = measurements.get((src, dst), "untested")
            status = result["status"] if isinstance(result, dict) else result
            data = {
                "source_executor_id": src,
                "destination_executor_id": dst,
                "status": status,
                "directional": True,
                "tested_at": result["tested_at"]
                if isinstance(result, dict)
                else datetime.now(UTC).isoformat()
                if status != "untested"
                else None,
                "reason": "peer_handshake_failed"
                if status == "unreachable"
                else None,
                "fallback": "control_relay_not_tested"
                if status == "unreachable"
                else None,
            }
        else:
            payload = request.post_data_json
            assert isinstance(payload, dict)
            src = payload["source_executor_id"]
            dst = payload["destination_executor_id"]
            # Deliberately asymmetric evidence.
            status = (
                "peer_succeeded"
                if src == rows[0]["executor_id"]
                else "unreachable"
            )
            measurements[(src, dst)] = status
            data = {
                "status": status,
                "source_executor_id": src,
                "destination_executor_id": dst,
                "directional": True,
                "tested_at": datetime.now(UTC).isoformat(),
                "reason": "peer_handshake_failed"
                if status == "unreachable"
                else None,
                "fallback": "control_relay_not_tested"
                if status == "unreachable"
                else None,
            }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "data": data}),
        )

    page = harness.page
    page.route("**/api/ui/executors/peer-link?*", evidence)
    page.route("**/api/ui/executors/peer-link", evidence)
    page.route("**/api/ui/executors", inventory)
    return rows, measurements, methods


def open_graph(harness: BrowserHarness, size: int):
    harness.wait_executor_online()
    rows, measurements, methods = graph_fixture(harness, size)
    page = harness.page
    harness.navigate("executors")
    page.locator("#executor-topology-open").click()
    expect(page.locator("#executor-topology")).to_be_visible()
    expect(page.locator("#executors-panel .executors-layout")).to_be_hidden()
    expect(page.locator("#topology-nodes .topology-node")).to_have_count(size)
    assert "Topology" not in page.locator("#executor-subtabs").inner_text()
    return rows, measurements, methods


def assert_no_node_overlaps(page):
    overlaps = page.locator("#topology-nodes .topology-node").evaluate_all(
        """nodes => {
          const boxes = nodes.map(node => node.getBoundingClientRect());
          const result = [];
          for (let i = 0; i < boxes.length; i++) {
            for (let j = i + 1; j < boxes.length; j++) {
              const x = Math.min(boxes[i].right, boxes[j].right) - Math.max(boxes[i].left, boxes[j].left);
              const y = Math.min(boxes[i].bottom, boxes[j].bottom) - Math.max(boxes[i].top, boxes[j].top);
              if (x > 3 && y > 3) result.push([i, j, x, y]);
            }
          }
          return result;
        }"""
    )
    assert not overlaps, overlaps


def test_two_node_topology_complete_edge_and_explicit_directional_probe(
    browser_harness: BrowserHarness,
):
    h = browser_harness
    h.login()
    rows, _, methods = open_graph(h, 2)
    page = h.page
    edges = page.locator("#topology-edges g[role=button]")
    expect(edges).to_have_count(1)
    expect(edges).to_have_attribute("data-status", "untested")
    expect(page.locator("#topology-graph")).to_have_attribute(
        "data-layout", "all"
    )
    assert "POST" not in methods

    edges.focus()
    edges.press("Enter")
    expect(page.locator("#topology-inspector")).to_be_visible()
    expect(
        page.locator("#topology-direction-rows .topology-direction")
    ).to_have_count(2)
    expect(page.locator("#topology-pair-status")).to_have_text("Not tested")
    expect(page.locator(".topology-legend")).to_contain_text("Direct OK")
    expect(page.locator(".topology-legend")).to_contain_text(
        "Failed or blocked"
    )
    assert "·" not in page.locator("#topology-inspector").inner_text()
    page.screenshot(path=str(h.artifacts / "topology-2-desktop.png"))

    page.get_by_role(
        "button",
        name=f"Test connection from {rows[0]['name']} to {rows[1]['name']}",
    ).click()
    expect(page.locator("#topology-pair-status")).to_have_text(
        "Direct OK one way"
    )
    assert methods.count("POST") == 1
    expect(page.locator("#topology-edges g")).to_have_attribute(
        "data-status", "one-way"
    )
    page.get_by_role(
        "button",
        name=f"Test connection from {rows[1]['name']} to {rows[0]['name']}",
    ).click()
    expect(page.locator("#topology-pair-status")).to_have_text(
        "Direct OK one way"
    )
    expect(page.locator("#topology-direction-rows")).to_contain_text(
        "Relay not tested"
    )
    assert methods.count("POST") == 2
    # A pending probe cannot leave its status message on a newly focused node.
    page.evaluate("""() => {
      document.querySelector('#topology-direction-rows button').click();
      document.querySelector('#topology-nodes button').click();
    }""")
    expect(page.locator("#topology-inspector")).to_be_hidden()
    expect(page.locator("#topology-feedback")).to_have_text("")
    page.wait_for_timeout(350)
    expect(page.locator("#topology-feedback")).to_have_text("")
    assert methods.count("POST") == 3
    # The dropdown works even when an SVG edge is hard to hit visually.
    page.locator("#topology-source").select_option(rows[0]["executor_id"])
    page.locator("#topology-destination").select_option(rows[1]["executor_id"])
    page.locator("#topology-show-pair").click()
    expect(page.locator("#topology-inspector")).to_be_visible()
    page.screenshot(path=str(h.artifacts / "topology-2-tested.png"))
    # Time passing must retire positive evidence without another GET/probe.
    page.evaluate("""() => {
      const now = Date.now();
      Date.now = () => now + 301000;
    }""")
    expect(page.locator("#topology-pair-status")).to_have_text(
        "Expired result", timeout=10000
    )
    assert methods.count("POST") == 3
    page.locator("#topology-back").click()
    expect(page.locator("#executor-topology")).to_be_hidden()
    expect(page.locator("#executors-panel .executors-layout")).to_be_visible()
    h.assert_clean_browser()


def test_five_nodes_all_ten_pairs_even_offline_unprobed(
    browser_harness: BrowserHarness,
):
    h = browser_harness
    h.login()
    rows, measurements, methods = open_graph(h, 5)
    page = h.page
    edges = page.locator("#topology-edges g[role=button]")
    expect(edges).to_have_count(10)
    assert (
        page.locator(
            "#topology-nodes .topology-node[data-online=false]"
        ).count()
        == 1
    )
    expect(edges.filter(has=page.locator("title"))).to_have_count(10)
    assert all(
        edges.nth(i).get_attribute("data-status") == "untested"
        for i in range(10)
    )
    assert methods.count("POST") == 0
    expect(page.locator("#topology-summary")).to_contain_text("10 pairs")
    assert "·" not in page.locator("#topology-summary").inner_text()
    page.screenshot(path=str(h.artifacts / "topology-5-desktop.png"))
    edges.first.click()
    expect(page.locator("#topology-inspector")).to_be_visible()
    expect(edges).to_have_count(10)
    expect(page.locator("#topology-edges g.selected")).to_have_count(1)
    page.screenshot(path=str(h.artifacts / "topology-5-selected.png"))
    assert methods.count("POST") == 0

    page.locator("#topology-source").select_option(rows[0]["executor_id"])
    page.locator("#topology-destination").select_option(rows[-1]["executor_id"])
    page.locator("#topology-show-pair").click()
    expect(edges).to_have_count(1)
    expect(page.locator("#topology-inspector")).to_be_visible()
    measurements[(rows[0]["executor_id"], rows[-1]["executor_id"])] = "stale"
    measurements[(rows[-1]["executor_id"], rows[0]["executor_id"])] = (
        "unreachable"
    )
    page.locator("#topology-refresh").click()
    expect(edges.first).to_have_attribute("data-status", "stale")
    page.locator("#topology-reset").click()
    expect(edges).to_have_count(10)
    assert methods.count("POST") == 0
    h.assert_clean_browser()


def test_probe_result_beats_a_pending_old_get_and_keyboard_focus_survives(
    browser_harness: BrowserHarness,
):
    h = browser_harness
    h.login()
    rows, _, methods = open_graph(h, 2)
    page = h.page
    # Wait until the initial evidence has been read before intercepting a refresh.
    page.wait_for_function(
        """() => document.querySelector('#topology-edges g')?.dataset.status === 'untested'"""
    )
    page.evaluate(
        """({from, to}) => {
          const original = window.fetch.bind(window);
          window.__heldEvidence = {pending: false, release: null};
          window.fetch = async (...args) => {
            const url = new URL(String(args[0]), location.href);
            if (url.pathname.endsWith('/executors/peer-link') &&
                url.searchParams.get('source_executor_id') === from &&
                url.searchParams.get('destination_executor_id') === to) {
              // Snapshot cached evidence before the explicit POST.
              const oldResponse = await original(...args);
              window.__heldEvidence.pending = true;
              return new Promise(resolve => {
                window.__heldEvidence.release = () => resolve(oldResponse);
              });
            }
            return original(...args);
          };
        }""",
        {"from": rows[0]["executor_id"], "to": rows[1]["executor_id"]},
    )
    edge = page.locator("#topology-edges g[role=button]").first
    edge.focus()
    edge.press("Enter")
    page.locator("#topology-refresh").click()
    page.wait_for_function("window.__heldEvidence?.pending === true")
    test = page.get_by_role(
        "button",
        name=f"Test connection from {rows[0]['name']} to {rows[1]['name']}",
    )
    test.focus()
    test.press("Enter")
    expect(page.locator("#topology-pair-status")).to_have_text(
        "Direct OK one way"
    )
    expect(test).to_be_focused()
    page.evaluate("window.__heldEvidence.release()")
    page.wait_for_timeout(250)
    expect(page.locator("#topology-pair-status")).to_have_text(
        "Direct OK one way"
    )
    expect(test).to_be_focused()
    assert methods.count("POST") == 1
    h.assert_clean_browser()


def test_inflight_probe_survives_closing_topology_without_stale_notice(
    browser_harness: BrowserHarness,
):
    h = browser_harness
    h.login()
    rows, _, methods = open_graph(h, 2)
    page = h.page
    page.evaluate("""() => {
      const original = window.fetch.bind(window);
      window.__heldPost = {pending: false, release: null};
      window.fetch = async (...args) => {
        if (new URL(String(args[0]), location.href).pathname.endsWith('/executors/peer-link') &&
            String(args[1]?.method || 'GET').toUpperCase() === 'POST') {
          const response = await original(...args);
          window.__heldPost.pending = true;
          return new Promise(resolve => {
            window.__heldPost.release = () => resolve(response);
          });
        }
        return original(...args);
      };
    }""")
    edge = page.locator("#topology-edges g[role=button]").first
    edge.focus()
    edge.press("Enter")
    page.get_by_role(
        "button",
        name=f"Test connection from {rows[0]['name']} to {rows[1]['name']}",
    ).click()
    page.wait_for_function("window.__heldPost?.pending === true")
    page.locator("#topology-back").click()
    page.locator("#executor-topology-open").click()
    expect(page.locator("#topology-pair-status")).to_have_text("Not tested")
    page.evaluate("window.__heldPost.release()")
    expect(page.locator("#topology-pair-status")).to_have_text(
        "Direct OK one way"
    )
    expect(page.locator("#topology-feedback")).to_have_text("")
    assert methods.count("POST") == 1
    h.assert_clean_browser()


def test_direction_expires_even_if_pair_summary_does_not_change(
    browser_harness: BrowserHarness,
):
    h = browser_harness
    h.login()
    rows, measurements, methods = open_graph(h, 2)
    a, b = rows[0]["executor_id"], rows[1]["executor_id"]
    measurements[a, b] = {
        "status": "peer_succeeded",
        "tested_at": (datetime.now(UTC) - timedelta(seconds=5)).isoformat(),
    }
    measurements[b, a] = {
        "status": "unreachable",
        "tested_at": (datetime.now(UTC) - timedelta(seconds=299)).isoformat(),
    }
    page = h.page
    edge = page.locator("#topology-edges g[role=button]").first
    edge.focus()
    edge.press("Enter")
    page.locator("#topology-refresh").click()
    states = page.locator("#topology-direction-rows .topology-direction-status")
    expect(states).to_have_text(["Direct OK", "Failed"])
    expect(page.locator("#topology-pair-status")).to_have_text(
        "Direct OK one way"
    )
    page.evaluate("""() => {
      const now = Date.now();
      Date.now = () => now + 3000;
    }""")
    # Inventory polling must update the inspector despite unchanged pair status.
    expect(states).to_have_text(["Direct OK", "Expired"], timeout=10000)
    expect(page.locator("#topology-pair-status")).to_have_text(
        "Direct OK one way"
    )
    assert methods.count("POST") == 0
    h.assert_clean_browser()


@pytest.mark.parametrize(
    ("denied_status", "reason"),
    [(403, "Permission denied"), (429, "Probe rate limit reached")],
)
def test_bulk_stops_on_permission_denied_or_rate_limit(
    browser_harness: BrowserHarness,
    denied_status: int,
    reason: str,
):
    h = browser_harness
    h.login()
    open_graph(h, 5)
    page = h.page
    attempts = []

    def deny(route: Route):
        attempts.append(route.request.method)
        route.fulfill(
            status=denied_status,
            content_type="application/json",
            body=json.dumps({"ok": False, "message": reason}),
        )

    page.route("**/api/ui/executors/peer-link", deny)
    page.locator("#topology-test-all").click()
    expect(page.locator("#topology-test-progress")).to_have_text(
        f"Stopped: 1/20 directions, 1 could not be tested ({reason})."
    )
    assert attempts == ["POST"]
    expect(page.locator("#topology-test-all")).to_be_enabled()
    # Chromium logs intentional 4xx API responses as console resource errors.
    assert all(
        f"status of {denied_status}" in line for line in h.console_errors
    )
    h.console_errors.clear()
    h.assert_clean_browser()


def test_bulk_tests_every_visible_direction_only_after_explicit_click(
    browser_harness: BrowserHarness,
):
    h = browser_harness
    h.login()
    rows, _, methods = open_graph(h, 5)
    page = h.page
    bulk = page.locator("#topology-test-all")
    expect(bulk).to_be_enabled()
    assert methods.count("POST") == 0
    bulk.click()
    expect(page.locator("#topology-test-progress")).to_have_text(
        "Finished: 20/20 directions.", timeout=30000
    )
    assert methods.count("POST") == 20
    expect(bulk).to_have_text("Test all links")
    expect(page.locator("#topology-edges g[role=button]")).to_have_count(10)
    page.locator("#topology-source").select_option(rows[0]["executor_id"])
    page.locator("#topology-destination").select_option(rows[1]["executor_id"])
    page.locator("#topology-show-pair").click()
    expect(page.locator("#topology-edges g[role=button]")).to_have_count(1)
    assert methods.count("POST") == 20
    h.assert_clean_browser()


def test_bulk_stop_and_sparse_fleet_only_tests_visible_links(
    browser_harness: BrowserHarness,
):
    h = browser_harness
    h.login()
    _rows, _, methods = open_graph(h, 15)
    page = h.page
    bulk = page.locator("#topology-test-all")
    expect(bulk).to_be_disabled()
    assert methods.count("POST") == 0
    page.locator("#topology-nodes .topology-node").first.click()
    expect(page.locator("#topology-edges g[role=button]")).to_have_count(14)
    expect(bulk).to_be_enabled()
    page.evaluate("""() => {
      const original = window.fetch.bind(window);
      window.__bulkHeld = {pending: false, release: null};
      window.fetch = async (...args) => {
        if (new URL(String(args[0]), location.href).pathname.endsWith('/executors/peer-link') &&
            String(args[1]?.method || 'GET').toUpperCase() === 'POST') {
          const response = await original(...args);
          window.__bulkHeld.pending = true;
          return new Promise(resolve => {
            window.__bulkHeld.release = () => resolve(response);
          });
        }
        return original(...args);
      };
    }""")
    bulk.click()
    page.wait_for_function("window.__bulkHeld?.pending === true")
    expect(bulk).to_have_text("Stop")
    bulk.click()
    expect(bulk).to_have_text("Stopping…")
    page.evaluate("window.__bulkHeld.release()")
    expect(page.locator("#topology-test-progress")).to_have_text(
        "Stopped: 1/28 directions."
    )
    assert methods.count("POST") == 1
    page.locator("#topology-reset").click()
    expect(bulk).to_be_disabled()
    assert methods.count("POST") == 1
    h.assert_clean_browser()


def test_many_nodes_default_sparse_focus_pair_keyboard_and_mobile(
    browser_harness: BrowserHarness,
):
    h = browser_harness
    h.login()
    rows, _, methods = open_graph(h, 15)
    page = h.page
    edges = page.locator("#topology-edges g[role=button]")
    expect(page.locator("#topology-graph")).to_have_attribute(
        "data-layout", "grid"
    )
    expect(edges).to_have_count(0)
    assert methods == []
    page.screenshot(path=str(h.artifacts / "topology-15-overview.png"))

    node = page.locator("#topology-node-missing")
    assert node.count() == 0
    page.locator("#topology-nodes .topology-node").first.click()
    expect(page.locator("#topology-graph")).to_have_attribute(
        "data-layout", "focus"
    )
    expect(edges).to_have_count(14)
    assert methods.count("POST") == 0
    page.screenshot(path=str(h.artifacts / "topology-15-focus.png"))
    # Every node must remain individually selectable; neither radial ring
    # may cover another Executor button.
    assert_no_node_overlaps(page)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(edges).to_have_count(14)
    assert_no_node_overlaps(page)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    page.screenshot(path=str(h.artifacts / "topology-15-mobile-focus.png"))
    page.set_viewport_size({"width": 1440, "height": 1000})
    # Inventory refresh must preserve focus and selected pair.
    page.locator("#executor-topology-open").is_visible()
    page.locator("#topology-refresh").click()
    expect(edges).to_have_count(14)
    page.wait_for_timeout(4200)
    expect(page.locator("#topology-graph")).to_have_attribute(
        "data-layout", "focus"
    )
    expect(edges).to_have_count(14)
    page.locator("#topology-source").select_option(rows[1]["executor_id"])
    page.locator("#topology-destination").select_option(rows[6]["executor_id"])
    page.locator("#topology-show-pair").click()
    expect(edges).to_have_count(1)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(edges).to_have_count(1)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    page.screenshot(path=str(h.artifacts / "topology-15-mobile-pair.png"))
    page.locator("#topology-reset").click()
    expect(page.locator("#topology-graph")).to_have_attribute(
        "data-layout", "grid"
    )
    assert methods.count("POST") == 0
    page.locator("#topology-back").click()
    expect(page.locator("#executor-topology")).to_be_hidden()
    h.assert_clean_browser()


def test_thirty_five_nodes_compact_initial_view_and_phone_focus(
    browser_harness: BrowserHarness,
):
    harness = browser_harness
    harness.login()
    _rows, _measurements, methods = open_graph(harness, 35)
    page = harness.page
    edges = page.locator("#topology-edges g[role=button]")
    expect(page.locator("#topology-graph")).to_have_attribute(
        "data-layout", "grid"
    )
    expect(edges).to_have_count(0)
    assert methods == []
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#topology-nodes .topology-node").first.click()
    expect(edges).to_have_count(34)
    expect(page.locator("#topology-graph")).to_have_attribute(
        "data-layout", "focus"
    )
    expect(page.locator("#topology-graph")).to_have_attribute(
        "data-scrollable", "true"
    )
    assert page.locator("#topology-graph").evaluate(
        "el => el.scrollWidth > el.clientWidth"
    )
    assert page.locator("#topology-graph").evaluate("el => el.scrollLeft > 0")
    assert_no_node_overlaps(page)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    assert methods.count("POST") == 0
    page.locator("#topology-stage").screenshot(
        path=str(harness.artifacts / "topology-35-full-stage.png")
    )
    page.screenshot(
        path=str(harness.artifacts / "topology-35-mobile-focus.png"),
        full_page=True,
    )
    harness.assert_clean_browser()
