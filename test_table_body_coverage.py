"""A matching typed header cannot replace the governed source data rows."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from evidence_selection import SectionEvidenceIndex
from table_context import parse_table_unit,serialize_table_unit,table_unit_texts


THEMES=(('Кобальт','обычной заявки','количество единиц','рублей за единицу'),
        ('Малахит','обычного отправления','масса в килограммах','рублей за килограмм'))


def unit(row,values,*,table='a'*64,headers=None,kind='body'):
    cells=[{'row':row,'column':column,'span':1,'text':text,'start':0,'end':len(text),'total_chars':len(text)}
           for column,text in enumerate(values)]
    header=[{'row':0,'column':column,'span':1,'text':text} for column,text in enumerate(headers or ())]
    return serialize_table_unit({'schema':'table-grid-v1','table_id':table,'row':row,'row_kind':kind,
        'header_status':'explicit','headers':header,'cells':cells,'context':[],'warnings':[]})


def corpus(theme,*,noise=True):
    mode,subject,volume,tariff=theme
    headers=('Показатель',f'Режим {mode}','Резервный режим')
    data=[unit(1,('Формула','[MATH: S=V*T]','[MATH: S=V*T*1.1]'),headers=headers),
          unit(2,('Единицы',f'V — {volume}; T — {tariff}.','Надбавка задается отдельно.'),headers=headers),
          unit(3,('Условия','Обычная операция, без надбавки. Источник тарифа — договор.',
                  'Только срочная операция при количестве более 100 единиц.'),headers=headers)]
    main={'file':'opaque.docx','file_name':'opaque.docx','file_path':'/owned/opaque.docx','source_id':'source-A',
          'revision_id':'revision-A','section_path':'1. Порядок расчета','section_title':'1. Порядок расчета',
          'text':'\n\n'.join([f'Расчет стоимости {subject} выполняется по следующей таблице.',
                            unit(0,headers,kind='header'),*data]),'score':1.0,'status':'active'}
    rows=[main]
    if noise:
        rows.append({'file':'interface.docx','file_name':'interface.docx','file_path':'/owned/interface.docx',
            'source_id':'source-B','revision_id':'revision-A','section_path':f'2. Экран режима {mode}',
            'section_title':f'2. Экран режима {mode}','text':f'Экран режима {mode} отображает стоимость {subject}.\n\n'+
                unit(0,(f'Режим {mode}','Формула и условия на экране'),table='b'*64,kind='header')+'\n\n'+
                unit(1,('Окно параметров','Пользователь выбирает цвет окна в меню.'),table='b'*64,
                     headers=(f'Режим {mode}','Формула и условия на экране'))+'\n\n'+
                'Пользователь открывает меню, выбирает карточку и меняет внешний вид панели. '*12,
            'score':1.4,'status':'active'})
    return rows,data,f'Как рассчитывается стоимость {subject} по режиму {mode}? Укажи формулу, единицы и условия.'


def select(rows,question,**kwargs):
    nodes=[SimpleNamespace(text=row['text'],metadata=row) for row in rows]
    return SectionEvidenceIndex(nodes).select_v2(question,rows,resolve_references=False,**kwargs)


@pytest.mark.parametrize('theme',THEMES)
@pytest.mark.parametrize('noise',(False,True))
def test_requested_table_body_formula_units_conditions_survive_header_hit(theme,noise):
    rows,data,question=corpus(theme,noise=noise)
    original=deepcopy(rows)
    budget=sum(len(t) for t in data)+600
    result=select(rows,question,max_chars=budget,top_k=3)
    text='\n\n'.join(row['text'] for row in result if row['file']=='opaque.docx')
    assert all(native in text for native in data), 'Governed formula, units and conditions must beat header-only/UI evidence when the native body fits'
    assert sum(len(row['text']) for row in result)<=budget
    assert rows==original


@pytest.mark.parametrize('theme',THEMES)
def test_header_only_packet_needs_honest_incomplete_coverage_when_body_cannot_fit(theme):
    rows,data,question=corpus(theme,noise=False)
    header=table_unit_texts(rows[0]['text'])[0]
    budget=len(header)+40
    assert all(len(text)>budget for text in data)
    meta={}
    result=select(rows,question,max_chars=budget,top_k=1,selection_meta=meta)
    payload=[parse_table_unit(t) for row in result for t in table_unit_texts(row['text'])]
    only_header=bool(payload) and all(u['row_kind']=='header' for u in payload)
    assert not only_header or meta.get('table_body_coverage_incomplete') is True
    assert sum(len(row['text']) for row in result)<=budget


@pytest.mark.parametrize('theme',THEMES)
def test_excluded_source_cannot_donate_body_to_another_header(theme):
    rows,data,question=corpus(theme)
    result=select(rows,question,max_chars=10000,top_k=3,allowed_sources={'interface.docx'})
    assert all(row['file']=='interface.docx' for row in result)
    assert not any(native in row['text'] for row in result for native in data)


@pytest.mark.parametrize('theme',THEMES)
def test_foreign_explicit_category_does_not_supply_table_body(theme):
    rows,data,question=corpus(theme)
    rows[0]['section_path']='1. Порядок расчета > 3 ЦК'
    question+=' Только для 1 ЦК.'
    result=select(rows,question,max_chars=10000,top_k=3)
    assert all(row['file']!='opaque.docx' for row in result)


@pytest.mark.parametrize('theme',THEMES)
def test_negative_only_descendant_cannot_be_added_as_header_support(theme):
    rows,data,question=corpus(theme)
    rows[0]['section_path']=rows[0]['section_title']='Срочные заявки'
    question+=' Срочные заявки не рассматривай.'
    result=select(rows,question,max_chars=10000,top_k=3)
    assert all(row['file']!='opaque.docx' for row in result)


@pytest.mark.parametrize('theme',THEMES)
def test_same_table_id_cannot_merge_different_source_revision_coordinates(theme):
    rows,data,question=corpus(theme,noise=False)
    foreign={**rows[0],'revision_id':'revision-B','file_path':'/other/opaque.docx',
             'text':unit(1,('Формула','FOREIGN-ONLY-91','Отдельный источник'),headers=('Показатель',f'Режим {theme[0]}','Резервный режим')),
             'score':1.5}
    rows.append(foreign)
    result=select(rows,question,max_chars=12000,top_k=3)
    for row in result:
        if row.get('revision_id')=='revision-A':
            assert 'FOREIGN-ONLY-91' not in row['text']
        if row.get('revision_id')=='revision-B':
            assert not any(native in row['text'] for native in data)


@pytest.mark.parametrize('theme',THEMES)
@pytest.mark.parametrize('clipped',(False,True))
def test_candidate_first_row_with_formula_value_and_unit_is_not_discarded_as_metadata(theme,clipped):
    rows,_,_=corpus(theme,noise=False)
    values=('Предел','25 единиц','[MATH: S=V*T]')
    first=unit(0,values,kind='body')
    payload=parse_table_unit(first);payload['header_status']='candidate'
    first=serialize_table_unit(payload)
    following=unit(1,('Примечание','Без дополнительной надбавки.','Отдельная операция.'),headers=values)
    payload=parse_table_unit(following);payload['header_status']='candidate'
    following=serialize_table_unit(payload)
    rows[0].update(section_path=f'Режим {theme[0]}',section_title=f'Режим {theme[0]}',text=first+'\n\n'+following)
    result=select(rows,f'Каков предел режима {theme[0]}?',max_chars=len(first)+80 if clipped else 8000,top_k=2)
    assert any(first in row['text'] for row in result), 'A structural first-row witness cannot erase actual formula/value/unit facts'


@pytest.mark.parametrize('theme',THEMES)
def test_incomplete_table_body_is_source_free_and_does_not_survive_next_complete_selection(theme):
    rows,data,question=corpus(theme,noise=False)
    meta={}
    result=select(rows,question,max_chars=sum(len(t) for t in data)//2,top_k=1,selection_meta=meta)
    assert meta.get('table_body_coverage_incomplete') is True
    assert meta.get('scoped_cohort_incomplete') is True
    assert not any('opaque.docx' in str(value) or '/owned/' in str(value) for value in meta.values())
    select(rows,question,max_chars=8000,top_k=1,selection_meta=meta)
    assert not meta.get('table_body_coverage_incomplete') and not meta.get('scoped_cohort_incomplete')


@pytest.mark.parametrize('theme',THEMES)
def test_candidate_first_row_with_plain_native_factual_assertion_is_preserved_under_clipping(theme):
    rows,_,_=corpus(theme,noise=False)
    values=('Условия','Перенос запрещен','Документ подписан')
    first=unit(0,values,kind='body')
    payload=parse_table_unit(first);payload['header_status']='candidate'
    first=serialize_table_unit(payload)
    following=unit(1,('Примечание','Без дополнительной надбавки.','Отдельная операция.'),headers=values)
    payload=parse_table_unit(following);payload['header_status']='candidate'
    following=serialize_table_unit(payload)
    rows[0].update(section_path=f'Режим {theme[0]}',section_title=f'Режим {theme[0]}',text=first+'\n\n'+following)
    result=select(rows,f'Какие условия режима {theme[0]}?',max_chars=len(first)+80,top_k=1)
    assert any(first in row['text'] for row in result), 'A plain factual first-row assertion is not erased by an unverified header inference'


@pytest.mark.parametrize('theme',THEMES)
def test_explicit_column_names_question_keeps_native_header(theme):
    rows,_,_=corpus(theme,noise=False)
    header=table_unit_texts(rows[0]['text'])[0]
    result=select(rows,f'Какие названия столбцов таблицы режима {theme[0]}?',
                  max_chars=len(header)+100,top_k=1)
    assert any(header in row['text'] for row in result)


@pytest.mark.parametrize('theme',THEMES)
def test_header_without_admitted_native_body_remains_source_data(theme):
    rows,_,question=corpus(theme,noise=False)
    header=table_unit_texts(rows[0]['text'])[0]
    rows[0]['text']=header+'\n\n'+'Порядок поясняется отдельно. '*40
    result=select(rows,question,max_chars=len(header)+100,top_k=1)
    assert any(header in row['text'] for row in result)


@pytest.mark.parametrize('theme',THEMES)
@pytest.mark.parametrize('independent',(False,True))
def test_declined_duplicate_is_optional_but_independent_requested_table_is_required(theme,independent):
    rows,data,question=corpus(theme,noise=False)
    alternative=corpus(('Сапфир',*theme[1:]) if independent else theme,noise=False)[0][0]
    alternative.update(file='another.docx',file_name='another.docx',file_path='/owned/another.docx',
                       source_id='source-C',section_path='2. Расчет',section_title='2. Расчет',score=0.5)
    for native in table_unit_texts(alternative['text']):
        payload=parse_table_unit(native)
        payload['table_id']='c'*64
        alternative['text']=alternative['text'].replace(native,serialize_table_unit(payload))
    if independent:
        # Shared numeric expression does not discharge another explicit mode,
        # whose native applicability and full conditions are independently asked.
        question+=f' А также как рассчитывается стоимость {theme[1]} по режиму Сапфир? Укажи формулу, единицы и условия.'
    rows.append(alternative)
    meta={}
    result=select(rows,question,max_chars=sum(len(t) for t in data)+650,top_k=2,selection_meta=meta)
    assert any(all(native in row['text'] for native in data) for row in result if row['file']=='opaque.docx')
    assert meta.get('table_body_coverage_incomplete',False) is independent
    assert meta.get('scoped_cohort_incomplete',False) is independent
    assert not any('opaque.docx' in str(value) or 'another.docx' in str(value) for value in meta.values())
    if not independent:
        assert meta.get('table_body_declined_alternatives')==1


@pytest.mark.parametrize('theme',THEMES)
@pytest.mark.parametrize('reason',('distinct-condition','explicit-negative'))
def test_similar_cohort_or_negative_constraint_cannot_be_discharged_as_optional(theme,reason):
    rows,data,question=corpus(theme,noise=False)
    alternative=deepcopy(rows[0])
    alternative.update(file='another.docx',file_name='another.docx',file_path='/owned/another.docx',
                       source_id='source-C',section_path='2. Расчет',section_title='2. Расчет',score=0.5)
    for native in table_unit_texts(alternative['text']):
        payload=parse_table_unit(native)
        payload['table_id']='d'*64
        if reason=='distinct-condition' and payload['row']==3:
            cell=payload['cells'][1]
            cell['text']+=' Требуется письменное согласование.'
            cell['end']=cell['total_chars']=len(cell['text'])
        alternative['text']=alternative['text'].replace(native,serialize_table_unit(payload))
    if reason=='explicit-negative':
        question+='; не рассматривай хранение журнала.'
    meta={}
    result=select([*rows,alternative],question,max_chars=sum(len(t) for t in data)+650,top_k=2,selection_meta=meta)
    assert any(all(native in row['text'] for native in data) for row in result if row['file']=='opaque.docx')
    assert meta.get('table_body_coverage_incomplete') is True
    assert meta.get('scoped_cohort_incomplete') is True
    assert not meta.get('table_body_declined_alternatives')
