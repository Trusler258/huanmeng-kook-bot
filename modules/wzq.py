"""
五子棋游戏逻辑模块
- 15x15 棋盘，按 chat 隔离对局
- 支持 duel/accept/落子/认输/board/status/cancel
- 五连检测，长连(>5)算胜
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.logger import get_logger

# 专用线程池，不抢默认 executor（避免阻塞 Playwright 渲染等 IO 操作）
_AI_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wzq_ai")

logger = get_logger("wzq")

# ════════════════════════════════════════════════════════════
#  数据结构
# ════════════════════════════════════════════════════════════

BOARD_SIZE = 15
COL_LABELS = "ABCDEFGHIJKLMNO"

DIRECTIONS = [
    (1, 0),   # 竖
    (0, 1),   # 横
    (1, 1),   # 斜↘
    (1, -1),  # 斜↙
]


@dataclass
class WzqGame:
    black: int          # 黑方 QQ
    white: int          # 白方 QQ (0=AI)
    board: list = field(default_factory=lambda: [[0] * BOARD_SIZE for _ in range(BOARD_SIZE)])
    turn: int = 1               # 1=黑, 2=白
    move_count: int = 0
    status: str = "waiting"     # waiting/playing/finished
    winner: int | None = None   # 1/2/None(平局)
    last_move: tuple | None = None
    start_time: float = 0
    last_move_time: float = 0
    undo_request: int | None = None  # 谁申请了悔棋
    move_history: list = field(default_factory=list)
    ai_difficulty: str = ""     # ""=PVP, "easy"/"normal"/"hard"/"expert"
    forbidden_enabled: bool = True  # 禁手规则 (仅黑方)


# 按 chat_id 存储对局
_games: dict[int, WzqGame] = {}
_SAVE_FILE = Path(__file__).resolve().parent.parent / "data" / "wzq_games.json"


# ════════════════════════════════════════════════════════════
#  持久化
# ════════════════════════════════════════════════════════════

def _game_to_dict(game: WzqGame) -> dict:
    """序列化棋局为字典"""
    return {
        "black": game.black,
        "white": game.white,
        "board": game.board,
        "turn": game.turn,
        "move_count": game.move_count,
        "status": game.status,
        "winner": game.winner,
        "last_move": list(game.last_move) if game.last_move else None,
        "start_time": game.start_time,
        "last_move_time": game.last_move_time,
        "undo_request": game.undo_request,
        "move_history": [(r, c, t) for r, c, t in game.move_history],
        "ai_difficulty": game.ai_difficulty,
        "forbidden_enabled": game.forbidden_enabled,
    }


def _dict_to_game(d: dict) -> WzqGame:
    """反序列化字典为棋局"""
    return WzqGame(
        black=d["black"],
        white=d["white"],
        board=d["board"],
        turn=d["turn"],
        move_count=d["move_count"],
        status=d["status"],
        winner=d.get("winner"),
        last_move=tuple(d["last_move"]) if d.get("last_move") else None,
        start_time=d.get("start_time", time.time()),
        last_move_time=d.get("last_move_time", 0),
        undo_request=d.get("undo_request"),
        move_history=[(r, c, t) for r, c, t in d.get("move_history", [])],
        ai_difficulty=d.get("ai_difficulty", ""),
        forbidden_enabled=d.get("forbidden_enabled", True),
    )


def save_games():
    """持久化所有进行中的对局"""
    data = {}
    for chat_id, game in _games.items():
        if game.status in ("playing", "waiting"):
            data[str(chat_id)] = _game_to_dict(game)
    if data:
        _SAVE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    elif _SAVE_FILE.exists():
        _SAVE_FILE.unlink()


def load_games():
    """启动时恢复进行中的对局"""
    if not _SAVE_FILE.exists():
        return
    try:
        data = json.loads(_SAVE_FILE.read_text(encoding="utf-8"))
        count = 0
        for chat_id_str, d in data.items():
            chat_id = int(chat_id_str)
            if chat_id not in _games:
                _games[chat_id] = _dict_to_game(d)
                count += 1
            logger.info("恢复对局: chat=%d status=%s moves=%d", chat_id, d["status"], d["move_count"])
        if count:
            logger.info("共恢复 %d 个棋局", count)
        _SAVE_FILE.unlink()  # 恢复后清理
    except Exception as e:
        logger.warning("棋局恢复失败: %s", e)


# ════════════════════════════════════════════════════════════
#  游戏操作
# ════════════════════════════════════════════════════════════

def get_game(chat_id: int) -> WzqGame | None:
    return _games.get(chat_id)


def create_duel(chat_id: int, black: int, white: int, forbidden: bool = True) -> str:
    """发起挑战。返回提示文本。"""
    if black == white:
        return "不能自己跟自己下喵~ 不过你真想的话...找个人@一下吧"

    existing = _games.get(chat_id)
    if existing and existing.status != "finished":
        return "当前有对局进行中喵~ 等结束了再来"

    game = WzqGame(black=black, white=white, start_time=time.time(),
                   forbidden_enabled=forbidden)
    _games[chat_id] = game
    save_games()
    fb = " (无禁手)" if not forbidden else ""
    logger.info("五子棋挑战: chat=%d black=%d white=%d forbidden=%s", chat_id, black, white, forbidden)
    return f"waiting{fb}"


def accept_duel(chat_id: int, user_id: int) -> str:
    """接受挑战。返回提示文本或 None(开始游戏)。"""
    game = _games.get(chat_id)
    if not game:
        return "没有待接受的挑战喵~"

    if game.status != "waiting":
        return "这个挑战已经被处理了喵~"

    if user_id != game.white:
        return "只有被挑战的人才能接受喵~"

    game.status = "playing"
    game.last_move_time = time.time()
    save_games()
    logger.info("五子棋开始: chat=%d black=%d white=%d", chat_id, game.black, game.white)
    return "started"


def decline_duel(chat_id: int, user_id: int) -> str:
    """拒绝挑战。返回提示文本。"""
    game = _games.get(chat_id)
    if not game or game.status != "waiting":
        return "没有待处理的挑战喵~"
    if user_id != game.white:
        return "只有被挑战的人才能拒绝喵~"
    del _games[chat_id]
    save_games()
    return "挑战已拒绝"


def make_move(chat_id: int, user_id: int, row: int, col: int) -> tuple[bool, str]:
    """落子。返回 (是否成功, 提示)"""
    game = _games.get(chat_id)
    if not game or game.status != "playing":
        return False, "当前没有进行中的对局喵~"

    # 轮次检查
    expected_user = game.black if game.turn == 1 else game.white
    if user_id != expected_user:
        return False, "还没轮到你呢，耐心点喵~"

    # 位置检查
    if not (0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE):
        return False, "棋盘只有 A1 到 O15 喵~"
    if game.board[row][col] != 0:
        return False, "这里已经有子了喵~"

    # 落子
    game.board[row][col] = game.turn
    game.move_history.append((row, col, game.turn))
    game.move_count += 1
    game.last_move = (row, col)
    game.last_move_time = time.time()
    game.undo_request = None  # 落子后清除悔棋请求

    # 禁手检查（仅黑方）
    if game.forbidden_enabled and game.turn == 1:
        reason = _check_forbidden(game.board, row, col)
        if reason:
            # 禁手犯规，判黑负
            game.status = "finished"
            game.winner = 2
            from datetime import datetime
            _save_result(game, chat_id, datetime.now().strftime("%Y-%m-%d %H:%M"))
            save_games()  # 清理持久化（已结束）
            return True, f"forbidden:{reason}"

    # 判赢
    if _check_win(game.board, row, col, game.turn):
        game.status = "finished"
        game.winner = game.turn
        from datetime import datetime
        _save_result(game, chat_id, datetime.now().strftime("%Y-%m-%d %H:%M"))
        save_games()
        logger.info("五子棋结束: chat=%d winner=%d moves=%d", chat_id, game.turn, game.move_count)
        return True, "win"

    # 平局
    if game.move_count >= BOARD_SIZE * BOARD_SIZE:
        game.status = "finished"
        from datetime import datetime
        _save_result(game, chat_id, datetime.now().strftime("%Y-%m-%d %H:%M"))
        save_games()
        return True, "draw"

    # 换手
    game.turn = 3 - game.turn
    save_games()
    return True, "ok"


def surrender(chat_id: int, user_id: int) -> tuple[bool, str]:
    """认输。"""
    game = _games.get(chat_id)
    if not game or game.status != "playing":
        return False, "当前没有进行中的对局喵~"
    if user_id not in (game.black, game.white):
        return False, "你不是这局的参与者喵~"

    game.status = "finished"
    game.winner = 1 if user_id == game.white else 2  # 认输者对方赢
    from datetime import datetime
    _save_result(game, chat_id, datetime.now().strftime("%Y-%m-%d %H:%M"))
    save_games()
    return True, "surrender"


def cancel_duel(chat_id: int, user_id: int) -> str:
    """取消未开始的挑战"""
    game = _games.get(chat_id)
    if not game or game.status != "waiting":
        return "没有待取消的挑战喵~"
    if user_id != game.black:
        return "只有发起者才能取消喵~"
    del _games[chat_id]
    save_games()
    return "挑战已取消"


def request_undo(chat_id: int, user_id: int) -> str:
    """申请悔棋"""
    game = _games.get(chat_id)
    if not game or game.status != "playing":
        return "当前没有进行中的对局喵~"
    if user_id not in (game.black, game.white):
        return "你不是这局的参与者喵~"
    if game.move_count < 2:
        return "还没走几步呢，悔什么棋喵~"

    game.undo_request = user_id
    return "undo_request"


def confirm_undo(chat_id: int, user_id: int) -> tuple[bool, str]:
    """确认悔棋"""
    game = _games.get(chat_id)
    if not game or game.status != "playing":
        return False, "当前没有进行中的对局喵~"
    if game.undo_request is None:
        return False, "没有待处理的悔棋申请喵~"
    if user_id == game.undo_request:
        return False, "自己申请的自己不能确认喵~"

    # 回退两步（对手+自己）
    for _ in range(2):
        if game.move_history:
            r, c, t = game.move_history.pop()
            game.board[r][c] = 0
            game.move_count -= 1
    game.turn = 1 if game.move_count % 2 == 0 else 2
    game.undo_request = None
    game.last_move = game.move_history[-1][:2] if game.move_history else None
    save_games()
    return True, "悔棋成功"


def force_end(chat_id: int) -> str:
    """强制结束当前对局（超时等）"""
    game = _games.get(chat_id)
    if not game:
        return "当前没有对局"
    status = game.status
    del _games[chat_id]
    save_games()
    return f"对局已结束 (状态={status})"


# ════════════════════════════════════════════════════════════
#  判赢逻辑
# ════════════════════════════════════════════════════════════

def _check_win(board: list, row: int, col: int, stone: int) -> bool:
    for dr, dc in DIRECTIONS:
        count = 1
        win_positions = [(row, col)]

        # 正方向
        r, c = row + dr, col + dc
        while 0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE and board[r][c] == stone:
            win_positions.append((r, c))
            count += 1
            r += dr
            c += dc

        # 反方向
        r, c = row - dr, col - dc
        while 0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE and board[r][c] == stone:
            win_positions.append((r, c))
            count += 1
            r -= dr
            c -= dc

        if count >= 5:
            return True

    return False


# ════════════════════════════════════════════════════════════
#  禁手检测（仅黑方）
# ════════════════════════════════════════════════════════════

def _count_line(board: list, r: int, c: int, dr: int, dc: int, stone: int) -> tuple[int, bool, bool]:
    """统计 (r,c) 位置在 (dr,dc) 方向连续 stone 数。
    返回 (连续数, 正向是否开放, 反向是否开放)"""
    count = 1
    r1, c1 = r + dr, c + dc
    while 0 <= r1 < BOARD_SIZE and 0 <= c1 < BOARD_SIZE and board[r1][c1] == stone:
        count += 1; r1 += dr; c1 += dc
    open_pos = 0 <= r1 < BOARD_SIZE and 0 <= c1 < BOARD_SIZE and board[r1][c1] == 0
    r2, c2 = r - dr, c - dc
    while 0 <= r2 < BOARD_SIZE and 0 <= c2 < BOARD_SIZE and board[r2][c2] == stone:
        count += 1; r2 -= dr; c2 -= dc
    open_neg = 0 <= r2 < BOARD_SIZE and 0 <= c2 < BOARD_SIZE and board[r2][c2] == 0
    return count, open_pos, open_neg


def _is_four(board: list, r: int, c: int, dr: int, dc: int) -> bool:
    cnt, op, on = _count_line(board, r, c, dr, dc, 1)
    return cnt == 4 and (op or on)


def _is_three(board: list, r: int, c: int, dr: int, dc: int) -> bool:
    cnt, op, on = _count_line(board, r, c, dr, dc, 1)
    return cnt == 3 and op and on


def _check_forbidden(board: list, row: int, col: int) -> str:
    """黑方禁手检测。返回空=合法，否则=禁手原因"""
    three_count = 0; four_count = 0
    for dr, dc in DIRECTIONS:
        cnt, _, _ = _count_line(board, row, col, dr, dc, 1)
        if cnt >= 6:
            return "长连禁手（6子及以上）"
        if _is_four(board, row, col, dr, dc):
            four_count += 1
        if _is_three(board, row, col, dr, dc):
            three_count += 1
    if four_count >= 2:
        return "四四禁手"
    if three_count >= 2:
        return "三三禁手"
    return ""


# ════════════════════════════════════════════════════════════
#  坐标解析
# ════════════════════════════════════════════════════════════

def parse_coord(text: str) -> tuple[int, int] | None:
    """解析坐标: H8 / H,8 / H 8 / 8,8 / (8,8) / 8 8"""
    t = text.strip().upper()
    # 字母+数字 (无分隔): H8, h8
    m = re.match(r'^([A-O])\s*(\d{1,2})$', t)
    if m:
        col = COL_LABELS.index(m.group(1))
        row = int(m.group(2)) - 1
        if 0 <= row < BOARD_SIZE:
            return (row, col)
    # 字母+分隔符+数字: H,8 / H 8 / H-8
    m = re.match(r'^([A-O])\s*[,，\s-]\s*(\d{1,2})$', t)
    if m:
        col = COL_LABELS.index(m.group(1))
        row = int(m.group(2)) - 1
        if 0 <= row < BOARD_SIZE:
            return (row, col)
    # 数字,数字: 8,8 / (8,8) / 8 8
    m = re.match(r'\(?\s*(\d{1,2})\s*[,，\s-]\s*(\d{1,2})\s*\)?', t)
    if m:
        row = int(m.group(1)) - 1
        col = int(m.group(2)) - 1
        if 0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE:
            return (row, col)
    return None


def coord_label(row: int, col: int) -> str:
    return f"{COL_LABELS[col]}{row + 1}"


async def render_test_board(cfg) -> str | None:
    """渲染测试棋盘：Player1 vs Player2，展示各种落子场景"""
    game = WzqGame(black=0, white=0)
    game.status = "playing"
    game.move_count = 9
    game.turn = 1
    game.last_move = (7, 7)

    # 黑子形成活三
    game.board[2][2] = 1; game.board[2][3] = 1; game.board[2][4] = 1
    game.board[3][4] = 1; game.board[4][4] = 1
    # 白子右下
    game.board[10][10] = 2; game.board[10][11] = 2
    game.board[11][10] = 2; game.board[11][12] = 2
    # 天元
    game.board[7][7] = 1; game.board[6][6] = 2

    return await render_board(0, cfg, test_mode=True, test_game=game)


# ════════════════════════════════════════════════════════════
#  测试渲染
# ════════════════════════════════════════════════════════════

async def render_test_board(cfg) -> str | None:
    """渲染测试棋盘：Player1 vs Player2，展示各种落子场景"""
    from datetime import datetime

    game = WzqGame(black=999999, white=888888)
    game.status = "playing"
    game.move_count = 12
    game.turn = 1
    game.start_time = datetime.now().timestamp()
    game.last_move = (7, 7)  # H8

    # 布一个有趣的测试棋盘
    # 黑子在左上角形成活三
    game.board[2][2] = 1  # C3
    game.board[2][3] = 1  # D3
    game.board[2][4] = 1  # E3
    game.board[3][4] = 1  # E4
    game.board[4][4] = 1  # E5
    # 白子在右下角
    game.board[10][10] = 2  # K11
    game.board[10][11] = 2  # L11
    game.board[10][12] = 2  # M11
    game.board[11][10] = 2  # K12
    game.board[11][12] = 2  # M12
    # 天元附近混战
    game.board[7][7] = 1  # H8 (最后一手)
    game.board[6][6] = 2  # G7
    game.board[7][6] = 1  # G8

    # 临时注册并渲染
    test_id = 0
    _games[test_id] = game

    try:
        img = await render_board(test_id, cfg, test_mode=True, test_game=game)
        return img
    finally:
        _games.pop(test_id, None)


# ════════════════════════════════════════════════════════════
#  战绩永久存储
# ════════════════════════════════════════════════════════════

_RESULTS_FILE = Path(__file__).resolve().parent.parent / "data" / "wzq_results.json"


def _save_result(game: WzqGame, chat_id: int, date_str: str):
    """保存对局结果到永久存储"""
    results = []
    if _RESULTS_FILE.exists():
        try:
            results = json.loads(_RESULTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            results = []

    # 保存最终棋盘状态
    final_board = [row[:] for row in game.board]

    results.append({
        "time": date_str,
        "chat_id": chat_id,
        "black": game.black,
        "white": game.white,
        "winner": game.winner,  # 1=黑, 2=白, None=平
        "moves": game.move_count,
        "board": final_board,
    })

    # 保留最近 200 条
    if len(results) > 200:
        results = results[-200:]

    _RESULTS_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("五子棋结果已保存: black=%d white=%d winner=%s moves=%d",
               game.black, game.white, game.winner, game.move_count)


def get_history(chat_id: int = 0, limit: int = 10) -> list[dict]:
    """获取历史对局记录。chat_id=0 表示全部。"""
    if not _RESULTS_FILE.exists():
        return []
    try:
        results = json.loads(_RESULTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if chat_id:
        results = [r for r in results if r["chat_id"] == chat_id]
    return list(reversed(results[-limit:]))


def finish_game(chat_id: int) -> dict | None:
    """结束游戏并返回结果摘要"""
    game = _games.pop(chat_id, None)
    if not game or game.status != "finished":
        return None
    return {"black": game.black, "white": game.white, "winner": game.winner, "moves": game.move_count}


# ════════════════════════════════════════════════════════════
#  卡片渲染
# ════════════════════════════════════════════════════════════

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "data" / "templates" / "wzq_board.html"


# ════════════════════════════════════════════════════════════
#  棋盘渲染
# ════════════════════════════════════════════════════════════

async def render_history_board(record: dict, cfg) -> str | None:
    """渲染历史对局的最终棋盘卡片"""
    game = WzqGame(black=record.get("black", 0), white=record.get("white", 0))
    game.status = "finished"
    game.winner = record.get("winner")
    game.move_count = record.get("moves", 0)
    saved_board = record.get("board")
    if saved_board:
        game.board = saved_board
    game.last_move = None  # 历史对局不高亮最后一手
    return await render_board(-1, cfg, test_mode=True, test_game=game, history_date=record.get("time", ""))


async def render_board(chat_id: int, cfg, test_mode: bool = False,
                       test_game: WzqGame = None, history_date: str = "") -> str | None:
    """生成棋盘卡片图片，返回本地文件路径或 None"""
    from pathlib import Path as _Path
    game = test_game if test_mode else get_game(chat_id)
    if not game:
        return None

    tmpl = _TEMPLATE_PATH.read_text(encoding="utf-8")

    # 列标签 A B C ... O
    col_labels_html = "".join(
        f'<div class="col-label">{c}</div>' for c in COL_LABELS
    )

    # 棋盘格子（行1在底部，行15在顶部）
    star_points = {(3, 3), (3, 7), (3, 11), (7, 3), (7, 7), (7, 11), (11, 3), (11, 7), (11, 11)}
    cells_html = ""
    for r in reversed(range(BOARD_SIZE)):
        cells_html += f'<div class="row-label">{r + 1}</div>'
        for c in range(BOARD_SIZE):
            stone = game.board[r][c]
            content = ""
            if stone != 0:
                color = "black" if stone == 1 else "white"
                last = " last-move" if game.last_move and game.last_move == (r, c) else ""
                content = f'<div class="stone-piece {color}{last}"></div>'
            elif (r, c) in star_points:
                content = '<div class="star-point"></div>'
            cells_html += f'<div class="cell">{content}</div>'

    # 玩家名
    if test_mode:
        black_name = "Player1"
        white_name = "Player2"
    else:
        def qq_name(qq: int) -> str:
            return cfg.qq_name_map.get(str(qq), str(qq))
        black_name = qq_name(game.black)
        white_name = qq_name(game.white)

    # 激活状态
    black_active = "active-turn" if game.status == "playing" and game.turn == 1 else ""
    white_active = "active-turn" if game.status == "playing" and game.turn == 2 else ""

    # 状态文本
    if game.status == "waiting":
        status_class = "waiting"
        status_text = f"等待 {white_name} 接受挑战... (/{'~'}wzq accept)"
    elif game.status == "playing":
        current = black_name if game.turn == 1 else white_name
        status_class = "playing"
        status_text = f"轮到 {current} 落子 ({'黑' if game.turn == 1 else '白'})"
    elif game.winner == 1:
        status_class = "win"
        status_text = f"{black_name} (黑) 获胜！"
    elif game.winner == 2:
        status_class = "win"
        status_text = f"{white_name} (白) 获胜！"
    else:
        status_class = "draw"
        status_text = "平局！"

    sub_text = f"手数 {game.move_count} | {status_text.split('：')[0] if '：' in status_text else status_text}"
    from datetime import datetime
    date_str = history_date if history_date else (datetime.now().strftime("%Y-%m-%d %H:%M") if not test_mode else "12:34:56")

    html = tmpl \
        .replace("${SUB_TEXT}", sub_text) \
        .replace("${DATE}", date_str) \
        .replace("${BLACK_NAME}", black_name) \
        .replace("${WHITE_NAME}", white_name) \
        .replace("${BLACK_ACTIVE}", black_active) \
        .replace("${WHITE_ACTIVE}", white_active) \
        .replace("${COL_LABELS}", col_labels_html) \
        .replace("${CELLS}", cells_html) \
        .replace("${STATUS_CLASS}", status_class) \
        .replace("${STATUS_TEXT}", status_text) \
        .replace("${MOVE_COUNT}", str(game.move_count)) \
        .replace("${DATE}", date_str)

    from modules.changelog import render_card_to_image
    import uuid
    filename = f"wzq_{chat_id}_{uuid.uuid4().hex[:8]}.png"
    return await render_card_to_image(html, filename, width=680)


# ════════════════════════════════════════════════════════════
#  AI 对手 — Minimax + Alpha-Beta Pruning
# ════════════════════════════════════════════════════════════

import random as _random
from math import inf as _INF


# ── 棋盘评估 ────────────────────────────────────────────────

# 单线模式评分
_LINE_SCORES: dict[int, dict[int, int]] = {
    # count → { open_ends → score }
    5: {0: 1_000_000, 1: 1_000_000, 2: 1_000_000},  # 五连必胜
    4: {0: 0,         1: 1_000,     2: 50_000},       # 0端=死四, 1端=冲四, 2端=活四
    3: {0: 0,         1: 100,       2: 1_000},         # 1端=眠三, 2端=活三
    2: {0: 0,         1: 10,        2: 100},           # 1端=眠二, 2端=活二
    1: {0: 0,         1: 1,         2: 10},            # 1端=单子, 2端=活一
}


def _evaluate_board(board: list, player: int) -> int:
    """全盘扫描评估，返回 player 视角的净优势分"""
    opponent = 3 - player
    my_score = 0
    op_score = 0

    # 扫描所有行
    for r in range(BOARD_SIZE):
        my_score += _scan_line(board, r, 0, 0, 1, player)
        op_score += _scan_line(board, r, 0, 0, 1, opponent)

    # 扫描所有列
    for c in range(BOARD_SIZE):
        my_score += _scan_line(board, 0, c, 1, 0, player)
        op_score += _scan_line(board, 0, c, 1, 0, opponent)

    # 扫描对角线 (↘)
    for start_r in range(BOARD_SIZE):
        my_score += _scan_line(board, start_r, 0, 1, 1, player)
        op_score += _scan_line(board, start_r, 0, 1, 1, opponent)
    for start_c in range(1, BOARD_SIZE):
        my_score += _scan_line(board, 0, start_c, 1, 1, player)
        op_score += _scan_line(board, 0, start_c, 1, 1, opponent)

    # 扫描反对角线 (↙)
    for start_r in range(BOARD_SIZE):
        my_score += _scan_line(board, start_r, BOARD_SIZE - 1, 1, -1, player)
        op_score += _scan_line(board, start_r, BOARD_SIZE - 1, 1, -1, opponent)
    for start_c in range(BOARD_SIZE - 1):
        my_score += _scan_line(board, 0, start_c, 1, -1, player)
        op_score += _scan_line(board, 0, start_c, 1, -1, opponent)

    # 防守偏置：对手的活四/活三对我威胁更大
    return my_score - int(op_score * 1.1)


def _scan_line(board: list, r: int, c: int, dr: int, dc: int, stone: int) -> int:
    """沿方向扫描一行，累加 stone 的所有模式分"""
    score = 0
    while 0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE:
        if board[r][c] == stone:
            # 从 (r,c) 开始统计该连续段
            cnt = 0
            sr, sc = r, c
            while 0 <= sr < BOARD_SIZE and 0 <= sc < BOARD_SIZE and board[sr][sc] == stone:
                cnt += 1
                sr += dr
                sc += dc
            # 检查两端
            open_ends = 0
            if 0 <= r - dr < BOARD_SIZE and 0 <= c - dc < BOARD_SIZE and board[r - dr][c - dc] == 0:
                open_ends += 1
            if 0 <= sr < BOARD_SIZE and 0 <= sc < BOARD_SIZE and board[sr][sc] == 0:
                open_ends += 1
            # 查表评分
            if cnt in _LINE_SCORES:
                score += _LINE_SCORES[cnt].get(open_ends, 0)
            elif cnt > 5:
                score += 1_000_000  # 超过五连也计为胜
            r, c = sr, sc
        else:
            r += dr
            c += dc
    return score


# ── 候选走法 ────────────────────────────────────────────────

def _get_neighbor_cells(board: list, radius: int = 2) -> list[tuple[int, int]]:
    """返回已有棋子周围 radius 格内的空位（去重）"""
    seen = set()
    result = []
    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
            if board[r][c] != 0:
                for dr in range(-radius, radius + 1):
                    for dc in range(-radius, radius + 1):
                        nr, nc = r + dr, c + dc
                        if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and board[nr][nc] == 0:
                            key = (nr << 8) | nc
                            if key not in seen:
                                seen.add(key)
                                result.append((nr, nc))
    return result


# ── 走法排序（提升 α-β 剪枝效率） ──────────────────────────

def _quick_eval(board: list, r: int, c: int, stone: int) -> int:
    """快速评估在 (r,c) 落 stone 的立即威胁分（不落子，用 _count_line）"""
    score = 0
    for dr, dc in DIRECTIONS:
        cnt, op, on = _count_line(board, r, c, dr, dc, stone)
        open_ends = (1 if op else 0) + (1 if on else 0)
        if cnt >= 5: score += 1_000_000
        elif cnt in _LINE_SCORES: score += _LINE_SCORES[cnt].get(open_ends, 0)
    return score


def _order_moves(board: list, moves: list, player: int, opponent: int) -> list:
    """按威胁程度排序走法：先搜高分走法，剪枝效果最好"""
    scored = []
    for r, c in moves:
        atk = _quick_eval(board, r, c, player)
        deff = _quick_eval(board, r, c, opponent)
        # 五连/活四的走法优先级最高
        s = atk + deff
        if atk >= 50_000 or deff >= 50_000:
            s += 1_000_000
        scored.append((s, r, c))
    scored.sort(reverse=True, key=lambda x: x[0])
    return [(r, c) for _, r, c in scored]


# ── Minimax + Alpha-Beta ────────────────────────────────────

def _minimax(board: list, depth: int, alpha: float, beta: float,
             is_max: bool, player: int, opponent: int,
             deadline: float = _INF) -> float:
    """Minimax 搜索，返回棋盘评估分（player 视角）"""
    import time as _t
    if _t.time() >= deadline:
        raise TimeoutError("minimax timeout")

    # 终局检测
    if _check_win_from_last(board, opponent):
        return -100_000 - depth
    if depth == 0:
        return float(_evaluate_board(board, player))

    moves = _get_neighbor_cells(board, radius=2)
    if not moves:
        return float(_evaluate_board(board, player))

    ordered = _order_moves(board, moves, player if is_max else opponent,
                           opponent if is_max else player)

    if is_max:
        best = -_INF
        for r, c in ordered:
            board[r][c] = player
            val = _minimax(board, depth - 1, alpha, beta, False, player, opponent, deadline)
            board[r][c] = 0
            if val > best:
                best = val
            if best > alpha:
                alpha = best
            if beta <= alpha:
                break
        return best
    else:
        best = _INF
        for r, c in ordered:
            board[r][c] = opponent
            val = _minimax(board, depth - 1, alpha, beta, True, player, opponent, deadline)
            board[r][c] = 0
            if val < best:
                best = val
            if best < beta:
                beta = best
            if beta <= alpha:
                break
        return best


def _check_win_from_last(board: list, stone: int) -> bool:
    """检查 stone 是否在棋盘上任一处五连（终局检测用）"""
    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
            if board[r][c] == stone:
                for dr, dc in DIRECTIONS:
                    cnt = 1
                    nr, nc = r + dr, c + dc
                    while 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and board[nr][nc] == stone:
                        cnt += 1
                        nr += dr
                        nc += dc
                    nr, nc = r - dr, c - dc
                    while 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and board[nr][nc] == stone:
                        cnt += 1
                        nr -= dr
                        nc -= dc
                    if cnt >= 5:
                        return True
    return False


def _ai_minimax_move(board: list, stone: int, depth: int,
                     deadline: float = _INF) -> tuple[int, int]:
    """Minimax 选择最佳落子"""
    import time as _t
    opponent = 3 - stone
    moves = _get_neighbor_cells(board, radius=2)
    if not moves:
        return (7, 7)

    total_stones = sum(1 for r in range(BOARD_SIZE) for c in range(BOARD_SIZE) if board[r][c] != 0)
    if total_stones == 0:
        return (7, 7)

    # 立即防守
    for r, c in moves:
        board[r][c] = opponent
        won = _check_win_from_last(board, opponent)
        board[r][c] = 0
        if won:
            return (r, c)
    for r, c in moves:
        if _quick_eval(board, r, c, opponent) >= 50_000:
            return (r, c)

    ordered = _order_moves(board, moves, stone, opponent)
    best_val = -_INF
    best_move = ordered[0]

    for r, c in ordered:
        if _t.time() >= deadline:
            break
        board[r][c] = stone
        try:
            val = _minimax(board, depth - 1, -_INF, _INF, False, stone, opponent, deadline)
        except TimeoutError:
            board[r][c] = 0
            break
        board[r][c] = 0
        if val > best_val:
            best_val = val
            best_move = (r, c)

    return best_move


# ── 难度包装 ────────────────────────────────────────────────

def _ai_easy(board: list, stone: int) -> tuple[int, int]:
    """新手：深度 1 + 30% 随机"""
    row, col = _ai_minimax_move(board, stone, depth=1)
    if _random.random() < 0.3:
        moves = _get_neighbor_cells(board, radius=2)
        if moves:
            return _random.choice(moves)
    return (row, col)


def _ai_normal(board: list, stone: int) -> tuple[int, int]:
    """普通：深度 2"""
    return _ai_minimax_move(board, stone, depth=2)


def _ai_hard(board: list, stone: int) -> tuple[int, int]:
    """困难：深度 2 + 更优走法排序"""
    return _ai_minimax_move(board, stone, depth=2)


def _ai_expert(board: list, stone: int) -> tuple[int, int]:
    """专家：迭代加深 depth 1→2→3，最多 8 秒"""
    import time as _t
    deadline = _t.time() + 8.0
    best = (7, 7)

    for d in (1, 2, 3):
        try:
            move = _ai_minimax_move(board, stone, depth=d, deadline=deadline)
            if move:
                best = move
        except TimeoutError:
            break
        if _t.time() >= deadline:
            break

    return best


# ── 公共入口 ────────────────────────────────────────────────

def _get_empty_cells(board: list) -> list[tuple[int, int]]:
    cells = []
    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
            if board[r][c] == 0:
                cells.append((r, c))
    return cells


def _ai_compute(board: list, stone: int, difficulty: str) -> tuple[int, int]:
    """纯计算：根据难度走棋（线程安全，不修改全局状态）"""
    if difficulty == "easy":
        return _ai_easy(board, stone)
    elif difficulty == "normal":
        return _ai_normal(board, stone)
    elif difficulty == "hard":
        return _ai_hard(board, stone)
    else:
        return _ai_expert(board, stone)


def ai_move(chat_id: int) -> tuple[bool, str]:
    """AI 走一步（同步，供线程池调用）"""
    game = _games.get(chat_id)
    if not game or game.status != "playing":
        return False, "no_game"
    if game.ai_difficulty == "":
        return False, "not_ai"

    # 拷贝棋盘（避免线程竞态）
    board_copy = [row[:] for row in game.board]
    diff = game.ai_difficulty
    _diff_map = {"easy": "easy", "normal": "normal", "hard": "hard", "expert": "expert",
                 "新手": "easy", "普通": "normal", "困难": "hard", "专家": "expert"}
    diff = _diff_map.get(diff, "normal")
    opponent = 1

    import time as _time
    t0 = _time.time()
    try:
        r, c = _ai_compute(board_copy, opponent, diff)
    except Exception as e:
        logger.error("AI 落子失败: %s, 降级为随机", e)
        cells = _get_neighbor_cells(board_copy, radius=2) or [(7, 7)]
        r, c = _random.choice(cells)

    elapsed = _time.time() - t0
    _depth_map = {"easy": 1, "normal": 2, "hard": 2, "expert": 3}
    logger.info("AI(%s depth<=%s) 落子 %s %.1fs",
               diff, _depth_map[diff], coord_label(r, c), elapsed)

    return make_move(chat_id, 0, r, c)


async def ai_move_async(chat_id: int) -> tuple[bool, str]:
    """AI 走一步（异步，线程池执行不阻塞主线程）"""
    game = _games.get(chat_id)
    if not game or game.status != "playing" or game.ai_difficulty == "":
        return False, "not_ai"

    # 拷贝棋盘 + 难度（线程安全读取）
    board_copy = [row[:] for row in game.board]
    diff = game.ai_difficulty
    _diff_map = {"easy": "easy", "normal": "normal", "hard": "hard", "expert": "expert",
                 "新手": "easy", "普通": "normal", "困难": "hard", "专家": "expert"}
    diff = _diff_map.get(diff, "normal")
    opponent = 1

    import asyncio, time as _time
    t0 = _time.time()
    loop = asyncio.get_running_loop()

    try:
        r, c = await loop.run_in_executor(_AI_EXECUTOR, _ai_compute, board_copy, opponent, diff)
    except Exception as e:
        logger.error("AI 落子失败: %s, 降级为随机", e)
        cells = _get_neighbor_cells(board_copy, radius=2) or [(7, 7)]
        r, c = _random.choice(cells)

    elapsed = _time.time() - t0
    _depth_map = {"easy": 1, "normal": 2, "hard": 2, "expert": 3}
    logger.info("AI(%s depth<=%s) 落子 %s %.1fs",
               diff, _depth_map[diff], coord_label(r, c), elapsed)

    return make_move(chat_id, 0, r, c)


def create_duel_ai(chat_id: int, user_id: int, difficulty: str, forbidden: bool = True) -> str:
    """发起人机对战。用户执黑，AI 执白。"""
    existing = _games.get(chat_id)
    if existing and existing.status != "finished":
        return "当前有对局进行中喵~ 等结束了再来"

    game = WzqGame(black=user_id, white=0, start_time=time.time(),
                   ai_difficulty=difficulty, forbidden_enabled=forbidden)
    game.status = "playing"
    game.last_move_time = time.time()
    _games[chat_id] = game
    save_games()
    diff_names = {"新手": "新手", "普通": "普通", "困难": "困难", "专家": "专家", "easy": "新手", "normal": "普通", "hard": "困难", "expert": "专家"}
    fb = "" if forbidden else "(无禁手)"
    logger.info("五子棋人机: chat=%d user=%d difficulty=%s forbidden=%s", chat_id, user_id, difficulty, forbidden)
    return f"started_ai:{diff_names.get(difficulty, difficulty)}{fb}"
