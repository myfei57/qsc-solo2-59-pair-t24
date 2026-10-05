"""电站拓扑与控制链顺序。

联控链按 ``bms → pcs → temp → grid → fire → audit`` 排列，每一步都声明自己
依赖的前序步骤。:meth:`Topology.verify` 用来在启动时核对现场已就绪的步骤是否
覆盖了全部前置条件，缺哪一步就在启动报告里点出来；:meth:`Topology.order` 供
控制台按真实顺序展示链路。
"""

from __future__ import annotations

STAGES = (
    ("rack", "cluster acquisition", ()),
    ("bms", "rack ready check", ("rack",)),
    ("pcs", "transfer and ramp", ("bms",)),
    ("temp", "derate and cooling", ("pcs",)),
    ("grid", "tie-point limit", ("pcs", "temp")),
    ("fire", "suppression linkage", ("bms", "pcs")),
    ("audit", "operation trail", ("bms", "pcs", "fire")),
)


class Topology:
    """控制链顺序与前置条件。"""

    def __init__(self, stages: tuple[tuple[str, str, tuple[str, ...]], ...] = STAGES) -> None:
        self._stages = tuple(stages)
        names = [name for name, _detail, _needs in self._stages]
        if len(set(names)) != len(names):
            raise ValueError("控制链步骤名不能重复")
        for name, _detail, needs in self._stages:
            unknown = [need for need in needs if need not in names]
            if unknown:
                raise ValueError(f"步骤 {name} 依赖了未声明的步骤: {unknown}")

    def order(self) -> list[str]:
        """返回联控链的步骤顺序。"""

        return [name for name, _detail, _needs in self._stages]

    def describe(self) -> list[dict]:
        """返回每一步的名称、职责与前置步骤。"""

        return [
            {"stage": name, "detail": detail, "requires": list(needs)} for name, detail, needs in self._stages
        ]

    def verify(self, ready: set[str]) -> dict:
        """核对已就绪步骤，返回缺失的前置条件。"""

        present = set(ready)
        missing: list[str] = []
        for name, _detail, needs in self._stages:
            if name not in present:
                continue
            for need in needs:
                if need not in present and need not in missing:
                    missing.append(need)
        return {"ready": sorted(present), "missing": sorted(missing), "ok": not missing}

    def check(self, ready: set[str]) -> dict:
        """逐步骤核对前置条件，把每一步的就绪状态与缺失明细都列出来。

        与 :meth:`verify` 只汇总缺失前置不同，这里把"步骤自身未就绪"也单列，
        启动自检据此判定链路是否完整接上，而不是只看缺失名字的汇总。
        """

        present = set(ready)
        stages: list[dict] = []
        missing: list[str] = []
        for name, detail, needs in self._stages:
            stage_missing = [need for need in needs if need not in present]
            stages.append(
                {
                    "stage": name,
                    "detail": detail,
                    "ready": name in present,
                    "requires": list(needs),
                    "missing": stage_missing,
                }
            )
            for need in stage_missing:
                if need not in missing:
                    missing.append(need)
        not_ready = [stage["stage"] for stage in stages if not stage["ready"]]
        return {
            "ok": not missing and not not_ready,
            "stages": stages,
            "missing": sorted(missing),
            "not_ready": not_ready,
        }

    def stages_after(self, stage: str) -> list[str]:
        """返回某步骤之后的步骤名，供控制台高亮未完成的链路。"""

        names = self.order()
        if stage not in names:
            raise ValueError(f"未知步骤: {stage}")
        return names[names.index(stage) + 1 :]


def build_default_topology() -> Topology:
    """构造现场默认的联控链。"""

    return Topology(STAGES)
