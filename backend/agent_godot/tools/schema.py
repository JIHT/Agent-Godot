"""tools/schema.py —— JSON Schema 清洗（M04 §3 难点 / M05 桥接复用）

pydantic 生成的 schema 含 title、$defs、$ref 等 FC API 不接受/不一致的字段
——90% 的手写教程漏掉这步，跑到真 API 才炸。

两个入口：
- to_fc_schema(pydantic 模型)：本地工具用（M04）
- clean_schema(dict)：已有 JSON Schema dict 用（M05 桥接 MCP inputSchema）
"""
from __future__ import annotations

from pydantic import BaseModel


def clean_schema(schema: dict) -> dict:
    """清洗已有 JSON Schema dict：剥 title、内联展开 $defs/$ref（递归解引用）。"""
    schema = dict(schema)
    defs = schema.pop("$defs", schema.pop("definitions", {}))

    def deref(node):
        """递归：$ref 展开 + title 剥除。"""
        if isinstance(node, dict):
            if "$ref" in node:
                return deref(defs[node["$ref"].split("/")[-1]])
            return {k: deref(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [deref(x) for x in node]
        return node

    return deref(schema)


def to_fc_schema(params: type[BaseModel]) -> dict:
    """把 pydantic 参数模型清洗成 Function Calling 兼容的 JSON Schema。"""
    return clean_schema(params.model_json_schema())
