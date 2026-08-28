"""graphrag/project_graph.py —— 项目结构图：代码→图（解析式）+ 增量同步
（M11 §1.3 / §4 步骤 3）

照着施工图纸誊关系：M06 解析器（parse_tscn / gd_symbols）已经把事实
算准了——节点树、信号连线、脚本挂载是 100% 确定的，翻译成图就行，
**不请 LLM**（用概率模型抄写确定性数据是纯减分，§7 面试 6）。

两类同步：
- full_sync：遍历项目 .tscn/.gd 全量誊写（首次建图）
- upsert_file：文件级原子替换——删旧子图（DETACH DELETE）+ 重插，
  同一事务（run_tx），中途失败不留残图（§1.3 易错点②）。

使能的查询：变更影响分析（改信号签名影响谁）、死信号体检
（声明了但没人监听）——都是"关系型问题"，向量检索答不了。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..tools.godot.scenes import SceneFile, parse_tscn
from ..tools.godot.script_tools import gd_symbols
from .cypher import CYPHER_TEMPLATES, query

logger = logging.getLogger(__name__)


@dataclass
class ImpactEdge:
    """影响分析的一条命中：信号 → 监听场景节点 → 处理脚本。"""
    signal: str
    node: str                    # 监听节点名（connection 的 to 侧）
    path: str                    # 监听节点全路径（场景文件#节点路径）
    script: str                  # 处理脚本 res:// 路径
    method: str = ""             # 处理方法（挂在 LISTENS 边上）


def _res(path: Path) -> str:
    """项目内路径 → res:// 路径（正斜杠统一，图谱里的路径度量衡）。"""
    return "res://" + Path(path).as_posix().removeprefix("res://").lstrip("/")


def _node_path(scene_res: str, node_abs: str) -> str:
    """节点全路径 = 场景 res 路径 + '#' + 场景内绝对路径。

    唯一键用全路径而非节点名——不同场景可同名节点（§1.3 易错点①）。
    """
    return f"{scene_res}#{node_abs}"


class ProjectGraphSync:
    """项目结构图同步器：解析器事实 → 图语句批 → 事务执行。"""

    def __init__(self, driver, root: Path | None = None) -> None:
        self.driver = driver
        self._root = root if root is None else Path(root)   # 项目根（可后设）

    # ---------------------------------------------------------- 全量

    async def full_sync(self, project_id: str, root: Path) -> int:
        """遍历 root 下 .tscn/.gd 逐文件 upsert，返回入图的节点总数。

        节点数口径：SceneNode + Script（验收：= 解析器节点总数）。
        """
        self._root = Path(root)
        files = sorted(p for p in self._root.rglob("*")
                       if p.suffix in (".tscn", ".gd"))
        for p in files:
            rel = p.relative_to(self._root)
            await self.upsert_file(project_id, rel)
        await query(self.driver, "merge_project_meta", project=project_id)
        total = await self._node_total()
        logger.info("full_sync: %s 个文件 → %d 节点", len(files), total)
        return total

    async def _node_total(self) -> int:
        """图内 SceneNode+Script 总数（内存驱动直读，Neo4j 走查询）。"""
        from .cypher import InMemoryGraphDriver
        if isinstance(self.driver, InMemoryGraphDriver):
            return (self.driver.count("SceneNode")
                    + self.driver.count("Script"))
        rows = await self.driver.run(
            "MATCH (n) WHERE n:SceneNode OR n:Script "
            "RETURN count(n) AS n")
        return rows[0]["n"] if rows else 0

    # ---------------------------------------------------------- 增量（文件级原子）

    async def upsert_file(self, project_id: str, path: Path) -> None:
        """单文件原子替换：删旧子图 → 解析 → 语句批 → 单事务重插。

        path = 项目内相对路径（"player.gd"）；传绝对路径且在 root 下时
        自动转相对（res:// 度量衡不变）。
        """
        p = Path(path)
        if p.is_absolute() and self._root is not None:
            rel = p.relative_to(self._root)
        else:
            rel = p
        res = _res(rel)
        statements: list[tuple[str, dict]] = [
            (CYPHER_TEMPLATES["merge_project_meta"], {"project": project_id})
        ]

        if rel.suffix == ".tscn":
            # 删旧：该场景的 SceneNode 子图（前缀 "场景#..." 精确圈定）
            statements.append((CYPHER_TEMPLATES["delete_scene_subgraph"],
                               {"project": project_id, "prefix": res + "#"}))
            sf = parse_tscn(self._read(rel))
            statements.extend(self._scene_statements(project_id, res, sf))
        elif rel.suffix == ".gd":
            statements.append((CYPHER_TEMPLATES["delete_script_node"],
                               {"project": project_id, "path": res}))
            statements.extend(self._script_statements(
                project_id, res, self._read(rel)))

        await self.driver.run_tx(statements)
        logger.debug("upsert_file: %s（%d 条语句）", res, len(statements))

    def _read(self, rel: Path) -> str:
        """读文件内容：root 下优先（full_sync 记住的项目根）。"""
        if self._root is not None and (self._root / rel).exists():
            p = self._root / rel
        else:
            p = rel
        return p.read_text(encoding="utf-8", errors="replace")

    # ---------------------------------------------------------- .tscn → 语句批

    def _scene_statements(self, project_id: str, res: str,
                          sf: SceneFile) -> list[tuple[str, dict]]:
        """场景文件 → 图语句批（SceneNode 树 + CHILD + LISTENS + 脚本挂载）。"""
        stmts: list[tuple[str, dict]] = []

        # ① 先建脚本节点（ATTACHED_SCRIPT 边的两端要都在）
        script_paths: set[str] = set()
        for n in sf.nodes:
            sp = sf._script_path(n)
            if sp:
                script_paths.add(sp)
                stmts.append((CYPHER_TEMPLATES["merge_script_node"],
                              {"project": project_id, "path": sp,
                               "name": Path(sp).stem}))
        # 实例化依赖：本场景脚本 → 被实例化场景的脚本（DEPENDS_ON）
        inst_paths = {sf._script_path_inst(n) for n in sf.nodes
                      if n.instance_of and sf._script_path_inst(n)}

        # ② 节点树 + CHILD 边
        for n in sf.nodes:
            abs_path = sf._abs_path(n)
            node_key = _node_path(res, abs_path)
            stmts.append((CYPHER_TEMPLATES["merge_scene_node"],
                          {"project": project_id, "path": node_key,
                           "name": n.name,
                           "type": n.type or "(instance)"}))
            if n.parent and n.parent not in (".", "") and n.parent != abs_path:
                # parent 本身就是绝对路径（M06 语义），首个节点无父
                stmts.append((CYPHER_TEMPLATES["merge_child_edge"],
                              {"project": project_id,
                               "parent": _node_path(res, n.parent),
                               "child": node_key}))
            sp = sf._script_path(n)
            if sp:
                stmts.append((CYPHER_TEMPLATES["merge_attached_script_edge"],
                              {"project": project_id, "path": node_key,
                               "script": sp}))
            ip = sf._script_path_inst(n)
            if ip:
                # 场景实例化：节点记 instance 属性；脚本间建 DEPENDS_ON
                stmts.append((CYPHER_TEMPLATES["merge_scene_node"],
                              {"project": project_id, "path": node_key,
                               "name": n.name,
                               "type": f"(instance {Path(ip).stem})"}))

        # ③ 脚本依赖边（A 场景实例化 B 场景 → A 的脚本依赖 B 的脚本）
        own_scripts = script_paths
        for a in own_scripts:
            for b in inst_paths:
                if a != b:
                    stmts.append((CYPHER_TEMPLATES["merge_script_dep"],
                                  {"project": project_id, "from_script": a,
                                   "to_script": b}))

        # ④ 信号连线：监听者 = to 侧节点（含 to="." 即场景根）；
        #    from/method 挂边属性——connection 的另一半事实
        for c in sf.connections:
            to = c.get("to", ".")
            if to in (".", ""):
                to_abs = sf._abs_path(sf.nodes[0]) if sf.nodes else ""
            else:
                to_abs = sf._norm(to)
            if not to_abs:
                continue
            stmts.append((CYPHER_TEMPLATES["merge_listens_edge"],
                          {"project": project_id,
                           "path": _node_path(res, to_abs),
                           "signal": c.get("signal", ""),
                           "method": c.get("method", ""),
                           "from_node": c.get("from", "")}))
        return stmts

    # ---------------------------------------------------------- .gd → 语句批

    def _script_statements(self, project_id: str, res: str,
                           text: str) -> list[tuple[str, dict]]:
        """脚本文件 → 图语句批（Script + EXTENDS + DECLARES）。

        .gd 的信号声明进图（DECLARES）是死信号体检的数据源：连声明都不在
        图里，"没人监听"就无从谈起。
        """
        stmts: list[tuple[str, dict]] = [
            (CYPHER_TEMPLATES["merge_script_node"],
             {"project": project_id, "path": res,
              "name": Path(res).stem}),
        ]
        for _line, kind, name in gd_symbols(text):
            if kind == "extends":
                stmts.append((CYPHER_TEMPLATES["merge_script_extends"],
                              {"project": project_id, "path": res,
                               "base": name}))
            elif kind == "signal":
                stmts.append((CYPHER_TEMPLATES["merge_script_declares"],
                              {"project": project_id, "path": res,
                               "signal": name}))
            elif kind == "class_name":
                stmts.append((CYPHER_TEMPLATES["merge_script_node"],
                              {"project": project_id, "path": res,
                               "name": name}))
        return stmts

    # ---------------------------------------------------------- 查询

    async def impact_of_signal(self, project_id: str,
                               signal: str) -> list[ImpactEdge]:
        """变更影响分析：signal ← 监听场景节点 → 处理脚本（两跳）。"""
        rows = await query(self.driver, "impact_of_signal",
                           signal=signal, project=project_id, limit=50)
        return [ImpactEdge(signal=signal, node=r["node"], path=r["path"],
                           script=r["script"], method=r.get("method", ""))
                for r in rows]

    async def dead_signals(self, project_id: str) -> list[str]:
        """死信号体检：有声明（DECLARES）无监听（LISTENS）的信号清单。"""
        rows = await query(self.driver, "dead_signals",
                           project=project_id, limit=100)
        return [r["signal"] for r in rows]

    async def signals_of_project(self, project_id: str) -> list[str]:
        """项目全部信号名（fusion.trace 的问题信号词典）。"""
        rows = await query(self.driver, "all_signals_of_project",
                           project=project_id, limit=100)
        return [r["signal"] for r in rows]

    async def graph_exists(self, project_id: str) -> bool:
        """项目图是否已建（降级判定的第一步：查 meta 节点，§7 面试 7）。"""
        rows = await query(self.driver, "project_exists", project=project_id)
        return bool(rows and rows[0]["n"])


__all__ = ["ImpactEdge", "ProjectGraphSync"]
