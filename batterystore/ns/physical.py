"""现场实物配置核算。

启动自检不只计算配置口径下的理论容量，还要把运行时已经装配进来的电池簇、
电池包、单体数量和额定电压逐簇列清楚。每一项差异都保留期望值、实际值和来源，
值班人员可以直接定位到配置项或具体设备。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..rack.rack import Rack

TOLERANCE = 1e-6


@dataclass(frozen=True)
class PhysicalCapacity:
    """现场实际装配的容量基础数据。"""

    rack_count: int
    cells_per_rack: int
    nominal_voltage_v: float
    racks: list[dict]

    def as_dict(self) -> dict:
        return {
            "rack_count": self.rack_count,
            "cells_per_rack": self.cells_per_rack,
            "nominal_voltage_v": round(self.nominal_voltage_v, 6),
            "racks": self.racks,
        }


@dataclass(frozen=True)
class CapacityReconciliation:
    """理论容量与现场实物配置的核对结果。"""

    ok: bool
    planned: dict
    physical: PhysicalCapacity
    mismatches: list[dict]

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "planned": self.planned,
            "physical": self.physical.as_dict(),
            "mismatches": self.mismatches,
        }


def inspect_physical_capacity(racks: dict[str, Rack]) -> PhysicalCapacity:
    """汇总实际装配的簇、包、单体和额定电压。"""

    details: list[dict] = []
    cell_counts: list[int] = []
    voltages: list[float] = []
    for rack_id in sorted(racks):
        rack = racks[rack_id]
        packs = rack.packs()
        cells = rack.cells()
        nominal_voltage = sum(cell.nominal_v for cell in cells)
        cell_counts.append(len(cells))
        voltages.append(nominal_voltage)
        details.append(
            {
                "rack_id": rack_id,
                "zone": rack.zone,
                "pack_count": len(packs),
                "cell_count": len(cells),
                "nominal_voltage_v": round(nominal_voltage, 6),
                "source": f"physical.rack[{rack_id}]",
                "packs": [
                    {
                        "pack_id": pack.pack_id,
                        "cell_count": len(pack.cells()),
                        "cell_nominal_v": round(pack.cells()[0].nominal_v, 6),
                        "source": f"physical.pack[{pack.pack_id}]",
                    }
                    for pack in packs
                ],
            }
        )
    if not cell_counts:
        return PhysicalCapacity(0, 0, 0.0, [])
    return PhysicalCapacity(
        rack_count=len(details),
        cells_per_rack=max(cell_counts),
        nominal_voltage_v=max(voltages),
        racks=details,
    )


def _mismatch(item: str, expected: float, actual: float, expected_source: str, actual_source: str) -> dict:
    return {
        "item": item,
        "expected": round(float(expected), 6),
        "actual": round(float(actual), 6),
        "expected_source": expected_source,
        "actual_source": actual_source,
    }


def reconcile_capacity(plan, racks: dict[str, Rack]) -> CapacityReconciliation:
    """把容量核算结果与逐簇实物配置核对。"""

    physical = inspect_physical_capacity(racks)
    planned = plan.as_dict()
    mismatches: list[dict] = []

    if physical.rack_count != plan.rack_count:
        mismatches.append(
            _mismatch(
                "rack_count",
                plan.rack_count,
                physical.rack_count,
                "runtime.rack_count",
                "physical.racks",
            )
        )
    if physical.cells_per_rack != plan.cells_per_rack:
        mismatches.append(
            _mismatch(
                "cells_per_rack",
                plan.cells_per_rack,
                physical.cells_per_rack,
                "limits.rack.cell_count",
                "physical.rack[*].cell_count",
            )
        )
    if abs(physical.nominal_voltage_v - plan.nominal_voltage_v) > TOLERANCE:
        mismatches.append(
            _mismatch(
                "nominal_voltage_v",
                plan.nominal_voltage_v,
                physical.nominal_voltage_v,
                "limits.rack.cell_count × limits.rack.cell_nominal_v",
                "physical.rack[*].nominal_voltage_v",
            )
        )

    for detail in physical.racks:
        if detail["cell_count"] != plan.cells_per_rack:
            mismatches.append(
                _mismatch(
                    f"rack[{detail['rack_id']}].cell_count",
                    plan.cells_per_rack,
                    detail["cell_count"],
                    "limits.rack.cell_count",
                    f"physical.rack[{detail['rack_id']}].cell_count",
                )
            )
        if abs(detail["nominal_voltage_v"] - plan.nominal_voltage_v) > TOLERANCE:
            mismatches.append(
                _mismatch(
                    f"rack[{detail['rack_id']}].nominal_voltage_v",
                    plan.nominal_voltage_v,
                    detail["nominal_voltage_v"],
                    "limits.rack.cell_count × limits.rack.cell_nominal_v",
                    f"physical.rack[{detail['rack_id']}].nominal_voltage_v",
                )
            )

    return CapacityReconciliation(not mismatches, planned, physical, mismatches)
