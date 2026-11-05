"""Provider-backed AT-SPI identities must remain stable and fail closed."""

import hashlib
from types import SimpleNamespace

import pytest

from workgate.executor.gui import linux_atspi_helper as helper


def _element(
    *,
    accessible_id: str = "",
    bus: str = ":1.57",
    path: str = "/org/a11y/atspi/accessible/42",
    role: str = "push button",
    name: str = "Apply",
):
    return SimpleNamespace(
        get_accessible_id=lambda: accessible_id,
        get_role_name=lambda: role,
        get_name=lambda: name,
        app=SimpleNamespace(bus_name=bus),
        path=path,
    )


def test_accessible_id_signatures_are_unchanged(monkeypatch):
    obj = _element(accessible_id="old-id")
    monkeypatch.setattr(
        helper,
        "_bounds",
        lambda _obj: {"x": 1, "y": 2, "width": 3, "height": 4},
    )
    assert (
        helper._window_signature(obj)
        == hashlib.sha256(b"id\0push button\0old-id").hexdigest()[:12]
    )
    assert (
        helper._element_signature(obj)
        == hashlib.sha256(
            "\0".join(
                ("old-id", "push button", "Apply", "1", "2", "3", "4")
            ).encode()
        ).hexdigest()[:16]
    )


def test_provider_bus_and_path_supply_identity_without_accessible_id(
    monkeypatch,
):
    obj = _element()
    monkeypatch.setattr(
        helper,
        "_bounds",
        lambda _obj: {"x": 1, "y": 2, "width": 3, "height": 4},
    )
    identity = helper._provider_identity(obj, helper.GUI_MAX_ELEMENT_TEXT_BYTES)
    assert identity == "object\0:1.57\0/org/a11y/atspi/accessible/42"
    assert helper._window_signature(obj)
    assert helper._element_signature(obj)
    helper._validate_locator_identity(obj, identity, "")


@pytest.mark.parametrize(
    ("bus", "path"),
    [
        ("", "/org/a11y/atspi/accessible/42"),
        (":1.57", ""),
        ("x" * 1025, "/org/a11y/atspi/accessible/42"),
        (":1.57", "/" + "界" * 350),
    ],
)
def test_unstable_or_unbounded_provider_is_rejected(bus, path):
    obj = _element(bus=bus, path=path)
    assert (
        helper._provider_identity(obj, helper.GUI_MAX_ELEMENT_TEXT_BYTES)
        is None
    )
    assert helper._window_signature(obj) is None
    assert helper._element_signature(obj) is None


def test_provider_locator_refuses_changed_bus_or_path(monkeypatch):
    current = _element()
    monkeypatch.setattr(
        helper, "_resolve_window", lambda _id: (None, object(), 0)
    )
    monkeypatch.setattr(helper, "_resolve_path", lambda _window, _path: current)
    monkeypatch.setattr(
        helper,
        "_bounds",
        lambda _obj: {"x": 1, "y": 2, "width": 3, "height": 4},
    )
    identity = helper._provider_identity(
        current, helper.GUI_MAX_ELEMENT_TEXT_BYTES
    )
    payload = {
        "window_id": "atspi:1:abc",
        "locator": {
            "path": [0],
            "identity": identity,
            "accessible_id": None,
            "fingerprint": helper._element_signature(current),
        },
    }
    assert helper._resolve_locator(payload)["bounds"]["width"] == 3
    current.path = "/org/a11y/atspi/accessible/99"
    with pytest.raises(LookupError, match="changed since observation"):
        helper._resolve_locator(payload)
    current.path = "/org/a11y/atspi/accessible/42"
    current.app.bus_name = ":1.99"
    with pytest.raises(LookupError, match="changed since observation"):
        helper._resolve_locator(payload)


def test_legacy_locator_without_identity_remains_valid(monkeypatch):
    obj = _element(accessible_id="existing-id")
    monkeypatch.setattr(
        helper, "_resolve_window", lambda _id: (None, object(), 0)
    )
    monkeypatch.setattr(helper, "_resolve_path", lambda _window, _path: obj)
    monkeypatch.setattr(
        helper,
        "_bounds",
        lambda _obj: {"x": 1, "y": 2, "width": 3, "height": 4},
    )
    payload = {
        "window_id": "atspi:1:abc",
        "locator": {
            "path": [0],
            "accessible_id": "existing-id",
            "fingerprint": helper._element_signature(obj),
        },
    }
    assert helper._resolve_locator(payload)["bounds"]["width"] == 3
    obj.get_accessible_id = lambda: "replacement-id"
    with pytest.raises(LookupError, match="changed since observation"):
        helper._resolve_locator(payload)


def test_missing_legacy_identity_never_grants_authority():
    with pytest.raises(LookupError, match="changed since observation"):
        helper._validate_locator_identity(_element(), "", "")


def test_snapshot_locator_allows_provider_only_semantic_click(monkeypatch):
    clicked = []
    element = _element()
    element.get_child_count = lambda: 0
    element.get_action_iface = lambda: SimpleNamespace(
        get_n_actions=lambda: 1,
        get_action_name=lambda _index: "click",
        do_action=lambda index: clicked.append(index) or True,
    )
    window = _element(role="frame", name="Main")
    window.get_child_count = lambda: 1
    window.get_child_at_index = lambda index: element if index == 0 else None
    window.get_action_iface = lambda: None
    app = SimpleNamespace(get_process_id=lambda: 42, get_name=lambda: "Example")
    monkeypatch.setattr(helper, "_resolve_window", lambda _id: (app, window, 0))
    monkeypatch.setattr(helper, "_state", lambda _obj, _state: False)
    monkeypatch.setattr(
        helper,
        "Atspi",
        SimpleNamespace(
            StateType=SimpleNamespace(ENABLED=1, FOCUSED=2, EDITABLE=3)
        ),
    )
    monkeypatch.setattr(
        helper,
        "_bounds",
        lambda _obj: {"x": 1, "y": 2, "width": 3, "height": 4},
    )
    observed = helper._snapshot({"window_id": "atspi:42:example"})
    # The child is e2; the window itself is e1.
    locator = observed["locators"]["e2"]
    assert locator["accessible_id"] is None
    assert locator["identity"] == "object\0:1.57\0/org/a11y/atspi/accessible/42"
    assert (
        helper._resolve_locator(
            {"window_id": "atspi:42:example", "locator": locator}
        )["bounds"]["width"]
        == 3
    )
    result = helper._semantic_action(
        {
            "window_id": "atspi:42:example",
            "locator": locator,
            "action": {"type": "click"},
        }
    )
    assert result["semantic"] is True
    assert clicked == [0]
