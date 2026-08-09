import time

_ignored = {}

_IGNORE_DURATION = 300  # 5分钟


def is_ignored(user_id):
    uid = str(user_id)
    if uid in _ignored:
        if time.time() < _ignored[uid]:
            return True
        del _ignored[uid]
    return False


def ignore_user(user_id):
    _ignored[str(user_id)] = time.time() + _IGNORE_DURATION


def unignore_user(user_id):
    _ignored.pop(str(user_id), None)


def remaining_seconds(user_id):
    uid = str(user_id)
    if uid in _ignored:
        left = _ignored[uid] - time.time()
        return int(left) if left > 0 else 0
    return 0
