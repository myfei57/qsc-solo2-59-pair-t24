"""命令行、演示流程与运行期计数。"""

from __future__ import annotations

import pytest

from batterystore.cli import build_parser, main
from batterystore.errors import StartupSelfCheckError


def test_cli_parser_exposes_subcommands():
    parser = build_parser()
    names = set(parser._subparsers._group_actions[0].choices)  # noqa: SLF001 - 解析器自检
    assert {"serve", "demo", "status", "config", "alerts", "startup-check", "reset"} <= names


def test_cli_config_and_status(capsys, tmp_path):
    storage = str(tmp_path / "state")
    assert main(["--storage-root", storage, "config"]) == 0
    assert "limits" in capsys.readouterr().out
    assert main(["--storage-root", storage, "status"]) == 0
    assert "station1" in capsys.readouterr().out
    assert main(["--storage-root", storage, "alerts"]) == 0
    assert "alerts" in capsys.readouterr().out
    assert main(["--storage-root", storage, "startup-check"]) == 0
    assert "policy" in capsys.readouterr().out
    assert main(["--storage-root", storage, "reset"]) == 0


def test_cli_demo_runs_end_to_end(capsys, tmp_path):
    code = main(
        [
            "--storage-root",
            str(tmp_path / "state"),
            "demo",
            "--fast",
            "--racks",
            "2",
            "--rounds",
            "8",
        ]
    )
    assert code == 0, capsys.readouterr().err
    assert "concurrency" in capsys.readouterr().out


def test_runtime_counters_survive_rebuild(tmp_path):
    from batterystore.app import build_platform
    from batterystore.clock import ManualClock
    from batterystore.config import default_config

    storage = str(tmp_path / "state")
    first = build_platform(default_config(), clock=ManualClock(), storage_root=storage, rack_count=1)
    first.start()
    first.cycle()
    assert first.store.exists("platform_state")
    second = build_platform(default_config(), clock=ManualClock(), storage_root=storage, rack_count=1)
    status = second.status()
    assert status["cycles"] >= 1


def test_store_summary_and_config_export(platform):
    platform.start()
    platform.monitor_frequency(49.98)
    assert platform.config_export()["station"]["storage_root"]
    assert platform.namespaces_describe()
    assert platform.topology_summary()["zone_racks"]
    assert platform.rack_snapshot("rk-01")["rack_id"] == "rk-01"
    assert len(platform.rack_snapshots()) == 2


def test_start_persists_structured_self_check_and_rejects_missing_prerequisite(platform):
    startup = platform.start()
    assert startup["self_check"]["ok"]
    stored = platform.startup_self_checks()
    assert stored[0]["check_id"] == startup["self_check"]["check_id"]
    assert stored[0]["checks"]["capacity"]["planned"]["trace"]["cell_nominal_v"]
    assert platform.store.exists("platform_state") is False
    assert platform.audit.query(action="started", limit=10)

    with pytest.raises(StartupSelfCheckError) as excinfo:
        platform.start(ready_stages=set(platform.topology.order()) - {"bms"})
    report = excinfo.value.report
    assert report["decision"] == "reject_start"
    assert any(failure["code"] == "missing_prerequisite" for failure in report["failures"])
    assert platform.startup_self_checks(limit=1)[0]["check_id"] == report["check_id"]
    assert platform.audit.query(action="self_check_rejected", limit=10)
