"""启动自检。

平台每次启动前把三件事一次查完，结果落成结构化报告供事后复核：

1. **设备地址**：联控链每一步需要的设备地址是否都在命名空间登记在册。重复登记
   在 :class:`~batterystore.ns.registry.NamespaceRegistry.register` 登记时就被当场
   拦下，这里核对的是另一侧——"该登记的有没有都登记上"，以及每簇的消防分区是否
   落在配置的分区范围内；
2. **联控链前置条件**：每一步声明的前置步骤是否都有已就绪的步骤承接。此前启动
   流程把所有步骤无条件标成就绪，缺步骤要等跑到某条联锁才暴露；自检把就绪状态
   改成从实际登记情况推导，在启动瞬间就把缺口点出来；
3. **容量核算**：配置口径与实物口径（实际建出来的簇）逐项对照，每个派生总量都
   标注它由哪些配置项算出，复核时能追溯到具体配置项而不是只看一个总数。

**缺前置条件的处理策略：拒绝启动（fail-closed）。** 这条联控链下游挂着保护联锁
与消防联动（audit 依赖 fire，fire 依赖 bms/pcs），缺前置条件意味着某条联锁可能
要到运行中才被发现没接上——降级启动只是把同一个风险正式化。拒绝启动的适用
条件：链路下游存在安全联锁、缺失步骤无法在运行期被隔离，本电站正是这种情况。
降级启动（标记缺失步骤后继续）只在下游是纯监视功能、缺失步骤可被明确隔离且有
人工时限确认的前提下才可考虑，本平台不采用。

容量核算不一致记为 warning 级发现：必须落进报告与审计让人看见，但不阻断启动，
因为容量口径偏差不影响联锁接线的正确性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping

from ..config import StationConfig
from .capacity import plan_capacity
from .registry import NamespaceRegistry
from .topology import Topology

if TYPE_CHECKING:
    from ..rack.rack import Rack

BLOCKING = "blocking"
WARNING = "warning"
POLICY_FAIL_CLOSED = "fail_closed"

CAPACITY_FORMULA = "rated_energy_kwh = racks × cells_per_rack × cell_nominal_v × capacity_ah / 1000"
CAPACITY_INPUTS = [
    "station.racks",
    "limits.rack.cell_count",
    "limits.rack.cell_nominal_v",
    "limits.soc.capacity_ah",
    "limits.rack.soc_min_pct",
    "limits.rack.soc_max_pct",
    "limits.pcs.max_power_kw",
]


@dataclass(frozen=True)
class Finding:
    """一条自检发现。"""

    check: str
    severity: str
    item: str
    message: str

    def as_dict(self) -> dict:
        return {
            "check": self.check,
            "severity": self.severity,
            "item": self.item,
            "message": self.message,
        }


@dataclass(frozen=True)
class CapacityLine:
    """容量核算的一条对照行：配置口径 vs 实物口径。"""

    item: str
    configured: float | None
    observed: float | None
    unit: str
    match: bool
    note: str = ""
    per_rack: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        line = {
            "item": self.item,
            "configured": self.configured,
            "observed": self.observed,
            "unit": self.unit,
            "match": self.match,
        }
        if self.per_rack:
            line["per_rack"] = dict(self.per_rack)
        if self.note:
            line["note"] = self.note
        return line


@dataclass(frozen=True)
class StartupSelfCheck:
    """一次启动自检的完整报告。"""

    station: str
    checked_at: str
    verdict: str
    policy: str
    findings: tuple[Finding, ...]
    chain: dict
    addresses: dict
    capacity: dict

    @property
    def ok(self) -> bool:
        """自检是否通过（无阻断级发现，允许启动）。"""

        return self.verdict == "started"

    def findings_of(self, severity: str) -> list[dict]:
        """按严重度筛出发现的字典形式。"""

        return [finding.as_dict() for finding in self.findings if finding.severity == severity]

    def as_dict(self) -> dict:
        return {
            "station": self.station,
            "checked_at": self.checked_at,
            "ok": self.ok,
            "verdict": self.verdict,
            "policy": self.policy,
            "findings": [finding.as_dict() for finding in self.findings],
            "chain": self.chain,
            "addresses": self.addresses,
            "capacity": self.capacity,
        }


def _expected_addresses(topology: Topology, racks: Mapping[str, "Rack"]) -> dict[str, list[str]]:
    """按拓扑步骤推导应在命名空间登记的地址。"""

    expected: dict[str, list[str]] = {}
    for stage in topology.order():
        if stage == "rack":
            addresses: list[str] = []
            for rack_id, rack in sorted(racks.items()):
                addresses.append(f"rack:{rack_id}")
                addresses.append(f"{rack.zone}:{rack_id}")
            expected[stage] = addresses
        else:
            expected[stage] = [f"station:{stage}"]
    return expected


def _check_addresses(
    topology: Topology,
    namespaces: NamespaceRegistry,
    racks: Mapping[str, "Rack"],
    zone_count: int,
) -> tuple[dict, set[str], list[Finding]]:
    """核对设备地址，返回报告、可推导为就绪的步骤集合与发现。"""

    registered = {entry["address"] for entry in namespaces.addresses()}
    expected = _expected_addresses(topology, racks)
    findings: list[Finding] = []
    missing: list[str] = []
    ready: set[str] = set()
    for stage, addresses in expected.items():
        absent = [address for address in addresses if address not in registered]
        if absent:
            missing.extend(absent)
            findings.append(
                Finding(
                    check="addresses",
                    severity=BLOCKING,
                    item=stage,
                    message=f"步骤 {stage} 的设备地址未登记: {', '.join(absent)}",
                )
            )
        else:
            ready.add(stage)
    configured_zones = {f"Z{index + 1}" for index in range(zone_count)}
    unknown_zones = sorted({rack.zone for rack in racks.values()} - configured_zones)
    if unknown_zones:
        findings.append(
            Finding(
                check="addresses",
                severity=BLOCKING,
                item="fire.zones",
                message=f"簇所在消防分区超出配置范围: {', '.join(unknown_zones)}",
            )
        )
    report = {
        "ok": not missing and not unknown_zones,
        "registered": len(registered),
        "expected": sorted(address for addresses in expected.values() for address in addresses),
        "missing": sorted(missing),
        "unknown_zones": unknown_zones,
    }
    return report, ready, findings


def _check_chain(topology: Topology, ready: set[str]) -> tuple[dict, list[Finding]]:
    """核对联控链：每一步的前置条件都有已就绪步骤承接。"""

    chain = topology.check(ready)
    findings: list[Finding] = []
    for stage in chain["stages"]:
        if not stage["ready"]:
            findings.append(
                Finding(
                    check="chain",
                    severity=BLOCKING,
                    item=stage["stage"],
                    message=f"步骤 {stage['stage']} 未就绪（设备地址未登记）",
                )
            )
        for need in stage["missing"]:
            findings.append(
                Finding(
                    check="chain",
                    severity=BLOCKING,
                    item=stage["stage"],
                    message=f"步骤 {stage['stage']} 缺前置条件: {need}",
                )
            )
    return chain, findings


def _uniform(values: Mapping[str, float]) -> float | None:
    """各簇取值一致时返回该值，不一致返回 ``None``（明细看 per_rack）。"""

    distinct = set(values.values())
    if len(distinct) == 1:
        return next(iter(distinct))
    return None


def _check_capacity(
    config: StationConfig,
    racks: Mapping[str, "Rack"],
    declared_rack_count: int,
) -> tuple[dict, list[Finding]]:
    """容量核算：配置口径与实物口径逐项对照，派生总量标注来源配置项。"""

    cells_per_rack = {rack_id: len(rack.cells()) for rack_id, rack in sorted(racks.items())}
    nominal_per_rack = {
        rack_id: round(sum(cell.nominal_v for cell in rack.cells()), 4)
        for rack_id, rack in sorted(racks.items())
    }
    cell_voltage_per_rack = {
        rack_id: round(nominal_per_rack[rack_id] / cells_per_rack[rack_id], 6)
        for rack_id in cells_per_rack
        if cells_per_rack[rack_id] > 0
    }
    observed_cells = _uniform({key: float(value) for key, value in cells_per_rack.items()})
    observed_cell_v = _uniform(cell_voltage_per_rack)
    lines = [
        CapacityLine(
            item="station.racks",
            configured=float(declared_rack_count),
            observed=float(len(racks)),
            unit="簇",
            match=declared_rack_count == len(racks),
        ),
        CapacityLine(
            item="limits.rack.cell_count",
            configured=float(config.rack.cell_count),
            observed=observed_cells,
            unit="节/簇",
            match=observed_cells == float(config.rack.cell_count),
            per_rack={key: float(value) for key, value in cells_per_rack.items()},
        ),
        CapacityLine(
            item="limits.rack.cell_nominal_v",
            configured=config.rack.cell_nominal_v,
            observed=observed_cell_v,
            unit="V",
            match=observed_cell_v is not None
            and abs(observed_cell_v - config.rack.cell_nominal_v) <= 1e-6,
            per_rack=cell_voltage_per_rack,
        ),
        CapacityLine(
            item="limits.soc.capacity_ah",
            configured=config.soc.capacity_ah,
            observed=None,
            unit="Ah",
            match=True,
            note="仅配置口径，实物侧无可观测量",
        ),
        CapacityLine(
            item="limits.pcs.max_power_kw",
            configured=config.pcs.max_power_kw,
            observed=None,
            unit="kW",
            match=True,
            note="仅配置口径，实物侧无可观测量",
        ),
        CapacityLine(
            item="limits.rack.soc_min_pct",
            configured=config.rack.soc_min_pct,
            observed=None,
            unit="%",
            match=True,
            note="仅配置口径，可用窗口下沿",
        ),
        CapacityLine(
            item="limits.rack.soc_max_pct",
            configured=config.rack.soc_max_pct,
            observed=None,
            unit="%",
            match=True,
            note="仅配置口径，可用窗口上沿",
        ),
    ]
    configured_plan = plan_capacity(config, max(1, declared_rack_count))
    observed_rated = sum(nominal_per_rack.values()) * config.soc.capacity_ah / 1000.0
    soc_span = (config.rack.soc_max_pct - config.rack.soc_min_pct) / 100.0
    observed_usable = observed_rated * soc_span
    observed_ratio = config.pcs.max_power_kw * len(racks) / observed_rated if observed_rated else 0.0
    totals = {
        "configured": configured_plan.as_dict(),
        "observed": {
            "rack_count": len(racks),
            "rated_energy_kwh": round(observed_rated, 3),
            "usable_energy_kwh": round(observed_usable, 3),
            "power_ratio": round(observed_ratio, 4),
        },
        "delta_energy_kwh": round(observed_rated - configured_plan.rated_energy_kwh, 3),
        "formula": CAPACITY_FORMULA,
        "inputs": list(CAPACITY_INPUTS),
    }
    mismatches = [line.item for line in lines if not line.match]
    findings = [
        Finding(
            check="capacity",
            severity=WARNING,
            item=line.item,
            message=f"容量对照 {line.item} 不一致: 配置 {line.configured} {line.unit} vs 实物 {line.observed} {line.unit}",
        )
        for line in lines
        if not line.match
    ]
    report = {
        "ok": not mismatches,
        "lines": [line.as_dict() for line in lines],
        "mismatches": mismatches,
        "totals": totals,
    }
    return report, findings


def run_startup_selfcheck(
    *,
    station: str,
    topology: Topology,
    namespaces: NamespaceRegistry,
    config: StationConfig,
    racks: Mapping[str, "Rack"],
    declared_rack_count: int,
    checked_at: str,
) -> StartupSelfCheck:
    """跑一遍启动自检，返回完整报告（落盘与发布由调用方负责）。"""

    addresses, ready, findings = _check_addresses(topology, namespaces, racks, config.fire.zone_count)
    chain, chain_findings = _check_chain(topology, ready)
    capacity, capacity_findings = _check_capacity(config, racks, declared_rack_count)
    findings.extend(chain_findings)
    findings.extend(capacity_findings)
    blocked = any(finding.severity == BLOCKING for finding in findings)
    return StartupSelfCheck(
        station=station,
        checked_at=checked_at,
        verdict="refused" if blocked else "started",
        policy=POLICY_FAIL_CLOSED,
        findings=tuple(findings),
        chain=chain,
        addresses=addresses,
        capacity=capacity,
    )
