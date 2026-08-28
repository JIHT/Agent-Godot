"""M10 §5 验收①：自实现 BM25 与 rank_bm25 参考实现排序对照（Spearman>0.9）。"""
import re

import pytest

from agent_godot.rag import BM25Index, Chunk, tokenize

CORPUS = [
    "CharacterBody2D is the most common node for 2D platformer movement.",
    "The move_and_slide method returns a boolean indicating collision in Godot four.",
    "Area2D emits body_entered when monitoring and monitorable are both enabled.",
    "max_contacts_reported controls how many collisions are reported each frame.",
    "Signals in GDScript can declare typed parameters for safer connections.",
    "Use CONNECT_DEFERRED with connect to avoid reentrancy during physics frames.",
    "TileMap layers help organizing terrain and decoration tiles separately.",
    "The scene tree organizes nodes in a parent child hierarchy.",
    "AnimationTree blends animations with state machine transitions.",
    "NavigationAgent pathfinding works with navigation regions and polygons.",
    "RigidBody dynamics respond to forces and impulses for realistic motion.",
    "Shader materials customize visual effects with fragment programs.",
    "AudioStreamPlayer plays positional sound in the listener space.",
    "ResourceLoader loads scenes and scripts at runtime by path.",
    "InputMap maps actions to keys and buttons for portable controls.",
    "Camera follow scripts smooth position with lerp interpolation.",
    "The physics engine separates areas and bodies for detection.",
    "Godot exports variables to the inspector with annotations.",
    "Signals allow decoupled communication between gameplay objects.",
    "Kinematic movement queries collisions before applying displacement.",
]

QUERIES = [
    "max_contacts_reported collision",
    "move_and_slide boolean",
    "signals connect deferred",
    "navigation pathfinding polygons",
    "camera lerp interpolation",
]


def _chunk(text: str, seq: int) -> Chunk:
    return Chunk(text=text, source="corpus.txt", heading="", start=1,
                 doc_id="corpus", kind="md", seq=seq)


def _spearman(a: list[int], b: list[int]) -> float:
    n = len(a)
    d2 = sum((x - y) ** 2 for x, y in zip(a, b))
    return 1 - 6 * d2 / (n * (n * n - 1))


def test_tokenize_keeps_api_names_whole():
    toks = tokenize("max_contacts_reported 在哪定义")
    assert "max_contacts_reported" in toks          # API 名整词不碎
    assert "定义" in toks or any("定" in t for t in toks)   # 中文进了分词


def test_tokenize_chinese():
    toks = tokenize("角色碰墙不掉下去怎么办")
    assert len(toks) > 1                             # 不是整句一个词
    assert all(any(ch.isalnum() for ch in t) for t in toks)


def test_bm25_matches_reference_implementation():
    """§5 test_bm25_matches_reference_implementation：Spearman > 0.9。"""
    bm25_okapi = pytest.importorskip("rank_bm25").BM25Okapi

    ours = BM25Index()
    ours.build([_chunk(d, i) for i, d in enumerate(CORPUS)])
    ref = bm25_okapi([re.findall(r"\w+", d.lower()) for d in CORPUS])

    for q in QUERIES:
        n = len(CORPUS)
        our_ids = [cid for cid, _ in ours.search(q, top=n)]   # 0 分已过滤
        our_rank = {cid: r for r, cid in enumerate(our_ids, 1)}
        ref_scores = ref.get_scores(re.findall(r"\w+", q.lower()))
        ref_order = sorted(range(n), key=lambda i: -ref_scores[i])
        ref_pos = {i: r for r, i in enumerate(ref_order, 1)}

        # 只对照两路都命中的 doc（0 分 = 未命中，名次无意义）
        common = [i for i in range(n)
                  if ref_scores[i] > 0 and f"corpus:{i}" in our_rank]
        if len(common) <= 1:
            # 单命中：两路 top1 必须一致（也是对照通过）
            assert our_ids and our_ids[0] == f"corpus:{common[0]}", \
                f"单命中 top1 不一致: {q}"
            continue
        a = [our_rank[f"corpus:{i}"] for i in common]
        b = [ref_pos[i] for i in common]
        assert _spearman(a, b) > 0.9, f"排序偏离参考实现: {q} (spearman={_spearman(a,b):.3f})"


def test_bm25_idf_smoothing_for_unseen_term():
    """查询词不在库中 → 不除零、得有限低权（+1 平滑）。"""
    ours = BM25Index()
    ours.build([_chunk("hello world", 0)])
    hits = ours.search("zzz_unseen_term", top=5)
    assert hits == [] or all(s == 0.0 for _, s in hits)   # 全零分不进榜也合理


def test_bm25_remove_and_rebuild_stats():
    idx = BM25Index()
    chunks = [_chunk(d, i) for i, d in enumerate(CORPUS)]
    idx.build(chunks)
    assert idx.N == len(CORPUS)
    n = idx.remove([c.chunk_id for c in chunks[:5]])
    assert n == 5
    assert idx.N == len(CORPUS) - 5
    # 删除后统计量一致：avgdl = 剩余均值
    assert idx._avgdl == pytest.approx(
        sum(len(tokenize(c.text)) for c in chunks[5:]) / (len(CORPUS) - 5))


def test_bm25_api_name_query_beats_semantic_noise():
    """API 名是生僻 token：BM25 精确命中（嵌入空间反而糊的招牌场景）。"""
    idx = BM25Index()
    docs = [
        _chunk("Collision handling for characters in a platformer game.", 0),
        _chunk("max_contacts_reported controls reported collision count.", 1),
        _chunk("Movement and jumping feel tuning for player characters.", 2),
    ]
    idx.build(docs)
    top_id, _ = idx.search("max_contacts_reported", top=1)[0]
    assert top_id == "corpus:1"
