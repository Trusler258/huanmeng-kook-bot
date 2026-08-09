"""回溯历史 msglog 建用户画像（一次性脚本）"""
import json, sys, os
from pathlib import Path

# 确保能 import core 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.user_profile import (_quick_extract, update_profile, get_profile,
                                build_profile_text, _load_all, _save_all)
from core.logger import get_logger
logger = get_logger("backfill")

MSGLOG_DIR = Path(__file__).resolve().parent.parent / "data" / "msglog"

def main():
    if not MSGLOG_DIR.exists():
        print(f"✗ {MSGLOG_DIR} 不存在")
        return

    files = sorted(MSGLOG_DIR.glob("msglog_*.jsonl"))
    if not files:
        print("✗ 无 msglog 文件")
        return

    total_msgs = 0
    total_users = set()
    total_hits = 0  # 成功提取次数

    for fpath in files:
        chat_id = fpath.stem.replace("msglog_", "")
        with open(fpath, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "文字":
                    continue
                uid = d.get("user_id", 0)
                content = d.get("content", "")
                if not uid or not content or len(content) < 4:
                    continue
                # 跳过 bot 自己
                if uid in (3682248514,):
                    continue

                total_msgs += 1
                total_users.add(uid)

                extracted = _quick_extract(content)
                if extracted:
                    update_profile(uid, extracted)
                    total_hits += 1

    print(f"\n{'='*50}")
    print(f"📊 回溯完成:")
    print(f"  文件: {len(files)} 个群")
    print(f"  消息: {total_msgs} 条 (已过滤)")
    print(f"  用户: {len(total_users)} 人")
    print(f"  提取: {total_hits} 次画像更新")
    print(f"{'='*50}\n")

    # 打印每个用户的画像
    data = _load_all()
    for uid, profile in sorted(data.items(), key=lambda x: -len(str(x[1]))):
        txt = build_profile_text(int(uid))
        if txt:
            print(f"\n--- uid={uid} ---")
            print(txt)


if __name__ == "__main__":
    main()
