"""Main native calculation and its own legend must survive cheaper side math."""
from copy import deepcopy
import pytest
from evidence_selection import _content_blocks,_focused_excerpt,_query_groups
from test_table_body_coverage import unit

def native_case(category,letters):
    y,a,p,f,g=letters
    headers=('Обозначение','Описание','Ед. изм.')
    local=[unit(1,(f'[MATH: {a}]',f'Количество обычной операции для {category} ЦК.','шт.'),headers=headers),
           unit(2,(f'[MATH: {p}]',f'Стоимость единицы обычной операции для {category} ЦК.','руб./шт.'),headers=headers)]
    far=[unit(n,(f'[MATH: {x}]','Дополнительная настройка особого варианта. '+('Условие действия сохраняется целиком. '*10),'руб.'),table='b'*64,headers=headers)
         for n,x in enumerate((f,g,p,a),1)]
    core=[f'Применение {category} ЦК: обычная операция при выполнении условий договора.',
          f'[MATH: {y}={a}*{p}]']
    text='\n\n'.join([f'# Расчет для {category} ЦК',*core,'Где:',*local,
        'Отдельный дополнительный расчет стоимости:',f'[MATH: {p}={f}+{g}]','Где:',*far])
    return text,core,local,far

@pytest.mark.parametrize('category',(1,6))
@pytest.mark.parametrize('letters',('YAPFG','ZBTHK'))
def test_primary_literal_calculation_keeps_entire_nearest_native_legend(category,letters):
    text,core,local,_=native_case(category,letters)
    before=deepcopy(text);question=f'Как рассчитывается {category} ЦК?'
    budget=sum(map(len,local))+650;sig={}
    excerpt,_=_focused_excerpt(text,budget,_query_groups(question),{},question=question,diagnostics=sig)
    assert all(value in excerpt for value in [*core,*local])
    assert len(excerpt)<=budget and text==before
    assert not sig.get('definition_closure_incomplete')

@pytest.mark.parametrize('category',(1,6))
def test_unfit_primary_legend_never_becomes_a_complete_calculation(category):
    text,_,local,_=native_case(category,'YAPFG');question=f'Как рассчитывается {category} ЦК?';sig={}
    excerpt,_=_focused_excerpt(text,len(local[0]),_query_groups(question),{},question=question,diagnostics=sig)
    assert sig.get('scoped_cohort_incomplete') or sig.get('definition_closure_incomplete')

def test_ordinary_narrative_does_not_invent_a_calculation_root():
    text,_,_,_=native_case(6,'YAPFG');text=text.replace('Применение 6 ЦК:', 'Пример записи:')
    q='Как рассчитывается 6 ЦК?';sig={}
    _focused_excerpt(text,2500,_query_groups(q),{},question=q,diagnostics=sig)
    assert not sig.get('scoped_cohort_blocks')
