from docx_parser import _heading_level

def test_visible_numbering_overrides_inconsistent_word_style():
    assert _heading_level('Heading3', 'Heading 3', '5.ПОРЯДОК ОПРЕДЕЛЕНИЯ ЦЕН') == 1
    assert _heading_level('Heading1', 'Heading 1', '5.3 Порядок определения стоимости 3 ЦК и 4 ЦК') == 2
    assert _heading_level('Heading4', 'Heading 4', '4.1.2 Договорные условия') == 3

def test_category_number_is_not_a_chapter_number():
    assert _heading_level('Heading2', 'Heading 2', '2 ЦК для субъектов РФ') == 2
