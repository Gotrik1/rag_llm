from types import SimpleNamespace
from unittest.mock import Mock

from evidence_selection import SectionEvidenceIndex
from rag_agent import RAGAgent


def node(text, file='short.docx', path=''):
    return SimpleNamespace(text=text, metadata={'file_name':file, 'section_path':path, 'status':'active'})


def test_headingless_fact_survives_inflection_and_generic_cost_hits():
    index = SectionEvidenceIndex([
        node('Ящик водки стоит 3 ежа'),
        node('Расчет стоимости электроэнергии по оптовому рынку.', 'regulation.docx', 'Стоимость электроэнергии'),
    ])
    seeds = index.body_seeds('Какова стоимость ящика водки?')
    assert [item['file'] for item in seeds] == ['short.docx']
    selected = index.select('сколько стоит ящик водки?', [
        {'file':'regulation.docx', 'text':'Стоимость электроэнергии', 'score':100}, *seeds,
    ])
    assert [item['file'] for item in selected] == ['short.docx']
    assert '3 ежа' in selected[0]['text']


def test_body_lookup_is_not_specific_to_one_document_or_question():
    index = SectionEvidenceIndex([node('Насос Берилл потребляет 17 ватт.', 'specification.txt')])
    seeds = index.body_seeds('Сколько потребляет насос Берилл?')
    assert seeds and seeds[0]['file'] == 'specification.txt'
    assert '17 ватт' in seeds[0]['text']


def test_partial_subject_and_prefix_collision_are_not_direct_facts():
    index = SectionEvidenceIndex([
        node('Цена проводки составляет 25 рублей. Ящик управления расположен у стены.'),
        node('Стоимость ящика молока 80 рублей.', 'milk.txt'),
    ])
    assert index.body_seeds('Сколько стоит ящик водки?') == []


def test_final_body_evidence_respects_acl_and_deleted_sources():
    index = SectionEvidenceIndex([node('Насос Берилл потребляет 17 ватт.', 'private.txt')])
    seeds = index.body_seeds('Сколько потребляет насос Берилл?')
    assert index.select('Сколько потребляет насос Берилл?', seeds, allowed_sources={'public.txt'}) == []
    assert index.select('Сколько потребляет насос Берилл?', seeds, excluded_sources={'private.txt'}) == []


def test_original_question_reaches_retriever_and_headingless_body_reaches_final_context():
    agent = object.__new__(RAGAgent)
    agent._all_nodes = [node('Ящик водки стоит 3 ежа', 'price.docx')]
    agent._refresh_docstore_if_changed = Mock()
    retriever = Mock()
    retriever.retrieve.return_value = []
    agent._build_query_engine = Mock(return_value=SimpleNamespace(retriever=retriever))
    context = agent.retrieve_context(
        'Какова стоимость ящика водки?', retrieval_queries=(), subject_question='сколько стоит ящик водки?',
        source_aliases={'price.docx':'price.docx'},
    )
    assert context and context[0]['file'] == 'price.docx'
    assert retriever.retrieve.call_args_list[0].args[0].query_str == 'сколько стоит ящик водки?'
