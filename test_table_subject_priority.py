"""A table cannot buy priority over the actual original question's subject."""
from types import SimpleNamespace

import pytest

from evidence_selection import SectionEvidenceIndex
from evidence_selection import _original_table_subjects,_table_body_subject_supported
from test_table_body_coverage import unit


THEMES=(('глубина резервуара','Глубина резервуара','H=V/A'),
        ('допуск отверстия','Допуск отверстия','D=R-L'))


@pytest.mark.parametrize('theme',THEMES)
@pytest.mark.parametrize('question_style',('formula','comparison'))
def test_unrelated_giant_equation_table_cannot_displace_original_subject(theme,question_style):
    subject,title,equation=theme
    question=(f'Как рассчитывается {subject}? Приведи формулу.' if question_style=='formula'
              else f'Чем {subject} отличается от расчета стоимости?')
    main=(f'{title} определяется по формуле [MATH: {equation}]. '
          'Условие применимости: используется исправный инструмент и выполняется предварительная проверка. '*10).rstrip()
    headers=('Обозначение','Описание','Единицы')
    table=[unit(1,('[MATH: Q]','Количество товаров для отдельной отгрузки. '*9,'шт'),headers=headers),
           unit(2,('[MATH: T]','Тариф перевозчика для отдельной отгрузки. '*9,'руб/шт'),headers=headers)]
    unrelated='Стоимость отгрузки определяется по формуле [MATH: C=Q*T].\n\n'+'\n\n'.join(table)
    rows=[{'file':'target.md','file_name':'target.md','file_path':'/fixture/target.md','source_id':'target',
           'revision_id':'revision-A','section_path':title,'section_title':title,'text':main,'status':'active','score':1.4},
          {'file':'other.docx','file_name':'other.docx','file_path':'/fixture/other.docx','source_id':'other',
           'revision_id':'revision-A','section_path':'Стоимость отгрузки','section_title':'Стоимость отгрузки',
           'text':unrelated,'status':'active','score':0.1}]
    nodes=[SimpleNamespace(text=row['text'],metadata=row) for row in rows]
    budget=sum(len(row) for row in table)+250
    assert len(main)<budget and len(main)>250
    result=SectionEvidenceIndex(nodes).select_v2(question,rows,top_k=2,max_chars=budget,resolve_references=False)
    assert any(row['file']=='target.md' and row['text']==main for row in result), 'A weaker unrelated table cannot consume the main native subject packet allowance'
    assert sum(len(row['text']) for row in result)<=budget


@pytest.mark.parametrize('subject',('цена упаковки','цена отправления'))
def test_full_generic_noun_phrase_can_reserve_its_own_native_table(subject):
    headers=('Обозначение','Описание','Единицы')
    data=[unit(1,('[MATH: P]',subject.capitalize()+'. При обычной операции без дополнительной надбавки.','руб/шт'),headers=headers),
          unit(2,('[MATH: S]','Итоговая сумма по договору.','руб'),headers=headers),
          unit(3,('[MATH: Q]','Количество единиц по договору.','шт'),headers=headers)]
    text=f'{subject.capitalize()} рассчитывается по формуле [MATH: P=S/Q].\n\n'+'\n\n'.join(data)+'\n\n'+'Архивные служебные записи. '*200
    row={'file':'neutral.docx','file_name':'neutral.docx','file_path':'/fixture/neutral.docx','source_id':'neutral',
         'revision_id':'revision-A','section_path':subject.capitalize(),'section_title':subject.capitalize(),
         'text':text,'status':'active','score':1.0}
    result=SectionEvidenceIndex([SimpleNamespace(text=text,metadata=row)]).select_v2(
        f'Как рассчитывается {subject}?', [row],top_k=1,max_chars=sum(len(native) for native in data)+250,
        resolve_references=False)
    assert result and all(native in result[0]['text'] for native in data)


@pytest.mark.parametrize('subject',('column','header_status'))
def test_codec_metadata_is_not_positive_native_subject_support(subject):
    text='Отдельная операция [MATH: S=Q*T].\n\n'+unit(1,('[MATH: Q]','Количество единиц.','шт'),headers=('Обозначение','Описание','Единицы'))
    row={'file':subject+'.docx','file_name':subject+'.docx','text':text,'section_path':'Порядок операции','section_title':'Порядок операции'}
    scopes=_original_table_subjects(f'Как рассчитывается {subject}?')
    assert not _table_body_subject_supported(row,scopes,[row]), 'Codec keys and source basename cannot establish a real source subject'
