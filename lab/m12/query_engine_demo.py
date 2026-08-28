"""lab/m12/query_engine_demo.py —— MI-3 收官验收五连发（M12 §5）

分诊台全链演示（离线可跑）：
  "Area2D 怎么检测碰撞？"        knowledge → RAG 带引用
  "那它的信号呢？"              ambiguous → 改写后命中（多轮）
  "删掉 hitbox 信号影响哪些场景？" 多跳 → GRAPH 推理链
  "Godot 4.4 是什么时候发布的？"  search → 联网（mock 页面 + 信封）
  "帮我加双跳"                  code_edit → craft 模式（M13 接棒）

通道装配（全部教学版内存实现，零外部服务）：
- RAG：FakeEmbedding + InMemoryVectorIndex + BM25（M10 全家桶）
- GRAPH：InMemoryGraphDriver + ProjectGraphSync（lab/m06 样例项目）
- WEB：MockSearchEngine + httpx.MockTransport（本地替身页面）
- 意图/改写：剧本式 FakeLLM（--real 换真实模型，见 main 注释）

--real：走 config/models.yaml 的 routing（小模型场景自由配置的实况）——
  intent/rewrite 未配置时回落 ask 主 LLM；配了同名键就走专属小模型。

接线演示（§4 步骤 5）：QueryResult → rag_messages → ContextBuilder 的
  rag 分区（M07 分区预算治理下的第五分区）。

运行：cd backend && uv run python ../lab/m12/query_engine_demo.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402

from agent_godot.context import BudgetConfig, ContextBuilder, TokenCounter  # noqa: E402
from agent_godot.core import LLMResponse, Message  # noqa: E402
from agent_godot.graphrag import (InMemoryGraphDriver,  # noqa: E402
                                  ProjectGraphSync, GraphVectorFusion)
from agent_godot.query_engine import (IntentClassifier, QueryEngine,  # noqa: E402
                                      QueryRewriter, QueryRouter, RoutingContext,
                                      SearchHit, WebSearchProvider,
                                      build_query_engine, rag_messages)
from agent_godot.rag import (BM25Index, Chunk, FakeEmbeddingService,  # noqa: E402
                             HybridRetriever, InMemoryVectorIndex)

# ---- lab/m06 样例项目（内嵌，测试/lab 不互相依赖） ----
MAIN_TSCN = """[gd_scene load_steps=4 format=3]

[ext_resource type="Script" path="res://main.gd" id="1_main"]

[node name="Main" type="Node2D"]
script = ExtResource("1_main")

[node name="Player" parent="." instance=ExtResource("2_player")]

[connection signal="health_changed" from="Player" to="." method="_on_player_health_changed"]
"""
PLAYER_GD = """extends CharacterBody2D

signal health_changed(new_health: int)
signal hitbox(area)

func take_damage(amount: int) -> void:
	health_changed.emit(amount)
"""
DOCS = [
    ("docs/area2d.md", "Area2D 用于检测区域重叠。body_entered 信号在 "
     "monitoring 与 monitorable 同时为 true 时触发，常用于拾取物与伤害判定。"),
    ("docs/physics.md", "CharacterBody2D 的 move_and_slide 返回布尔，"
     "表示本帧是否发生碰撞，可用于落地检测。"),
]
WEB_PAGE = """<html><body><article><h1>Godot 4.4 released</h1>
<p>Godot 4.4 于 2025 年 3 月正式发布。本版本带来多项重要特性：
physics interpolation（物理插值）默认可用，让运动在物理帧率与渲染
帧率不一致时保持平滑；多线程场景加载显著缩短大场景的切换卡顿；
2D 与 3D 渲染均有性能优化，包括多线程批处理与视锥剔除改进。</p>
<p>此外编辑器侧新增了代码折叠、项目管理器改进与更好的输入向导。
官方建议所有 4.x 用户升级，API 保持向后兼容。</p>
</article></body></html>"""


class ScriptedLLM:
    """剧本式假模型：意图/改写各配一张剧本表。"""

    def __init__(self, intents: dict[str, str], rewrites: dict[str, str]):
        self.intents = intents
        self.rewrites = rewrites
        self.calls = 0

    async def complete(self, req):
        self.calls += 1
        text = req.messages[0].content
        if "标签：" in text:                       # 意图分类提示
            q = text.rsplit("输入：", 1)[-1].rsplit("\n标签", 1)[0].strip()
            out = self.intents.get(q, "knowledge")
        else:                                      # 改写提示
            q = text.rsplit("最新输入：", 1)[-1].split("\n", 1)[0].strip()
            out = self.rewrites.get(q, q)
        return LLMResponse(content=out, tool_calls=[], usage=None,
                           finish_reason="stop")


class MockEngine:
    async def search(self, query, n):
        return [SearchHit("Godot 4.4 released - 官方博客",
                          "https://godotengine.org/release/4.4",
                          "Godot 4.4 发布说明")] * 1


QUERIES = [
    ("① 知识问答", "Area2D 怎么检测碰撞？", []),
    ("② 追问改写", "那它的信号呢？", [
        Message(role="user", content="Area2D 怎么检测碰撞？"),
        Message(role="assistant", content="用 body_entered 信号。")]),
    ("③ 多跳图谱", "删掉 health_changed 信号影响哪些场景？", []),
    ("④ 联网时效", "Godot 4.4 是什么时候发布的？", []),
    ("⑤ 改代码分流", "帮我加双跳", []),
]


async def build_engine(real: bool) -> QueryEngine:
    tmp = Path(__file__).parent / "sample"
    tmp.mkdir(exist_ok=True)
    (tmp / "main.tscn").write_text(MAIN_TSCN, encoding="utf-8")
    (tmp / "player.gd").write_text(PLAYER_GD, encoding="utf-8")

    # GRAPH 通道（项目图谱）
    driver = InMemoryGraphDriver()
    graph_sync = ProjectGraphSync(driver)
    await graph_sync.full_sync("m06", tmp)
    fusion = GraphVectorFusion(graph_sync, None)

    # RAG 通道（M10 全家桶内存版）
    embedder = FakeEmbeddingService(dim=64)
    vec, bm25 = InMemoryVectorIndex(), BM25Index()
    chunks = [Chunk(text=t, source=s, heading="", start=1,
                    doc_id=f"doc{i}", kind="md", seq=0)
              for i, (s, t) in enumerate(DOCS)]
    embs = await embedder.embed_documents([c.text for c in chunks])
    vec.upsert(chunks, embs)
    bm25.build(chunks)
    hybrid = HybridRetriever(vec, bm25, embedder, top_per_route=10)

    # WEB 通道（mock 页面 + 信封）
    web = WebSearchProvider(
        MockEngine(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, text=WEB_PAGE))))

    intents = {
        "Area2D 怎么检测碰撞？": "knowledge",
        "那它的信号呢？": "ambiguous",
        "Area2D 的检测信号 body_entered": "knowledge",   # 二次分类的输入
        "删掉 health_changed 信号影响哪些场景？": "knowledge",
        "Godot 4.4 是什么时候发布的？": "search",
        "帮我加双跳": "code_edit",
    }
    llm = ScriptedLLM(intents, {"那它的信号呢？": "Area2D 的检测信号 body_entered"})

    if real:
        # 真实装配：models.yaml 的 routing 里配了 intent/rewrite 键就走
        # 专属小模型，没配回落 ask 主 LLM（--real 看配置生效）
        from agent_godot.core import load_registry
        return build_query_engine(
            load_registry(), rag=hybrid, graph=fusion, web_engine=MockEngine())
    return QueryEngine(
        IntentClassifier(llm, model="scripted"),
        QueryRewriter(llm, model="scripted"),
        QueryRouter(), hybrid, fusion, web)


async def main() -> None:
    real = "--real" in sys.argv
    engine = await build_engine(real)
    ctx = RoutingContext(kb_enabled=True, graph_ready=True, web_enabled=True,
                         project_id="m06")

    print("=" * 72)
    print("M12 Query Engine 验收五连发（MI-3 收官：RAG + Graph + Query 三件套）")
    print("=" * 72)

    last_result = None
    knowledge_result = None
    for title, q, history in QUERIES:
        result = await engine.process(q, history, ctx)
        last_result = result
        if title.startswith("①"):
            knowledge_result = result        # 接线演示用带注入块的结果
        print(f"\n{'─' * 72}\n{title}  输入：{q}")
        print(f"  意图: {result.intent.value}   耗时: {result.elapsed_ms}ms")
        if result.rewritten != q:
            print(f"  改写: {result.rewritten}")
        print(f"  路由: {[c.value for c in result.plan.channels]}"
              f"{' (craft)' if result.plan.mode == 'craft' else ''}")
        print(f"  理由: {result.plan.reason}")
        for s in result.trace["channels"]:
            print(f"    通道 {s['channel']:6s} ok={s['ok']} "
                  f"count={s['count']} {s['ms']}ms")
        if result.context_block:
            print(f"  注入块 ({result.trace['inject_tokens']} tokens 估):")
            for line in result.context_block.splitlines()[:14]:
                print(f"    {line}")

    # ---- §4 步骤 5 接线演示：rag 分区进 ContextBuilder ----
    print(f"\n{'─' * 72}\n⑥ 接线：QueryResult → ContextBuilder rag 分区")

    async def rag_provider(session):
        return rag_messages(knowledge_result)

    builder = ContextBuilder(
        counter=TokenCounter(), config=BudgetConfig(),
        rag_provider=rag_provider)
    msgs = await builder.build(_FakeSession([
        Message(role="system", content="你是 Godot 游戏开发助手"),
        Message(role="user", content="Area2D 怎么检测碰撞？")]))
    layout = builder.last_layout()
    print(f"  分区 layout: {layout}")
    injected = any("<retrieved_context" in (m.content or "") for m in msgs)
    print(f"  rag 分区注入成功: {injected}")
    print("\n[验收] 五连发完成——trace 五段决策（意图/改写/路由/通道/注入）全可见")


class _FakeSession:
    def __init__(self, messages):
        self.session_id = "demo"
        self.messages = messages


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
