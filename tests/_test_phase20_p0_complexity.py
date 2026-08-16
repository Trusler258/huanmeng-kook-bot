"""Phase 20 P0 复杂度/路由 回归验证："" + 代码"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.complexity import assess_complexity

cases = {
    # (msg, 期望level, 期望needs_agent)
    "mysql历史": ("knowledge", True),
    "说说mysql历史": ("knowledge", True),
    "讲解TCP三次握手原理": ("knowledge", True),
    "mysql和redis有什么区别": ("knowledge", True),
    "帮我分析这个项目并优化": ("task", True),
    "用python写一个2048小游戏": ("task", True),
    "帮我部署kook机器人": ("task", True),
    "为啥python的eval()不安全": ("knowledge", True),
    "eval咋不安全": ("knowledge", True),
    "什么是递归": ("knowledge", True),
    "tcp怎么实现可靠传输": ("knowledge", True),
    "python怎么用": ("knowledge", True),
    "递归是啥原理": ("knowledge", True),
    "你好": ("chat", False),
    "哈哈哈哈哈": ("chat", False),
    "好的谢谢": ("chat", False),
    "在吗": ("chat", False),
    "晚安": ("chat", False),
    "今天天气怎么样": ("chat", False),  # 知识词不多，偏chat/但可能knowledge；这里验证不误伤
}

fails = 0
for msg, (exp_level, exp_agent) in cases.items():
    cx = assess_complexity(msg)
    ok = (cx.level == exp_level and cx.needs_agent == exp_agent)
    print(f"[{'PASS' if ok else 'FAIL'}] {msg!r:30} -> level={cx.level:10} agent={cx.needs_agent} (exp {exp_level}/{exp_agent})")
    if not ok:
        fails += 1

# 验证输出预算/上下文扩容
cx_k = assess_complexity("mysql历史")
assert cx_k.output_max_tokens == 2000, cx_k.output_max_tokens
assert cx_k.context_scale == 1.6, cx_k.context_scale
assert "展开" in cx_k.detail_hint
cx_t = assess_complexity("帮我分析这个项目并优化")
assert cx_t.output_max_tokens == 3000, cx_t.output_max_tokens
assert cx_t.context_scale == 1.8, cx_t.context_scale
assert "执行" in cx_t.detail_hint
cx_c = assess_complexity("哈哈哈")
assert cx_c.output_max_tokens == 0 and cx_c.context_scale == 1.0

print("\n输出预算/扩容断言通过")
print(f"\n{'ALL PASS' if fails == 0 else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)