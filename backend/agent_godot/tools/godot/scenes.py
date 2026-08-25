"""tools/godot/scenes.py —— .tscn 解析/编辑/序列化（M06 §1.2 / §3 / §4 步骤 2-3）

.tscn 是 Godot 场景的文本序列化格式（乐高说明书），三个语义块：
- ext_resource / sub_resource：资源声明（ID 引用）
- node：树——parent 是**相对根、不含根名**的挂接地址：
  首个节点 = 场景根（无 parent 属性）；parent="." = 根的直接子节点；
  parent="Body/Sprite" = 根下 Body 的 Sprite 里（不是绝对路径！）
- connection：信号连线

结构化编辑 = 解析三块 → 内存树 → 修改 → 序列化回写，而非正则替换文本
（蒙眼改说明书——改 "position" 恰好匹配到某节点名就完蛋）。

序列化约定（保证 parse→serialize 往返一致，也是 Godot 4 的书写习惯）：
- gd_scene 头：load_steps（资源数+1，无资源时省略）/ format / uid
- node 头：name → type → parent → instance=ExtResource(...)
- 属性体：script 最先，其余按字母序（避免 Diff 噪音）
- 节之间空一行；实例化节点的内部子树不在此文件（tree() 标记 opaque）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADER_ATTR = re.compile(r'(\w+)=("[^"]*"|[^\s\]]+)')   # 带引号 或 数字/裸词
_INSTANCE_ATTR = re.compile(r'instance=ExtResource\("([^"]+)"\)')
_SCRIPT_REF = re.compile(r'^ExtResource\("([^"]+)"\)$')
_STEM_JUNK = re.compile(r"\W+")

_KNOWN_NODE_KEYS = {"name", "type", "parent", "instance"}


def _parse_attrs(s: str) -> dict[str, str]:
    """节头属性解析：带引号去引号，数字/裸词保持原样（load_steps=2 / format=3）。"""
    attrs: dict[str, str] = {}
    for m in _HEADER_ATTR.finditer(s):
        v = m.group(2)
        if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
            v = v[1:-1]
        attrs[m.group(1)] = v
    return attrs


def _fmt_attr(k: str, v: str) -> str:
    """属性序列化：纯数字不加引号（Godot 习惯：load_steps=4 / format=3）。"""
    return f"{k}={v}" if v.isdigit() else f'{k}="{v}"'


class SceneFormatError(ValueError):
    """不支持的 .tscn 格式（如 Godot 3 的 format=2——解析器明确拒绝而非静默错读）。"""


@dataclass
class SceneNode:
    """场景树上的一个节点（props 值保持原样字符串——Vector2(120,64) 不解析成对象）。"""
    name: str
    type: str | None = None
    parent: str = "."                      # "." = 根的直接子节点
    instance_of: str | None = None         # ExtResource id（类型来自被实例化的场景）
    props: dict[str, str] = field(default_factory=dict)
    script: str | None = None              # ExtResource id
    extra_header: dict[str, str] = field(default_factory=dict)  # 其余 header 属性原样保留


@dataclass
class SceneFile:
    """一个 .tscn 的内存模型：nodes 列表（声明序）+ 资源表 + 信号连线表。"""
    nodes: list[SceneNode] = field(default_factory=list)
    connections: list[dict] = field(default_factory=list)
    resources: dict[str, dict] = field(default_factory=dict)  # id -> {"kind","attrs"[,"props"]}
    header: dict[str, str] = field(default_factory=dict)      # gd_scene 头属性（保持原序）

    # ---------- 路径语义（本模块最烧脑的 30 行，§3） ----------

    def _abs_path(self, n: SceneNode) -> str:
        """节点的"项目内"绝对路径：parent 本身已是绝对路径（不含根名）。"""
        return n.name if n.parent in (".", "") else f"{n.parent}/{n.name}"

    def _index(self) -> dict[str, SceneNode]:
        return {self._abs_path(n): n for n in self.nodes}

    def _norm(self, path: str) -> str:
        """路径归一化：容忍以根名开头（"Main/Player" → "Player"）。"""
        path = path.strip().strip("/")
        if self.nodes:
            root_name = self.nodes[0].name
            if path.startswith(root_name + "/"):
                return path[len(root_name) + 1:]
        return path

    def find(self, path: str) -> SceneNode:
        """按路径定位节点：根用其名；子节点 "Sprite" / "Body/Sprite"。"""
        path = self._norm(path)
        for n in self.nodes:
            if self._abs_path(n) == path:
                return n
        raise KeyError(f"节点不存在: {path}（可用: "
                       f"{', '.join(self._abs_path(n) for n in self.nodes)}）")

    def _script_path(self, n: SceneNode) -> str:
        """节点脚本的 res:// 路径（给模型看；拿不到返回空串）。"""
        if not n.script:
            return ""
        res = self.resources.get(n.script) or {}
        return res.get("attrs", {}).get("path", "")

    # ---------- 树视图 ----------

    def tree(self) -> dict:
        """nodes 列表 → 嵌套树（实例节点标 opaque——内部结构在被实例化的场景里）。

        挂接规则：首个节点 = 场景根（挂到 (root)）；之后 parent="." 的节点
        都是**场景根的子节点**；parent="Body/Sprite" 挂到对应容器。
        """
        root = {"name": "(root)", "type": "", "children": []}
        containers: dict[str, list] = {"": root["children"]}   # abs路径 -> children 列表
        root_path: str | None = None
        for n in self.nodes:
            if root_path is None:                 # 场景根
                parent_key = ""
                root_path = self._abs_path(n)
            elif n.parent in (".", ""):
                parent_key = root_path            # 根的直接子节点
            else:
                parent_key = n.parent
            children: list = []
            d = {"name": n.name, "type": n.type or "",
                 "path": self._abs_path(n),
                 "instance": self._script_path_inst(n),
                 "script": self._script_path(n),
                 "children": children}
            if n.instance_of:
                d["opaque"] = True          # 实例内部不展开（§7 面试题 2）
            containers[self._abs_path(n)] = children
            # 父路径缺失（覆盖实例内部节点）→ 挂到最深的已知祖先，同样标 opaque
            key = parent_key
            while key not in containers:
                key = key.rsplit("/", 1)[0] if "/" in key else ""
            if key != parent_key:
                d["opaque"] = True
            containers[key].append(d)
        return root

    def _script_path_inst(self, n: SceneNode) -> str:
        """实例化来源场景的 res:// 路径（instance=ExtResource 的目标）。"""
        if not n.instance_of:
            return ""
        res = self.resources.get(n.instance_of) or {}
        return res.get("attrs", {}).get("path", "")

    # ---------- 结构化编辑（语法/引用错误在工具层拦截，不让坏数据落盘） ----------

    def add_node(self, parent: str, node: SceneNode) -> None:
        """挂接新节点。parent="." / 根名 / 已存在的节点路径；同父重名、实例带 type 都拒绝。"""
        if self.nodes and parent in (self.nodes[0].name,):     # 根名归一化为 "."
            parent = "."
        parent = self._norm(parent)                            # 容忍 "根名/子" 前缀
        node.parent = parent if parent else "."
        if node.parent not in (".", "") and node.parent not in self._index():
            raise KeyError(f"父节点不存在: {node.parent}")
        if self._abs_path(node) in self._index():
            raise ValueError(f"节点已存在: {self._abs_path(node)}")
        if node.instance_of and node.type:
            raise ValueError("实例化节点不能再指定 type（类型来自被实例化的场景）")
        if not node.instance_of and not node.type:
            raise ValueError("非实例化节点必须指定 type")
        if node.script and node.script not in self.resources:
            raise KeyError(f"script 引用的 ExtResource 不存在: {node.script}")
        if node.instance_of and node.instance_of not in self.resources:
            raise KeyError(f"instance 引用的 ExtResource 不存在: {node.instance_of}")
        self.nodes.append(node)

    def set_prop(self, path: str, key: str, value: str) -> None:
        """设置节点属性。key="script" 且 value 是 res:// 路径时自动解析资源 ID。"""
        n = self.find(path)
        if key == "script" and value.startswith("res://"):
            n.script = self.resource_for("Script", value)
            return
        n.props[key] = value

    def connect_signal(self, signal_: str, from_: str, to: str, method: str) -> None:
        """追加信号连线（from 必须存在；to="." 表示根；重复连线跳过）。"""
        self.find(from_)                                # 校验 from 是真实节点
        to = "." if (self.nodes and to == self.nodes[0].name) else self._norm(to)
        if to not in (".",) and to not in self._index():
            raise KeyError(f"connection 的 to 节点不存在: {to}")
        if any(c.get("signal") == signal_ and c.get("from") == from_
               and c.get("to") == to for c in self.connections):
            return                                      # 去重
        self.connections.append({"signal": signal_, "from": from_,
                                 "to": to, "method": method})

    def remove_node(self, path: str) -> int:
        """删除节点及其整棵子树（含引用它的信号连线），返回删除的节点数。"""
        target = self.find(path)
        prefix = self._abs_path(target)
        doomed = {self._abs_path(n) for n in self.nodes
                  if self._abs_path(n) == prefix
                  or self._abs_path(n).startswith(prefix + "/")}
        self.nodes = [n for n in self.nodes if self._abs_path(n) not in doomed]
        self.connections = [c for c in self.connections
                            if not self._conn_hits(c, prefix)]
        return len(doomed)

    def _conn_hits(self, conn: dict, prefix: str) -> bool:
        """连线是否引用了被删子树（from/to 以该路径为前缀；to="." 不受影响）。"""
        for key in ("from", "to"):
            p = conn.get(key, "")
            if p.startswith("res://"):
                continue
            if p == prefix or p.startswith(prefix + "/"):
                return True
        return False

    def resource_for(self, res_type: str, path: str) -> str:
        """按 (type, path) 找 ExtResource id；没有则新建一条资源声明。

        场景工具与脚本工具互引的关键（M06 §1.1 易错点①）：
        模型只需传 res:// 路径，ID 语义由本方法收口。
        """
        for rid, res in self.resources.items():
            if (res["kind"] == "ext"
                    and res["attrs"].get("type") == res_type
                    and res["attrs"].get("path") == path):
                return rid
        num = 1
        for rid in self.resources:
            head = rid.split("_", 1)[0]
            if head.isdigit():
                num = max(num, int(head) + 1)
        stem = _STEM_JUNK.sub("", path.rsplit("/", 1)[-1].split(".")[0]).lower()[:8]
        rid = f"{num}_{stem or 'res'}"
        self.resources[rid] = {"kind": "ext",
                               "attrs": {"type": res_type, "path": path, "id": rid}}
        return rid

    # ---------- 序列化（反解析：格式顺序还原 Godot 习惯，否则 Diff 全是噪音） ----------

    def serialize(self) -> str:
        blocks: list[tuple[bool, str]] = []          # (是否资源块, 文本)

        # ① gd_scene 头（load_steps 重算 = 资源数 + 1；无资源时省略）
        head = dict(self.header)
        if self.resources:
            head["load_steps"] = str(len(self.resources) + 1)
        else:
            head.pop("load_steps", None)
        head.setdefault("format", "3")
        blocks.append((False, "[gd_scene " + " ".join(_fmt_attr(k, v)
                                                      for k, v in head.items()) + "]"))

        # ② 资源块（保持原文件顺序，新增资源追加尾部；sub_resource 带属性体）
        for rid, res in self.resources.items():
            attrs = " ".join(_fmt_attr(k, v) for k, v in res["attrs"].items())
            block = f'[{res["kind"]}_resource {attrs}]'
            if res["kind"] == "sub":
                block += "".join(
                    f"\n{k} = {v}" for k, v in sorted(res.get("props", {}).items()))
            blocks.append((True, block))

        # ③ 节点块（首节点=场景根不写 parent；实例节点 instance=ExtResource）
        for i, n in enumerate(self.nodes):
            parts = [f'name="{n.name}"']
            if n.type:
                parts.append(f'type="{n.type}"')
            if i > 0 and n.parent:                      # 根之后的节点必须写 parent
                parts.append(f'parent="{n.parent}"')
            if n.instance_of:
                parts.append(f'instance=ExtResource("{n.instance_of}")')
            parts.extend(_fmt_attr(k, v) for k, v in n.extra_header.items())
            block = "[node " + " ".join(parts) + "]"
            body: list[str] = []
            if n.script:
                body.append(f'script = ExtResource("{n.script}")')
            body.extend(f"{k} = {n.props[k]}" for k in sorted(n.props))
            if body:
                block += "\n" + "\n".join(body)
            blocks.append((False, block))

        # ④ 信号连线块
        for c in self.connections:
            parts = [f'signal="{c["signal"]}"', f'from="{c["from"]}"',
                     f'to="{c["to"]}"', f'method="{c["method"]}"']
            parts.extend(_fmt_attr(k, v) for k, v in c.get("extra", {}).items())
            blocks.append((False, "[connection " + " ".join(parts) + "]"))

        # 拼接：Godot 习惯——同类资源块之间不空行，其余节之间空一行
        out: list[str] = []
        prev_is_res = False
        for is_res, block in blocks:
            if out:
                out.append("\n" if (is_res and prev_is_res) else "\n\n")
            out.append(block)
            prev_is_res = is_res
        return "".join(out) + "\n"


# ---------- 解析 ----------

def parse_props(lines: list[str]) -> dict[str, str]:
    """属性行解析：逐行 partition("=")，左为 key，右原样保留（不解析 Vector2 等字面量）。"""
    props: dict[str, str] = {}
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith(";") or s.startswith("["):
            continue
        key, sep, value = s.partition("=")
        if not sep:
            continue
        props[key.strip()] = value.strip()
    return props


def parse_tscn(text: str) -> SceneFile:
    """解析 .tscn 文本 → SceneFile。按 [节头 分块，header 提取 kind+属性。"""
    sf = SceneFile()
    # 按节头分块（前瞻分组：保留 "[" 在节内，空行留在上一节 body 里被跳过）
    sections = re.split(r"\n(?=\[)", "\n" + text.lstrip("\n"))
    for section in sections:
        if not section.startswith("["):
            continue                                    # 节头前的杂项（空行/注释）丢弃
        header, _, body = section.partition("]")
        tokens = header[1:].strip().split(" ", 1)
        kind = tokens[0]
        attrs_str = tokens[1] if len(tokens) > 1 else ""
        attrs = _parse_attrs(attrs_str)
        body_lines = body.splitlines()

        if kind == "gd_scene":
            if attrs.get("format") != "3":
                raise SceneFormatError(
                    f"不支持的 .tscn format={attrs.get('format')!r}"
                    f"（仅支持 Godot 4 的 format=3）")
            sf.header = attrs
        elif kind == "ext_resource":
            sf.resources[attrs["id"]] = {"kind": "ext", "attrs": attrs}
        elif kind == "sub_resource":
            sf.resources[attrs["id"]] = {"kind": "sub", "attrs": attrs,
                                         "props": parse_props(body_lines)}
        elif kind == "node":
            m = _INSTANCE_ATTR.search(attrs_str)
            extra = {k: v for k, v in attrs.items() if k not in _KNOWN_NODE_KEYS}
            props = parse_props(body_lines)
            script = None
            if "script" in props:
                sm = _SCRIPT_REF.match(props["script"])
                if sm:
                    script = sm.group(1)
                    del props["script"]
            sf.nodes.append(SceneNode(
                name=attrs["name"], type=attrs.get("type"),
                parent=attrs.get("parent", "."),       # 无 parent 属性 = 场景根
                instance_of=m.group(1) if m else None,
                props=props, script=script, extra_header=extra))
        elif kind == "connection":
            extra = {k: v for k, v in attrs.items()
                     if k not in ("signal", "from", "to", "method")}
            sf.connections.append({"signal": attrs.get("signal", ""),
                                   "from": attrs.get("from", ""),
                                   "to": attrs.get("to", "."),
                                   "method": attrs.get("method", ""),
                                   "extra": extra})
        # 其余节头（[editable] 等）忽略——不破坏原文的可加载性
    return sf
