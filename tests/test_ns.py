"""命名空间、拓扑与容量。"""

from __future__ import annotations

import pytest

from batterystore.errors import NamespaceError
from batterystore.config import default_config
from batterystore.ns.capacity import plan_capacity
from batterystore.ns.physical import reconcile_capacity
from batterystore.ns.registry import NamespaceRegistry
from batterystore.ns.topology import Topology, build_default_topology
from batterystore.rack.cells import Cell
from batterystore.rack.pack import Pack
from batterystore.rack.rack import Rack
from batterystore.startup import check_device_addresses, run_startup_self_check


def test_registry_registers_and_resolves():
    registry = NamespaceRegistry("station1")
    address = registry.register("station", "pcs", "power-conversion")
    assert address == "station:pcs"
    assert registry.resolve(address)["kind"] == "power-conversion"
    assert registry.of_kind("power-conversion") == [address]
    assert registry.count() == 1
    assert registry.addresses()[0]["device"] == "pcs"


def test_registry_rejects_duplicate_address():
    registry = NamespaceRegistry("station1")
    registry.register("station", "pcs", "power-conversion")
    with pytest.raises(NamespaceError):
        registry.register("station", "pcs", "power-conversion")
    with pytest.raises(NamespaceError):
        registry.resolve("station:unknown")


def test_topology_order_and_verify():
    topology = build_default_topology()
    order = topology.order()
    assert order[0] == "rack"
    assert order.index("bms") < order.index("pcs") < order.index("grid")
    assert topology.verify({"rack", "bms"})["missing"] == []
    assert topology.verify({"bms"})["missing"] == ["rack"]
    assert topology.stages_after("pcs")[:2] == ["temp", "grid"]
    assert [stage["stage"] for stage in topology.describe()] == order


def test_topology_rejects_unknown_dependency():
    with pytest.raises(ValueError):
        Topology((("a", "detail", ("missing",)),))


def test_plan_capacity_scales_with_racks():
    config = default_config()
    single = plan_capacity(config, 1)
    double = plan_capacity(config, 2)
    assert double.rated_energy_kwh == pytest.approx(single.rated_energy_kwh * 2)
    assert double.usable_energy_kwh < double.rated_energy_kwh
    assert single.as_dict()["cells_per_rack"] == config.rack.cell_count
    assert single.as_dict()["trace"]["rated_energy_kwh"]["source"]
    with pytest.raises(ValueError):
        plan_capacity(config, 0)


def _rack(rack_id: str, cell_count: int = 96, nominal_v: float = 3.2) -> Rack:
    cells = [
        Cell(f"{rack_id}-c{index}", nominal_v, nominal_v, nominal_v)
        for index in range(cell_count)
    ]
    return Rack(rack_id, [Pack(f"{rack_id}-p1", cells)], "Z1")


def test_capacity_reconciliation_reports_each_configuration_source():
    config = default_config()
    plan = plan_capacity(config, 1)
    reconciliation = reconcile_capacity(plan, {"rk-01": _rack("rk-01")})
    assert reconciliation.ok
    payload = reconciliation.as_dict()
    assert payload["physical"]["rack_count"] == 1
    assert payload["planned"]["trace"]["cells_per_rack"]["source"] == "limits.rack.cell_count"
    assert payload["planned"]["trace"]["usable_energy_kwh"]["formula"]

    changed = _rack("rk-01", cell_count=90)
    failed = reconcile_capacity(plan, {"rk-01": changed}).as_dict()
    assert not failed["ok"]
    mismatch = failed["mismatches"][0]
    assert mismatch["expected_source"] == "limits.rack.cell_count"
    assert mismatch["actual_source"] == "physical.rack[*].cell_count"


def test_startup_self_check_links_addresses_topology_and_capacity():
    config = default_config()
    topology = build_default_topology()
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
    racks = {"rk-01": _rack("rk-01")}
    registry.register("rack", "rk-01", "battery-cluster")

    report = run_startup_self_check(
        station="station1",
        check_id="check-1",
        ts=1.0,
        now_iso="start",
        config=config,
        registry=registry,
        topology=topology,
        racks=racks,
    )
    assert report["ok"]
    assert report["checks"]["device_addresses"]["ok"]
    assert report["checks"]["topology"]["ok"]
    assert report["checks"]["capacity"]["ok"]

    report = run_startup_self_check(
        station="station1",
        check_id="check-2",
        ts=2.0,
        now_iso="later",
        config=config,
        registry=registry,
        topology=topology,
        racks=racks,
        ready_stages=set(topology.order()) - {"bms"},
    )
    assert not report["ok"]
    assert report["decision"] == "reject_start"
    codes = {failure["code"] for failure in report["failures"]}
    assert "missing_prerequisite" in codes
    assert {item["requires"] for item in report["topology"]["missing_prerequisites"]} == {"bms"}


def test_startup_self_check_reports_duplicate_and_missing_device_addresses():
    topology = build_default_topology()
    registry = NamespaceRegistry("station1")
    report_devices = check_device_addresses(registry, topology)
    assert not report_devices["ok"]
    assert {item["stage"] for item in report_devices["missing"]} == set(topology.order())
    assert registry.duplicate_addresses(
        [
            {"address": "station:pcs", "kind": "power-conversion"},
            {"address": "station:pcs", "kind": "other"},
        ]
    )[0]["address"] == "station:pcs"
