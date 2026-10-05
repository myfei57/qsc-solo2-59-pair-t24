"""电站容量核算。

电站容量按"簇数 × 单体数 × 额定电压 × 额定安时"折算成可用电量，并给出
功率配比。运维用它核对新建电站的铭牌与现场实际配置是否一致，控制台把它
展示在概览页顶部。核算结果同时保留每个输入量的配置口径，复核时可以从总数
追到具体配置项，而不是只看到一个汇总值。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import StationConfig


@dataclass(frozen=True)
class CapacityTraceItem:
    """一个容量输入量的数值与来源。"""

    value: float
    unit: str
    source: str
    formula: str = ""

    def as_dict(self) -> dict:
        return {
            "value": round(self.value, 6),
            "unit": self.unit,
            "source": self.source,
            "formula": self.formula,
        }


@dataclass(frozen=True)
class CapacityPlan:
    """一次容量核算的结果。"""

    rack_count: int
    cells_per_rack: int
    nominal_voltage_v: float
    rated_energy_kwh: float
    usable_energy_kwh: float
    power_ratio: float
    trace: dict[str, CapacityTraceItem] | None = None

    def as_dict(self) -> dict:
        payload = {
            "rack_count": self.rack_count,
            "cells_per_rack": self.cells_per_rack,
            "nominal_voltage_v": round(self.nominal_voltage_v, 2),
            "rated_energy_kwh": round(self.rated_energy_kwh, 3),
            "usable_energy_kwh": round(self.usable_energy_kwh, 3),
            "power_ratio": round(self.power_ratio, 4),
        }
        if self.trace is not None:
            payload["trace"] = {key: item.as_dict() for key, item in self.trace.items()}
        return payload


def plan_capacity(config: StationConfig, rack_count: int) -> CapacityPlan:
    """按配置与簇数核算额定／可用容量与功率配比。"""

    if rack_count <= 0:
        raise ValueError("簇数必须为正")
    cells = config.rack.cell_count
    pack_voltage = cells * config.rack.cell_nominal_v
    rated_energy = pack_voltage * config.soc.capacity_ah * rack_count / 1000.0
    soc_span = (config.rack.soc_max_pct - config.rack.soc_min_pct) / 100.0
    usable = rated_energy * soc_span
    power_ratio = config.pcs.max_power_kw * rack_count / rated_energy if rated_energy else 0.0
    trace = {
        "rack_count": CapacityTraceItem(
            float(rack_count), "个", "runtime.rack_count", "由本次实际登记的电池簇计数"
        ),
        "cells_per_rack": CapacityTraceItem(
            float(cells), "颗/簇", "limits.rack.cell_count", "簇内串联单体数量"
        ),
        "cell_nominal_v": CapacityTraceItem(
            config.rack.cell_nominal_v, "V/颗", "limits.rack.cell_nominal_v", "单体额定电压"
        ),
        "nominal_voltage_v": CapacityTraceItem(
            pack_voltage,
            "V/簇",
            "limits.rack.cell_count × limits.rack.cell_nominal_v",
            "cells_per_rack * cell_nominal_v",
        ),
        "capacity_ah": CapacityTraceItem(
            config.soc.capacity_ah, "Ah", "limits.soc.capacity_ah", "单簇额定安时容量"
        ),
        "soc_min_pct": CapacityTraceItem(
            config.rack.soc_min_pct, "%", "limits.rack.soc_min_pct", "允许放电下限"
        ),
        "soc_max_pct": CapacityTraceItem(
            config.rack.soc_max_pct, "%", "limits.rack.soc_max_pct", "允许充电上限"
        ),
        "usable_window": CapacityTraceItem(
            soc_span,
            "ratio",
            "limits.rack.soc_max_pct - limits.rack.soc_min_pct",
            "(soc_max_pct - soc_min_pct) / 100",
        ),
        "pcs_max_power_kw": CapacityTraceItem(
            config.pcs.max_power_kw, "kW", "limits.pcs.max_power_kw", "单台 PCS 最大功率"
        ),
        "rated_energy_kwh": CapacityTraceItem(
            rated_energy,
            "kWh",
            "runtime.rack_count × limits.rack.cell_count × limits.rack.cell_nominal_v × limits.soc.capacity_ah",
            "rack_count * nominal_voltage_v * capacity_ah / 1000",
        ),
        "usable_energy_kwh": CapacityTraceItem(
            usable,
            "kWh",
            "rated_energy_kwh × (limits.rack.soc_max_pct - limits.rack.soc_min_pct)",
            "rated_energy_kwh * usable_window",
        ),
        "power_ratio": CapacityTraceItem(
            power_ratio,
            "kW/kWh",
            "limits.pcs.max_power_kw × runtime.rack_count / rated_energy_kwh",
            "pcs_max_power_kw * rack_count / rated_energy_kwh",
        ),
    }
    return CapacityPlan(
        rack_count=rack_count,
        cells_per_rack=cells,
        nominal_voltage_v=pack_voltage,
        rated_energy_kwh=rated_energy,
        usable_energy_kwh=usable,
        power_ratio=power_ratio,
        trace=trace,
    )
