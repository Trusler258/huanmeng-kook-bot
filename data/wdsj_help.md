# 洛花星雨 Nexus 战绩查询

## 战绩查询

`.wdsj <模式> <玩家> [img]`

| 模式 | 简写 | 完整ID |
|------|------|--------|
| 起床战争 | `bw` | bedwars-stats |
| 击退战场 | `kbw` | knockbackwars-stats |
| 空岛战争 | `sw` | skywars-stats |
| 职业战争 | `kp` | kitpvp-stats |
| 天坑乱斗 | `pit` | thepit-stats |
| 色盲战争 | `cw` | colorwars-stats |
| 极限生存 | `uhc` | uhc-stats |
| 神秘谋杀 | `mm` | murdermystery-stats |
| 你画我猜 | `dg` | drawguess-stats |
| 建筑战争 | `bb` | buildbattle-stats |
| 星跃水立方 | `wc` | watercube-stats |
| 躲猫猫 | `has` | hideandseek-stats |
| 竞技场 | `are` | arena-stats |

---

## 排行榜

`.wdsj lb <游戏> <指标> [周期] [img]`

### 起床战争 `bw`

| 简写 | 指标 | API ID |
|------|------|--------|
| `bw win` | 胜利 | bedwars-wins |
| `bw kill` | 击杀 | 起床战争-击杀 |
| `bw beds` | 摧床 | bedwars-beds |
| `bw fk` | 最终击杀 | 起床战争-最终击杀 |
| `bw 1k` | 首杀 | 起床战争-首杀 |
| `bw void` | 自走虚空 | 起床战争-自走虚空 |
| `bw egg` | 鸡蛋击杀 | 起床战争-鸡蛋击杀 |
| `bw fb` | 火球击杀 | 起床战争-火球击杀 |

### 击退战场 `kbw`

| 简写 | 指标 | API ID |
|------|------|--------|
| `kbw kill` | 击杀 | knockbackwars-kills |
| `kbw dead` | 死亡 | 击退战场-死亡 |
| `kbw tnt` | TNT击杀 | 击退战场-TNT击杀 |
| `kbw arrow` | 弓箭击杀 | 击退战场-弓箭击杀 |
| `kbw rod` | 鱼竿击杀 | 击退战场-鱼竿击杀 |
| `kbw jp` | 跳板击杀 | 击退战场-跳板击杀 |

### 空岛战争 `sw`

| 简写 | 指标 | API ID |
|------|------|--------|
| `sw kill` | 击杀 | skywars-kills |
| `sw win` | 胜利 | 空岛战争-胜利 |
| `sw dead` | 死亡 | 空岛战争-死亡 |
| `sw 1k` | 首杀 | 空岛战争-首杀 |

### 其他榜单

| 简写 | 指标 | API ID |
|------|------|--------|
| `pt` | 在线时长 | playtime-minutes |
| `cp` | 情侣亲密值 | 情侣-亲密值 |
| `kp kill` | 职业击杀 | 职业战争-击杀 |
| `kp xp` | 职业经验 | 职业战争-经验 |
| `title` | 全服称号 | 全服-称号数量 |
| `guild` | 公会贡献 | 公会-总贡献 |
| `dg win` | 画猜获胜 | 你画我猜-获胜 |
| `cw win` | 色盲获胜 | 色盲战争-获胜 |
| `has win` | 躲猫猫获胜 | 躲猫猫-获胜 |

---

## 周期

| 完整写法 | 简写 | 说明 |
|----------|------|------|
| `alltime` | `all` | 总榜 |
| `monthly` | `month` | 月榜 |
| `weekly` | `week` | 周榜 |
| `daily` | `day` | 日榜 |

---

## 示例

```text
.wdsj bw ymb099 img               起床战争战绩卡片
.wdsj lb bw kill month img        床战击杀月榜
.wdsj lb pt week                  在线时长周榜
.wdsj lb beds all img             摧床总榜
.wdsj lb kbw tnt day              击退TNT日榜
.wdsj boards                      排行榜简写速查
```
