"""M10 §4 步骤 4：切分层——递归切分 + 结构感知切分。"""
from agent_godot.rag import (Chunk, RecursiveChunker, StructureAwareChunker,
                              hard_split, recursive_split)

from .conftest import PLAYER_GD


# ---------------------------------------------------------------- 递归切分

def test_recursive_short_text_unchanged():
    assert recursive_split("短文本", max_len=100) == ["短文本"]


def test_recursive_splits_on_paragraph_boundary():
    text = "\n\n".join(f"第{i}段" + "内容" * 20 for i in range(5))
    pieces = recursive_split(text, max_len=60)
    assert len(pieces) > 1
    for p in pieces:
        assert len(p) <= 60


def test_recursive_falls_through_separators():
    """高级分隔符不在文本里 → 降级到低级分隔符（句子级）。"""
    text = "第一句。第二句。第三句。" * 10
    pieces = recursive_split(text, max_len=30)
    assert len(pieces) > 1
    assert all(len(p) <= 30 for p in pieces)


def test_hard_split_with_overlap():
    text = "字" * 100
    pieces = hard_split(text, max_len=40, overlap=10)
    assert len(pieces) > 1
    # 相邻块重叠 10 字（边界语义断裂的保险）
    assert pieces[1][:10] == pieces[0][-10:]


def test_recursive_no_separator_hard_split():
    """无任何分隔符的超长串 → hard_split 兜底（"" 分隔符不进 split）。"""
    text = "a" * 250
    pieces = recursive_split(text, max_len=100, overlap=20)
    # step=80：i=0,80,160,240 → 4 片（最后一片仅 10 字）
    assert len(pieces) == 4
    assert all(len(p) <= 100 for p in pieces)


def test_recursive_chunker_metadata_roundtrip(md_doc):
    chunks = RecursiveChunker(max_len=200).split(md_doc)
    assert chunks
    for c in chunks:
        assert isinstance(c, Chunk)
        assert c.source == "docs/physics.md"
        assert c.doc_id == md_doc.doc_id
        assert c.kind == "md"
        assert c.start >= 1
    # chunk_id 唯一（RRF 两路同构主键的前提）
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))


def test_recursive_chunker_breadcrumb(md_doc):
    chunks = RecursiveChunker(max_len=150).split(md_doc)
    # 第二块起的 heading 至少能命中 "角色物理" 面包屑（H1）
    assert any("角色物理" in c.heading for c in chunks)


# ---------------------------------------------------------------- 结构感知（.gd）

def test_structure_aware_gd_splits_by_function(gd_doc, chunker):
    chunks = chunker.split(gd_doc)
    # 文件头 + _physics_process + _check_landing（前导注释归函数块）
    funcs = [c.heading for c in chunks]
    assert any("_physics_process" in h for h in funcs)
    assert any("_check_landing" in h for h in funcs)
    # 每个函数块完整（语义完整铁律）
    phys = next(c for c in chunks if "_physics_process" in c.heading)
    assert "move_and_slide()" in phys.text
    assert "velocity.y" in phys.text
    # 类名回填头部（零成本 contextual chunking）
    assert "# [player.gd · Player]" in phys.text


def test_structure_aware_gd_leading_comment_belongs_to_func(gd_doc, chunker):
    chunks = chunker.split(gd_doc)
    landing = next(c for c in chunks if "_check_landing" in c.heading)
    assert "# 落地检测：4.3 起看返回值" in landing.text
    phys = next(c for c in chunks if "_physics_process" in c.heading)
    assert "# 速度向量积分" in phys.text


def test_structure_aware_gd_file_header_chunk(gd_doc, chunker):
    chunks = chunker.split(gd_doc)
    header = next(c for c in chunks if "file header" in c.heading)
    assert "extends CharacterBody2D" in header.text
    assert "class_name Player" in header.text


def test_structure_aware_gd_no_symbols_falls_back(tmp_path, chunker):
    doc_text = "# 只有注释\n\nvar a := 1\n"        # 无顶层 func/signal/class
    from agent_godot.rag import ParsedDoc
    doc = ParsedDoc.make(source="flat.gd", kind="gdscript", text=doc_text)
    chunks = chunker.split(doc)
    assert chunks and chunks[0].text == doc_text     # 递归兜底整块


# ---------------------------------------------------------------- 结构感知（文档）

def test_structure_aware_md_splits_by_h2(md_doc, chunker):
    chunks = chunker.split(md_doc)
    headings = [c.heading for c in chunks]
    # 按 H2 切节（move_and_slide 一节、Area2D 信号一节）
    assert any("move_and_slide" in h for h in headings)
    assert any("Area2D 信号" in h for h in headings)


def test_structure_aware_md_breadcrumb(md_doc, chunker):
    """面包屑路径：H1 > H2（H3 不单独成节，归父 H2 节——文档按 H2 切）。"""
    chunks = chunker.split(md_doc)
    area = [c for c in chunks if "Area2D 信号" in c.heading]
    assert area, "Area2D 信号节应存在"
    assert "角色物理 > Area2D 信号" in area[0].heading
    # H3 碰撞报告内容归入 Area2D 信号节（不单独成 chunk）
    assert any("max_contacts_reported" in c.text for c in area)


def test_structure_aware_md_seq_unique(md_doc, chunker):
    chunks = chunker.split(md_doc)
    seqs = [c.seq for c in chunks]
    assert sorted(seqs) == list(range(len(chunks)))
