"""启动自检：设备地址、联控链前置条件与容量核算。"""

from __future__ import annotations

import dataclasses
import json
import urllib.request

import pytest

from batterystore.app import build_platform, build_rack
from batterystore.clock import ManualClock
from batterystore.config import default_config
from batterystore.console.server import ConsoleServer
from batterystore.ns.registry import NamespaceRegistry
from batterystore.ns.selfcheck import run_startup_selfcheck
from batterystore.ns.topology import STAGES, Topology, build_default_topology


def _unwired_topology() -> Topology:
    """构造一条带了未登记设备步骤的联控链（模拟现场漏接）。"""

    return Topology(STAGES + (("telemetry", "remote telemetry upload", ("audit",)),))


def _station_registry() -> NamespaceRegistry:
    """只登记站级设备、不登记任何簇地址的注册表。"""

    registry = NamespaceRegistry("station1")
    for area, device, kind in (
        ("station", "bms", "battery-management"),
        ("station", "pcs", "power-conversion"),
        ("station", "temp", "thermal-control"),
        ("station", "fire", "suppression-panel"),
        ("station", "grid", "tie-point"),
        ("station", "audit", "run-audit"),
    ):
        registry.register(area, device, kind)
    return registry


def _get(base_url: str, path: str):
    with urllib.request.urlopen(f"{base_url}{path}", timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_startup_selfcheck_passes_and_persists(platform):
    report = platform.start()
    assert report["ok"] and report["running"]
    assert report["pending_stages"] == []
    selfcheck = report["selfcheck"]
    assert selfcheck["verdict"] == "started"
    assert selfcheck["policy"] == "fail_closed"
    assert selfcheck["findings"] == []
    assert selfcheck["chain"]["ok"]
    assert selfcheck["addresses"]["ok"]
    assert selfcheck["capacity"]["ok"]
    # 报告整份落盘，事后复核不靠日志
    persisted = platform.store.read("startup_selfcheck")
    assert persisted["verdict"] == "started"
    assert persisted["checked_at"] == selfcheck["checked_at"]
    assert platform.selfcheck_report()["checked_at"] == selfcheck["checked_at"]


def test_startup_selfcheck_writes_audit_event(platform):
    platform.start()
    entries = platform.audit.query(component="selfcheck", action="startup_selfcheck")
    assert len(entries) == 1
    assert entries[0]["severity"] == "notice"
    assert entries[0]["payload"]["verdict"] == "started"
    assert entries[0]["payload"]["policy"] == "fail_closed"


def test_startup_refused_when_chain_step_unwired(tmp_path):
    platform = build_platform(
        default_config(),
        clock=ManualClock(),
        storage_root=str(tmp_path / "state"),
        rack_count=2,
        topology=_unwired_topology(),
    )
    report = platform.start()
    assert report["ok"] is False
    assert report["running"] is False
    assert report["refused"] is True
    assert report["pending_stages"] == ["telemetry"]
    assert platform.status()["running"] is False
    selfcheck = report["selfcheck"]
    assert selfcheck["verdict"] == "refused"
    checks = {(finding["check"], finding["item"]) for finding in selfcheck["findings"]}
    assert ("addresses", "telemetry") in checks
    assert ("chain", "telemetry") in checks
    assert "telemetry" in selfcheck["chain"]["not_ready"]
    assert "station:telemetry" in selfcheck["addresses"]["missing"]
    # 拒绝也要留痕：落盘报告 + alarm 级审计事件
    assert platform.store.read("startup_selfcheck")["verdict"] == "refused"
    alarms = [entry for entry in platform.audit.query(component="selfcheck") if entry["severity"] == "alarm"]
    assert len(alarms) == 1
    assert alarms[0]["payload"]["blocking"]
    # 不修复就反复拒绝
    assert platform.start()["refused"] is True


def test_capacity_mismatch_marks_warning_but_starts(tmp_path):
    # 95 节不能被 8 包整除：实物每簇只能装 88 节，配置口径与实物口径对不上
    config = dataclasses.replace(
        default_config(), rack=dataclasses.replace(default_config().rack, cell_count=95)
    )
    platform = build_platform(config, clock=ManualClock(), storage_root=str(tmp_path / "state"), rack_count=2)
    report = platform.start()
    assert report["running"] is True, report["selfcheck"]["findings"]
    selfcheck = report["selfcheck"]
    assert selfcheck["ok"] is True
    assert selfcheck["capacity"]["ok"] is False
    assert selfcheck["capacity"]["mismatches"] == ["limits.rack.cell_count"]
    line = next(item for item in selfcheck["capacity"]["lines"] if item["item"] == "limits.rack.cell_count")
    assert line["configured"] == 95.0
    assert line["observed"] == 88.0
    assert line["per_rack"] == {"rk-01": 88.0, "rk-02": 88.0}
    # 派生总量两套口径并排，差值可追溯回具体配置项
    totals = selfcheck["capacity"]["totals"]
    assert totals["observed"]["rated_energy_kwh"] < totals["configured"]["rated_energy_kwh"]
    assert totals["delta_energy_kwh"] == pytest.approx(
        totals["observed"]["rated_energy_kwh"] - totals["configured"]["rated_energy_kwh"], abs=1e-3
    )
    assert "limits.rack.cell_count" in totals["inputs"]
    findings = [finding for finding in selfcheck["findings"] if finding["check"] == "capacity"]
    assert findings and all(finding["severity"] == "warning" for finding in findings)
    entries = platform.audit.query(component="selfcheck")
    assert entries[0]["severity"] == "warning"


def test_capacity_lines_trace_to_config_items(platform):
    platform.start()
    capacity = platform.selfcheck_report()["capacity"]
    items = [line["item"] for line in capacity["lines"]]
    assert items == [
        "station.racks",
        "limits.rack.cell_count",
        "limits.rack.cell_nominal_v",
        "limits.soc.capacity_ah",
        "limits.pcs.max_power_kw",
        "limits.rack.soc_min_pct",
        "limits.rack.soc_max_pct",
    ]
    totals = capacity["totals"]
    assert totals["configured"]["rated_energy_kwh"] == pytest.approx(96 * 3.2 * 280 * 2 / 1000.0)
    assert totals["observed"]["rated_energy_kwh"] == pytest.approx(totals["configured"]["rated_energy_kwh"])
    assert totals["delta_energy_kwh"] == pytest.approx(0.0, abs=1e-3)
    assert set(totals["inputs"]) >= {"limits.rack.cell_count", "limits.soc.capacity_ah"}


def test_selfcheck_report_survives_restart(tmp_path):
    storage = str(tmp_path / "state")
    first = build_platform(default_config(), clock=ManualClock(), storage_root=storage, rack_count=2)
    started = first.start()
    assert started["ok"]
    second = build_platform(default_config(), clock=ManualClock(), storage_root=storage, rack_count=2)
    restored = second.selfcheck_report()
    assert restored["verdict"] == "started"
    assert restored["checked_at"] == started["selfcheck"]["checked_at"]
    assert restored["capacity"]["totals"]["configured"]["rack_count"] == 2


def test_selfcheck_report_before_any_start(platform):
    report = platform.selfcheck_report()
    assert report["verdict"] == "never_run"
    assert report["ok"] is False


def test_selfcheck_unit_flags_unregistered_rack_addresses(platform):
    report = run_startup_selfcheck(
        station="station1",
        topology=platform.topology,
        namespaces=_station_registry(),
        config=platform.config,
        racks=platform.racks,
        declared_rack_count=2,
        checked_at=platform.clock.utc_iso(),
    )
    assert report.verdict == "refused"
    checks = {(finding.check, finding.item) for finding in report.findings}
    assert ("addresses", "rack") in checks
    assert ("chain", "rack") in checks
    # bms 声明依赖 rack，rack 没接上 → bms 缺前置条件
    assert ("chain", "bms") in checks
    assert "rack:rk-01" in report.addresses["missing"]


def test_selfcheck_unit_flags_unknown_fire_zone(tmp_path):
    config = default_config()
    rack = build_rack("rk-09", "R9", "Z9", config)
    registry = _station_registry()
    registry.register("rack", "rk-09", "battery-cluster")
    registry.register("Z9", "rk-09", "fire-zone-member")
    report = run_startup_selfcheck(
        station="station1",
        topology=build_default_topology(),
        namespaces=registry,
        config=config,
        racks={"rk-09": rack},
        declared_rack_count=1,
        checked_at=ManualClock().utc_iso(),
    )
    assert report.verdict == "refused"
    assert report.addresses["unknown_zones"] == ["Z9"]
    assert any(finding.item == "fire.zones" for finding in report.findings)


def test_topology_check_reports_per_stage_detail():
    topology = build_default_topology()
    partial = topology.check({"rack", "bms"})
    assert partial["ok"] is False
    assert partial["not_ready"] == ["pcs", "temp", "grid", "fire", "audit"]
    stages = {stage["stage"]: stage for stage in partial["stages"]}
    assert stages["bms"]["ready"] is True
    assert stages["bms"]["missing"] == []
    assert stages["pcs"]["ready"] is False
    full = topology.check(set(topology.order()))
    assert full["ok"] is True
    assert full["missing"] == []


def test_console_serves_selfcheck_report(platform):
    platform.start()
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        status, payload = _get(base_url, "/api/selfcheck")
        assert status == 200
        assert payload["verdict"] == "started"
        assert payload["capacity"]["ok"]
    finally:
        server.stop()
