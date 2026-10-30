"""Bounded actual query ranks diversify evidence, never authorize or prove it."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import pytest

from evidence_selection import SectionEvidenceIndex, new_query_rank_context
from query_rank import IDENTITY_FIELDS, native_identity
from rag_agent import RAGAgent


QUESTION = 'Срок действия пароля оператора и срок хранения резервной копии.'
QUERIES = ('пароль оператора', 'резервная копия')
FACTS = ('Пароль оператора действует девяносто дней.', 'Резервная копия хранится четырнадцать суток.')


def row(text, path='Раздел', file='policy.md', **fields):
    return {'file': file, 'text': text, 'section_path': path, 'status': 'active', **fields}


def node(text, path='Раздел', file='policy.md', **fields):
    return SimpleNamespace(text=text, metadata={'file_name': file, 'section_path': path, 'status': 'active', **fields})


def agent_for(nodes):
    agent = object.__new__(RAGAgent)
    agent._all_nodes = nodes
    agent._section_evidence_index = SectionEvidenceIndex(nodes)
    agent._section_evidence_nodes = nodes
    agent._refresh_docstore_if_changed = Mock()
    agent._build_query_engine = Mock(return_value=SimpleNamespace(retriever=Mock(retrieve=Mock(return_value=[]))))
    return agent


def corpus():
    # Independent non-identical prose, not ordinal stripping/deduplication.
    endings = ('Сводный документ отсылает к отдельным процедурам.',
               'Общие положения предлагают проверить специализированные разделы.',
               'Описание не устанавливает конкретные значения.',
               'Подробности публикуются в тематических инструкциях.')
    nodes = [node(QUESTION + ' ' + ending, path=f'Обзор {i}') for i, ending in enumerate(endings)]
    nodes += [node(FACTS[0], path='Правило А'), node(FACTS[1], path='Правило Б')]
    nodes += [node('Нейтральные служебные записи.', path=f'Шум {i}') for i in range(90)]
    return nodes


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('Query-rank tests must not call models/network')
    monkeypatch.setattr('requests.sessions.Session.request', unexpected)
    monkeypatch.setenv('RAG_REFERENCE_EXPANSION', 'off')


@pytest.mark.parametrize('field', IDENTITY_FIELDS)
def test_exact_identity_never_crosses_file_location_or_any_revision_field(field):
    original = row('x' * 700 + ' условие 12.', file_path='/visible/policy.md',
                   document_id='doc1', source_id='src1', revision_id='rev1', version_id='v1',
                   document_version='1', content_hash='h1', sha256='s1')
    context = new_query_rank_context(QUESTION)
    context.record(QUERIES[0], [original])
    changed = {**original, field: 'different'}
    assert native_identity(changed) != native_identity(original)
    assert context.origins(original) and not context.origins(changed)


def test_fulltext_and_field_presence_are_identity_not_prefix_or_truthiness():
    original = row('x' * 700 + ' размер 12.')
    changed = {**original, 'text': 'x' * 700 + ' размер 13.'}
    assert native_identity(changed) != native_identity(original)
    assert native_identity({**original, 'revision_id': None}) != native_identity(original)
    assert native_identity({**original, 'revision_id': ''}) != native_identity(original)


def test_rephrased_duplicates_share_weight_and_rewrites_sum_to_at_most_one():
    context = new_query_rank_context(QUESTION)
    item = row(FACTS[0])
    for query in (QUERIES[0], 'ПАРОЛЬ ОПЕРАТОРА', 'оператора пароль', 'пароли операторов'):
        context.record(query, [item])
    context.record(QUERIES[1], [row(FACTS[1])])
    context.record(QUESTION, [item])
    weights = context.allocator().weights
    assert len(weights) == 3
    assert weights['raw'] == 1
    assert sum(value for key, value in weights.items() if key != 'raw') <= 1


def test_a_query_with_a_second_subject_never_collapses_into_its_subset():
    context = new_query_rank_context(QUESTION)
    context.record(QUERIES[0], [row(FACTS[0])])
    context.record('пароль оператора резервная копия', [row(FACTS[1])])
    assert len(context.allocator().weights) == 3


@pytest.mark.parametrize('first,second', [
    ('пароль оператора 30 дней', 'пароль оператора 60 дней'),
    ('пароль оператора не хранить', 'пароль оператора хранить'),
    ('пароль оператора > лимита', 'пароль оператора < лимита'),
    ('пароль оператора кВт', 'пароль оператора МВт'),
    ('пароль оператора MW', 'пароль оператора mW'),
    ('пароль оператора %', 'пароль оператора ‰'),
    ('пароль оператора ≤ лимита', 'пароль оператора ≥ лимита'),
    ('пароль оператора вт', 'пароль оператора мвт'),
    ('пароль оператора ватт', 'пароль оператора киловатт'),
])
def test_protected_discriminators_are_never_collapsed(first, second):
    context = new_query_rank_context(QUESTION)
    context.record(first, [row(FACTS[0])])
    context.record(second, [row(FACTS[1])])
    assert len(context.allocator().weights) == 3


def test_facility_gain_rewards_local_ranks_and_updates_only_after_a_real_packet():
    overview, first, second = row(QUESTION, 'Обзор'), row(FACTS[0], 'А'), row(FACTS[1], 'Б')
    context = new_query_rank_context(QUESTION)
    context.record(QUESTION, [overview])
    context.record(QUERIES[0], [first, overview])
    context.record(QUERIES[1], [second, overview])
    allocator = context.allocator()
    assert allocator.gain(context.origins(overview)) > allocator.gain(context.origins(first))
    allocator.accept(overview, context.origins(overview))
    assert allocator.gain(context.origins(first)) == .25
    assert allocator.gain(context.origins(second)) == .25
    before = dict(allocator.covered)
    allocator.accept({**first, 'text': 'Не исходный packet.'}, context.origins(first))
    assert allocator.covered == before  # Unfit/omitted native content earns nothing.
    allocator.accept(first, context.origins(first))
    assert allocator.gain(context.origins(first)) == 0
    assert allocator.gain(context.origins(second)) == .25


def test_private_only_and_empty_queries_do_not_enable_or_dilute_an_allocator():
    allowed, private = row(FACTS[0]), row(FACTS[1], file='private.md')
    context = new_query_rank_context(QUESTION)
    context.record(QUESTION, [allowed])
    context.record(QUERIES[0], [])
    context.record(QUERIES[1], [private])
    assert not context.allocator(context.origins(allowed)).active
    assert context.allocator(context.origins(allowed)).gain(context.origins(allowed)) == 0


def test_one_actual_query_without_a_raw_hit_is_still_native_fallback():
    context = new_query_rank_context(QUESTION)
    context.record(QUERIES[0], [row(FACTS[0])])
    assert not context.allocator().active


def test_mutable_or_nonfinite_identity_metadata_cannot_supply_rank_provenance():
    context = new_query_rank_context(QUESTION)
    for value in ({'id': 'r1'}, ['r1'], float('nan')):
        item = row(FACTS[0], revision_id=value)
        context.record(QUERIES[0], [item])
        assert not context.origins(item)


@pytest.mark.parametrize('changed_version', [1.0, True, '1', None])
def test_retained_packet_cannot_coerce_revision_metadata_types(changed_version):
    original = row(FACTS[0], document_version=1)
    context = new_query_rank_context(QUESTION)
    context.record(QUESTION, [row(QUESTION)])
    context.record(QUERIES[0], [original])
    origins = context.origins(original)
    changed = {**original, 'document_version': changed_version}
    assert not context.origins(changed)
    assert not context.retained(changed, origins)
    allocator = context.allocator()
    allocator.accept(changed, origins)
    assert allocator.covered == {}
    allocator.accept({**original, 'text': 'Преамбула. ' + original['text']}, origins)
    assert allocator.covered


def test_native_wrapper_projection_keeps_full_condition_and_raw_identity():
    body = FACTS[0] + ' Только для штатных операторов при активном договоре.'
    original = row('[SECTION: Правило А] ' + body, 'Правило А')
    context = new_query_rank_context(QUESTION)
    context.record(QUESTION, [row(QUESTION)])
    context.record(QUERIES[0], [original])
    origins = context.origins(original)
    assert not context.origins({**original, 'text': body})
    assert not context.retained({**original, 'text': FACTS[0]}, origins)
    assert context.retained({**original, 'text': body}, origins) == origins
    assert not context.retained({**original, 'text': body.replace('активном', 'любом')}, origins)


@pytest.mark.parametrize('text', ['', ' ', '[SECTION: Правило А]', '[SECTION: Правило А] Правило А'])
def test_empty_or_title_only_records_never_earn_rank_coverage(text):
    context = new_query_rank_context(QUESTION)
    original = row(text, 'Правило А')
    context.record(QUERIES[0], [original])
    assert not context.origins(original)


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_native_section_wrappers_do_not_erase_marginal_coverage_after_packing(mode):
    nodes = corpus()
    for item in nodes:
        item.text = '[SECTION: ' + item.metadata['section_path'] + '] ' + item.text
    agent = agent_for(nodes)
    selected = agent.retrieve_context(QUESTION, top_k=3, retrieval_queries=QUERIES,
        selection_mode=mode, constraint_question=QUESTION, source_aliases={'policy.md': 'policy.md'})
    assert all(any(fact in item['text'] for item in selected) for fact in FACTS)


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_two_pass_native_packing_keeps_the_same_two_aspects_and_rank_ancestry(mode):
    from corrective_rag import assign_evidence
    overview = (QUESTION + ' учитываются в политике; подробные значения установлены '
                'в другом разделе. Запись ')
    nodes = [node('[SECTION: Обзор ' + str(i) + '] ' + overview + str(i) + '.', path=f'Обзор {i}')
             for i in range(20)]
    nodes += [node('[SECTION: Порядок А] ' + FACTS[0], path='Порядок А'),
              node('[SECTION: Порядок Б] ' + FACTS[1], path='Порядок Б')]
    nodes += [node('Служебная запись содержит прочие сведения.', path=f'Шум {i}') for i in range(300)]
    agent = agent_for(nodes)
    context = new_query_rank_context(QUESTION)
    inner = agent.retrieve_context(QUESTION, top_k=3, retrieval_queries=QUERIES,
        selection_mode=mode, constraint_question=QUESTION, query_rank_context=context,
        source_aliases={'policy.md': 'policy.md'})
    assert all(any(fact in item['text'] for item in inner) for fact in FACTS)
    admitted = assign_evidence(inner, max_chars=24000)
    select = agent._section_evidence_index.select_v2 if mode == 'coverage' else agent._section_evidence_index.select
    outer = select(QUESTION, admitted, top_k=3, max_chars=12000,
        constraint_question=QUESTION, query_rank_context=context, allowed_sources={'policy.md'})
    assert all(any(fact in item['text'] for item in outer) for fact in FACTS)
    assert all(context.origins(item) for item in inner)
    assert all(context.origins(item) for item in admitted)


def receipt_fixture(mode, *, raw_path='missing', ambiguous=False):
    path = '/native/policy.md'
    bodies = [FACTS[0] + ' Только при активном договоре.',
              'Проверку выполняет назначенный оператор по утвержденному порядку.']
    nodes = [node('[SECTION: ' + QUESTION + '] ' + body, path=QUESTION,
                  file_path=path, document_version=1) for body in bodies]
    if ambiguous:
        nodes += [node(item.text, path=QUESTION, file_path='/other/policy.md', document_version=1)
                  for item in nodes[:2]]
    index = SectionEvidenceIndex(nodes)
    raw = [dict(item) for item in index.body_items[:2]]
    for item in raw:
        if raw_path == 'missing':
            item.pop('file_path')
        else:
            item['file_path'] = raw_path
    context = new_query_rank_context(QUESTION)
    context.record(QUESTION, raw)
    context.record(QUERIES[0], raw[:1])
    select = index.select_v2 if mode == 'coverage' else index.select
    packed = select(QUESTION, raw, top_k=1, max_chars=12000,
        constraint_question=QUESTION, query_rank_context=context, allowed_sources={'policy.md'})
    assert packed
    return index, context, raw, packed[0]


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_actual_native_index_attests_missing_filepath_without_weakening_strict_retained(mode):
    _, context, raw, packed = receipt_fixture(mode)
    origins = set().union(*(context.origins(item) for item in raw))
    assert packed['file_path'] == '/native/policy.md'
    assert context.retained(packed, origins) == frozenset()
    assert context.origins(packed) == origins
    assert context.receipt_origins(packed, origins) == origins
    assert all(item['text'].split('] ', 1)[-1] in packed['text'] for item in raw)


@pytest.mark.parametrize('raw_path', [None, '', 1, '/wrong/policy.md'])
@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_native_witness_does_not_enrich_existing_or_typed_filepath(raw_path, mode):
    _, context, _, packed = receipt_fixture(mode, raw_path=raw_path)
    assert not context.origins(packed)
    assert not context._packed_receipts


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_ambiguous_native_paths_cannot_mint_a_missing_path_receipt(mode):
    _, context, _, packed = receipt_fixture(mode, ambiguous=True)
    assert not context.origins(packed)
    assert not context._packed_receipts


@pytest.mark.parametrize('field', IDENTITY_FIELDS)
def test_full_packed_receipt_never_survives_any_bound_metadata_mutation(field):
    _, context, _, packed = receipt_fixture('coverage')
    assert context.origins(packed)
    assert not context.origins({**packed, field: 'changed'})
    assert not context.origins({**packed, 'text': packed['text'] + ' Иное условие.'})


@pytest.mark.parametrize('version', [1.0, True, '1', None])
def test_receipt_does_not_coerce_native_revision_types(version):
    _, context, _, packed = receipt_fixture('coverage')
    assert not context.origins({**packed, 'document_version': version})


def test_clipped_condition_unfit_packet_and_forged_metadata_cannot_mint_receipts():
    index, context, raw, packed = receipt_fixture('coverage')
    other = new_query_rank_context(QUESTION)
    other.record(QUESTION, raw)
    origins = set().union(*(other.origins(item) for item in raw))
    clipped = {**packed, 'text': FACTS[0]}
    other.register_packed(clipped, origins, index._native_pack_witness)
    assert not other.origins(clipped)
    assert not other._packed_receipts
    # The source witness is authority only for actual supplied ancestry.
    other.register_packed(packed, (), index._native_pack_witness)
    assert not other.origins(packed)
    forged = {**packed, '_packed_receipts': context._packed_receipts,
              '_query_origins': origins, '_native_pack_witness': index._native_pack_witness}
    assert not other.origins(forged)
    other.register_packed(packed, origins, {'_bindings': 'uploaded metadata'})
    assert not other.origins(packed)
    select = index.select_v2
    select(QUESTION, raw, top_k=1, max_chars=1, query_rank_context=other,
           constraint_question=QUESTION, allowed_sources={'policy.md'})
    assert not other._packed_receipts


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_native_index_witness_alone_cannot_invent_current_pool_ancestry(mode):
    index, _, raw, packed = receipt_fixture(mode)
    other = new_query_rank_context(QUESTION)
    other.record(QUESTION, raw)
    other.record(QUERIES[0], raw[:1])
    # The current input is assembled evidence from another request. No receipt
    # is imported with it; its source witness alone does not confer raw ranks.
    assert not other.origins(packed)
    select = index.select_v2 if mode == 'coverage' else index.select
    result = select(QUESTION, [packed], top_k=1, max_chars=12000,
        query_rank_context=other, constraint_question=QUESTION, allowed_sources={'policy.md'})
    # V2 can independently offer actual current raw body hits. Do not confuse
    # that existing channel with a receipt imported from the foreign request.
    if mode is None:
        assert not other._packed_receipts
    assert result


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_index_only_root_cannot_borrow_previously_recorded_but_unoffered_ranks(mode):
    index = SectionEvidenceIndex([node(FACTS[0], path=QUESTION, file='guide.md')])
    context = new_query_rank_context(QUESTION)
    context.record(QUESTION, list(index.body_items))
    context.record(QUERIES[0], list(index.body_items))
    select = index.select_v2 if mode == 'coverage' else index.select
    selected = select(QUESTION, [], top_k=1, max_chars=12000,
        allowed_sources={'guide.md'}, query_rank_context=context)
    assert selected  # Existing native heading retrieval remains available.
    assert not context._packed_receipts


def test_accepted_packet_receipts_are_idempotent_and_bounded(monkeypatch):
    import query_rank
    monkeypatch.setattr(query_rank, 'MAX_PACK_RECEIPTS', 2)
    nodes = [node('[SECTION: ' + QUESTION + '] ' + FACTS[0] + ' Условие ' + str(i) + '.',
                  path=QUESTION, file=f'policy-{i}.md') for i in range(3)]
    index = SectionEvidenceIndex(nodes)
    raw = list(index.body_items)
    context = new_query_rank_context(QUESTION)
    context.record(QUESTION, raw)
    context.record(QUERIES[0], raw)
    for _ in range(3):
        selected = index.select_v2(QUESTION, raw, top_k=3, max_chars=12000,
            constraint_question=QUESTION, query_rank_context=context)
        assert len(selected) == 3  # Receipt exhaustion never drops evidence.
    assert len(context._packed_receipts) == 2


def test_native_witness_cannot_be_created_from_a_serialized_or_uploaded_mapping():
    from query_rank import NativePackWitness
    with pytest.raises(TypeError):
        NativePackWitness({'forged-raw-id': {'forged-native-fields'}})


def test_parallel_selectors_share_only_source_witness_not_request_receipts():
    index, first, raw, first_packet = receipt_fixture('coverage')
    second = new_query_rank_context(QUESTION)
    second.record(QUESTION, raw)
    second.record(QUERIES[0], raw[:1])
    assert not second.origins(first_packet)
    barrier = threading.Barrier(2)

    def work(context):
        barrier.wait(timeout=10)
        result = index.select_v2(QUESTION, raw, top_k=1, max_chars=12000,
            query_rank_context=context, constraint_question=QUESTION, allowed_sources={'policy.md'})
        assert context.origins(result[0])
        return context._packed_receipts

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(work, (first, second)))
    assert receipts[0] is not receipts[1]
    assert receipts[0] == receipts[1]  # Identical source ancestry is independently earned.


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_actual_native_packing_keeps_two_aspects_without_numeric_overview_suffixes(mode):
    agent = agent_for(corpus())
    before = deepcopy(agent._all_nodes)
    context = agent.retrieve_context(QUESTION, top_k=3, retrieval_queries=QUERIES,
        selection_mode=mode, constraint_question=QUESTION, source_aliases={'policy.md': 'policy.md'})
    assert len(context) == 3
    assert all(any(fact in item['text'] for item in context) for fact in FACTS)
    assert agent._all_nodes == before
    assert not any('_query_origins' in item or isinstance(value, type(new_query_rank_context(QUESTION)))
                   for item in context for value in item.values())


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_noisy_probe_private_deleted_and_explicit_source_never_gain_authority(mode):
    nodes = corpus() + [node('Рецепт медового печенья требует отдельного описания.', path='Рецепт', file='recipe.md'),
        node(FACTS[0], path='Секрет', file='private.md'),
        node(FACTS[1], path='Удалено', status='deleted')]
    agent = agent_for(nodes)
    context = agent.retrieve_context(QUESTION, top_k=3, retrieval_queries=(*QUERIES, 'рецепт медового печенья'),
        selection_mode=mode, constraint_question=QUESTION,
        source_aliases={'policy.md': 'policy.md', 'recipe.md': 'recipe.md'})
    assert all(any(fact in item['text'] for item in context) for fact in FACTS)
    assert all(item['file'] == 'policy.md' for item in context)
    explicit = agent.retrieve_context(QUESTION, top_k=3, retrieval_queries=QUERIES,
        selection_mode=mode, constraint_question='По policy.md: ' + QUESTION,
        source_aliases={'policy.md': 'policy.md', 'recipe.md': 'recipe.md'})
    assert all(item['file'] == 'policy.md' for item in explicit)


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_category_conflicting_probe_cannot_consume_final_slots(mode):
    question = 'Объем потребления для 2 ЦК и стоимость передачи.'
    nodes = [node('Первая ценовая категория. Объем потребления равен 100 единицам.', path=f'Чужая {i}') for i in range(10)]
    nodes += [node('Вторая ценовая категория. Объем потребления равен 200 единицам.', path='Объем'),
              node('Стоимость передачи составляет 300 единиц.', path='Стоимость')]
    nodes += [node('Прочие технические записи.', path=f'Шум {i}') for i in range(100)]
    agent = agent_for(nodes)
    context = agent.retrieve_context(question, top_k=2, retrieval_queries=('объем потребления', 'стоимость передачи'),
        selection_mode=mode, constraint_question=question, source_aliases={'policy.md': 'policy.md'})
    assert all('Первая ценовая категория' not in item['text'] for item in context)


def test_single_query_zero_gain_and_forged_metadata_keep_native_fallback():
    nodes = corpus()
    index = SectionEvidenceIndex(nodes)
    ranked = [row(FACTS[1], 'Правило Б', query_ranks={'raw': 0}), row(FACTS[0], 'Правило А')]
    original = deepcopy(ranked)
    empty = new_query_rank_context(QUESTION)
    single = new_query_rank_context(QUESTION)
    single.record(QUESTION, ranked[::-1])
    for select in (index.select, index.select_v2):
        baseline = select(QUESTION, ranked, top_k=2)
        assert select(QUESTION, ranked, top_k=2, query_rank_context=empty) == baseline
        assert select(QUESTION, ranked, top_k=2, query_rank_context=single) == baseline
    assert ranked == original


def test_numeric_variants_remain_distinct_and_zero_gain_does_not_stop_packing():
    nodes = [node(f'Пароль оператора действует {days} дней.', path=f'Вариант {days}') for days in (10, 20, 30)]
    index = SectionEvidenceIndex(nodes)
    ranked = [row(n.text, n.metadata['section_path']) for n in nodes]
    context = new_query_rank_context(QUESTION)
    context.record(QUERIES[0], ranked)
    context.record(QUERIES[1], [])
    for select in (index.select, index.select_v2):
        result = select(QUESTION, ranked, top_k=3, query_rank_context=context)
        assert len(result) == 3
        assert all(any(str(days) in item['text'] for item in result) for days in (10, 20, 30))


def test_corrective_closure_carries_current_context_and_resets_each_round(monkeypatch):
    from corrective_rag import CorrectiveResult
    seen = []

    def orchestration(question, search_question, *, retrieve, select, **kwargs):
        for queries in (QUERIES, (QUESTION,)):
            pool = retrieve(question, list(queries), [], {}, search_focus=None, constraint_question=QUESTION)
            result = select(question, pool, [], search_focus=None, constraint_question=QUESTION)
            assert result.evidence
        return CorrectiveResult('Готово.', [], {})

    monkeypatch.setattr('corrective_rag.run_corrective', orchestration)
    agent = agent_for(corpus())
    original = agent.retrieve_context

    def retrieve(*args, **kwargs):
        seen.append(kwargs['query_rank_context'])
        return original(*args, **kwargs)

    agent.retrieve_context = retrieve
    agent.llm = SimpleNamespace(model= 'synthetic', metadata=SimpleNamespace(context_window=32768))
    agent.llm_model = 'synthetic'
    agent._generation_state = threading.local()
    agent._ask_corrective(QUESTION, QUESTION, None, source_aliases={'policy.md': 'policy.md'})
    assert len(seen) == 2 and seen[0] is not seen[1]
    assert seen[0].allocator().active and not seen[1].allocator().active
    assert seen[0]._packed_receipts is not seen[1]._packed_receipts
    assert not any(isinstance(value, type(seen[0])) for value in vars(agent).values())


def test_parallel_request_contexts_cannot_leak_query_rows():
    def work(number):
        context = new_query_rank_context(QUESTION)
        own = row(FACTS[0], file=f'private-request-{number}.md')
        context.record(QUERIES[0], [own])
        return context, own
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(work, (1, 2)))
    assert first[0].origins(first[1]) and not first[0].origins(second[1])
    assert second[0].origins(second[1]) and not second[0].origins(first[1])


def test_actual_parallel_corrective_chats_use_distinct_closure_contexts(monkeypatch):
    from corrective_rag import CorrectiveResult
    barrier = threading.Barrier(2)
    seen, lock = {}, threading.Lock()
    agent = agent_for(corpus())
    agent.llm = SimpleNamespace(model='synthetic', metadata=SimpleNamespace(context_window=32768))
    agent.llm_model = 'synthetic'
    agent._generation_state = threading.local()
    native_retrieve = agent.retrieve_context

    def retrieve(*args, **kwargs):
        with lock:
            seen[threading.get_ident()] = kwargs['query_rank_context']
        return native_retrieve(*args, **kwargs)

    def orchestration(question, search_question, *, retrieve, select, **kwargs):
        pool = retrieve(question, list(QUERIES), [], {}, search_focus=None, constraint_question=question)
        barrier.wait(timeout=10)
        assert select(question, pool, [], search_focus=None, constraint_question=question).evidence
        return CorrectiveResult('Готово.', [], {})

    agent.retrieve_context = retrieve
    monkeypatch.setattr('corrective_rag.run_corrective', orchestration)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: agent._ask_corrective(
            QUESTION, QUESTION, None, source_aliases={'policy.md': 'policy.md'}), (1, 2)))
    assert all(result[0] == 'Готово.' for result in results)
    assert len(seen) == 2
    first, second = seen.values()
    assert first is not second and first._rows is not second._rows
    assert first._packed_receipts is not second._packed_receipts
