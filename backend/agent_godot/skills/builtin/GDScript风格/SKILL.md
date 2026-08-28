---
name: GDScript风格
triggers: [gdscript, 脚本风格, 代码风格, 命名规范, 重构脚本]
tools_needed: [godot_read_script, godot_write_script, godot_check]
version: 1
---
# GDScript 编写规范（本项目约定）

GDScript 的缩进是语法的一部分（Tab 强制），风格问题在这里不是审美问题——
**空格缩进直接 parse error**。写脚本前先读一遍本清单，写完必须跑 L1 校验。

## 缩进与排版
- 缩进一律用 **Tab**（一个 Tab = 一层），绝不用空格
- 行尾不留空格，连续空行最多一个，文件末尾恰好一个换行
- 一行只做一件事；`if` 单行写法（`if x: return`）仅用于提前返回的卫语句

## 命名
| 对象 | 风格 | 例 |
|---|---|---|
| 文件 | snake_case | `player_controller.gd` |
| 类 | PascalCase（`class_name`） | `class_name PlayerController` |
| 函数 / 变量 | snake_case | `func take_damage(amount: int)` |
| 常量 | UPPER_SNAKE | `const MAX_SPEED := 320.0` |
| 信号 | 过去式 snake_case | `signal health_changed(new_value: int)` |
| 私有成员 | 下划线前缀 | `var _cooldown := 0.0` |

## 类型标注
- 变量与函数参数尽量显式标注类型（`var hp: int = 10`）：补全更准，重构更安全
- 常量用 `:=`（类型推断），函数返回值显式写 `-> void` / `-> int`
- 节点引用用 `@onready var sprite := $Sprite2D as Sprite2D`（`as` 转换带类型检查）

## 生命周期顺序（按引擎调用序排列，别乱序）
```
_ready()      # 自身与子节点就绪，取引用、连信号
_process(delta)      # 每帧（帧率相关，慎用）
_physics_process(delta)   # 物理帧，移动与碰撞都放这里
_input(event) / _unhandled_input(event)
```

## 信号与解耦
- 子节点向上通信一律用信号（不要 `$"../Parent".method()` 反向拉引用）
- 连接用代码 + 类型安全签名：`health_changed.connect(_on_health_changed)`
- 跨场景通信优先用 Autoload 单例事件总线，而不是层层传引用

## 常见坑
- `_process` 里做重活 → 帧率崩，改定时器或挪到 `_physics_process`
- 在 `_ready` 里访问还没进树的节点 → 用 `await owner.ready` 或 `@onready`
- 字符串路径硬编码（`get_node("UI/HPBar")`）→ 结构一改全崩，用 `%UniqueName` 场景唯一名
- `delta` 未使用的帧逻辑 → 帧率不同行为不同（必须乘 delta）

## 写完必做
1. `godot_check`（L1 语法）零错误
2. 检查缩进是否全 Tab（格式化 hook 会自动兜一层，但不能依赖它）
3. 确认没有把 `print` 调试语句留在交付代码里
