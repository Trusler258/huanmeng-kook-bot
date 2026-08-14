"""Phase 6 Part4 测试：Context Builder + Prompt 预算 + Skill 按需加载"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.context_builder import (
    ContextProfile, truncate, discover_skills, load_skill,
    assemble_sections, reload_skill_discovery, DEFAULT_BUDGETS,
)


def test_truncate():
    s = "x" * 100
    out = truncate(s, 40)
    assert len(out) < 100 and "…" in out, out
    assert truncate("short", 100) == "short"
    assert truncate("exact", 5) == "exact"
    assert truncate("", 10) == ""
    print("OK test_truncate")


def test_discover_skills():
    meta = discover_skills()
    names = [m["name"] for m in meta]
    assert "prompt_header" in names, names
    assert "group_format" in names, names
    # metadata 不含正文：description 应很短
    for m in meta:
        assert len(m["description"]) <= 120, m
    print(f"OK test_discover_skills ({len(meta)} skills): {names}")


def test_load_skill_selected():
    full = load_skill("group_format", budget=0)  # budget=0 不截断 → 全文
    assert full and "##" in full, full
    capped = load_skill("group_format")  # 默认预算截断
    assert len(capped) <= DEFAULT_BUDGETS["skill"], len(capped)
    assert load_skill("不存在的skill") == ""
    print("OK test_load_skill_selected")


def test_assemble_sections_budget():
    profile = ContextProfile(budgets={"memory": 50, "skill": 30, "tool_result": 40})
    big = "m" * 1000
    out = assemble_sections(
        {"memory": big, "skill": "s" * 100, "tool_result": "t" * 200},
        profile=profile,
        order=["memory", "skill", "tool_result"],
    )
    assert "…" in out, out
    assert len(out) < 50 + 30 + 40 + 200, len(out)
    print("OK test_assemble_sections_budget")


def test_reload_discovery():
    reload_skill_discovery()
    assert discover_skills()  # reload 后仍能发现
    print("OK test_reload_discovery")


async def main():
    test_truncate()
    test_discover_skills()
    test_load_skill_selected()
    test_assemble_sections_budget()
    test_reload_discovery()
    print("\nALL Phase6-Part4 TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())