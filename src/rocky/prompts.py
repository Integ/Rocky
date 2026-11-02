"""Bundled, deterministic instructions. Task data never becomes a module."""

import difflib
import hashlib
import json
import re
from functools import lru_cache
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Purpose = Literal["planner", "reviewer", "contract", "decomposition", "revision"]
Guidance = Literal["coding", "research"]

_MODULES = {
    "planner": (1, "执行与交接", "始终用于任务规划"),
    "tools-full": (1, "完整工具策略", "使用完整工具模式时加载"),
    "tools-scoped": (1, "受限工具策略", "使用受限工具模式时加载"),
    "coding": (1, "编程指导", "目标、验收标准或交付文件涉及编程时加载"),
    "research": (1, "研究指导", "目标或验收标准涉及检索、研究和引用时加载"),
    "reviewer": (1, "独立验收", "始终用于结果验收"),
    "contract": (1, "任务要求", "生成任务要求时加载"),
    "decomposition": (1, "目标拆解", "拆解长期目标时加载"),
    "revision": (1, "计划调整", "检查长期目标的剩余步骤时加载"),
}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class PromptModule(BaseModel):
    module_id: str
    title: str
    version: int = 1
    sha256: str
    loading: str
    text: str


class PromptSnapshot(BaseModel):
    snapshot_id: str
    purpose: Purpose
    modules: list[PromptModule]
    instructions: str
    output_schema: str


class PromptPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: Purpose = "planner"
    guidance: list[Guidance] = Field(default_factory=list, max_length=2)


@lru_cache(maxsize=len(_MODULES))
def module(module_id: str) -> PromptModule:
    version, title, loading = _MODULES[module_id]
    text = files("rocky").joinpath("prompt_modules", module_id + ".md").read_text("utf-8")
    return PromptModule(
        module_id=module_id,
        title=title,
        version=version,
        loading=loading,
        sha256=_digest(text),
        text=text,
    )


def catalog() -> list[PromptModule]:
    return [module(key).model_copy(deep=True) for key in _MODULES]


def guidance_for(contract) -> list[Guidance]:
    """Only owner-confirmed scope selects fixed knowledge; never files or memory."""
    text = "\n".join([contract.objective, *contract.acceptance_criteria]).lower()
    paths = "\n".join(item.path for item in contract.deliverables).lower()
    result: list[Guidance] = []
    if re.search(
        r"\b(code|coding|programming|software|repository|repo|debug|refactor|implement|tests?|bugs?|build|api|scripts?|swift|python|javascript|typescript|sql)\b|代码|编程|程序|仓库|调试|重构|修复|开发|单元测试|构建|脚本",
        text,
    ) or re.search(r"\.(py|swift|js|jsx|ts|tsx|rs|go|java|c|cpp|sh|sql|html|css)(?:\n|$)", paths):
        result.append("coding")
    if re.search(
        r"\b(research|sources?|sourced|citations?|cite|investigate|survey|literature|search|browse|web|evidence|compare|comparison)\b|研究|调研|检索|搜索|引用|文献|来源|网页|对比|比较|调查",
        text,
    ):
        result.append("research")
    return result


def compose(
    purpose: Purpose,
    schema: type[BaseModel],
    *,
    tool_access: str = "full",
    guidance: list[Guidance] | None = None,
) -> PromptSnapshot:
    ids = [purpose]
    if purpose == "planner":
        ids += ["tools-full" if tool_access == "full" else "tools-scoped"]
        ids += [name for name in ("coding", "research") if name in (guidance or [])]
    modules = [module(key).model_copy(deep=True) for key in ids]
    instructions = "\n\n".join(item.text.strip() for item in modules) + "\n"
    data = {
        "purpose": purpose,
        "modules": [item.model_dump() for item in modules],
        "instructions": instructions,
        "output_schema": json.dumps(
            schema.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True
        ),
    }
    return PromptSnapshot(snapshot_id=_digest(_canonical(data)), **data)


def compare(saved: PromptSnapshot, current: PromptSnapshot) -> dict:
    # Include output-schema changes, even when instruction modules are identical.
    def rendered(value):
        return value.instructions + "\nOUTPUT SCHEMA\n" + value.output_schema

    return {
        "snapshot": saved,
        "current": current,
        "changed": saved.snapshot_id != current.snapshot_id,
        "diff": "".join(
            difflib.unified_diff(
                rendered(saved).splitlines(keepends=True),
                rendered(current).splitlines(keepends=True),
                fromfile="recorded",
                tofile="installed",
            )
        ),
    }
