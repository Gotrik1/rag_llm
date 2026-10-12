from rag_agent import matching_explicit_sources


def test_complete_single_word_filename_selects_visible_source():
    aliases = {'uuid_price.docx': 'цена.docx', 'uuid_other.docx': 'Методика расчета цены.docx'}
    assert matching_explicit_sources('Сколько стоит товар согласно документу цена.docx?', aliases) == {'uuid_price.docx'}
    assert matching_explicit_sources('Прочитай «ЦЕНА.DOCX».', aliases) == {'uuid_price.docx'}


def test_business_term_and_filename_substring_are_not_explicit_references():
    aliases = {'uuid_price.docx': 'цена.docx'}
    assert matching_explicit_sources('Как определяется цена?', aliases) == set()
    assert matching_explicit_sources('Что написано в суперцена.docx?', aliases) == set()
    assert matching_explicit_sources('цена.docx_backup', aliases) == set()


def test_filename_reference_cannot_resolve_an_invisible_source():
    assert matching_explicit_sources('Что в secret.docx?', {'uuid_price.docx': 'цена.docx'}) == set()
