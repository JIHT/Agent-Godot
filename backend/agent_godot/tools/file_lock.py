"""tools/file_lock.py —— 乐观锁文件读写（M04 §1.5）

共享文档的交接班制度：
- read 返回 (content, hash)——抄录图纸时记下版本号
- write 校验 hash——交回时核对"我读之后没人改过"；不匹配 → CONFLICT
  （hint 提示模型重读重改，绝不自动三方合并——缝合怪比冲突更难调试）

为什么用 content hash 而不是 mtime：
- mtime 同秒两次修改检测不到（精度坑）
- 文件被 touch（内容没变）会误报冲突
- hash 直接对内容负责
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .response import Artifact, ErrorKind, ToolError, ToolResponse
from .sandbox import resolve_in_root


def sha16(text: str) -> str:
    """内容指纹：SHA-256 前 16 位。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class OptimisticFileStore:
    """乐观锁文件读写器。root = 项目根（沙箱白名单边界）。"""

    def __init__(self, root: Path):
        self.root = root
        # 写前快照（内存版；M06 升级为 CheckpointStore 支持回滚）
        self._snapshots: dict[Path, str] = {}

    async def read(self, path: str) -> tuple[str, str]:
        """读文本文件，返回 (content, sha16)。文件不存在返回 ("", "")。

        ★ hash 为空字符串 = 文件不存在（空文件的 hash 是 sha16("") 非空）。
        """
        p = resolve_in_root(self.root, path)
        if not p.exists():
            return "", ""
        content = p.read_text(encoding="utf-8", errors="replace")
        return content, sha16(content)

    async def write(self, path: str, content: str, expect_hash: str) -> ToolResponse:
        """乐观锁写入：当前 hash ≠ expect_hash → CONFLICT；一致 → 快照+写入。"""
        p = resolve_in_root(self.root, path)
        cur = sha16(p.read_text(encoding="utf-8", errors="replace")) if p.exists() else ""
        if cur != expect_hash:
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.CONFLICT, tool="write_file",
                message=f"版本冲突: 期望 {expect_hash[:8] or '(新文件)'}，"
                        f"实际 {cur[:8] or '(新文件)'}（文件已被外部修改）",
                hint="先重新 read_file 获取最新内容与 hash，再基于新内容重新修改"))

        # 写前快照（顺序铁律：先快照后写入，反了没有回头路）
        if p.exists():
            self._snapshots[p] = p.read_text(encoding="utf-8", errors="replace")

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResponse(
            ok=True, summary=f"已写入 {path}（{len(content)} 字符，"
                             f"新 hash: {sha16(content)}）",
            artifacts=[Artifact(type="file", ref=str(path))])

    def snapshot_of(self, path: str) -> str | None:
        """取某文件的写前快照（M06 回滚用；当前内存版）。"""
        p = resolve_in_root(self.root, path)
        return self._snapshots.get(p)
