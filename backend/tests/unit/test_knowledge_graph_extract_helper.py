"""Pure KG extract helper is return-only and assigns client-stable ids."""

from __future__ import annotations

import json
from contextlib import nullcontext
from unittest.mock import MagicMock

import pytest

from utils.llm import knowledge_graph as kg
from utils.llm.capabilities import ModelCapabilityUnavailableError
from utils.llm.knowledge_graph import ExtractedEdge, ExtractedNode, KnowledgeGraphExtraction


def test_extraction_to_client_graph_assigns_stable_ids_and_skips_self_edges():
    extraction = KnowledgeGraphExtraction(
        nodes=[
            ExtractedNode(label='Neo', node_type='person', aliases=['Thomas']),
            ExtractedNode(label='Zion', node_type='place', aliases=[]),
        ],
        edges=[
            ExtractedEdge(source_label='Neo', target_label='Zion', label='lives in'),
            ExtractedEdge(source_label='Neo', target_label='Neo', label='is'),
        ],
    )
    graph = kg.extraction_to_client_graph(extraction, uid='uid-1')
    assert len(graph['nodes']) == 2
    assert len(graph['edges']) == 1
    assert graph['edges'][0]['source_id'] == graph['nodes'][0]['id']
    assert graph['edges'][0]['target_id'] == graph['nodes'][1]['id']
    assert 'id' in graph['edges'][0]


def test_repeated_extractions_reuse_ids_so_local_upserts_merge():
    """Both desktop graphs upsert by id: a fresh id per call would duplicate the entity."""

    def extract() -> KnowledgeGraphExtraction:
        return KnowledgeGraphExtraction(
            nodes=[ExtractedNode(label='Neo', node_type='person', aliases=['Thomas'])],
            edges=[],
        )

    first = kg.extraction_to_client_graph(extract(), uid='uid-1')
    second = kg.extraction_to_client_graph(extract(), uid='uid-1')
    assert first['nodes'][0]['id'] == second['nodes'][0]['id']

    # Label normalization is stable across casing and spacing.
    spaced = KnowledgeGraphExtraction(nodes=[ExtractedNode(label='  neo ', node_type='person', aliases=[])], edges=[])
    assert kg.extraction_to_client_graph(spaced, uid='uid-1')['nodes'][0]['id'] == first['nodes'][0]['id']

    # Ids are per-owner, so they are not comparable across accounts.
    assert kg.extraction_to_client_graph(extract(), uid='uid-2')['nodes'][0]['id'] != first['nodes'][0]['id']


def test_duplicate_labels_merge_into_one_node_instead_of_colliding_ids():
    """Two rows sharing an id would overwrite each other on upsert."""
    extraction = KnowledgeGraphExtraction(
        nodes=[
            ExtractedNode(label='Neo', node_type='person', aliases=['Thomas']),
            ExtractedNode(label='neo', node_type='person', aliases=['The One']),
        ],
        edges=[],
    )

    graph = kg.extraction_to_client_graph(extraction, uid='uid-1')

    assert len(graph['nodes']) == 1
    assert graph['nodes'][0]['aliases'] == ['Thomas', 'The One']


def test_duplicate_edges_are_emitted_once():
    extraction = KnowledgeGraphExtraction(
        nodes=[
            ExtractedNode(label='Neo', node_type='person', aliases=[]),
            ExtractedNode(label='Zion', node_type='place', aliases=[]),
        ],
        edges=[
            ExtractedEdge(source_label='Neo', target_label='Zion', label='lives in'),
            ExtractedEdge(source_label='neo', target_label='zion', label='Lives In'),
        ],
    )

    graph = kg.extraction_to_client_graph(extraction, uid='uid-1')

    assert len(graph['edges']) == 1


def test_extract_kg_from_text_uses_knowledge_graph_feature(monkeypatch):
    seen: dict[str, object] = {}

    def fake_get_llm(feature: str):
        seen['feature'] = feature
        client = MagicMock()
        client.invoke.side_effect = lambda prompt: MagicMock(
            content=json.dumps(
                {
                    'nodes': [{'label': 'Coffee', 'node_type': 'thing', 'aliases': []}],
                    'edges': [],
                }
            )
        )
        return client

    monkeypatch.setattr(kg, 'get_llm', fake_get_llm)
    monkeypatch.setattr(kg, 'track_usage', lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(kg.kg_db, 'get_knowledge_nodes', MagicMock(side_effect=AssertionError('should not load db')))

    extraction = kg.extract_kg_from_text('uid-1', 'User likes coffee', user_name='User')
    assert extraction is not None
    assert seen['feature'] == 'knowledge_graph'
    assert [node.label for node in extraction.nodes] == ['Coffee']


def test_extract_kg_from_text_raises_typed_unavailable_on_provider_failure(monkeypatch):
    client = MagicMock()
    client.invoke.side_effect = TimeoutError('provider timed out')
    monkeypatch.setattr(kg, 'get_llm', lambda _feature: client)
    monkeypatch.setattr(kg, 'track_usage', lambda *args, **kwargs: nullcontext())

    with pytest.raises(ModelCapabilityUnavailableError) as error:
        kg.extract_kg_from_text('uid-1', 'User likes coffee')

    assert error.value.as_dict() == {
        'code': 'model_capability_unavailable',
        'capability': 'memory_kg',
        'reason': 'TimeoutError',
        'retryable': True,
    }


def test_rebuild_does_not_report_provider_failure_as_an_empty_graph(monkeypatch):
    client = MagicMock()
    client.invoke.side_effect = TimeoutError('provider timed out')
    monkeypatch.setattr(kg, 'get_llm', lambda _feature: client)
    monkeypatch.setattr(kg, 'track_usage', lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(kg.kg_db, 'delete_knowledge_graph', MagicMock())
    monkeypatch.setattr(kg.kg_db, 'get_knowledge_nodes', MagicMock(return_value=[]))
    returned_graph = MagicMock(return_value={'nodes': [], 'edges': []})
    monkeypatch.setattr(kg.kg_db, 'get_knowledge_graph', returned_graph)

    with pytest.raises(ModelCapabilityUnavailableError) as error:
        kg.rebuild_knowledge_graph('uid-1', [{'id': 'memory-1', 'content': 'User likes coffee'}])

    assert error.value.route.capability == 'memory_kg'
    returned_graph.assert_not_called()
