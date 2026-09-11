from pathlib import Path
from types import SimpleNamespace

import pytest

import workgate.telemetry.system as telemetry_module


@pytest.fixture(autouse=True)
def _reset_samples():
    original_cpu = telemetry_module._CPU_SAMPLE
    original_network = telemetry_module._NETWORK_SAMPLE
    telemetry_module._CPU_SAMPLE = None
    telemetry_module._NETWORK_SAMPLE = None
    yield
    telemetry_module._CPU_SAMPLE = original_cpu
    telemetry_module._NETWORK_SAMPLE = original_network


def test_percent_bounds_and_rejects_zero_total() -> None:
    assert telemetry_module._percent(5, 10) == 50.0
    assert telemetry_module._percent(-5, 10) == 0.0
    assert telemetry_module._percent(50, 10) == 100.0
    assert telemetry_module._percent(1, 0) is None


def test_local_system_snapshot_uses_interval_cpu_and_network_samples(
    monkeypatch, tmp_path: Path
) -> None:
    telemetry_module._CPU_SAMPLE = (100, 40)
    telemetry_module._NETWORK_SAMPLE = (10.0, 1_000, 2_000)
    monkeypatch.setattr(
        telemetry_module, "_read_linux_cpu_times", lambda: (200, 70)
    )
    monkeypatch.setattr(
        telemetry_module, "_read_linux_memory", lambda: (1_000, 500)
    )
    monkeypatch.setattr(
        telemetry_module, "_read_linux_network", lambda: (3_000, 5_000)
    )
    monkeypatch.setattr(telemetry_module.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(telemetry_module.time, "monotonic", lambda: 12.0)
    monkeypatch.setattr(telemetry_module.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(
        telemetry_module.os,
        "getloadavg",
        lambda: (2.0, 1.0, 0.5),
        raising=False,
    )
    monkeypatch.setattr(
        telemetry_module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=10_000, used=4_000, free=6_000),
    )
    monkeypatch.setattr(
        telemetry_module.Path,
        "read_text",
        lambda self, encoding="utf-8": "123.5 0\n",
    )

    snapshot = telemetry_module.local_system_snapshot(tmp_path)

    assert snapshot["cpu_percent"] == 70.0
    assert snapshot["cpu_count"] == 8
    assert snapshot["memory_percent"] == 50.0
    assert snapshot["memory_used_bytes"] == 500
    assert snapshot["memory_total_bytes"] == 1_000
    assert snapshot["disk_percent"] == 40.0
    assert snapshot["network_rx_bps"] == 1_000.0
    assert snapshot["network_tx_bps"] == 1_500.0
    assert snapshot["load_1m"] == 2.0
    assert snapshot["uptime_s"] == 124


def test_local_system_snapshot_preserves_missing_metrics(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(telemetry_module, "_read_linux_cpu_times", lambda: None)
    monkeypatch.setattr(telemetry_module, "_read_linux_memory", lambda: None)
    monkeypatch.setattr(telemetry_module, "_read_linux_network", lambda: None)
    monkeypatch.setattr(telemetry_module.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(
        telemetry_module.os,
        "getloadavg",
        lambda: (_ for _ in ()).throw(OSError("missing")),
        raising=False,
    )
    monkeypatch.setattr(
        telemetry_module.shutil,
        "disk_usage",
        lambda path: (_ for _ in ()).throw(OSError("missing")),
    )
    monkeypatch.setattr(
        telemetry_module.Path,
        "read_text",
        lambda self, encoding="utf-8": (_ for _ in ()).throw(
            OSError("missing")
        ),
    )

    snapshot = telemetry_module.local_system_snapshot(tmp_path)

    assert snapshot["cpu_percent"] is None
    assert snapshot["memory_percent"] is None
    assert snapshot["disk_percent"] is None
    assert snapshot["network_rx_bps"] is None
    assert snapshot["network_tx_bps"] is None
    assert snapshot["load_1m"] is None
    assert snapshot["uptime_s"] >= 0


def test_linux_proc_readers_parse_and_filter_rows(monkeypatch) -> None:
    payloads = {
        "/proc/stat": "cpu 10 20 30 40 5\n",
        "/proc/meminfo": (
            "Ignored line\nMemTotal: 100 kB\nMemAvailable: 30 kB\n"
        ),
        "/proc/net/dev": (
            "header\nheader\n"
            "lo: 1 0 0 0 0 0 0 0 2 0 0 0 0 0 0 0\n"
            "broken row\n"
            "bad: nope 0 0 0 0 0 0 0 nope 0 0 0 0 0 0 0\n"
            "eth0: 100 0 0 0 0 0 0 0 200 0 0 0 0 0 0 0\n"
            "wlan0: 300 0 0 0 0 0 0 0 400 0 0 0 0 0 0 0\n"
        ),
    }

    def fake_read_text(path: Path, encoding: str = "utf-8") -> str:  # noqa: ARG001
        return payloads[path.as_posix()]

    monkeypatch.setattr(telemetry_module.Path, "read_text", fake_read_text)

    assert telemetry_module._read_linux_cpu_times() == (105, 45)
    assert telemetry_module._read_linux_memory() == (102_400, 71_680)
    assert telemetry_module._read_linux_network() == (400, 600)


def test_linux_proc_readers_reject_unusable_inputs(monkeypatch) -> None:
    monkeypatch.setattr(
        telemetry_module.Path,
        "read_text",
        lambda self, encoding="utf-8": "not-cpu 1 2 3 4\n",
    )
    assert telemetry_module._read_linux_cpu_times() is None

    monkeypatch.setattr(
        telemetry_module.Path,
        "read_text",
        lambda self, encoding="utf-8": "cpu 1 2 bad 4\n",
    )
    assert telemetry_module._read_linux_cpu_times() is None

    monkeypatch.setattr(
        telemetry_module.Path,
        "read_text",
        lambda self, encoding="utf-8": "MemFree: 5 kB\n",
    )
    assert telemetry_module._read_linux_memory() is None

    monkeypatch.setattr(
        telemetry_module.Path,
        "read_text",
        lambda self, encoding="utf-8": "header\nheader\nlo: 1 2 3\n",
    )
    assert telemetry_module._read_linux_network() is None

    monkeypatch.setattr(
        telemetry_module.Path,
        "read_text",
        lambda self, encoding="utf-8": (_ for _ in ()).throw(
            OSError("missing")
        ),
    )
    assert telemetry_module._read_linux_cpu_times() is None
    assert telemetry_module._read_linux_memory() is None
    assert telemetry_module._read_linux_network() is None


def test_local_system_snapshot_records_first_samples_and_uses_load_fallback(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        telemetry_module, "_read_linux_cpu_times", lambda: (100, 20)
    )
    monkeypatch.setattr(
        telemetry_module, "_read_linux_network", lambda: (10, 20)
    )
    monkeypatch.setattr(telemetry_module, "_read_linux_memory", lambda: None)
    monkeypatch.setattr(telemetry_module.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(telemetry_module.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(telemetry_module.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(
        telemetry_module.os,
        "getloadavg",
        lambda: (2.0, 0.0, 0.0),
        raising=False,
    )
    monkeypatch.setattr(
        telemetry_module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=100, used=25, free=75),
    )
    monkeypatch.setattr(
        telemetry_module.Path,
        "read_text",
        lambda self, encoding="utf-8": "not-a-number\n",
    )
    monkeypatch.setattr(telemetry_module, "_PROCESS_STARTED_AT", 900.0)

    snapshot = telemetry_module.local_system_snapshot(tmp_path)

    assert snapshot["cpu_percent"] == 50.0
    assert snapshot["network_rx_bps"] is None
    assert snapshot["network_tx_bps"] is None
    assert snapshot["uptime_s"] == 100
    assert telemetry_module._CPU_SAMPLE == (100, 20)
    assert telemetry_module._NETWORK_SAMPLE == (10.0, 10, 20)


def test_local_system_snapshot_ignores_nonpositive_sample_intervals(
    monkeypatch, tmp_path: Path
) -> None:
    telemetry_module._CPU_SAMPLE = (100, 40)
    telemetry_module._NETWORK_SAMPLE = (10.0, 100, 200)
    monkeypatch.setattr(
        telemetry_module, "_read_linux_cpu_times", lambda: (90, 30)
    )
    monkeypatch.setattr(
        telemetry_module, "_read_linux_network", lambda: (80, 150)
    )
    monkeypatch.setattr(
        telemetry_module, "_read_linux_memory", lambda: (100, 120)
    )
    monkeypatch.setattr(telemetry_module.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(telemetry_module.os, "cpu_count", lambda: None)
    monkeypatch.setattr(
        telemetry_module.os,
        "getloadavg",
        lambda: (3.0, 0.0, 0.0),
        raising=False,
    )
    monkeypatch.setattr(
        telemetry_module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=0, used=0, free=0),
    )
    monkeypatch.setattr(
        telemetry_module.Path,
        "read_text",
        lambda self, encoding="utf-8": "1 0\n",
    )

    snapshot = telemetry_module.local_system_snapshot(tmp_path)

    assert snapshot["cpu_count"] == 1
    assert snapshot["cpu_percent"] == 100.0
    assert snapshot["memory_percent"] == 100.0
    assert snapshot["disk_percent"] is None
    assert snapshot["network_rx_bps"] is None
    assert snapshot["network_tx_bps"] is None
