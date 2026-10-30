import pytest

from app.config import Config
from app.retrieval.policy import PolicyRetriever, load_chunks, tokenize


@pytest.fixture(scope="module")
def retriever():
    return PolicyRetriever(load_chunks(Config.POLICY_FILE), 3)


def test_handbook_is_split_into_clause_chunks():
    chunks = load_chunks(Config.POLICY_FILE)
    assert len(chunks) == 15 and chunks[0].chunk_id == "HB-1.1" and all(c.text for c in chunks)


@pytest.mark.parametrize("query,expected_top", [
    ("The payment portal failed. Can I pay on the next working day?", "HB-2.2"),
    ("How can I request a course change?", "HB-4.2"),
    ("Am I eligible for a scholarship?", "HB-3.2"),
    ("How do I escalate a support request?", "HB-6.1"),
])
def test_relevant_chunk_is_ranked_first(retriever, query, expected_top):
    res = retriever.search(query)
    assert res["found"] and res["chunks"][0]["chunk_id"] == expected_top


def test_only_a_few_chunks_are_returned_not_the_whole_handbook(retriever):
    assert len(retriever.search("payment portal failed")["chunks"]) <= 3


@pytest.mark.parametrize("query", ["What is the hostel deposit refund policy?", "Tell me a joke", "capital of France"])
def test_no_reliable_evidence_is_reported_honestly(retriever, query):
    res = retriever.search(query)
    assert res["found"] is False and res["chunks"] == [] and "does not establish" in res["note"]


def test_results_carry_source_references(retriever):
    c = retriever.search("late fee deadline")["chunks"][0]
    assert {"chunk_id", "section", "score", "text"} <= c.keys()


def test_tokenizer_drops_stopwords_and_stems():
    assert tokenize("The payments failed") == ["pay", "fail"]
