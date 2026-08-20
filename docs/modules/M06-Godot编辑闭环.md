# M06 Godot 编辑闭环（领域工具 · Diff · 检查点 · headless 校验）

| 项 | 值 |
|---|---|
| 生产进度 | Sprint 4 · 里程碑 **MI-1「Godot 闭环 MVP」——产品第一次真正可用** |
| 代码落点 | `backend/agent_godot/tools/godot/`（3 文件）+ `mcp/servers/godot/`（4 文件），见 §0.5 |
| 前置模块 | M04（工具协议）· M05（MCP 服务器是客户端的镜像）· M03 乐观锁 |
| 手写比例 | 100% 手写（Godot 无现成 Agent 生态，本模块是项目最大原创性资产） |
| 教程映射 | 📝笔记「DSH 领域工具设计」· Godot 官方 CLI 文档 · unified diff 规范 |

---

## 0. 本模块在项目中的位置

**大白话**：前五个模块搭好了通用骨架（会说话、会用工具、会接外部服务），本模块注入**领域灵魂**——让 Agent 真正"懂 Godot"。通用文件工具当然也能改游戏项目，但就像**用菜刀做外科手术**：能切开但风险大。领域工具 = 专业的手术刀套装——`read_scene` 返回结构化场景树（比裸读 .tscn 文本省 80% token 且不解析错）、场景修改走结构化编辑（语法错误在工具层拦截）、改完自动触发校验（错误秒级回传自修复）。完整闭环一句话：

```text
读项目（索引） → 改（脚本/场景，带 Diff+检查点） → 验（headless 编译/测试/运行） → 报（结果回填） → 兜底（回滚）
```

**交付后状态（MI-1 验收）**：`godot-agent craft "给玩家场景加一个会追人的敌人"`——模型查看场景树 → 写 enemy.gd → 修改 player.tscn → headless 校验通过 → 展示 Diff；说"撤销"即回滚到检查点。

---

## 0.5 ★ 施工文件清单（开工前必看的一页表）

**本模块你一共要新建 8 个文件 + 1 个测试项目**：

| # | 新建文件（完整路径） | 职责一句话 | 关键类/函数 | 预估行数 | 手敲步骤(§4) | 依赖 |
|---|---|---|---|---|---|---|
| 1 | `lab/m06/sample/` | 手工建最小 Godot 测试项目（2 场景 3 脚本） | — | — | 步骤 1 | Godot 4.x |
| 2 | `tools/godot/__init__.py` | 空包 | — | 1 | 步骤 0 | — |
| 3 | `tools/godot/scenes.py` | .tscn 解析/编辑/序列化 | `SceneFile`、`SceneNode`、`parse_tscn` | 180 | 步骤 2-3 | M04 |
| 4 | `tools/godot/headless.py` | Godot 命令行执行器（四级校验） | `GodotRunner`、`GD_ERROR` 正则 | 100 | 步骤 4 | M04 sandbox |
| 5 | `tools/godot/scene_tools.py` | 场景域 FC 工具（read/edit/create） | `GodotReadSceneTool` 等 6 个 | 120 | 步骤 5 | scenes+headless |
| 6 | `tools/godot/script_tools.py` | 脚本域 FC 工具（read/write/symbols） | 3 个工具类 | 80 | 步骤 5 | M04 file_lock |
| 7 | `tools/godot/checkpoints.py` | 任务级检查点聚合（M04 snapshot 之上） | `TaskCheckpoints` | 70 | 步骤 6 | M04 |
| 8 | `mcp/servers/godot/server.py` | 把上述工具包成 MCP 服务器 | `serve()` | 60 | 步骤 7 | M05 + tools |

**依赖链**：`sample 项目（靶子）→ scenes.py（解析）→ headless.py（校验）→ scene/script_tools（注册成 FC 工具）→ checkpoints（回滚）→ MCP server（复用出口）`。

**完成后你拥有**：MI-1 验收命令全程跑通 + `godot-agent rewind 1` 一键回滚 + stdio 起服被 M05 客户端桥接可见。

---

## 1. 知识点详解（每节五段：定义 → 大白话 → 举例 → 演进 → 易错点）

### 1.1 领域工具设计方法论（从能力面到工具集）

**① 严格定义**：领域工具设计四步法——①枚举领域操作面（Godot=项目/场景/脚本/资源/调试运行五域）②按用户任务聚类（"加敌人"实际需要 list→read_scene→write_script→edit_scene→validate 链）③定粒度（太细则模型走断头路，太粗不可控；准则：**一个工具=开发者手册里一个动词小节**）④定返回协议（每个 Observation 必含"下一步线索"，错误时给修复提示）。

**② 大白话**：给专家配**专业工具箱**而不是一把瑞士军刀。三个理由：**模型认知减负**（结构化场景树 vs 裸文本：省 token 且不解析错）、**安全收口**（结构化编辑在工具层拦截语法错误，不让坏数据落盘）、**验收内建**（改完自动校验，不用模型记得"还要检查一下"）。"下一步线索"不是过度设计——`godot_read_scene` 末尾附 `可编辑节点: Player, Camera2D`，模型下一步选择准确率显著提升。

**③ 举例**：本项目最终工具清单（25 个，核心 12 个）：

```text
项目管理: godot_open_project(路径) · godot_project_overview(资源统计/配置)
场景域:   godot_list_scenes · godot_read_scene(结构化场景树)
         godot_edit_scene(添加节点/改属性/连信号, 结构化patch) · godot_create_scene
脚本域:   godot_read_script(带符号大纲) · godot_write_script(乐观锁)
         godot_list_symbols(全项目符号表: 类/函数/信号)
校验域:   godot_check(语法) · godot_run_tests(gut) · godot_run_scene(headless 运行N帧截图)
```

**④ 演进**：通用文件工具（Cursor 起步形态）→ 领域 LSP/树协议（理解 AST）→ 领域 MCP（编辑器插件方向：社区 gdMCP 尝试）→ 本项目双轨：**结构化文本工具 + headless 校验**，不依赖编辑器插件，CI 环境也能跑。

**⑤ 易错点**：
- 场景工具与脚本工具要能互引（节点 `script: ExtResource("...")` 的资源 ID）——工具文档写清 ID 语义
- 别把 headless 运行做成同步阻塞大杀器：分级（check 秒级 / tests 分钟级 / run+截图 需确认）

### 1.2 .tscn / .gd 文件格式解析

**① 严格定义**：`.tscn` 是 Godot 场景的文本序列化格式，三个语义块——`ext_resource/sub_resource`（资源声明，ID 引用）、`node`（树，`parent="."` 相对路径挂树）、`connection`（信号连线）。**结构化编辑 = 解析三块 → 内存树 → 修改 → 序列化回写**，而非正则替换文本。`.gd` 用 `tokenize + ast` 轻量解析：缩进块树即可支撑符号大纲/函数级定位（完整语义分析是 Godot 编辑器的活）。

**② 大白话**：.tscn 是**乐高说明书**（文本但高度结构化）。正则替换=蒙眼改说明书——改"position"时恰好匹配到某节点名字就完蛋。正确做法：读懂说明书结构（解析）→ 在脑中搭好模型（内存树）→ 改对那一块 → 重新誊写（序列化）。`parent` 字段是**挂接地址**："挂在根下 Body 里的 Sprite 里"——相对路径，不含根名。

**③ 举例**：一个真实 .tscn 与解析器骨架：

```ini
[gd_scene load_steps=4 format=3 uid="uid://abc123"]

[ext_resource type="Script" path="res://enemy.gd" id="1_abcde"]

[node name="Player" type="CharacterBody2D"]
position = Vector2(120, 64)

[node name="Sprite" parent="." instance=ExtResource("2_xy")]

[connection signal="body_entered" from="Hitbox" to="." method="_on_hit"]
```

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

**④ 演进**：二进制 .scn（Godot 3 前，不可读）→ 文本 .tscn（3.x 起，git 友好）→ UID 机制（4.2 起资源带 uid，重命名不断引用）。**可读性换来了工具化空间**——AI Agent 能改 Godot 项目的前提就是文本格式。

**⑤ 易错点**：
- `parent` 是**兄弟相对路径**不是绝对路径：`parent="Body/Sprite"` = 根下 Body 的 Sprite 里
- `instance=ExtResource()` 的实例节点不能再加 `type`（类型来自被实例化场景）
- 序列化回写保持 Godot 的属性顺序习惯（name/type 在 header，其余字母序），否则 Diff 噪音巨大
- format=3 是 Godot 4 格式，解析器要拒绝 format=2 并明确报错

### 1.3 Diff 引擎与确认应用流

**① 严格定义**：统一 diff（unified format）——`@@ -l,s +l,s @@` 块头标注旧/新文件起始行与跨度，`-/+/空格` 三前缀行。本项目**不手写 diff 生成**（用标准库 `difflib.unified_diff`，LCS 算法了解即可），**手写"结构化 Diff 对象"与逐块应用器**：hunks 可**逐块审批**，应用器只回放被批准的块（按行号偏移重算）。

**② 大白话**：Diff 是**审稿意见单**，hunk 是一条条批注。全量应用=批注全收；逐块审查=编辑的权力（Cursor review 模式的核心体验）：批准 3 块中的 2 块，应用器要聪明地**跳过没批的那块并把后面块的行号对齐**——这就是 offset 累计的难点。

**③ 举例**：hunk 应用器（按批准偏移重算行号）：

```python
def apply_hunks(original: list[str], hunks: list[Hunk], approved: set[int]) -> list[str]:
    out, offset = [], 0
    for idx, h in enumerate(hunks):
        if idx not in approved:
            offset += len(h.old) - len(h.new)        # ★ 跳过的块也要累计行号漂移
            continue
        out.extend(original[h.old_start-2+offset : h.old_start-2+offset+len(h.old)])
        out.extend(h.new)
        offset += len(h.new) - len(h.old)
    return out
```

（手敲时建议重写干净：核心是 `offset` 累计已应用块造成的行号漂移，**跳过的块同样改变后续对齐**。测试必须覆盖"跳过中间块"场景。）

**④ 演进**：整文件覆盖（无审查）→ 全量 Diff 确认（patch 文档化）→ 逐 hunk 审查（现代标配）→ 语义 Diff（按 AST 节点展示，本项目 .gd 用函数级归组算轻量语义 Diff）。

**⑤ 易错点**：
- 行号 1-based，hunk 头 `@@ -5,3` = 从第 5 行起 3 行——数组换算 -2 的 off-by-one 是这类代码的坟场
- 空文件/末尾无换行 `\ No newline at end of file` 特殊处理
- 部分应用后必须**重新走 headless 校验**——用户批准的组合可能前所未有

### 1.4 检查点快照与回滚

**① 严格定义**：检查点=某时刻项目文件状态的可恢复记录。三要素——粒度：文件级快照聚合为"任务级检查点"；存储：`.agent_godot/checkpoints/{task_id}/{seq}__{path_hash}/{filename}` + `manifest.json`（seq/path/hash/mtime/existed/reason）；回滚：**逆序回放** manifest（同一文件多次修改时，逆序最终停在最初始状态）。

**② 大白话**：**游戏存档系统**。Agent 每次动手改文件前先存档（快照该文件）；一个任务所有存档打包成一个存档槽（task_id）；读档（回滚）时**从最后一格往回放**——先撤销最近的改动、再撤销更早的，最终回到动手前。为什么逆序：同一文件被改了三次（存档 1/2/3），正序回放会停在中间态，逆序才回到原点。为什么不用 git：**不打扰用户的版本管理**——Agent 的操作混进用户 commit 历史是污染，Agent 的失误由 Agent 自己的机制兜底。

**③ 举例**：完整实现（可直接参考）：

```python
class CheckpointStore:
    def snapshot(self, path: Path, task_id: str) -> str:
        seq = self._next_seq(task_id)
        snap_dir = self.root / task_id / f"{seq:03d}_{sha16(str(path))}"
        if path.exists():
            shutil.copy2(path, snap_dir / path.name)   # copy2 保 mtime——回滚后乐观锁还能对上
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

**④ 演进**：无（改错重来）→ 复制备份目录（无序、占空间）→ 带清单的文件级检查点（本项目）→ git 分支/stash（语义重、与用户 git 历史打架）。

**⑤ 易错点**：
- 快照"创建前不存在"的文件也要记录（`existed: false`），否则回滚留下幽灵文件
- `copy2` 保 mtime：回滚后 hash 版本号与 Agent 记忆一致，避免乐观锁误报
- manifest 写入要原子（临时文件+rename），进程中途被杀不能留半个清单

### 1.5 headless 校验闭环

**① 严格定义**：Godot 4 命令行模式是校验基石，四级分级（对应工具三档超时）：L1 语法 `--check-only`（秒级，拦 GDScript 解析错）/ L2 导入 `--import`（秒~分钟，拦资源断链）/ L3 测试 `-s tests/`（分钟，拦逻辑回归）/ L4 运行 场景跑 N 帧+stdout 断言（分钟+，拦运行时崩溃）。

**② 大白话**：**四级体检**。L1 问诊（秒级看有没有明显伤病）、L2 抽血（资源引用有没有断）、L3 专科检查（测试套件）、L4 上跑步机（真跑起来看崩不崩溃）。关键设计：**Agent 循环内嵌校验**——把 CI 的反馈周期从"分钟级人工节奏"压进"Agent 一轮循环秒级反馈"。错误不是灾难而是**Observation**：L1 挂→错误行号回填→模型改→再 L1——这就是"自我修复"闭环，也是 **Reflection 思想（M13）在领域层的第一次落地**。

**③ 举例**：错误解析器（把 Godot 输出翻译成模型友好的 Observation）：

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

**④ 演进**：人工点编辑器报错（无闭环）→ CI 里跑 headless（人类节奏）→ **Agent 循环内嵌校验**（秒级反馈闭环）。Godot 官方 headless 文档 + CI 模板是现成参考。

**⑤ 易错点**：
- 新建资源（贴图/场景）后**必须先 `--import`**，否则后续 check 找不到资源报假错
- Windows 下 Godot 首次 headless 启动可能弹安全对话框——CI 机器要预热过一次
- L4 运行校验必须配 `--quit-after` 帧数上限 + 进程 kill 兜底，否则游戏主循环永远不退出
- stdout 编码随系统 locale 变（GBK 陷阱）：统一 `errors="replace"` + 关键字用英文匹配

---

## 2. 接口设计（完整签名 = 你要手写的契约）

```python
# tools/godot/scenes.py
@dataclass
class SceneNode:
    name: str; type: str | None; parent: str
    instance_of: str | None            # ExtResource id
    props: dict[str, str]              # 原样字符串值
    script: str | None

class SceneFile:
    nodes: list[SceneNode]; connections: list[dict]; resources: dict[str, dict]
    def tree(self) -> dict: ...                       # 嵌套树视图（opaque 标记实例内部）
    def find(self, path: str) -> SceneNode: ...
    def add_node(self, parent: str, node: SceneNode) -> None: ...
    def set_prop(self, path: str, key: str, value: str) -> None: ...
    def connect_signal(self, signal_: str, from_: str, to: str, method: str) -> None: ...
    def serialize(self) -> str: ...

def parse_tscn(text: str) -> SceneFile: ...

# tools/godot/scene_tools.py（注册为 FC 工具，MCP 服务器复用同一实现）
class GodotReadSceneTool(BaseTool):     # 返回场景结构树（节点/类型/脚本/信号）
class GodotEditSceneTool(BaseTool):     # 结构化编辑 ops: add_node/set_prop/connect_signal/remove_node

# tools/godot/headless.py
class GodotRunner:
    def __init__(self, godot_bin: str, project_root: Path): ...
    async def check(self, script: str | None = None) -> CheckResult: ...
    async def import_assets(self) -> CheckResult: ...
    async def run_tests(self, timeout: int = 120) -> CheckResult: ...
    async def run_scene(self, scene: str, frames: int = 180) -> RunResult: ...

# tools/godot/checkpoints.py（M04 file_lock 的 snapshot 之上聚合）
class TaskCheckpoints:
    def open_task(self) -> str: ...                   # task_id
    def snapshot(self, path: Path) -> None: ...
    def rollback(self, task_id: str | None = None) -> list[str]: ...
    def list(self) -> list[CheckpointInfo]: ...
```

---

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

---

## 4. 手敲指引（函数级伪代码）

### 步骤 1：`lab/m06/sample/` 测试项目
用 Godot 编辑器手工建最小项目：player.tscn（CharacterBody2D+Sprite+脚本）、enemy.tscn、main.tscn（实例化前两者）。**验证**：Godot 编辑器能打开运行。

### 步骤 2：`tools/godot/scenes.py`——解析
| 函数 | 作用（伪代码） |
|---|---|
| `parse_tscn` | §1.2 ③ 代码：`正则按 [节头分块 → header 提取 kind+属性 → body 提取属性行（key = value 原样字符串）→ 装进 SceneNode/connections/resources` |
| `parse_props` | `逐行 partition("=") → 左为 key，右原样保留（Vector2(120,64) 不解析成对象，保持字符串）` |
**验证**：三个样本场景（纯手搭/实例化/信号复杂树）解析出的节点数与树深正确。

### 步骤 3：`tools/godot/scenes.py`——编辑与序列化
| 函数 | 作用（伪代码） |
|---|---|
| `add_node` | `校验 parent 存在 → 追加 SceneNode(parent=参数) → serialize 回写` |
| `set_prop` | `find 定位节点 → props[key]=value → serialize` |
| `connect_signal` | `connections 追加一条记录 → serialize` |
| `serialize` | `反解析：gd_scene 头 → ext/sub_resource 块 → node 块（header name/type/parent + 属性字母序）→ connection 块`。格式顺序必须还原 Godot 习惯，否则 Diff 全是噪音 |
**验证**：parse→改→serialize 的结果 Godot 编辑器能加载（黄金测试：原样 parse→serialize 输出==输入，或 diff 仅空白）。

### 步骤 4：`tools/godot/headless.py`
| 函数 | 作用（伪代码） |
|---|---|
| `check` | §1.5 ③ 代码：`create_subprocess_exec(godot_bin, --headless --path --check-only) → wait_for 15s → GD_ERROR 正则找错误行 → 无错 ok=True / 有错 ToolResponse(ok=False, hint=修复指引)` |
| `import_assets` | `同上但 --import，timeout 120s` |
| `run_tests` | `-s tests/run.gd，timeout 120s，解析通过数/失败数` |
| `run_scene` | `场景路径 + --quit-after N 帧 + kill 兜底 + stdout 断言` |
**验证**：故意在 sample 项目写错语法 → check 返回的行号正确；`--import` 后新资源可被引用。

### 步骤 5：`scene_tools.py` + `script_tools.py`（12 个 FC 工具注册）
| 工具 | 作用（伪代码） |
|---|---|
| `GodotReadSceneTool` | `读 .tscn → parse_tscn → tree() 嵌套树渲染（附"可编辑节点"线索）→ summary` |
| `GodotEditSceneTool` | `Params{scene, ops:[{op: add_node/set_prop/connect_signal/remove_node, ...}]} → 逐 op 调 SceneFile 方法 → 写前 checkpoint.snapshot → serialize 落盘 → 自动触发 godot_check → 结果并入 Observation` |
| `GodotWriteScriptTool` | `复用 M04 OptimisticFileStore.write（乐观锁）+ 写后自动 check` |
| `GodotListSymbolsTool` | `扫描 *.gd 的 class_name/func/signal 声明行 → 符号表` |
**验证**：M03 Loop 真机：`craft "给 sample 的 player 加速度属性"` 全链路走通（read_scene→edit→check）。

### 步骤 6：`tools/godot/checkpoints.py`
| 函数 | 作用（伪代码） |
|---|---|
| `open_task` | `uuid 生成 task_id → 建目录 → 返回 id` |
| `snapshot` | `调 M04 CheckpointStore.snapshot + manifest 追加（原子写）` |
| `rollback` | §1.4 ③ 代码：`逆序遍历 manifest → existed 则拷回 / 不存在则删 → 返回恢复清单` |
**验证**：多文件任务（改 2 个+新建 1 个）回滚后：改的恢复原样、新建的消失。

### 步骤 7：`mcp/servers/godot/server.py`
| 函数 | 作用（伪代码） |
|---|---|
| `serve` | `stdin 逐行读 JSON-RPC → initialize 回能力{tools} → tools/list 返回工具清单（复用 scene/script_tools 的注册表）→ tools/call 分发执行 → stdout 单行写回`——M05 客户端的镜像，协议同一套 |
**验证**：`python -m agent_godot.mcp.servers.godot` stdio 起服 → M05 客户端桥接后 `mcp__godot__*` 工具可见可用。

### 步骤 8：Diff 应用器（逐 hunk 审批）
§1.3 ③ 的 `apply_hunks` + 前端确认流（CLI 先做 y/n 逐块）。**验证**：跳块应用后 check 仍通过。

---

## 5. 测试与验收

```python
def test_parse_instance_scene_opaque():
    sf = parse_tscn(SAMPLE_WITH_INSTANCE)
    tree = sf.tree()
    assert tree["children"][1].get("opaque") is True      # 实例内部不展开

def test_serialize_roundtrip():
    text = Path("sample/player.tscn").read_text()
    assert SceneFile.serialize(parse_tscn(text)) == text  # 或仅空白差异

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

---

## 6. 踩坑记录（留白自填）

| 日期 | 坑 | 现象 | 根因 | 解法 | 关联知识点 |
|---|---|---|---|---|---|
|     |    |     |     |    |          |

---

## 7. 面试拷打（附详细参考答案）

**1. 有了通用文件工具为什么还要领域工具？三个理由展开。**
答：①**模型认知减负**：`godot_read_scene` 返回结构化场景树（节点/类型/父子/信号连线），比裸读 .tscn 文本省约 80% token 且不会解析错——模型把决策预算花在"怎么改"而不是"怎么读懂格式"上；②**安全收口**：场景修改走结构化编辑（add_node/set_prop），语法与引用错误在工具层拦截（写盘前校验 ExtResource 存在性），文本拼接则坏数据直接落盘；③**验收内建**：领域工具改完自动触发 headless 校验，错误秒级回传——通用工具改完模型经常"忘记"验证。本质：把领域知识从提示词（软约束）下沉到工具层（硬保证）。

**2. .tscn 的 parent 字段语义是什么？实例化节点为什么不展开？**
答：parent 是**相对于根、不含根名的挂接路径**——`parent="."` 表示根的直接子节点，`parent="Body/Sprite"` 表示挂在根下 Body 的 Sprite 里（不是绝对路径 `/root/Body/Sprite`）。实例化节点（`instance=ExtResource()`）不展开内部子树，因为其内部结构定义在**被实例化的那个场景文件**里——.tscn 中只有覆盖性修改（改属性/加子节点），不重复声明内部树。解析器对策：找不到父路径的子树标记为 opaque（黑盒），工具文档告知模型"实例内部要看原场景文件"。

**3. 为什么用结构化 patch 编辑场景而不是让模型直接改文本？**
答：文本编辑的失效模式：模型生成的 .tscn 语法错误（引号/缩进/属性格式）直接落盘，Godot 加载失败且错误信息离谱（解析器崩溃在错误行附近而非错误本身）；正则替换的失效模式：改"position"恰好匹配到名为 position 的节点名。结构化编辑：类型安全的 API（add_node 必须给合法 parent）、写前校验（ExtResource 引用存在）、序列化格式保证（Godot 习惯的属性顺序）——**错误在工具层被拦截而不是在游戏运行时爆炸**。这也是"编译器思想"：语法层挡掉一大类错误。

**4. 检查点为什么不用 git stash？逆序回放防的是什么场景？**
答：不用 git 的三个理由：①污染用户历史——Agent 的快照/回滚混进用户 commit 序列，将来 blame/revert 都是灾难；②git 语义重——分支/stash/索引对"文件级快照"这个需求是过度设计，且要求项目必须已是 git 仓库；③不可控副作用——stash 恢复可能触发 hooks/合并冲突。逆序回放防的场景：同一文件被多次修改（快照 1、2、3 分别对应 v0→v1→v2→v3），正序回放 v0→v1→v2 最终停在 v2（中间态），逆序 v3→v2→v1→v0 才回到初始。这是"多版本链条回退"的通用规律（数据库 undo log 同理）。

**5. 回滚后为什么要保留 mtime？**
答：乐观锁的版本一致性。M04 的文件编辑用 content hash 做版本号，但 mtime 是辅助信号（部分校验/缓存场景用）。`copy2`（保 mtime）而非 `copy`——回滚后的文件带着**快照时点的 mtime**，与 Agent 会话中记录的"我最后读到的时间戳"一致，后续编辑的冲突检测不会误报"文件被外部修改"。如果 mtime 变成回滚时刻，Agent 下一次 write 可能误判冲突。细节体现：分布式系统里"恢复状态要连同元数据一起恢复"的普遍原则。

**6. headless 四级校验各拦截什么错误？为什么新资源必须先 --import？**
答：L1 语法（--check-only）：GDScript 解析错（缩进/未定义变量/签名不匹配），秒级；L2 导入（--import）：资源引用断链、场景格式错——Godot 的资源系统需要先构建导入缓存（.godot 目录）才能被 check 引用，**新建的贴图/场景没导入过，缓存里没有**，后续校验全部报"资源不存在"的假错；L3 测试（-s）：逻辑回归（测试套件红）；L4 运行（跑 N 帧）：运行时崩溃（空引用/死循环/信号连接错）——编译期查不出的错。顺序也有讲究：L1 快失败快，先跑；L4 最贵且有副作用（截图/状态），最后跑且需用户确认。

**7. L4 运行校验的两个防挂死手段？**
答：①`--quit-after N` 帧数上限——命令行参数让 Godot 跑满 N 帧自动退出（正常路径）；②进程 kill 兜底——asyncio.wait_for 超时后 `Process.kill()` 强杀（异常路径：游戏改了主循环/弹了对话框/死循环导致帧计数不推进）。两层缺一不可：只有 --quit-after 遇到"游戏卡在第 1 帧"永不退出；只有 kill 兜底则正常长测试也被误杀。kill 后还要清理半成品（M04 沙箱的 on_cancel 钩子）。

**8. "校验错误回填驱动模型自修复"是哪个范式的雏形？**
答：**Reflection（反思/自我修正）**的雏形，且是其中最可靠的形态——**客观验证器回路**（M13 §1.2 展开）：验证器（headless 校验）是外部客观批评者，比模型自评可靠得多（模型自评倾向给自己打高分）。链路：执行→客观验证→错误作为反馈输入→修正→再验证，直到通过或 max_fixes 用尽。这也是 M18 GRPO 可验证奖励（语法检查/测试通过）的前身——同一思想在训练侧的复用。

**9. Windows 下 headless 集成最可能遇到的三个环境坑？**
答：①**首次启动安全对话框**——Windows SmartScreen 可能拦截未签名 exe，CI 机器必须预热跑过一次并放行；②**stdout 编码陷阱**——控制台默认 GBK，Godot 输出里的特殊字符 decode 崩溃：统一 `decode(errors="replace")` 且错误关键字匹配用英文；③**路径分隔符/大小写**——`res://` 正斜杠 vs Windows 反斜杠、大小写不敏感文件系统导致 `is_relative_to` 判定漂移（M04 同款坑）：所有路径先 posix 化再比较。附赠第四坑：Godot 进程残留（L4 kill 后子进程树未清，要用 taskkill /T 或 job object）。

**10. 开放题：把这套"读-改-验-回滚"方法论迁移到 Unity/Unreal，哪些能复用，哪些是 Unity 特有难题？**
答：可复用：方法论全套（领域工具四步法、乐观锁、检查点逆序回滚、校验错误回填自修复、Diff 逐块审批）——这些是领域无关的工程模式；结构化文本解析思路部分可复用（.unity 是 YAML）。Unity 特有难题：①**没有官方 headless CLI**——批处理模式（-batchmode -quit）功能有限，语法检查/测试运行依赖 Unity 编辑器进程，秒级反馈做不到了（编辑器启动就几十秒）；②**资产序列化半私有**——.meta 文件 GUID 系统复杂，场景 YAML 结构深且版本间变动；③**生态闭源**——引擎内部行为黑盒，错误信息质量不如 Godot 开源可查。Unreal 更难：.uasset 二进制为主，文本资产有限。结论：这套方法论对"文本资产+官方CLI"的引擎（Godot）是最优落地。

---

## 8. 教程映射与延伸

- Godot 官方：命令行教程、.tscn 格式文档（tscn 无正式规范——读源码 `resource_format_text.cpp` 是终极资料）
- 必读：unified diff 格式规范（GNU diffutils 手册附录）
- 选读：gut 测试框架文档（L3 级校验载体）
