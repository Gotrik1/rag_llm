"""Coverage-only attribution projection; never invent a document or fact.

The display answer is retained. A declared standalone source footer may bind
preceding claims in its original Markdown section to one physical revision.
This supplies citation syntax, not entailment or permission to publish claims.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata

from answer_citations import _protected_spans, passage_citation_references
from atomic_publication import publication_units
from evidence_selection import _VERSION_FIELDS
from grounding_verifier import _answer_structure, _CITATION, _evidence_map, citation_ids


PROJECTION_VERSION='declared-source-scope-v3'


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def _fold(value):
    return re.sub(r'\s+',' ',unicodedata.normalize('NFC',str(value or ''))).strip()


def _inside(offset,spans):
    return any(start<=offset<end for start,end in spans)


def _reference_example_line(text, token):
    """A quoted/escaped source-only example is not an author's declaration."""
    begin=text.rfind('\n',0,token.start())+1
    end=text.find('\n',token.end())
    if end<0:end=len(text)
    left,right=text[begin:token.start()],text[token.end():end]
    if right.strip():return False
    return bool(re.fullmatch(r' *(?:> *)+(?:[-+*] +)?',left) or
                re.fullmatch(r' *\\[-+*] +',left))


def _list_footer_scopes(text, records, tokens, protected):
    """Conventional list footer, backwards within its native parent list only.

    Space indentation and explicit list markers define ownership. Unclear lazy
    outdents, headings, quotes and protected blocks break it conservatively.
    Source IDs still come only from exact `_resolve`, never the list topic.
    """
    ordered=[];groups={};result=[];offset=0
    for raw in text.splitlines(keepends=True):
        line=raw.rstrip('\r\n');position=offset;offset+=len(raw)
        if not line.strip():continue
        if (_inside(position,protected) or _hard_heading(line) or re.match(r'^ *>+',line)):
            ordered=[];groups={};continue
        match=re.fullmatch(r'(?P<indent> *)(?P<marker>[-+*]|\d+[.)]) +(?P<body>.*)',line)
        if not match:
            indent=len(line)-len(line.lstrip(' '))
            while ordered and indent<ordered[-1]['content_indent']:ordered.pop()
            groups={key:start for key,start in groups.items() if indent>key[1]}
            continue
        indent=len(match['indent']);marker=match['marker']
        while ordered and indent<ordered[-1]['content_indent']:ordered.pop()
        groups={key:start for key,start in groups.items() if key[1]<=indent}
        if marker[0].isdigit():
            ordered.append({'start':position,'content_indent':match.start('body')})
            groups={};continue
        owner=ordered[-1]['start'] if ordered else None
        key=(owner,indent)
        start=owner if ordered and indent==ordered[-1]['content_indent'] else groups.get(key)
        # Without a real parent, 4+ spaces may be an indented code example.
        if not ordered and indent>=4:start=None
        body=match['body'].strip()
        citation=_CITATION.fullmatch(body)
        footer=next((token for token in tokens if position<=token.start()<offset and token.group()==body),None)
        if citation is not None and footer is not None:
            ids=_resolve(footer.group(),records)
            local=[token for token in tokens if start is not None and start<=token.start()<footer.start()]
            if (start is not None and ids and not passage_citation_references(text[start:footer.start()])
                    and all(set(_resolve(token.group(),records))==set(ids) for token in local)):
                result.append((start,footer.start(),tuple(ids),local))
        else:
            groups.setdefault(key,position)
    return result


def _hard_heading(text):
    return bool(re.match(r'^ {0,3}#{1,6}(?:\s|$)',text) or
        re.fullmatch(r'\s*(?:\*\*[^\n]+\*\*|__[^\n]+__):?\s*',text))


def _prepare_labels(answer):
    protected=_protected_spans(answer)
    lines=[];position=0
    for raw in answer.splitlines(keepends=True):
        lines.append((position,raw));position+=len(raw)
    pieces=[];hard=[];position=0
    for number,(old,raw) in enumerate(lines):
        text=raw.rstrip('\r\n');label=text.strip()
        if label.startswith('**') and label.endswith('**'):label=label[2:-2].strip()
        elif label.startswith('__') and label.endswith('__'):label=label[2:-2].strip()
        following=next((line.strip() for _,line in lines[number+1:] if line.strip()),'')
        soft=(not _inside(old,protected) and label.endswith(':') and bool(label[:-1].strip())
            and not re.match(r'^\s*(?:[-*+]\s|\d+[.)]\s|#{1,6}\s)',text)
            and bool(re.match(r'^(?:[-*+]\s|\d+[.)]\s|\$\$|\\\[|\\\(|\[MATH:)',following)))
        if soft and not _hard_heading(text):
            raw='**'+label+'**'+raw[len(text):]
        elif _hard_heading(text) and not soft and not _inside(old,protected):
            hard.append(position)
        pieces.append(raw);position+=len(raw)
    return ''.join(pieces),hard


def _compatible(records):
    if len(records)==1:return True
    physical=('file_path','source_id','document_id')
    if not any(all(row.get(key) for row in records) and len({_json(row[key]) for row in records})==1
               for key in physical):return False
    if any(len({_json(row[key]) for row in records if row.get(key)})>1 for key in physical):return False
    return all(len({_json(row.get(key)) for row in records})==1 for key in _VERSION_FIELDS)


def _document_name(row):
    reference=str(row.get('source_reference') or '')
    return _fold(re.split(r'\s+[—–]\s+(?:раздел|пункт|документ)\b',reference,maxsplit=1,flags=re.I)[0]
                 if reference else row.get('file',''))


def _resolve(token,records):
    match=_CITATION.fullmatch(token.strip())
    if match is None:return ()
    if match.group('ids'):
        ids,bad=citation_ids(token,records)
        candidates=[records[eid] for eid in ids] if not bad else []
    else:
        declared=_fold(match.group('human'))
        parts=re.split(r'\s+[—–]\s+',declared,maxsplit=1)
        rows=[row for row in records.values() if _document_name(row)==parts[0]]
        if len(parts)==1:
            candidates=rows
        else:
            locator=re.sub(r'^(?:раздел|пункт)\s*','',parts[1],flags=re.I).strip(' «»"')
            candidates=[row for row in rows if _fold(row.get('section_path'))==locator or
                _fold(row.get('section_path')).startswith(locator+' > ')]
            if not candidates:
                candidates=[row for row in rows if _fold(row.get('source_reference'))==declared]
    if not candidates or not _compatible(candidates):return ()
    return tuple(row['evidence_id'] for row in candidates)


@dataclass(frozen=True)
class PublicationProjection:
    """Bound display/verification projections and immutable serialized citation contexts for one answer."""
    display:str
    verification:str
    bindings_json:str
    contexts_json:str
    scopes_json:str
    unresolved_reference_count:int
    digest:str

    @property
    def bindings(self):
        """Return a detached citation-binding mapping from the serialized publication projection."""
        return json.loads(self.bindings_json)


def _compatible_declared_group(ids, records):
    rows = [records[eid] for eid in ids]
    for number, left in enumerate(rows):
        for right in rows[number+1:]:
            same_source = _document_name(left) == _document_name(right) or any(
                left.get(key) and left.get(key) == right.get(key)
                for key in ('file_path','source_id','document_id'))
            if same_source and not _compatible([left,right]):
                return False
    return True


def _terminal_line_scopes(text, records, tokens, protected):
    """A terminal inline source group declares only its own paragraph line.

    This is attribution syntax, not support. Never cross physical lines,
    previous citations, quotes, tables, headings, code or physical revisions.
    """
    position = 0
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip('\r\n')
        begin, end = position, position + len(line)
        position += len(raw)
        if (not line.strip() or line.startswith(('    ', '\t')) or
                line.lstrip().startswith(('#', '>', '|', '```', '~~~')) or
                _inside(begin, protected)):
            continue
        local = [token for token in tokens if begin <= token.start() and token.end() <= end]
        if not local or not re.fullmatch(r'[ \t.!?]*', text[local[-1].end():end]):
            continue
        group = [local[-1]]
        for previous in reversed(local[:-1]):
            if not re.fullmatch(r'[ \t,;]*', text[previous.end():group[0].start()]):
                break
            group.insert(0, previous)
        if len(group) != len(local) or not text[begin:group[0].start()].strip():
            continue
        ids = []
        for token in group:
            known = _resolve(token.group(), records)
            if not known:
                break
            ids.extend(eid for eid in known if eid not in ids)
        else:
            if ids and _compatible_declared_group(ids,records):
                yield begin, group[0].start(), tuple(ids), group


def compile_publication(answer,source_records):
    """Bind answer citations to supplied source records and compute verification scopes without changing source facts."""
    records=_evidence_map(source_records)
    text,hard=_prepare_labels(answer)
    protected=_protected_spans(text)
    tokens=[match for match in _CITATION.finditer(text) if not _inside(match.start(),protected)
            and not _reference_example_line(text,match)]
    claims,headings=_answer_structure(text)
    units=publication_units(text,claims,headings,protected=protected,citation_pattern=_CITATION)
    operations=[];scopes=[];unresolved=0
    for token in tokens:
        ids=_resolve(token.group(),records)
        if ids:operations.append((token.start(),token.end(),'['+', '.join(ids)+']'))
        else:unresolved+=1
    starts=sorted(set([0,*hard,len(text)]))
    for start,end in zip(starts,starts[1:]):
        local=[token for token in tokens if start<=token.start()<end]
        if not local or passage_citation_references(text[start:end]):continue
        footer=local[-1]
        prefix=text[start:footer.start()]
        # A footer is a separate paragraph. Attribution is backwards only;
        # any later text remains unbound, including a program-owned caution.
        if prefix.strip() and not re.search(r'\n[ \t]*\n[ \t]*$',prefix):continue
        suffix=text[footer.end():end]
        if suffix.strip() and not re.match(r'[ \t]*\r?\n[ \t]*\r?\n',suffix):continue
        ids=_resolve(footer.group(),records)
        if not ids:continue
        # Native F-ID rendering adds its own local citation. That known
        # transformation must not disable a model-declared footer when every
        # citation names the exact same evidence set. A sibling locator or
        # another revision cannot grant the same scope merely by filename.
        if any(set(_resolve(token.group(),records))!=set(ids) for token in local[:-1]):continue
        inserted=set()
        for claim in claims:
            if any(claim['start']<=token.start()<claim['end'] for token in local):continue
            if start<=claim['start'] and claim['end']<=footer.start() and not _inside(claim['start'],protected):
                inserted.add(claim['end'])
            elif start<=claim['start'] and claim['end']<=footer.start():
                # Display/inline equations are protected syntax, not code or
                # quoted service-reference examples. Keep them attributable.
                fragment=text[claim['start']:claim['end']].lstrip()
                if fragment.startswith(('$$','\\[','\\(','[MATH:')):inserted.add(claim['end'])
        # Exact native copies can exceed the structured claim count. Supplying
        # attribution does not upgrade their unassessed tail: the guard still
        # requires complete exact-copy proof or rejects the review overflow.
        for unit in units:
            if (len(claims)==100 and unit.start>=claims[-1]['end'] and
                    start<=unit.start and unit.end<=footer.start() and not unit.claim_ids and
                    not any(unit.start==heading['start'] for heading in headings) and not _inside(unit.start,protected)):
                inserted.add(unit.end)
        for offset in inserted:operations.append((offset,offset,' ['+', '.join(ids)+']'))
        scopes.append({'start':start,'end':footer.start(),'source_ids':list(ids)})
    for start,end,ids,local in _list_footer_scopes(text,records,tokens,protected):
        inserted=set()
        for claim in claims:
            if any(claim['start']<=token.start()<claim['end'] for token in local):continue
            if start<=claim['start'] and claim['end']<=end:
                fragment=text[claim['start']:claim['end']].lstrip()
                if not _inside(claim['start'],protected) or fragment.startswith(('$$','\\[','\\(','[MATH:')):
                    inserted.add(claim['end'])
        for unit in units:
            if (len(claims)==100 and unit.start>=claims[-1]['end'] and start<=unit.start and unit.end<=end
                    and not unit.claim_ids and not any(unit.start==heading['start'] for heading in headings)
                    and not _inside(unit.start,protected)):
                inserted.add(unit.end)
        operations.extend((offset,offset,' ['+', '.join(ids)+']') for offset in inserted)
        scopes.append({'start':start,'end':end,'source_ids':list(ids),'kind':'parent-list-footer'})
    for start,end,ids,local in _terminal_line_scopes(text,records,tokens,protected):
        inserted = set()
        for claim in claims:
            if start <= claim['start'] and claim['end'] <= end and not _inside(claim['start'],protected):
                if not any(claim['start'] <= token.start() < claim['end'] for token in local):
                    inserted.add(claim['end'])
        references = ' ' + ' '.join('['+eid+']' for eid in ids)
        operations.extend((offset,offset,references) for offset in inserted)
        if inserted:
            scopes.append({'start':start,'end':end,'source_ids':list(ids),'kind':'terminal-line-citation'})
    verification=text
    for start,end,replacement in sorted(set(operations),key=lambda row:(row[0],row[1]),reverse=True):
        verification=verification[:start]+replacement+verification[end:]
    final_protected=_protected_spans(verification)
    final_tokens=[token for token in _CITATION.finditer(verification) if not _inside(token.start(),final_protected)
                  and not _reference_example_line(verification,token)]
    final_claims,contexts=_answer_structure(verification)
    bindings={}
    for claim in final_claims:
        ids=[]
        for token in final_tokens:
            if claim['start']<=token.start()<claim['end']:
                known=_resolve(token.group(),records)
                ids.extend(eid for eid in known if eid not in ids)
        bindings[claim['claim_id']]=ids
    material={'version':PROJECTION_VERSION,'display':answer,'verification':verification,'bindings':bindings,
        'scopes':scopes,'unresolved_reference_count':unresolved}
    return PublicationProjection(answer,verification,_json(bindings),_json(contexts),_json(scopes),unresolved,
        hashlib.sha256(_json(material).encode()).hexdigest())
