"""电站命名空间：把现场设备名与内部标识绑成一套可查的地址。"""

from __future__ import annotations

from .capacity import CapacityPlan, plan_capacity
from .registry import NamespaceRegistry
from .selfcheck import Finding, StartupSelfCheck, run_startup_selfcheck
from .topology import Topology, build_default_topology

__all__ = [
    "CapacityPlan",
    "Finding",
    "NamespaceRegistry",
    "StartupSelfCheck",
    "Topology",
    "build_default_topology",
    "plan_capacity",
    "run_startup_selfcheck",
]
