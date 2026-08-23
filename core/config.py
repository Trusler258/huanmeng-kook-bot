"""
统一配置管理器
- 一次性加载所有 .toml / .env 配置
- 提供 BotConfig @dataclass 封装所有运行时参数
- 支持 reload（重新加载所有配置文件）
- 所有模块通过 BotConfig 单例访问配置，避免重复读盘
"""

from __future__ import annotations
from core.arch_loader import get_architecture_context

import os
import toml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(path): pass  # noqa: E701,E301


# ── 项目根目录 ──────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"


@dataclass
class ModelConfig:
    """单个模型配置"""
    name: str = ""
    provider: str = ""
    url: str = ""
    key: str = ""
    maxtoken: int = 300
    switch: bool = False


@dataclass
class BotConfig:
    """全量配置 — 所有运行时参数集中在此"""

    # ── KOOK 连接 ──
    kook_token: str = ""              # KOOK Bot Token
    # host/port 保留给内部服务（PC状态/TTS/日志等）使用，不再用于 NapCat
    host: str = "0.0.0.0"
    port: int = 8099

    # ── 机器人身份 ──
    bot_name: str = "KookBot"  # 开源模板默认名，请在 bot_config.toml [bot] 段中修改为你的机器人名称
    bot_qq: int = 0                   # KOOK bot 的 user_id（转 int）
    bot_id_str: str = ""              # KOOK bot 的原始 user_id 字符串
    reply_interest: int = 10         # 回复兴趣阈值
    context_length: int = 20         # 消息上下文最大条数
    enable_private: bool = False     # 允许私聊
    debug_mode: bool = False         # 调试开关

    # ── 角色权限（KOOK 中 user_id/channel_id 转 int 存储）──
    admin_qq: int = 0
    admin_id_str: str = ""            # 管理员原始 user_id 字符串
    friend_qqs: list[int] = field(default_factory=list)
    qq_name_map: dict[str, str] = field(default_factory=dict)
    op_qqs: list[int] = field(default_factory=list)           # OP（次级管理员）列表
    group_owners: dict[int, list[int]] = field(default_factory=dict) # 频道OP指派: {channel_id: [op_user_id, ...]}
    
    # ── 模型配置 ──
    reply_model: ModelConfig = field(default_factory=ModelConfig)
    judge_model: ModelConfig = field(default_factory=ModelConfig)
    cheap_model: ModelConfig = field(default_factory=ModelConfig)
    image_model: ModelConfig = field(default_factory=ModelConfig)
    
    # ── 人设 ──
    personality_core: str = ""
    personality_side: str = ""
    identity: str = ""
    system_prompt: str = ""           # 组装后的完整提示词

    # ── 私聊专属人格基底（仅私聊生效，群聊用 personality_*）──
    private_persona_version: int = 0   # 全局版本号，递增后旧 per-user persona 自动失效
    private_persona_core: str = ""     # 私聊人格核心（留空回退 personality_core）
    private_persona_side: str = ""     # 私聊人格侧面（留空回退 personality_side）
    private_identity: str = ""         # 私聊身份设定（留空回退 identity）
    
    # ── 字频道白名单（KOOK channel_id 转 int）──
    group_list: list[int] = field(default_factory=list)
    # ── 私聊白名单（KOOK user_id 转 int）──
    private_whitelist: list[int] = field(default_factory=list)
    # ── 字频道自定义回复策略 ──
    # {channel_id: {"reply_threshold": 8, "at_only": False}}
    group_settings: dict[int, dict] = field(default_factory=dict)
    
    # ── 文本替换 ──
    replace_words: list[str] = field(default_factory=list)
    be_replaced_words: list[str] = field(default_factory=list)
    
    # ── 判断关键词 ──
    search_trigger_words: list[str] = field(default_factory=list)
    realtime_words: list[str] = field(default_factory=list)

    # ── 反刷屏禁言 ──
    spam_threshold: int = 8           # 触发禁言的重复@次数
    mute_duration: int = 1800         # 禁言时长（秒），0=仅警告
    
    # ── 构建完整系统提示词 ──
    def build_system_prompt(self, is_group: bool = True) -> str:
        # 加载服务器表情
        emoji_text = ""
        emoji_path = _CONFIG_DIR / "emoji.md"
        if emoji_path.exists():
            emoji_text = emoji_path.read_text(encoding="utf-8")
        identity_with_emoji = f"{self.identity}\n\n{emoji_text}" if emoji_text else self.identity

        # 自动加载 skills/ 下所有 .md 文件（按文件名排序，支持 @scope 过滤）
        skills_text = ""
        skills_dir = Path(__file__).resolve().parent.parent / "skills"
        if skills_dir.is_dir():
            skill_files = sorted(skills_dir.glob("*.md"))
            parts = []
            for sf in skill_files:
                try:
                    content = sf.read_text(encoding="utf-8").strip()
                    if not content:
                        continue
                    # @scope 过滤：私聊专属 skill 在群聊中跳过
                    if is_group and "@scope:private" in content[:120]:
                        continue
                    parts.append(content)
                except Exception:
                    pass
            skills_text = "\n\n".join(parts)

        self.system_prompt = (
            f"# 核心人格\n{self.personality_core}\n"
            f"---\n"
            f"# 侧面人格\n{self.personality_side}\n"
            f"---\n"
            f"# 固定身份\n{identity_with_emoji}\n"
            f"---\n"
            f"# 技能\n{skills_text}\n"
            f"---\n"
            f"{self._build_self_awareness()}"
        )
        return self.system_prompt

    def _build_self_awareness(self) -> str:
        """构建自我认知：从 update_log.md 提取版本信息"""
        import re

        # 从更新日志提取最新版本
        version = "v0.9.8 Pro"
        changelog_lines = ""
        log_path = Path(__file__).resolve().parent.parent / "data" / "update_log.md"
        if log_path.exists():
            try:
                ltext = log_path.read_text(encoding="utf-8")
                vm = re.search(r"## (v[\d.]+ .+?)(?=\n## |\Z)", ltext, re.DOTALL)
                if vm:
                    version = vm.group(1).strip().split("\n")[0].strip("#- ")
                    lines = vm.group(1).strip().split("\n")
                    lines = [l.strip("- # ").strip() for l in lines if l.strip() and not l.startswith("|") and not l.startswith("###")]
                    lines = [l for l in lines[:6] if l]
                    changelog_lines = "\n".join(f"- {l}" for l in lines)
            except Exception:
                pass

        # 架构
        arch_lines = ""
        arch_path = Path(__file__).resolve().parent.parent / "data" / "architecture.mermaid"
        if arch_path.exists():
            try:
                arch_text = arch_path.read_text(encoding="utf-8")
                clean = re.sub(r'<br/>', ' · ', arch_text)
                clean = re.sub(r'[\\"]', '', clean)
                nodes = re.findall(r'\[([^\]]+)\]', clean)
                arch_lines = "\n".join(f"- {n.strip()}" for n in nodes)
            except Exception:
                pass

        model_info = "DeepSeek(回复) + Zhipu(视觉) + DuckDuckGo(搜索)"

        # 构建自我认知
        parts = []
        parts.append(f"# 自我认知\n你是{self.bot_name} {version}。")
        if changelog_lines:
            parts.append(f"最新更新：\n{changelog_lines}")
        if arch_lines:
            parts.append(f"\n完整架构:\n{arch_lines}")
        parts.append(f"\n当前设置: 兴趣度阈值={self.reply_interest}, 上下文长度={self.context_length}条")
        parts.append(f"已接入: {model_info}")
        parts.append(f"主人 ID: {self.admin_qq}, 内部服务: {self.host}:{self.port}")
        return "\n".join(parts)
    
    def get_user_tag(self, user_id: int, group_id: int = 0) -> str:
        """根据 user_id 返回角色标签（支持分频道 OP 判断）"""
        if user_id == self.admin_qq:
            return "admin"
        # 分群 OP：在指派的群内标签为 op，不混用 admin
        if group_id and user_id in self.group_owners.get(group_id, []):
            return "op"
        if user_id in self.friend_qqs:
            return "friend"
        return "用户"

    def is_op(self, user_id: int) -> bool:
        """是否为 OP（次级管理员）"""
        return user_id in self.op_qqs

    def is_admin(self, user_id: int, group_id: int = 0) -> bool:
        """检查用户是否有 admin 权限（主人 + 分频道 OP 指派）"""
        if user_id == self.admin_qq:
            return True
        if group_id and user_id in self.group_owners.get(group_id, []):
            return True
        return False

    def group_ids(self) -> list[int]:
        """返回所有已知字频道 ID（从频道统计归档 + group_owners 中提取）"""
        from pathlib import Path
        import re
        ids = set()
        _p = Path(__file__).resolve().parent.parent
        # 统计归档（格式: stats_{channel_id}_{date}.json）
        archive = _p / "data" / "stats_archive"
        if archive.exists():
            for f in archive.glob("stats_*.json"):
                m = re.match(r'stats_(\d+)_', f.name)
                if m:
                    ids.add(int(m.group(1)))
        # 数据目录下的当日文件
        for f in (_p / "data").glob("group_*_*.json"):
            try:
                ids.add(int(f.stem.split("_")[1]))
            except Exception:
                pass
        # group_owners
        for k in self.group_owners:
            try:
                ids.add(int(k))
            except (ValueError, TypeError):
                pass
        return sorted(ids) if ids else []

    def get_group_owner(self, group_id: int) -> list[int]:
        """获取字频道的 OP 指派列表"""
        return self.group_owners.get(group_id, [])

    def get_display_name(self, user_id: int, group_id: int = 0) -> str:
        """获取用户显示名：分频道昵称 > 全局昵称 > user_id"""
        uid = str(user_id)
        # 优先查分频道昵称
        if group_id and group_id in self.group_settings:
            gs_nicks = self.group_settings[group_id].get("nicknames", {})
            if uid in gs_nicks:
                return gs_nicks[uid]
        # fallback 全局昵称
        return self.qq_name_map.get(uid, uid)


# ── 单例实例 ────────────────────────────────────────────────
_instance: Optional[BotConfig] = None


def _load_env_config() -> dict[str, dict]:
    """从 .env 加载 API 密钥和 URL"""
    env_path = _CONFIG_DIR / ".env"
    if not env_path.exists():
        return {}
    load_dotenv(env_path)
    
    providers: dict[str, dict] = {}
    for key, value in os.environ.items():
        if key.endswith("_URL"):
            provider = key[:-4].lower()
            if provider not in providers:
                providers[provider] = {}
            providers[provider]["url"] = value
        elif key.endswith("_KEY"):
            provider = key[:-4].lower()
            if provider not in providers:
                providers[provider] = {}
            providers[provider]["key"] = value
    return providers


def _make_model(bot_model_cfg: dict, env_providers: dict[str, dict]) -> ModelConfig:
    """从 bot_config.toml 中的模型段 + env 配置构建 ModelConfig"""
    mc = ModelConfig()
    mc.name = bot_model_cfg.get("name", "")
    mc.provider = bot_model_cfg.get("provider", "")
    mc.maxtoken = bot_model_cfg.get("maxtoken", 300)
    mc.switch = bot_model_cfg.get("开关", False)
    
    env_cfg = env_providers.get(mc.provider.lower(), {})
    mc.url = env_cfg.get("url", "")
    mc.key = env_cfg.get("key", "")
    
    return mc


def load_bot_config() -> BotConfig:
    """
    加载所有配置文件并构建 BotConfig 实例。
    这是唯一需要调用的加载函数。
    """
    global _instance
    
    cfg_path = _CONFIG_DIR / "bot_config.toml"
    adapter_path = _CONFIG_DIR / "adapter_config.toml"
    roles_path = _CONFIG_DIR / "roles.toml"
    
    # ── bot_config.toml ──
    if not cfg_path.exists():
        raise FileNotFoundError(f"主配置文件不存在: {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as f:
        bot_toml = toml.load(f)
    
    bot_section = bot_toml.get("bot", {})
    judge_section = bot_toml.get("judge", {})
    personality = bot_toml.get("personality", {})
    models_section = bot_toml.get("model", {})
    kook_section = bot_toml.get("kook", {})

    # ── adapter_config.toml（仅用于频道白名单等，不再含 NapCat）──
    adapter_toml = {}
    if adapter_path.exists():
        with open(adapter_path, "r", encoding="utf-8") as f:
            adapter_toml = toml.load(f)
    
    # ── roles.toml ──
    admin_qq = 0
    admin_id_str = ""
    friend_qqs: list[int] = []
    qq_name_map: dict[str, str] = {}
    op_qqs: list[int] = []          # OP（次级管理员），roles.toml 缺失时默认空
    group_owners: dict[int, list[int]] = {}  # 频道 OP 指派，roles.toml 缺失时默认空
    if roles_path.exists():
        with open(roles_path, "r", encoding="utf-8") as f:
            roles_toml = toml.load(f)
        # admin_qq 兼容 int 和 str（KOOK user_id 是字符串）
        _admin_raw = roles_toml.get("admin_qq", 0)
        admin_id_str = str(_admin_raw)
        try:
            admin_qq = int(_admin_raw)
        except (ValueError, TypeError):
            admin_qq = hash(str(_admin_raw))
        friend_qqs_raw = roles_toml.get("friend_qqs", [])
        friend_qqs = []
        for q in friend_qqs_raw:
            try:
                friend_qqs.append(int(q))
            except (ValueError, TypeError):
                friend_qqs.append(hash(str(q)))
        qq_name_map = {str(k): v for k, v in roles_toml.get("qq_name_map", {}).items()}
        # ★ OP 次级管理员
        op_qqs_raw = roles_toml.get("op_qqs", [])
        op_qqs = []
        for q in op_qqs_raw:
            try:
                op_qqs.append(int(q))
            except (ValueError, TypeError):
                op_qqs.append(hash(str(q)))
        # ★ 频道 OP 指派: {channel_id: [op_user_id, ...]}
        group_owners_raw = roles_toml.get("group_owners", {})
        group_owners = {}
        for k, v in group_owners_raw.items():
            try:
                gid = int(k)
            except (ValueError, TypeError):
                gid = hash(str(k))
            if isinstance(v, list):
                group_owners[gid] = []
                for q in v:
                    try:
                        group_owners[gid].append(int(q))
                    except (ValueError, TypeError):
                        group_owners[gid].append(hash(str(q)))
            else:
                try:
                    group_owners[gid] = [int(v)]
                except (ValueError, TypeError):
                    group_owners[gid] = [hash(str(v))]

    # ── .env ──
    env_providers = _load_env_config()

    # ── KOOK token（优先 bot_config.toml [kook] 段，其次 .env 的 KOOK_KEY）──
    kook_token = kook_section.get("token", "") or env_providers.get("kook", {}).get("key", "")
    if not kook_token:
        raise ValueError("KOOK token 未配置：请在 bot_config.toml 的 [kook] 段或 .env 的 KOOK_KEY 中设置")

    # ── 构建 BotConfig ──
    # ── 私聊白名单 ──
    private_whitelist_raw = adapter_toml.get("chat", {}).get("private_whitelist", [])
    private_whitelist = []
    for q in private_whitelist_raw:
        try:
            private_whitelist.append(int(q))
        except (ValueError, TypeError):
            private_whitelist.append(hash(str(q)))

    # ── 字频道自定义设置 ──
    group_settings_raw = adapter_toml.get("group_settings", {})
    group_settings: dict[int, dict] = {}
    for k, v in group_settings_raw.items():
        try:
            gid = int(k)
        except (ValueError, TypeError):
            gid = hash(str(k))
        group_settings[gid] = {
            "reply_threshold": v.get("reply_threshold", None),
            "at_only": v.get("at_only", False),
            "welcome_msg": v.get("welcome_msg", "").strip() if isinstance(v.get("welcome_msg"), str) else "",
            "cmd_whitelist": v.get("cmd_whitelist", None),
            # ★ 分群昵称：{"3483585417": "trusler", ...}
            "nicknames": {str(qq): name for qq, name in v.get("nicknames", {}).items()},
        }

    # ── 字频道白名单（KOOK channel_id，可 int 或 str）──
    group_list_raw = adapter_toml.get("chat", {}).get("group_list", [])
    group_list = []
    for g in group_list_raw:
        try:
            group_list.append(int(g))
        except (ValueError, TypeError):
            group_list.append(hash(str(g)))

    # ── bot_id_str（KOOK bot 自身的 user_id，启动后由 bot.client.fetch_me() 填充）──
    bot_id_str_raw = bot_section.get("bot的id", "") or bot_section.get("bot的qq号", "")
    bot_id_str = str(bot_id_str_raw) if bot_id_str_raw else ""
    try:
        bot_qq_val = int(bot_id_str_raw) if bot_id_str_raw else 0
    except (ValueError, TypeError):
        bot_qq_val = hash(str(bot_id_str_raw)) if bot_id_str_raw else 0

    instance = BotConfig(
        kook_token=kook_token,
        host=bot_section.get("内部服务地址", "0.0.0.0"),
        port=int(bot_section.get("内部服务端口", 8099)),
        bot_name=bot_section.get("bot的名字", "KookBot"),
        bot_qq=bot_qq_val,
        bot_id_str=bot_id_str,
        reply_interest=bot_section.get("回复兴趣", 10),
        context_length=bot_section.get("消息记录长度", 20),
        enable_private=bot_section.get("enable_private_chat", False),
        debug_mode=bool(bot_section.get("调试模式", False)),
        admin_qq=admin_qq,
        admin_id_str=admin_id_str,
        friend_qqs=friend_qqs,
        qq_name_map=qq_name_map,
        op_qqs=op_qqs,
        group_owners=group_owners,
        group_list=group_list,
        private_whitelist=private_whitelist,
        group_settings=group_settings,
        replace_words=bot_section.get("替换词", []),
        be_replaced_words=bot_section.get("被替换词", []),
        search_trigger_words=judge_section.get("search_trigger_words", []),
        realtime_words=judge_section.get("realtime_words", []),
        spam_threshold=bot_section.get("spam_threshold", 8),
        mute_duration=bot_section.get("mute_duration", 1800),
        personality_core=personality.get("personality_core", ""),
        personality_side=personality.get("personality_side", ""),
        identity=personality.get("identity", ""),
        # ── 私聊专属人格基底 ──
        private_persona_version=int(bot_toml.get("private_persona", {}).get("version", 0)),
        private_persona_core=bot_toml.get("private_persona", {}).get("core", ""),
        private_persona_side=bot_toml.get("private_persona", {}).get("side", ""),
        private_identity=bot_toml.get("private_persona", {}).get("identity", ""),
    )
    
    # ── 模型配置 ──
    instance.reply_model = _make_model(models_section.get("replyer_1", {}), env_providers)
    instance.judge_model = _make_model(models_section.get("utils_small", {}), env_providers)
    instance.cheap_model = _make_model(models_section.get("judge_cheap", {}), env_providers)
    instance.image_model = _make_model(models_section.get("picture", {}), env_providers)
    
    # 组装提示词
    instance.build_system_prompt()
    
    _instance = instance
    return instance


def get_config() -> BotConfig:
    """获取当前 BotConfig 实例。未加载时自动加载。"""
    global _instance
    if _instance is None:
        return load_bot_config()
    return _instance


def reload_config() -> BotConfig:
    """重新加载所有配置文件，返回新实例。保留 fetch_me() 注入的 bot_id。"""
    from core.logger import info, set_debug_mode
    old_cfg = _instance
    new_cfg = load_bot_config()
    # KOOK bot 的 user_id 以启动时 fetch_me() 注入值为准（bot_id_str 非空即注入过），
    # 配置文件里的 bot的qq号可能是历史残留/错误值，重载时必须保留注入值，
    # 否则 @机器人 检测会失配（bot_qq 被覆盖成配置里的错误值）。
    if old_cfg and old_cfg.bot_id_str:
        new_cfg.bot_qq = old_cfg.bot_qq
        new_cfg.bot_id_str = old_cfg.bot_id_str
        info("保留 fetch_me 注入的 bot_id: %s", old_cfg.bot_id_str)
    set_debug_mode(new_cfg.debug_mode)
    info("配置已重新加载 | bot=%s | host=%s:%d | debug=%s",
         new_cfg.bot_name, new_cfg.host, new_cfg.port, new_cfg.debug_mode)
    return new_cfg


def full_reload() -> BotConfig:
    """全量热重载（极速，不重启进程）：bot_config + lang.toml + judge 关键词 + 技能缓存。

    `.reload` 命令与 SIGUSR1（bot.handle_reload）共用的统一入口，
    一次把所有可热载内容刷新到位。返回新的 BotConfig 实例。
    """
    import time
    from core.logger import info
    t0 = time.time()
    cfg = reload_config()
    try:
        from modules.judge import reload_keywords
        reload_keywords()
    except Exception as e:
        info("关键词重载失败: %s", e)
    try:
        from utils.format_lang import load_lang
        load_lang()
    except Exception as e:
        info("语言文件重载失败: %s", e)
    try:
        from services.llm import reload_skill_cache, _load_skill_sections
        reload_skill_cache()
        _load_skill_sections()
    except Exception as e:
        info("技能缓存重载失败: %s", e)
    info("全量热重载完成（config+lang+keywords+skills），耗时 %.0fms",
         (time.time() - t0) * 1000)
    return cfg


def load_roles_config() -> dict:
    """加载 roles.toml（供指令系统使用）"""
    roles_path = _CONFIG_DIR / "roles.toml"
    if not roles_path.exists():
        return {}
    with open(roles_path, "r", encoding="utf-8") as f:
        return toml.load(f)


def save_roles_config(config_dict: dict):
    """保存 roles.toml（供指令系统使用）"""
    roles_path = _CONFIG_DIR / "roles.toml"
    content = toml.dumps(config_dict)
    with open(roles_path, "w", encoding="utf-8") as f:
        f.write(content)


def set_debug_mode(enabled: bool):
    """动态切换调试模式（外部调用，转发给 logger）"""
    from core.logger import set_debug_mode as _set_debug_mode
    _set_debug_mode(enabled)

