# 幻梦 Bot 模板重构变更日志

## v2.0 — Crystal Aurora v2 视觉重构
**日期**: 2026-08-02
**范围**: 全部 7 个 HTML 模板

### 背景
用户反馈 v1 设计"太普通"，要求：
1. 背景渐变更强、有水晶效果
2. Markdown 渲染效果不如之前

### 设计系统升级

#### 背景
- 径向渐变 alpha 0.20 → 0.42（强度翻倍）
- 2 层径向 → 4 层径向（粉/紫/蓝/青四色极光）
- 新增 `conic-gradient` 极光漩涡（水晶折射感）
- 新增噪点纹理叠加（`::before` 多层 `radial-gradient`，3/5/7px 间距）

#### 卡片
- `backdrop-filter`: blur(16px) → blur(24px) saturate(1.4)
- 透明度: 0.65 → 0.55（更通透）
- 边框: 纯色 border → mask 渐变边框（粉→紫→蓝→青）
- 新增顶部高光条（`::after` 白色渐变模拟玻璃反光）
- 阴影: 单层 → 多层（外阴影 + 粉色光晕 + 紫色光晕 + 内高光 + 内底光）

#### 标题
- 三色渐变: #f472b6 → #ec4899 → #8b5cf6
- 新增 `drop-shadow` 发光效果
- h1: 底部下划线改为渐变 + 发光
- h2: 左边框改为渐变 + 发光 + 渐变背景
- h3: 改为 mono 字体大写 + 发光小条

#### 代码块（重大升级）
**v1**: 普通边框 + 左侧色条
**v2**: Mac 风格窗口
- 红黄绿三圆点（`::after` + box-shadow 实现）
- 语言标签（通过 `data-lang` 属性显示，右上角）
- 渐变标题栏
- 内部代码: One Dark 风格完整 token 配色

#### 列表
- 标记: 三角符号 → 发光圆点（box-shadow glow）
- 子项: 紫色小圆点

#### 表格
- 表头: 半透明粉 → 渐变背景 + 发光下边框
- mono 字体大写
- 偶数行斑马纹

#### 引用块
- 蓝色实线边框 → 渐变 `border-image`
- 新增引号装饰（`::before` + serif 字体）

#### Footer
- 品牌文字: 纯色低透明 → 渐变文字 clip
- 分隔线: 渐变发光

### 文件清单
| 文件 | 状态 | 宽度 |
|------|------|------|
| changelog_card.html | 重构 | 760px |
| md_card.html | 重构 | 580px |
| daily_report.html | 重构 | 720px |
| weather_card.html | 重构 | 560px |
| box_card.html | 重构 | 560px |
| leaderboard_card.html | 重构 | 440px |
| wzq_board.html | 重构 | 680px |

### 验证
- 全部模板通过 Playwright 截图测试
- Markdown 管道 (`markdown_to_enhanced_html`) 正常工作
- 代码块 `data-lang` 属性正确显示语言标签
- 截图: `G:\py\qqbot\data\img_temp\test_*.png`

### 部署
```bash
scp -P 20015 -i ~/.ssh/id_ed25519 -r G:\py\qqbot\data\templates\ root@01240820.xyz:/root/bot/data/templates/
```

---

## v1.0 — Terminal Lite（已废弃）
**日期**: 2026-08-02（早些时候）
- 暗色背景 #0e0f13 + 单色 #ec4899 强调
- 7 个模板初版
- 用户反馈"太普通"，触发 v2 重构