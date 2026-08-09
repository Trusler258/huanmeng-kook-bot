
def get_architecture_context() -> str:
    """按需加载完整架构（token消耗高，仅需要时调用）"""
    import re
    from pathlib import Path
    arch_path = Path(__file__).resolve().parent.parent / "data" / "architecture.mermaid"
    if not arch_path.exists():
        return ""
    try:
        arch_text = arch_path.read_text(encoding="utf-8")
        clean = re.sub(r"<br/>", " | ", arch_text)
        nodes = re.findall(r'\[([^\[\]]+)\]', clean)
        return "# 完整架构\n" + "\n".join(f"- {n.strip()}" for n in nodes)
    except Exception:
        return ""
