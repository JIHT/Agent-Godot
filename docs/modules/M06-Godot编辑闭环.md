# M06 Godot 编辑闭环（领域工具 · Diff · 检查点 · headless 校验）

| 项 | 值 |
|---|---|
| 生产进度 | Sprint 4 · 里程碑 **MI-1「Godot 闭环 MVP」——产品第一次真正可用** |
| 代码落点 | `backend/agent_godot/mcp/servers/godot/`（server/project/scenes/scripts/runner）+ `tools/godot/` |
| 前置模块 | M04（工具协议）· M05（MCP 服务器写法是客户端的镜像）· M03 乐观锁 |
| 手写比例 | 100% 手写（Godot 无现成 Agent 生态，本模块是项目最大原创性资产） |
| 教程映射 | 📝笔记「DSH 领域工具设计」· Godot 官方 CLI 文档 · unified diff 规范 |

---

## 0. 本模块在项目中的位置

前五个模块搭好了通用骨架，本模块注入**领域灵魂**：把"改 Godot 游戏项目"拆解成模型可安全调用的工具集，并构成完整闭环：

```text
读项目（索引） → 改（脚本/场景，带 Diff+检查点） → 验（headless 编译/测试/运行） → 报（结果回填） → 兜底（回滚）
```

**交付后状态（MI-1 验收）**：`godot-agent craft "给玩家场景加一个会追人的敌人"` ——模型查看场景树 → 写 enemy.gd → 修改 player.tscn → headless 校验通过 → 展示 Diff；说"撤销"即回滚到检查点。

---

## 1. 知识点详解

### 1.1 领域工具设计方法论（从能力面到工具集）

**① 原理**

通用 Agent 工具（读写文件）也能改 Godot 项目，为什么还要领域工具？三个理由：**模型认知减负**（`read_scene` 返回结构化场景树比裸读 .tscn 文本省 80% token 且不解析错）、**安全收口**（场景修改走结构化编辑而非文本拼接，语法错误在工具层拦截）、**验收内建**（改完自动触发导入校验）。

设计流程四步（可迁移到任何领域）：

```text
1. 枚举领域操作面：Godot 开发 = 项目管理/场景/脚本/资源/调试运行 五域
2. 按用户任务聚类：用户说"加敌人"实际需要 list→read_scene→write_script→edit_scene→validate
3. 定工具粒度：太细（读某属性）模型走断头路；太粗（make_game）不可控。
   准则：一个工具 = 开发者手册里一个动词小节
4. 定返回协议：每个工具的 Observation 必须包含"下一步线索"（错误时给修复提示）
```

**② 演进**：通用文件工具（Cursor 起步形态）→ 领域 LSP/树协议（理解 AST）→ 领域 MCP（Godot 编辑器插件方向：直接在编辑器内操作，社区 gdMCP 尝试）→ 本项目双轨：**结构化文本工具（.tscn/.gd 文件级）+ headless 校验**，不依赖编辑器插件，CI 环境也能跑。

**③ 最小案例**：本项目最终工具清单（25 个，节选核心 12 个）

```text
项目管理: godot_open_project(路径) · godot_project_overview(资源统计/配置)
场景域:   godot_list_scenes · godot_read_scene(结构化场景树)
         godot_edit_scene(添加节点/改属性/连信号, 结构化patch) · godot_create_scene
脚本域:   godot_read_script(带符号大纲) · godot_write_script(乐观锁)
         godot_list_symbols(全项目符号表: 类/函数/信号)
校验域:   godot_check(语法) · godot_run_tests(gut) · godot_run_scene(headless 运行N秒截图)
```

**④ 易错点**
- 工具返回"下一步线索"不是过度设计：`godot_read_scene` 末尾附 `可编辑节点: Player, Camera2D`，模型下一步选择准确率显著提升
- 场景工具与脚本工具要能互引（节点 `script: ExtResource("...")` 的资源 ID）——工具文档里写清 ID 语义
- 别把 headless 运行做成同步阻塞大杀器：分级（check 秒级 / tests 分钟级 / run+截图 需确认）

### 1.2 .tscn / .gd 文件格式解析

**① 原理**

`.tscn` 是 Godot 场景的文本序列化格式：

```ini
[gd_scene load_steps=4 format=3 uid="uid://abc123"]

[ext_resource type="Script" path="res://enemy.gd" id="1_abcde"]

[node name="Player" type="CharacterBody2D"]
position = Vector2(120, 64)

[node name="Sprite" parent="." instance=ExtResource("2_xy")]

[connection signal="body_entered" from="Hitbox" to="." method="_on_hit"]
```

三个语义块：`ext_resource/sub_resource`（资源声明，ID 引用）、`node`（树，`parent="."` 相对路径挂树）、`connection`（信号连线）。**结构化编辑 = 解析这三块 → 内存树 → 修改 → 序列化回写**，而不是正则替换文本——正则在"属性值里恰好出现 node 名"时必炸。

`.gd`（GDScript）用 `tokenize + ast` 做轻量解析：拿到缩进块树即可支撑"符号大纲/函数级定位"，不必完整语义分析（那是 Godot 编辑器的活）。

**② 演进**：二进制 .scn（Godot 3 前，不可读）→ 文本 .tscn（3.x 起，git 友好）→ UID 机制（4.2 起资源带 uid，重命名不断引用）。这个演进正是"可读性换来了工具化空间"的例证——AI Agent 能改 Godot 项目的前提就是文本格式。

**③ 最小案例**：场景树解析器骨架（完整签名见第 2 节）

```python
def parse_tscn(text: str) -> SceneFile:
    sf = SceneFile()
    for section in re.split(r"\n\[(?=\w)", text):        # 按 [gd_scene]/[node]/[connection] 分节
        header, _, body = section.partition("]")
        kind, *attrs = header.strip("[] ").split(" ", 1)
        meta = dict(re.findall(r'(\w+)="([^"]*)"', attrs[0] if attrs else ""))
        if kind == "node":
            node = SceneNode(name=meta["name"], type=meta.get("type"),
                             parent=meta.get("parent", "."),
                             props=parse_props(body))      # Vector2(120, 64) 等字面量
            sf.nodes.append(node)
        elif kind == "connection":
            sf.connections.append(parse_conn(meta))
    return sf

def scene_tree(sf: SceneFile) -> dict:
    """nodes(带 parent 相对路径) → 嵌套树。路径规则：
       parent="." 是根的直接子节点；parent="Hitbox" 是名为 Hitbox 节点的子节点"""
```

**④ 易错点**
- `parent` 是**兄弟相对路径**不是绝对路径：`parent="Body/Sprite"` 表示挂在根下 Body 的 Sprite 里
- `instance=ExtResource()` 的实例节点不能再加 `type`（类型来自被实例化场景）
- 序列化回写要保持 Godot 的属性顺序习惯（name/type 在 header，其余按字母序），否则 Diff 噪音巨大
- format=3 是 Godot 4 格式，解析器要拒绝 format=2（Godot 3 项目）并明确报错

### 1.3 Diff 引擎与确认应用流

**① 原理**

统一 diff（unified format）：`@@ -l,s +l,s @@` 块头标注旧/新文件的起始行与跨度，`-/+/空格` 三前缀行。本项目不手写 diff 生成（用标准库 `difflib.unified_diff`——算法 LCS 属于了解即可），**手写的是"结构化 Diff 对象"**：

```python
@dataclass
class FileDiff:
    path: str
    hunks: list[Hunk]           # 原始块
    checkpoint_ref: str         # 改前快照（M04 已埋）
```

价值：hunks 可以**逐块审批**——用户批准 3 块中的 2 块，应用器只回放被批准的块（用 `apply_patch` 思想按行号偏移重算）。全量 vs 逐块是产品体验分水岭（Cursor 的 review 模式即此）。

**② 演进**：整文件覆盖（无审查）→ 全量 Diff 确认（patch 文档化）→ 逐 hunk 审查（现代标配）→ 语义 Diff（按 AST 节点展示，本项目 .gd 用函数级归组算轻量语义 Diff）。

**③ 最小案例**：hunk 应用器（按批准偏移重算行号）

```python
def apply_hunks(original: list[str], hunks: list[Hunk], approved: set[int]) -> list[str]:
    out, offset = [], 0
    for idx, h in enumerate(hunks):
        if idx not in approved:
            offset += len(h.old) - len(h.new)        # ★ 跳过的块也要累计行号漂移
            continue
        out.extend(original[h.old_start - 1 + offset - 1 : h.old_start - 1 + offset - 1 + len(h.old) - 1][:0] or original[h.old_start-2+offset : h.old_start-2+offset+len(h.old)])
        out.extend(h.new)
        offset += len(h.new) - len(h.old)
    return out
```

（上面刻意保留了一行"思考现场"——你手敲时应重写干净：核心是 `offset` 累计已应用块造成的行号漂移，跳过的块同样改变后续对齐。测试用例必须覆盖"跳过中间块"的场景。）

**④ 易错点**
- 行号是 1-based，hunk 头 `@@ -5,3` 表示从第 5 行起 3 行——数组索引换算 -2 的 off-by-one 是这类代码的坟场
- 空文件/文件末尾无换行符 `\ No newline at end of file` 要特殊处理
- 部分应用后必须**重新走 headless 校验**——用户批准的组合可能前所未有

### 1.4 检查点快照与回滚

**① 原理**

检查点 = 某时刻项目文件状态的**可恢复记录**。设计三要素：

```text
粒度：文件级（每次写前快照该文件）聚合为"任务级检查点"（一次 craft 任务的全部写入）
存储：.agent_godot/checkpoints/{task_id}/{seq}__{path_hash}/{filename}（项目内隐藏目录）
      + manifest.json 记录 (seq, path, hash, mtime, reason)
回滚：逆序回放——把 manifest 里每条记录的快照拷回原位；多文件改动按写入逆序恢复
```

逆序是关键：同一文件可能被多次修改（快照 1、2、3），逆序回放最终停在最初始状态；正序回放会停在中间态。

**② 演进**：无（改错重来）→ 复制备份目录（无序、占空间）→ 带清单的文件级检查点（本项目）→ git 分支/stash（语义重、与用户自己的 git 历史打架：Agent 的操作混进用户 commit 污染历史）。选文件级快照而非 git：**不打扰用户版本管理**，Agent 的失误由 Agent 自己的机制兜底。

**③ 最小案例**

```python
class CheckpointStore:
    def snapshot(self, path: Path, task_id: str) -> str:
        seq = self._next_seq(task_id)
        snap_dir = self.root / task_id / f"{seq:03d}_{sha16(str(path))}"
        if path.exists():
            shutil.copy2(path, snap_dir / path.name)   # copy2 保留 mtime——回滚后乐观锁还能对上
        self._manifest(task_id).append(
            {"seq": seq, "path": str(path.relative_to(self.project_root)),
             "existed": path.exists(), "hash": sha16(path.read_text()) if path.exists() else None})
        return f"{task_id}:{seq}"

    def rollback(self, task_id: str) -> list[str]:
        restored = []
        for rec in reversed(self._manifest(task_id)):       # ★ 逆序
            dst = self.project_root / rec["path"]
            src = self.root / task_id / f"{rec['seq']:03d}_{sha16(str(dst))}"
            if rec["existed"]:
                shutil.copy2(src / dst.name, dst)
            else:
                dst.unlink(missing_ok=True)                 # 当初不存在的文件，回滚=删除
            restored.append(rec["path"])
        return restored
```

**④ 易错点**
- 快照"创建前不存在"的文件也要记录（`existed: false`），否则回滚会留下幽灵文件
- `copy2` 保 mtime：回滚后文件的 hash 版本号与 Agent 记忆一致，避免乐观锁误报
- manifest 写入要原子（临时文件+rename），进程中途被杀不能留下半个清单

### 1.5 headless 校验闭环

**① 原理**

Godot 4 的命令行模式是校验的基石：

```bash
godot --headless --path <project> --import            # 重新导入资源（新资源必须先 import）
godot --headless --path <project> --check-only --script res://enemy.gd   # 语法检查（单文件）
godot --headless --path <project> -s res://tests/run.gd  # 跑测试脚本（接 gut 框架）
godot --headless --path <project> res://player.tscn --quit-after 120     # 运行场景120帧
```

校验分级（对应工具的三档超时）：

| 级别 | 命令 | 耗时 | 拦截 |
|---|---|---|---|
| L1 语法 | `--check-only` | 秒级 | GDScript 解析错 |
| L2 导入 | `--import` | 秒~分钟 | 资源引用断链/场景格式错 |
| L3 测试 | `-s tests/` | 分钟 | 逻辑回归 |
| L4 运行 | 场景跑 N 帧 + stdout 断言 | 分钟+ | 运行时崩溃（空引用/死循环） |

**Agent 的"自我修复"闭环**：L1 挂 → 错误行号回填 Observation → 模型改 → 再 L1；L2 挂 → 缺资源提示 → 模型找/建资源。这是 Reflection 思想（M13）在领域层的第一次落地。

**② 演进**：人工点编辑器报错（无闭环）→ CI 里跑 headless（人类节奏）→ **Agent 循环内嵌校验**（秒级反馈闭环——把 CI 的反馈周期从分钟压进 Agent 的一轮循环）。Godot 官方 headless 文档 + CI 模板是现成参考。

**③ 最小案例**：错误解析器（把 Godot 输出翻译成模型友好的 Observation）

```python
GD_ERROR = re.compile(
    r"^(?P<file>res://\S+?):(?P<line>\d+)(?:-(?P<end>\d+))?\s*[-·]?\s*(?P<msg>.+)$",
    re.MULTILINE)

async def godot_check(project: Path, target: str | None = None) -> ToolResponse:
    args = ["--headless", "--path", str(project), "--check-only"]
    if target: args += ["--script", target]
    proc = await asyncio.create_subprocess_exec(
        GODOT_BIN, *args, stdout=PIPE, stderr=STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    errors = [m.groupdict() for m in GD_ERROR.finditer(out.decode(errors="replace"))]
    if not errors:
        return ToolResponse(ok=True, summary="语法检查通过")
    hints = "\n".join(f"- {e['file']}:{e['line']} {e['msg']}" for e in errors[:8])
    return ToolResponse(ok=False, error=ToolError(
        kind=ErrorKind.VALIDATION, tool="godot_check", message=hints,
        hint="按行号逐条修复；常见：缩进错误/未定义变量/信号签名不匹配"))
```

**④ 易错点**
- 新建资源（贴图/场景）后**必须先 `--import`**，否则后续 check 找不到资源报假错
- Windows 下 Godot 首次 headless 启动可能弹安全对话框——CI 机器要预热过一次
- 运行场景类校验（L4）必须配 `--quit-after` 帧数上限 + 进程 kill 兜底，否则游戏主循环永远不退出
- stdout 编码随系统 locale 叼变（GBK 陷阱）：统一 `errors="replace"` + 关键字用英文匹配

---

## 2. 接口设计（完整签名）

```python
# mcp/servers/godot/scenes.py
@dataclass
class SceneNode:
    name: str; type: str | None; parent: str
    instance_of: str | None            # ExtResource id
    props: dict[str, str]              # 原样字符串值
    script: str | None

class SceneFile:
    nodes: list[SceneNode]; connections: list[dict]; resources: dict[str, dict]
    def tree(self) -> dict: ...                       # 嵌套树视图
    def find(self, path: str) -> SceneNode: ...
    def add_node(self, parent: str, node: SceneNode) -> None: ...
    def set_prop(self, path: str, key: str, value: str) -> None: ...
    def connect_signal(self, signal_: str, from_: str, to: str, method: str) -> None: ...
    def serialize(self) -> str: ...

def parse_tscn(text: str) -> SceneFile: ...

# tools/godot/scene_tools.py（注册为 FC 工具，MCP 服务器复用同一实现）
class GodotReadSceneTool(BaseTool):
    """返回场景结构树（节点/类型/脚本/信号）。"""
class GodotEditSceneTool(BaseTool):
    """结构化场景编辑。ops: add_node/set_prop/connect_signal/remove_node"""

# tools/godot/headless.py
class GodotRunner:
    def __init__(self, godot_bin: str, project_root: Path): ...
    async def check(self, script: str | None = None) -> CheckResult: ...
    async def import_assets(self) -> CheckResult: ...
    async def run_tests(self, timeout: int = 120) -> CheckResult: ...
    async def run_scene(self, scene: str, frames: int = 180) -> RunResult: ...

# 检查点（M04 file_lock.py 已给 snapshot 接口，此处聚合）
class TaskCheckpoints:
    def open_task(self) -> str: ...                   # task_id
    def snapshot(self, path: Path) -> None: ...
    def rollback(self, task_id: str | None = None) -> list[str]: ...
    def list(self) -> list[CheckpointInfo]: ...
```

## 3. 关键难点参考片段：场景树构造

nodes 列表 → 嵌套树的挂接算法（parent 相对路径语义是本模块最烧脑的 30 行）：

```python
def tree(self) -> dict:
    index: dict[str, SceneNode] = {"": SceneNode(name="(root)", type="", parent="")}
    for n in self.nodes:                       # 第一遍：登记（根的子节点 parent="."）
        normalized = "." if n.parent == "." else n.parent
        index[self._abs_path(n)] = n
    def _abs_path(n: SceneNode) -> str:
        if n.parent in (".", ""):
            return n.name
        return f"{n.parent}/{n.name}"          # parent 本身已是绝对路径（不含根）
    root = {"name": "(root)", "children": []}
    for n in self.nodes:
        parent_path = "" if n.parent == "." else n.parent
        # ... 按 _abs_path 挂接；缺失父节点(实例场景内部)标记为 opaque 节点
    return root
```

为什么难：Godot 的 parent 约定是"相对于根、不含根名"，且**实例化场景的内部子树不展开**（.tscn 里看不到）。测试要用三个真实场景覆盖：纯手搭树 / 实例化嵌套 / 信号连线复杂树。

## 4. 手敲指引

| 步骤 | 文件 | 做什么 | 验证 |
|---|---|---|---|
| 1 | lab/m06/sample 项目 | 手工建最小 Godot 项目（2 场景 3 脚本） | Godot 编辑器能开 |
| 2 | scenes.py | parse_tscn + tree | 三个样本场景树正确 |
| 3 | scenes.py | add_node/set_prop + serialize | 序列化结果 Godot 能加载 |
| 4 | headless.py | check/import/run_scene | 故意写错语法 → 错误行号正确 |
| 5 | tools/godot/ | 12 个 FC 工具注册 | M03 Loop 真机自选工具 |
| 6 | 检查点聚合 | TaskCheckpoints | 多文件任务回滚完整 |
| 7 | mcp/servers/godot/ | 同实现包成 MCP 服务器 | stdio 起服 → M05 客户端桥接可见 |
| 8 | Diff 应用器 | 逐 hunk 审批 | 跳块应用后 check 仍通过 |

## 5. 测试与验收

```python
def test_parse_instance_scene_opaque():
    sf = parse_tscn(SAMPLE_WITH_INSTANCE)
    tree = sf.tree()
    assert tree["children"][1].get("opaque") is True      # 实例内部不展开

async def test_rollback_restores_deleted_and_created():
    ck = store.open_task()
    (root/"new_file.gd").write_text("v1")                 # Agent 新建
    (root/"existing.gd").write_text("changed")            # Agent 修改
    store.snapshot(root/"new_file.gd"); store.snapshot(root/"existing.gd")
    # ... 写入后 rollback：new_file.gd 消失、existing.gd 恢复原文

async def test_check_feedback_drives_self_fix():
    # 集成测试：给模型一个含语法错误的脚本修改任务，
    # 断言事件流出现 check失败→修改→check通过 的完整回路
```

**验收 Demo（MI-1 里程碑）**：
`godot-agent craft "给 player.tscn 加一个 Area2D 陷阱区域，碰到就减血"` → 观察：read_scene → write_script(trap.gd) → edit_scene → godot_check 通过 → Diff 展示 → `godot-agent rewind 1` 全部恢复原状。

## 6. 踩坑记录（留白）

| 日期 | 坑 | 现象 | 根因 | 解法 | 关联知识点 |
|---|---|---|---|---|---|
|     |    |     |     |    |          |

## 7. 面试拷打

1. 有了通用文件工具为什么还要领域工具？三个理由展开；
2. .tscn 的 parent 字段语义是什么？实例化节点为什么不展开？
3. 为什么用结构化 patch 编辑场景而不是让模型直接改文本？
4. 检查点为什么不用 git stash？逆序回放防的是什么场景？
5. 回滚后为什么要保留 mtime？（乐观锁版本一致性）
6. headless 四级校验各拦截什么错误？为什么新资源必须先 --import？
7. L4 运行校验的两个防挂死手段？（--quit-after + kill 兜底）
8. "校验错误回填驱动模型自修复"是哪个范式的雏形？（Reflection，M13 展开）
9. Windows 下 headless 集成最可能遇到的三个环境坑？
10. 开放题：把这套"读-改-验-回滚"方法论迁移到 Unity/Unreal，哪些能复用，哪些是 Unity 特有难题？（.unity 是 YAML 但生态闭源、无官方 headless CLI）

## 8. 教程映射与延伸

- Godot 官方：命令行教程、.tscn 格式文档（tscn 无正式规范——读源码 `resource_format_text.cpp` 是终极资料）
- 必读：unified diff 格式规范（GNU diffutils 手册附录）
- 选读：gut 测试框架文档（L3 级校验载体）
