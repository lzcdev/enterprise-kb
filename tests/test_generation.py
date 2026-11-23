"""任务5 单元测试：prompt 拼装、引用解析、LLM 生成与端到端服务。"""
from app.factory import get_embedder, get_vectorstore
from app.generation.generator import (
    MockLLM, build_prompt, get_llm, parse_citations,
)
from app.interfaces import ScoredChunk, VectorRecord
from app.rag_service import RAGService
from app.retrieval.retriever import Retriever


def sample_candidates() -> list[ScoredChunk]:
    return [
        ScoredChunk(id="a", text="七天无理由退款，签收起七日内可申请。",
                    score=0.9, metadata={"filename": "手册.md", "page": 2,
                                        "title_path": "手册 > 退款", "doc_id": "d1"}),
        ScoredChunk(id="b", text="退款例外情况：定制商品不支持。",
                    score=0.7, metadata={"filename": "手册.md", "page": 3,
                                        "title_path": "手册 > 例外", "doc_id": "d1"}),
    ]


def test_build_prompt_contains_rules_context_question():
    prompt = build_prompt("退款怎么申请", sample_candidates())
    assert "只使用参考资料" in prompt          # 防幻觉规则
    assert "[1]" in prompt and "[2]" in prompt  # 编号证据
    assert "问题：退款怎么申请" in prompt        # 用户问题


def test_parse_citations_maps_to_sources():
    answer = "七日内可退[1]，定制商品例外[2]。"
    sources = parse_citations(answer, sample_candidates())
    assert len(sources) == 2
    assert sources[0].filename == "手册.md"
    assert sources[0].page == 2
    assert sources[1].page == 3
    # 越界编号应被忽略
    assert len(parse_citations("引用[9]", sample_candidates())) == 0


def test_mock_llm_extractive_and_get_llm():
    prompt = build_prompt("七天无理由退款", sample_candidates())
    out = MockLLM().generate(prompt)
    assert "[1]" in out or "[2]" in out  # 抽取式回答带引用
    assert get_llm("mock").model_id == "mock-extractive-v1"
    assert get_llm("off") is None


def test_rag_service_end_to_end():
    """端到端：建库 -> 检索 -> 生成 -> 溯源。"""
    store = get_vectorstore()
    emb = get_embedder()
    ns = "test_rag"
    vecs = emb.embed(["七天无理由退款，签收起七日内可申请退款",
                      "商品颜色和尺寸可在详情页查看"])
    store.create_namespace(ns, emb.dimension)
    store.upsert(ns, [
        VectorRecord(id="x1", vector=vecs[0], text="七天无理由退款，签收起七日内可申请退款",
                     metadata={"tenant_id": "t1", "embedding_model": emb.model_id,
                               "filename": "手册.md", "page": 1, "doc_id": "d1"}),
        VectorRecord(id="x2", vector=vecs[1], text="商品颜色和尺寸可在详情页查看",
                     metadata={"tenant_id": "t1", "embedding_model": emb.model_id,
                               "filename": "手册.md", "page": 2, "doc_id": "d1"}),
    ])
    service = RAGService(
        retriever=Retriever(embedder=emb, store=store, namespace=ns),
        reranker=False,  # 显式关闭重排（哨兵语义）
        llm=get_llm("mock"),
    )
    ans = service.answer("七天无理由退款", tenant_id="t1", score_threshold=0.0)
    assert ans.question == "七天无理由退款"
    assert ans.answer  # 有答案
    assert len(ans.sources) >= 1  # 有溯源
    assert ans.elapsed_ms > 0
    store.delete_namespace(ns)


def test_rag_service_no_candidates_short_circuit():
    """空结果分支：阈值设到超过任何分数，应返回未找到且不调 LLM。"""
    store = get_vectorstore()
    emb = get_embedder()
    ns = "test_rag_empty"
    vec = emb.embed(["退款"])[0]
    store.create_namespace(ns, emb.dimension)
    store.upsert(ns, [
        VectorRecord(id="e1", vector=vec, text="退款说明",
                     metadata={"tenant_id": "t1", "embedding_model": emb.model_id}),
    ])
    service = RAGService(
        retriever=Retriever(embedder=emb, store=store, namespace=ns),
        reranker=False,
        llm=get_llm("mock"),
    )
    ans = service.answer("退款", score_threshold=1.01)
    assert "没有相关信息" in ans.answer
    assert ans.sources == []
    store.delete_namespace(ns)
