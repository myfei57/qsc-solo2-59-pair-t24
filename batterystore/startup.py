"""启动自检。

启动自检在平台进入运行态前同时核对三件事：

1. 设备地址是否按全站唯一地址登记，联控链要求的设备是否存在；
2. 联控链每个步骤的前置条件是否全部接上；
3. 容量核算所用的配置项与现场实际簇／包／单体数、额定电压是否一致。

结果使用结构化报告，调用方负责把它写入持久化流水。默认策略是
``reject_missing_prerequisite``：任一安全联锁链的前置条件缺失就拒绝启动。
"""

from __future__ import annotations

from typing import Iterable

from .config import StationConfig
from .ns.capacity import plan_capacity
from .ns.physical import reconcile_capacity
from .ns.registry import NamespaceRegistry
from .ns.topology import Topology
from .rack.rack import Rack

STAGE_DEVICE_KIND = {
    "rack": "battery-cluster",
    "bms": "battery-management",
    "pcs": "power-conversion",
    "temp": "thermal-control",
    "fire": "suppression-panel",
    "grid": "tie-point",
    "audit": "run-audit",
}

REJECT_POLICY = "reject_missing_prerequisite"


def check_device_addresses(registry: NamespaceRegistry, topology: Topology, physical_rack_count: int = 0) -> dict:
    """核对地址唯一性和联控链设备地址。"""

    duplicates: list[dict] = []
    for duplicate in registry.duplicate_addresses():
        duplicates.append(
            {
                **duplicate,
                "message": f"全站设备地址重复登记: {duplicate['address']}",
            }
        )

    registered = {entry["address"]: entry for entry in registry.addresses()}
    stages: list[dict] = []
    missing: list[dict] = []
    for stage in topology.order():
        kind = STAGE_DEVICE_KIND.get(stage)
        addresses = sorted(registry.of_kind(kind)) if kind else []
        present = [registered[address] for address in addresses]
        item = {
            "stage": stage,
            "required_kind": kind,
            "addresses": [entry["address"] for entry in present],
            "present": bool(present),
        }
        stages.append(item)
        if not item["present"]:
            missing.append(
                {
                    "stage": stage,
                    "required_kind": kind,
                    "message": f"联控步骤 {stage} 缺少设备地址",
                }
            )

    rack_addresses = registry.of_kind("battery-cluster")
    if physical_rack_count and len(rack_addresses) != physical_rack_count:
        missing.append(
            {
                "stage": "rack",
                "required_kind": STAGE_DEVICE_KIND["rack"],
                "code": "rack_address_count_mismatch",
                "expected": physical_rack_count,
                "actual": len(rack_addresses),
                "message": (
                    f"实物电池簇 {physical_rack_count} 个，但登记电池簇地址 "
                    f"{len(rack_addresses)} 个"
                ),
            }
        )

    ok = not duplicates and not missing
    return {
        "ok": ok,
        "station": registry.station,
        "registered_count": len(registered),
        "physical_rack_count": physical_rack_count,
        "rack_address_count": len(registry.of_kind("battery-cluster")),
        "duplicates": duplicates,
        "missing": missing,
        "stages": stages,
    }


def ready_stages_from_addresses(devices: dict) -> set[str]:
    """从设备地址自检结果推导已接入联控链的步骤。"""

    return {item["stage"] for item in devices.get("stages", []) if item.get("present")}


def run_startup_self_check(
    *,
    station: str,
    check_id: str,
    ts: float,
    now_iso: str,
    config: StationConfig,
    registry: NamespaceRegistry,
    topology: Topology,
    racks: dict[str, Rack],
    ready_stages: Iterable[str] | None = None,
    policy: str = REJECT_POLICY,
) -> dict:
    """执行一次完整启动自检并返回可落盘的结构化报告。"""

    devices = check_device_addresses(registry, topology, physical_rack_count=len(racks))
    if ready_stages is None:
        ready = ready_stages_from_addresses(devices)
    else:
        ready = set(ready_stages)
    links = topology.verify_startup(ready)
    plan = plan_capacity(config, len(racks))
    capacity = reconcile_capacity(plan, racks).as_dict()

    failures: list[dict] = []
    warnings: list[dict] = []
    for duplicate in devices["duplicates"]:
        failures.append({"code": "duplicate_device_address", **duplicate})
    for missing in devices["missing"]:
        code = missing.get("code", "missing_device_address")
        failures.append({"code": code, **{key: value for key, value in missing.items() if key != "code"}})
    for item in links["missing_prerequisites"]:
        failures.append(
            {
                "code": "missing_prerequisite",
                "stage": item["stage"],
                "requires": item["requires"],
                "message": f"{item['stage']} 缺少前置条件 {item['requires']}",
            }
        )
    for stage in links["missing_stages"]:
        if not any(
            failure["code"] == "missing_device_address" and failure.get("stage") == stage
            for failure in failures
        ):
            failures.append(
                {
                    "code": "missing_topology_stage",
                    "stage": stage,
                    "message": f"联控步骤未接入: {stage}",
                }
            )
    for mismatch in capacity["mismatches"]:
        failures.append({"code": "capacity_mismatch", **mismatch})

    ok = not failures
    result = {
        "check_id": check_id,
        "station": station,
        "ts": round(ts, 3),
        "now_iso": now_iso,
        "ok": ok,
        "policy": policy,
        "policy_applicable_condition": (
            "生产运行中的充放电、并离网切换和消防联锁一旦带缺陷启动可能误动作，"
            "因此缺任一前置条件都拒绝启动；仅在隔离演练或只读排障时才可考虑降级启动。"
        ),
        "decision": "allow_start" if ok else "reject_start",
        "failure_count": len(failures),
        "warning_count": len(warnings),
        "failures": failures,
        "warnings": warnings,
        "checks": {
            "device_addresses": devices,
            "topology": links,
            "capacity": capacity,
        },
        "device_addresses": devices,
        "topology": links,
        "capacity": capacity,
    }
    if not ok:
        result["applicable_condition"] = (
            "缺前置条件时采用拒绝启动；适用于充放电、并网和消防联锁存在误动作风险的生产运行。"
        )
    return result
