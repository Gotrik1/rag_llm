"""Typed model authorship; the renderer never selects a source or proves a fact."""
import json,re
from native_source_rows import NativeRow

VERSION='evidence-bound-blocks-v2-native-rows'

def _unique(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError('duplicate-key')
        value[key]=item
    return value

def _line(value):
    if not isinstance(value,str) or not value.strip() or '\n' in value or '\r' in value:
        raise ValueError('one-complete-paragraph-required')
    return value.strip()


def _author_ids(ids,source_ids):
    if (not isinstance(ids,list) or not ids or any(not isinstance(eid,str) or eid not in source_ids for eid in ids)
            or len(set(ids))!=len(ids)):
        raise ValueError('invalid-author-source-choice')
    return ' '.join('['+eid+']' for eid in ids)


def _content(value,formula_ids):
    text=_line(value)
    if any(fid not in formula_ids for fid in re.findall(r'\[\[FORMULA:([^\]]+)\]\]',text)):
        raise ValueError('unknown-native-formula')
    if re.search(r'\[(?:E\d|Источник:)',text,re.I):
        raise ValueError('sources-belong-in-typed-field')
    return text


def _formula_owners(texts,ids,formula_ids):
    owners={formula_ids[fid] for text in texts
            for fid in re.findall(r'\[\[FORMULA:([^\]]+)\]\]',text)}
    if not owners.issubset(ids):
        raise ValueError('formula-source-not-selected-by-author')


def _table(block,source_ids,formula_ids):
    if set(block)!={'headers','rows'} or not isinstance(block['headers'],list) or not block['headers']:
        raise ValueError('invalid-table-schema')
    headers=[_content(header,formula_ids) for header in block['headers']]
    if not isinstance(block['rows'],list) or not block['rows']:
        raise ValueError('invalid-table-rows')
    def cell(value):return re.sub(r'(?<!\\)\|',r'\\|',value)
    result=['| '+' | '.join([*(cell(h) for h in headers),'Источник'])+' |',
            '| '+' | '.join(['---']*(len(headers)+1))+' |']
    for row in block['rows']:
        if (not isinstance(row,dict) or set(row)!={'cells','source_ids'}
                or not isinstance(row['cells'],list) or len(row['cells'])!=len(headers)):
            raise ValueError('invalid-table-row')
        native_cells=[_content(value,formula_ids) for value in row['cells']]
        references=_author_ids(row['source_ids'],source_ids)
        _formula_owners(native_cells,row['source_ids'],formula_ids)
        cells=[cell(value) for value in native_cells]
        result.append('| '+' | '.join([*cells,references])+' |')
    return '\n'.join(result)

def render(raw,source_ids,formula_ids,native_rows=None):
    """Validate structured writer blocks and resolve only offered source/formula/native-row IDs into answer text."""
    if not isinstance(raw,str) or len(raw.encode('utf-8'))>1024*1024:
        raise ValueError('invalid-wire-payload')
    data=json.loads(raw,object_pairs_hook=_unique)
    if not isinstance(data,dict) or set(data)!={'blocks'} or not isinstance(data['blocks'],list) or not data['blocks']:
        raise ValueError('invalid-draft-schema')
    result=[]
    for block in data['blocks']:
        if isinstance(block,dict) and 'row_id' in block:
            if set(block)!={'row_id','source_ids'} or not isinstance(block['row_id'],str):
                raise ValueError('invalid-native-row-block')
            row=(native_rows or {}).get(block['row_id'])
            if not isinstance(row,NativeRow):
                raise ValueError('unknown-native-row')
            references=_author_ids(block['source_ids'],source_ids)
            if row.evidence_id not in block['source_ids']:
                raise ValueError('native-row-source-not-selected-by-author')
            result.append(row.display+' '+references)
            continue
        if isinstance(block,dict) and 'headers' in block:
            result.append(_table(block,source_ids,formula_ids))
            continue
        if not isinstance(block,dict) or set(block)!={'text','source_ids'}:
            raise ValueError('invalid-block-schema')
        text=_content(block['text'],formula_ids);references=_author_ids(block['source_ids'],source_ids)
        _formula_owners([text],block['source_ids'],formula_ids)
        # Every paragraph has an explicit source choice by the model. The
        # renderer cannot fill missing IDs or find a more convenient source.
        result.append(text+' '+references)
    return '\n\n'.join(result)

def contract(source_ids,native_rows=()):
    """Describe the internal writer-block wire format without relaxing later source and publication verification."""
    return ('\nФОРМАТ ВНУТРЕННЕГО ЧЕРНОВИКА '+VERSION+' — JSON, не Markdown-ответ целиком. '
        'Верни только {"blocks":[{"text":"один законченный абзац без переводов строки",'
        '"source_ids":["реальный E-ID"]}]}. '
        'Каждый блок содержит самостоятельное утверждение, формулу, определение, пункт списка '
        'или краткий подзаголовок. Выбери подтверждающие source_ids для каждого блока сам; '
        'пустые или неизвестные ID запрещены. Источник для общего расчета должен подтверждать '
        'общее правило, а не похожий частный случай. В text не ставь E-маркеры или названия источников: '
        'их добавит программа строго по твоему source_ids. Допустимые source_ids: '+json.dumps(list(source_ids))+'. '
        'Формулу передавай через [[FORMULA:F-ID]] из native_formulae. '
        'Для выбранной формулы source_ids должны включать её исходный evidence_id из native_formulae. '
        'Для расшифровки величин, единиц, источников данных и совместных условий из таблицы '
        'обязательно используй блок {"row_id":"реальный R-ID","source_ids":["исходный E-ID"]}: '
        'программа воспроизведёт полную исходную строку. Не пересказывай определение своими словами '
        'и не расшифровывай аббревиатуру названием организации, которого нет в источнике. '
        'Не добавляй исполнителя или поставщика данных к объяснению формулы на основании догадки. '
        'ДОСТУПНЫЕ ИСХОДНЫЕ СТРОКИ (ячейки уже приведены в SOURCE DATA): '+
        json.dumps([row.descriptor() for row in native_rows],ensure_ascii=False)+'. '
        'Если нужна таблица, вместо text-блока используй блок {"headers":["Название столбца"],'
        '"rows":[{"cells":["Значение"],"source_ids":["реальный E-ID"]}]}. '
        'В каждой строке столько cells, сколько headers; источник выбирается отдельно для каждой строки. '
        'Количество абзацев и строк определяется полнотой ответа, а не этой схемой. '
        'Не добавляй справочные рекомендации и конвертации единиц, если они не нужны для вопроса '
        'или не подтверждаются пакетом. Не заявляй об отсутствии сведений в документе по обрывку. '
        'Это только формат авторства: все факты, условия, математические знаки и выбранные источники '
        'потом проходят ту же независимую проверку. Пользователь увидит обычный русский ответ.\n')
