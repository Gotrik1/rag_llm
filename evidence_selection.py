"""Select complete logical sections before assembling model evidence.

The index is a view of the existing docstore, not a second knowledge base.
Source visibility is enforced before heading lookup or relationship expansion.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

from nltk.stem.snowball import RussianStemmer

from search_focus import SearchFocus, SearchOperation

_STEMMER = RussianStemmer()
_STOP = {
    "а", "в", "во", "и", "из", "на", "по", "к", "с", "со", "для", "от", "о", "об",
    "это", "что", "как", "какой", "какая", "какие", "каких", "каком", "чем", "ли",
    "такое", "такой", "таком", "почему", "где", "когда", "есть", "надо", "можно",
    "расскажи", "объясни", "приведи", "покажи", "дай", "напиши", "полный", "все",
    "бывают", "бывает", "ведется", "ведутся", "реализованы", "система", "системе",
    "документ", "документах", "документации", "инструкция", "инструкции",
}
_EXCEPTION = re.compile(r"исключени|специальн\w* случа|особ\w* случа|частн\w* случа", re.I)
_GENERIC_STEMS = frozenset(_STEMMER.stem(word) for word in (
    "рассчитывается", "расчет", "определяется", "постановления", "методики", "сравни", "отличаются",
    "задать", "задаются", "задавать", "настроить", "настраиваются", "создать", "создаются",
    "рассчитать", "рассчитывать", "начислить", "начисляются", "применяются", "используются",
))
_CATEGORY_STEMS = frozenset(_STEMMER.stem(word) for word in ('первой', 'второй', 'третьей', 'четвертой', 'пятой', 'шестой'))
_LOOKUP_WORDS = frozenset(_STEMMER.stem(word) for word in (
    'сколько', 'какова', 'каков', 'каковы', 'стоит', 'стоить', 'стоимость', 'цена',
    'нужно', 'нужен', 'нужна', 'равен', 'равна', 'укажи', 'сообщи',
))
_V2_FILLER = frozenset(_STEMMER.stem(word) for word in (
    'пожалуйста', 'подробно', 'кратко', 'ответь', 'простыми', 'словами', 'расскажи',
    'объясни', 'смотри', 'учитывай', 'рассматривай', 'только', 'также', 'либо', 'или',
    'сравни', 'сравнение', 'разница', 'отличие', 'между', 'про', 'считается',
))
_CATEGORY_WORDS = {'1': 'первой', '2': 'второй', '3': 'третьей',
                   '4': 'четвертой', '5': 'пятой', '6': 'шестой'}
_CATEGORY_IDS = {_STEMMER.stem(word): number for number, word in _CATEGORY_WORDS.items()}
_VERSION_FIELDS = ('document_id', 'source_id', 'revision_id', 'version_id', 'document_version', 'content_hash', 'sha256')
_ORDINAL_ABBREVIATION = re.compile(r'((?:[а-яё]+\s*(?:,|и)\s*)*[а-яё]+)\s+цк\b', re.I)
_NUMERIC_ORDINAL_CATEGORY = re.compile(
    r'(?<![\w.])([1-6])(?:[ \t\u00a0\u202f]*[-\u2010\u2011][ \t\u00a0\u202f]*|[ \t\u00a0\u202f]+)'
    r'(?:ой|ая|ую|й|я|ю)[ \t\u00a0\u202f]+'
    r'(?:цк\b|ценов(?:ая|ой|ую|ою|ые|ых|ым|ыми)[ \t\u00a0\u202f]+'
    r'категори(?:я|и|ю|ей|ею|й|ям|ями|ях)\b)', re.I,
)


def _normalize_numeric_ordinal_categories(text: str) -> str:
    """Normalize an explicitly named category, never a bare ordinal or count.

    This is a lexical/scoping view only. Native evidence text is not rewritten.
    Sharing it before both category detection and stemming prevents an ordinal
    suffix and category boilerplate from competing as semantic subject words.
    """
    return _NUMERIC_ORDINAL_CATEGORY.sub(
        lambda match: _CATEGORY_WORDS[match[1]] + ' ценовой категории', text,
    )



def _expand_ordinal_categories(text: str) -> str:
    text = _normalize_numeric_ordinal_categories(text)

    def expand(match):
        words = re.findall(r'[а-яё]+', match[1].casefold().replace('ё', 'е'))
        ordinals = [_CATEGORY_IDS.get(_stem_word(word)) for word in words if word != 'и']
        if not ordinals or any(number is None for number in ordinals):
            return match[0]
        return ' '.join(_CATEGORY_WORDS[number] + ' ценовой категории' for number in ordinals)
    return _ORDINAL_ABBREVIATION.sub(expand, text)


def _retrieval_subject(question: str) -> str:
    """Decode the application's query envelope, not arbitrary topic vocabulary.

    Host labels must not become AND retrieval terms or a paragraph following
    a trailing negative verb. The envelope contract retains the actual topic,
    user focus and exclusions; no assistant facts are introduced here.
    """
    fields = {}
    labels = {
        'Тема диалога — вопрос пользователя': 'subject',
        'Условия и уточнения пользователя': 'constraints',
        'Исключить из предмета ответа': 'exclusions',
        'Текущий запрос пользователя': 'current',
    }
    for line in str(question).splitlines():
        label, separator, value = line.partition(':')
        if separator and label.strip() in labels:
            fields[labels[label.strip()]] = value.strip()
    if not fields.get('subject') or not fields.get('current'):
        return question
    clauses = list(dict.fromkeys(fields[key] for key in ('subject', 'constraints', 'current') if fields.get(key)))
    if fields.get('exclusions'):
        clauses.append('не рассматривай ' + fields['exclusions'])
    return '; '.join(clauses)


def _positive_query(question: str) -> tuple[str, list[str]]:
    """Separate retrieval subjects from explicit negative search instructions."""
    excluded = []

    def remove(match):
        excluded.append(match.group(1).strip())
        return ' '

    value = re.sub(
        r'\b(?:не[ \t]+(?:учитывай|смотри|рассматривай|включай|используй)|кроме|исключая)'
        r'[ \t]+([^;\r\n.!?,]+)', remove, _retrieval_subject(question), flags=re.I,
    )
    value = re.sub(r'\b((?:[а-яёa-z]+[ \t]+){0,1}[а-яёa-z]+)[ \t]+не[ \t]+'
                   r'(?:учитывай|смотри|рассматривай|включай|используй)\b',
                   remove, value, flags=re.I)
    return value, excluded


def _query_groups(question: str, *, subject_phrase: bool = False) -> tuple[frozenset[str], ...]:
    """Slash/parenthesized acronym spellings are alternatives, not AND terms.

    This is syntax based, not a maintained dictionary of document-specific
    acronyms. Ordinary substantive words are retained, even when unknown.
    """
    groups = []
    acronym = r'(?:[A-ZА-ЯЁ][A-ZА-ЯЁ0-9]{1,11}|[а-яёa-z]{2,5})'

    def aliases(match):
        words = re.findall(acronym, match.group(0))
        groups.append(frozenset().union(*(term_keys(word) for word in words)))
        return ' '

    value = re.sub(rf'\b{acronym}\s*(?:/\s*{acronym}|\(\s*{acronym}\s*\)|или\s+{acronym})',
                   aliases, _expand_ordinal_categories(question))
    value = re.sub(r'(?<!\d)([1-6](?:\s*(?:,|или|и)\s*[1-6])+)\s*цк',
                   lambda m: ' '.join(_CATEGORY_WORDS[n] + ' ценовой категории'
                                      for n in re.findall(r'[1-6]', m[1])), value, flags=re.I)
    value = re.sub(r'(?<!\d)([1-6])\s*[-–—]\s*([1-6])\s*цк',
                   lambda m: ' '.join(_CATEGORY_WORDS[str(n)] + ' ценовой категории'
                                      for n in range(int(m[1]), int(m[2]) + 1)), value, flags=re.I)
    keys = term_keys(value)
    if not subject_phrase:
        keys -= _V2_FILLER
        subject = keys - _LOOKUP_WORDS
        if subject:
            keys = subject
    groups.extend(frozenset((key,)) for key in sorted(keys))
    return tuple(dict.fromkeys(group for group in groups if group))


def _group_coverage(groups: tuple[frozenset[str], ...], keys: frozenset[str]) -> float:
    return sum(bool(group & keys) for group in groups) / len(groups) if groups else 0.0


def _subject_profiles(question: str, focus: SearchFocus | None = None):
    """Independent subjects/aliases are alternatives, not one giant AND query.

    These profiles only propose and rank evidence. Applicability and visibility
    always come from the original request, never from a generated subject.
    """
    if focus is not None:
        profiles = tuple(dict.fromkeys(
            groups for subject in focus.subjects
            for phrase in (subject.text, *subject.alternatives)
            if (groups := _query_groups(phrase, subject_phrase=True))
        ))
        if profiles:
            return profiles
    return (_query_groups(_positive_query(question)[0]),)


def _profile_coverage(profiles, keys, requested=frozenset(), scope=frozenset()):
    return max((_group_coverage(_applicable_groups(groups, requested, scope), keys)
                for groups in profiles), default=0.0)


def _category_ids(text: str) -> frozenset[str]:
    # Category ordinals are scoped by ЦК / ценовая категория, so an unrelated
    # phrase such as "первый платеж" does not become a tariff-category filter.
    text = _expand_ordinal_categories(text)
    numbers = set()
    for match in re.finditer(r'(?<!\d)([1-6](?:\s*(?:,|или|и|[-–—])\s*[1-6])*)\s*цк\b', text, re.I):
        values = re.findall(r'[1-6]', match[1])
        if re.search(r'[-–—]', match[1]) and len(values) == 2:
            numbers.update(str(n) for n in range(int(values[0]), int(values[1]) + 1))
        else:
            numbers.update(values)
    for match in re.finditer(r'([а-яё]+(?:\s+и\s+[а-яё]+)?)\s+ценов\w*\s+категор\w*', text, re.I):
        numbers.update(_CATEGORY_IDS[key] for key in term_keys(match[1]) if key in _CATEGORY_IDS)
    return frozenset(numbers)


def category_ids(text: str) -> frozenset[str]:
    """Explicit price-category constraints shared by planning and selection."""
    return _category_ids(text)


def _scope_categories(item: dict) -> frozenset[str]:
    for part in reversed(str(item.get('section_path') or item.get('section_title') or '').split('>')):
        categories = _category_ids(part)
        if categories:
            return categories
    # The opening establishes applicability. Later mentions can be comparisons
    # or exceptions; they must not reclassify a third-category rule as sixth.
    text = re.sub(r'\[SECTION:[^\]]*\]', '', str(item.get('text', ''))).strip()
    text = re.sub(r'^\s*\d+(?:\.\d+)*[.)]?\s+(?!цк\b)', '', text, flags=re.I)
    opening = text.split('\n\n', 1)[0].split('. ', 1)[0]
    return _category_ids(opening[:700])


def _version_compatible(first: dict, second: dict) -> bool:
    return all(not first.get(key) or not second.get(key) or first[key] == second[key]
               for key in _VERSION_FIELDS)


def _applicable_groups(groups, requested, scope):
    if not scope or len(requested) < 2:
        return groups
    other = frozenset(_STEMMER.stem(_CATEGORY_WORDS[number]) for number in requested - scope)
    return tuple(group for group in groups if not group <= other)


def _content_blocks(text: str) -> list[str]:
    """Paragraph units without splitting display equations at blank lines."""
    blocks, pending, in_display, in_native = [], [], False, False
    for paragraph in str(text).split('\n\n'):
        pending.append(paragraph)
        if paragraph.count('$$') % 2 or paragraph.count('\\[') != paragraph.count('\\]'):
            in_display = not in_display
        for marker in re.finditer(r'\[MATH:|(?<!\])\](?!\])', paragraph):
            if marker[0] == '[MATH:':
                in_native = True
            elif in_native:
                in_native = False
        if not in_display and not in_native:
            blocks.append('\n\n'.join(pending))
            pending = []
    if pending:
        blocks.append('\n\n'.join(pending))
    return [row for block in blocks if block.strip() for row in _legend_rows(block)]


def _legend_rows(block: str) -> list[str]:
    """Split explicit table rows, never a Word line break inside native math.

    A notation row retains every cell and its continuation/conditions. Requiring
    both a notation cell and multiple structural rows avoids treating narrative
    line breaks or a vertical bar inside an equation as table boundaries.
    """
    spans = list(re.finditer(r'\[MATH:\s*[^\]]+\]|\$\$.*?\$\$|\\\[.*?\\\]', block, re.S))
    starts, notation, offset = [], False, 0
    for line in block.splitlines(keepends=True):
        pipes = [offset + match.start() for match in re.finditer(r'\|', line)
                 if not any(span.start() <= offset + match.start() < span.end() for span in spans)]
        cell_start = offset
        if len(pipes) > 1 and not block[offset:pipes[0]].strip():
            cell_start = pipes[0] + 1
            pipes = pipes[1:]  # Optional Markdown opening cell border.
        if pipes:
            # A cell's notation can itself contain a Word line break. Its
            # structural separator belongs to the row that began before it.
            start = next((block.rfind('\n', 0, span.start()) + 1 for span in spans
                          if span.start() < offset < span.end()), offset)
            cell = block[cell_start if start == offset else start:pipes[0]].strip()
            if cell:
                if start not in starts:
                    starts.append(start)
                notation |= any(span.start() >= start and span.end() <= pipes[0] for span in spans)
        offset += len(line)
    if not notation or len(starts) < 2:
        return [block]
    if starts[0]:
        starts.insert(0, 0)
    return [block[start:end].rstrip('\r\n') for start, end in zip(starts, [*starts[1:], len(block)])
            if block[start:end].strip()]


def _notation_cell(text: str) -> tuple[str, str | None]:
    """A table/prose definition delimiter is outside mathematical markup."""
    start = 0
    for match in re.finditer(r'\[MATH:\s*[^\]]+\]|\$\$.*?\$\$|\\\[.*?\\\]|\||—|\s[–-]\s', text, re.S):
        if match[0] == '|':
            if not text[:match.start()].strip():
                start = match.end()
                continue
            return text[start:match.start()], text[match.end():]
        if not match[0].startswith(('[MATH:', '$$', '\\[')):
            return text[start:match.start()], text[match.end():]
    return text, None


def _notation_expressions(text: str) -> list[str]:
    """Whitespace-separated notation labels are a list, not an equation chain.

    Only the explicitly delimited definition cell uses this treatment. Native
    equations continue through the verifier's strict connected-run parser.
    """
    labels = list(re.finditer(r'\[MATH:\s*[^\]]+\]|\$\$.*?\$\$|\\\[.*?\\\]', text, re.S))
    remainder = re.sub(r'\[\[MATHML_REF:[^\]\r\n]+\]\]', '', text)
    remainder = re.sub(r'\[MATH:\s*[^\]]+\]|\$\$.*?\$\$|\\\[.*?\\\]', '', remainder, flags=re.S)
    if len(labels) > 1 and not remainder.strip():
        return [expression for label in labels for expression in _math_expressions(label[0])]
    return _math_expressions(text)


def _substantive_text(text: str, title: str = '') -> str:
    value = re.sub(r'\[SECTION:[^\]]*\]', '', str(text))
    value = re.sub(r'\[(?:ПРОПУСК АБЗАЦЕВ|[.·…\s]+)\]', '', value)
    value = re.sub(r'(?m)^\s*#{1,6}\s+[^\n]*(?:\n|$)', '', value)
    title = re.sub(r'[#*`_]+', '', title).strip()
    value = '\n'.join(line for line in value.splitlines()
                      if re.sub(r'[#*`_]+', '', line).strip() != title).strip()
    words = re.findall(r'[^\W\d_]+', value.casefold(), re.UNICODE)
    placeholders = {'где', 'формула', 'формулы', 'обозначения', 'переменные', 'утверждены', 'where', 'definition'}
    if not words or set(words) <= placeholders:
        return ''
    return value


def _section_role(item: dict) -> str:
    """Heading-level provenance hint, not a legal validity determination."""
    path = str(item.get('section_path') or item.get('section_title') or '').casefold()
    if re.search(r'истори\w*\s+(?:изменен|редакц)|журнал\w*\s+изменен|change\s*log|revision\s+history', path):
        return 'change_log'
    if re.search(r'изменени\w*\s*,?\s*которые\s+вносятся|поправк\w*\s+к\b|внесени\w*\s+изменен', path):
        return 'amendment'
    if re.search(r'архивн\w*\s+редакц|историческ\w*\s+верс|утративш\w*\s+силу', path) or item.get('chunk_type') == 'history':
        return 'historical'
    return 'operative'


def _math_expressions(text: str) -> list[str]:
    """Read only explicit formula markup; prose equalities are not a math AST."""
    from grounding_verifier import _native_math_runs

    view = str(text)
    expressions = [(run['start'], run['formula']) for run in _native_math_runs(view) if run['complete']]
    expressions.extend((match.start(), next(value for value in match.groups() if value is not None))
                       for match in re.finditer(r'\$\$(.*?)\$\$|\\\[(.*?)\\\]', view, re.S))
    return [expression for _, expression in sorted(expressions)]


def _math_symbols(expression: str) -> frozenset[str]:
    # Preserve descriptive subscripts: X_temperature and X_pressure must not
    # collapse into the same variable. This is deliberately conservative and
    # local; no synonym inference or source-specific symbol dictionary.
    value = re.sub(r'\\(?:mathrm|text|operatorname)\{([^{}]*)\}', r'\1', expression)
    symbols = re.findall(r'\\?[A-Za-zА-Яа-яЁё]+(?:(?:_sub|_sup)\([^)]*\)|_\{[^}]*\}|_\w+)*', value)
    ignored = {'sub', 'sup', 'frac', 'sum', 'sqrt', 'left', 'right', 'times', 'cdot'}
    def identity(symbol):
        def qualifier(match):
            # Tables commonly omit trailing period/entity indices. Keep the
            # descriptive first qualifier (cold != hot), never strip numbered
            # variants such as X_1 / X_2. No domain symbol names are assumed.
            descriptor = match[1].split(',', 1)[0].strip()
            descriptor = re.sub(r'(?<=\S)\s+[A-Za-zА-Яа-яЁё]$', '', descriptor)
            return '_sub(' + descriptor + ')'
        symbol = re.sub(r'_sub\(([^)]*,[^)]*)\)', qualifier, symbol)
        symbol = re.sub(r'_\{([^},]+),[^}]+\}', lambda m: '_{' + m[1] + '}', symbol)
        return re.sub(r'\s+', '', symbol).casefold()

    return frozenset(identity(symbol) for symbol in symbols
                     if symbol.casefold().lstrip('\\') not in ignored)


def _equation_guards(blocks, number):
    """Keep explicit neighboring applicability clauses indivisible with math."""
    positions = []
    conditional = r'\s*(?:При\b|Если\b|Только\b|За исключением\b)'
    if number and re.match(conditional, blocks[number - 1], re.I) and not _math_expressions(blocks[number - 1]):
        positions.append(number - 1)
    for following in range(number + 1, len(blocks)):
        if _math_expressions(blocks[following]) or not re.match(
            r'\s*(?:Где\b|Здесь\b|Обозначения\b|При\b|Если\b|Только\b|За исключением\b)',
            blocks[following], re.I,
        ):
            break
        positions.append(following)
    return positions


def _formula_dependencies(blocks, question, groups):
    """Prioritize locally defined quantities and one-hop native equations.

    Lookup words (e.g. price versus amount, concentration versus mass) matter
    here even if retrieval normalized them away. A definition row identifies
    the requested symbol; an equation *defining* it is stronger than an early
    equation merely using it. Never generate or transform a source equation.
    """
    requested = term_keys(_positive_query(question)[0]) - _V2_FILLER - _CATEGORY_STEMS
    requested -= term_keys('ценовой категории') if _category_ids(question) else frozenset()
    requested = frozenset(key for key in requested if not key.isdigit())
    if not requested:
        requested = frozenset().union(*groups) if groups else frozenset()
    definitions, equations, definition_blocks = {}, [], set()
    for number, block in enumerate(blocks):
        for line in _legend_rows(block):
            notation, description = _notation_cell(line)
            if description is None:
                continue
            # A table's cell separator is not a dangling mathematical operator.
            expressions = _notation_expressions(notation)
            if not expressions or any('=' in expression for expression in expressions):
                continue
            # Native table rows / explicit notation sentences, not incidental
            # symbol mentions in arbitrary narrative text.
            description = description.replace('|', ' ')
            relevance = len(requested & term_keys(description)) / max(1, len(requested))
            for symbol in frozenset().union(*(_math_symbols(expression) for expression in expressions)):
                definition_blocks.add(number)
                previous = definitions.get(symbol)
                if previous is None or relevance > previous[0]:
                    definitions[symbol] = (relevance, number)
        for expression in _math_expressions(block):
            if '=' not in expression:
                continue
            lhs, rhs = expression.split('=', 1)
            symbols = _math_symbols(lhs)
            if len(symbols) != 1:
                continue
            symbol = next(iter(symbols))
            # A short lead-in provides applicability and the semantic subject.
            prelude = []
            for preceding in range(number - 1, max(-1, number - 4), -1):
                if (len(blocks[preceding]) > 700 or preceding in definition_blocks or
                        _math_expressions(blocks[preceding]) or '[TABLE]' in blocks[preceding]):
                    break
                prelude.append(preceding)
            prose = ' '.join(blocks[position] for position in prelude)
            local = len(requested & term_keys(prose)) / max(1, len(requested))
            direct = definitions.get(symbol, (0, number))[0]
            relevance = 3 * direct + local
            guards = _equation_guards(blocks, number)
            equations.append({'block': number, 'symbol': symbol, 'rhs': _math_symbols(rhs),
                              'score': relevance, 'core': sorted([*prelude, number, *guards]),
                              'expression': re.sub(r'\s+', '', expression)})
    if not equations:
        return [], definitions
    best = max(equation['score'] for equation in equations)
    if best <= 0:
        return [], definitions
    roots, seen = [], set()
    for equation in sorted(equations, key=lambda item: (-item['score'], item['block'])):
        if equation['score'] < max(0.65, best - 0.25) or equation['expression'] in seen:
            continue
        roots.append(equation)
        seen.add(equation['expression'])
        if len(roots) == 3:
            break
    # One edge only: no recursive symbolic traversal, and no cross-section,
    # version, source or ACL widening. Keep root equations ahead of supports.
    dependencies, seen_blocks = [], {item['block'] for item in roots}
    for root in roots:
        for equation in equations:
            if equation['symbol'] in root['rhs'] and equation['block'] not in seen_blocks:
                dependencies.append(equation)
                seen_blocks.add(equation['block'])
                if len(dependencies) == 3:
                    break
        if len(dependencies) == 3:
            break
    return roots + dependencies, definitions


def _scoped_cohorts(blocks, question, groups):
    """Keep a compact applicability clause and its native enumeration atomic."""
    requested = _category_ids(question)
    overview = bool(re.search(r'какие|перечисл|обзор|виды|состав|структур|этапы|что\s+входит', question, re.I))
    category_keys = _CATEGORY_STEMS | term_keys('ценовая категория')
    if not overview and any(not group <= category_keys for group in groups):
        return []

    def mode_ids(value):
        # Explicit local identifiers, not a maintained list of mode names.
        return {match[1].casefold() for match in re.finditer(
            r'\b(?i:режим\w*|вариант\w*|mode|variant)\s+([A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9_-]{0,20}|\d+)\b', value,
        )}

    requested_modes = mode_ids(question)
    cohorts = []
    for number, block in enumerate(blocks):
        if len(block) > 700 or _math_expressions(block) or '[TABLE]' in block:
            continue
        if not re.match(r'\s*(?:Применени[ея]\b|Для\b|Режим\b|При\b)', block, re.I):
            continue
        scope = _category_ids(block)
        if requested and (not scope or not requested & scope):
            continue
        modes = mode_ids(block)
        if requested_modes and modes and not requested_modes & modes:
            continue
        if not requested and not _group_coverage(groups, term_keys(block)):
            continue
        core, count = [number], 0
        for following in range(number + 1, len(blocks)):
            value = blocks[following]
            if re.match(r'\s*(?:Где\b|Здесь\b|Обозначения\b|#{1,6}\s|\[TABLE\]|Применени[ея]\b|Режим\b|Для\b|При\b)', value, re.I):
                break
            if len(value) < 100 and re.fullmatch(r'\s*(?:Формул[аы]|Расчет|Состав)\s*:\s*', value, re.I):
                core.append(following)
                continue
            expressions = _math_expressions(value)
            equations = sum('=' in expression for expression in expressions)
            list_items = len(re.findall(r'(?m)^\s*(?:[-*•]|\d+[.)])\s+\S', value))
            if '|' in value or not (equations or list_items):
                break
            core.append(following)
            count += max(equations, list_items)
            if count > 8 or sum(len(blocks[position]) for position in core) > 2000:
                break
        if count < 2:
            continue
        size = sum(len(blocks[position]) for position in core)
        cohorts.append({'anchor': number, 'core': core, 'scope_categories': sorted(scope),
                        'bounded': count <= 8 and size <= 2000})
        if len(cohorts) == 3:
            break
    return cohorts


def _focused_excerpt(text, budget, groups, facet_groups, *, question='', diagnostics=None,
                     subject_profiles=None, formula_subjects=None):
    """Pack relevant whole paragraphs with neighbors, in source order.

    A 50k collapsed Markdown section must not contribute its unrelated preface
    merely because the actual matching paragraph is near the end. Keep equation
    blocks atomic and explicitly mark omissions; this is an excerpt, not the
    complete logical section.
    """
    if len(text) <= budget:
        return text, []
    blocks = _content_blocks(text)
    keys = [term_keys(re.sub(r'\[SECTION:[^\]]*\]', '', block)) for block in blocks]
    profiles = subject_profiles or (groups,)
    scores = [2 * _profile_coverage(profiles, value) + max(
        (_group_coverage(facet, value) for facet in facet_groups.values() if facet), default=0,
    ) for value in keys]
    order = sorted(range(len(blocks)), key=lambda number: (-scores[number], number))
    selected, size = set(), 0
    # Leave room for omission markers, which are metadata-like labels, not
    # invented source content. A prefix-only excerpt needs no extra markers.
    marker = '\n\n[ПРОПУСК АБЗАЦЕВ]\n\n'
    guarded = {number: _equation_guards(blocks, number) for number, block in enumerate(blocks)
               if any('=' in expression for expression in _math_expressions(block))}
    cohorts = _scoped_cohorts(blocks, question, groups)
    atomic = {position: cohort['core'] for cohort in cohorts if cohort['bounded'] for position in cohort['core']}

    def reserve(positions):
        nonlocal size
        positions = set(positions)
        positions.update(member for position in list(positions) for member in atomic.get(position, ()))
        positions.update(guard for position in list(positions) for guard in guarded.get(position, ()))
        positions -= selected
        cost = sum(len(blocks[position]) + len(marker) for position in positions)
        if size + cost > budget:
            return False
        selected.update(positions)
        size += cost
        return True

    cohort_blocks, incomplete_cohorts = [], []
    for cohort in cohorts:
        if cohort['bounded'] and reserve(cohort['core']):
            cohort_blocks.append(cohort['core'])
        else:
            incomplete_cohorts.append({'anchor': cohort['anchor'], 'scope_categories': cohort['scope_categories'],
                                       'reason': 'payload-budget' if cohort['bounded'] else 'cohort-limit'})
    # Formatting instructions must not dilute the requested mathematical
    # subject. Keep the user question separately for category/condition cohorts.
    if formula_subjects:
        # Clean phrases retain price/quantity and other semantic nouns. Probe
        # each independently, interleaving their packages under the same six-
        # package bound instead of making one comparison side own the budget.
        probes, definitions = [], {}
        for phrase in formula_subjects:
            found, defined = _formula_dependencies(blocks, phrase, _query_groups(phrase, subject_phrase=True))
            probes.append(found)
            for symbol, definition in defined.items():
                if symbol not in definitions or definition[0] > definitions[symbol][0]:
                    definitions[symbol] = definition
        packages, seen_packages = [], set()
        for number in range(max((len(probe) for probe in probes), default=0)):
            for probe in probes:
                if number < len(probe) and probe[number]['block'] not in seen_packages:
                    packages.append(probe[number])
                    seen_packages.add(probe[number]['block'])
            if len(packages) >= 6:
                break
        packages = packages[:6]
    else:
        packages, definitions = _formula_dependencies(blocks, question, groups)
    for _, number in definitions.values():
        guarded[number] = sorted({*guarded.get(number, ()), *_equation_guards(blocks, number)})
    formula_blocks = []
    # The mandatory equation + its short applicability lead-in is reserved
    # before generic lexical matches / huge notation tables consume the cap.
    for package in packages:
        if reserve(package['core']):
            formula_blocks.append(package['block'])
    # Definitions are supporting context, not a substitute for an equation.
    # They receive budget only after the defining equation packets are safe.
    for package in packages:
        if package['block'] not in formula_blocks:
            continue
        for symbol in sorted({package['symbol'], *package['rhs']}):
            if symbol in definitions:
                number = definitions[symbol][1]
                reserve([number, *_equation_guards(blocks, number)])
    if diagnostics is not None:
        diagnostics['scoped_cohort_blocks'] = cohort_blocks
        diagnostics['scoped_cohort_incomplete'] = incomplete_cohorts
        diagnostics['formula_dependency_blocks'] = formula_blocks
        diagnostics['formula_dependency_max_hops'] = 1
    for number in order:
        # Adjacent notation/conditions are more valuable than an unrelated
        # introduction. The anchor paragraph gets budget before its neighbors.
        for position in (number, number - 1, number + 1, number + 2):
            if position < 0 or position >= len(blocks) or position in selected:
                continue
            reserve([position])
    if diagnostics is not None:
        # These are locally recognized definitions lost during packing, not a
        # certification that every source symbol has a definition or meaning.
        missing = {symbol for package in packages if package['block'] in selected
                   for symbol in {package['symbol'], *package['rhs']}
                   if symbol in definitions and
                   not {definitions[symbol][1], *guarded.get(definitions[symbol][1], ())} <= selected}
        diagnostics['definition_closure_incomplete'] = bool(missing)
        diagnostics['missing_definition_symbols'] = sorted(missing)
    if not selected:
        return '', []
    ordered = sorted(selected)
    result = blocks[ordered[0]]
    for previous, number in zip(ordered, ordered[1:]):
        result += ('\n\n' if number == previous + 1 else marker) + blocks[number]
    return result, ordered


def _reference_clauses(text: str) -> list[dict]:
    """Extract explicit local references, never infer a semantic graph edge."""
    references = []
    for block in _content_blocks(text):
        # Paragraphs retain the component/conditions governing an analogy.
        # Limiting the quote bounds metadata, not the document's truth scope.
        if len(block) > 2400:
            continue
        for match in re.finditer(r'\b(?:см\.?\s*|в\s+|по\s+|согласно\s+)?'
                                 r'(?:раздел\w*|подпункт\w*|пункт\w*|п\.)\s+(\d+(?:\.\d+)*)', block, re.I):
            prefix = block[:match.start()]
            if match.start() == 0 and not re.search(r'см\.?|согласно|\bв\s|\bпо\s', match[0], re.I):
                continue  # A section heading is not a link to itself.
            if prefix or re.search(r'см\.?|согласно|\bв\s|\bпо\s', match[0], re.I):
                references.append({'kind': 'section', 'locator': match[1], 'quote': block})
        for match in re.finditer(r'\b(?:аналогично|также\s+как|так\s+же\s+как|как)\s+(?:и\s+)?(?:для\s+)?', block, re.I):
            after = block[match.end():]
            target = re.match(r'(?:[1-6](?:\s*(?:,|или|и|[-–—])\s*[1-6])*\s*цк\b'
                              r'|[а-яё]+(?:\s+(?:или|и)\s+[а-яё]+)?\s+(?:цк\b|ценов\w*\s+категор\w*))', after, re.I)
            if target and _category_ids(target[0]):
                references.append({'kind': 'category', 'categories': sorted(_category_ids(target[0])), 'quote': block})
    # Duplicate source spellings must not consume the bounded target budget.
    return list({(ref['kind'], ref.get('locator'), tuple(ref.get('categories', ())), ref['quote']): ref
                 for ref in references}.values())


def _section_locator(item: dict) -> str:
    leaf = str(item.get('section_title') or item.get('section_path', '').rsplit('>', 1)[-1]).strip(' #*')
    match = re.match(r'(?:пункт\s+|подпункт\s+|раздел\s+)?(\d+(?:\.\d+)*)(?!\d)', leaf, re.I)
    return match[1] if match else ''


def _sibling_family(sections, candidates, groups, visible, requested):
    """Find one compact, topic-governed family, not arbitrary neighboring hits.

    Two independently relevant children or an exact governing topic parent
    are required. The parent's own topic must cover the full requested subject:
    one named method must not import all other methods sharing an ancestor.
    """
    structural = term_keys('виды способы методы алгоритмы порядок основные возможные выполняется проводится осуществляется работает')
    subject = tuple(group for group in groups if not group <= structural)
    if not subject:
        return None
    seeds, governing = {}, set()
    for item in candidates:
        path = str(item.get('section_path', ''))
        if item.get('reference_from') or item['evidence_role'] == 'exception':
            continue
        revision = tuple(str(item.get(field) or '') for field in _VERSION_FIELDS)
        if _group_coverage(subject, term_keys(path.rsplit(' > ', 1)[-1])) == 1:
            key = source_key(item['file']), path, revision
            governing.add(key)
            seeds.setdefault(key, []).append(item)
        if ' > ' in path and (item['selection_reason'] == 'subject-child'
                               or max(item['_heading_coverage'], item['_local_coverage']) >= 0.65):
            parent = path.rsplit(' > ', 1)[0]
            if _group_coverage(subject, term_keys(parent.rsplit(' > ', 1)[-1])) == 1:
                seeds.setdefault((source_key(item['file']), parent, revision), []).append(item)
    choices = []
    for (file, parent, revision), members in seeds.items():
        if (file, parent, revision) not in governing and len({item['section_path'] for item in members}) < 2:
            continue
        children = {}
        for child in sections:
            if source_key(child['file']) != file or not visible(child) or not _version_compatible(members[0], child):
                continue
            path = str(child.get('section_path', ''))
            if not path.startswith(parent + ' > ') or ' > ' in path[len(parent) + 3:]:
                continue
            scope = _scope_categories(child)
            if requested and scope and not requested & scope:
                continue
            if evidence_role(child) == 'exception' or not _substantive_text(child.get('text', ''), child.get('section_title', '')):
                continue
            if any(not _version_compatible(child, previous) for previous in children.values()):
                children = {}  # An unknown-version seed cannot join conflicting revisions.
                break
            children.setdefault(child['section_path'], child)
        if len(children) < 2:
            continue
        ordered = sorted(children.values(), key=lambda child: child.get('position', 100000))
        identity = hashlib.sha256((file + '\n' + parent + '\n' + repr(revision)).encode()).hexdigest()[:16]
        choices.append({
            'id': identity, 'file': members[0]['file'], 'parent_section_path': parent, 'children': ordered,
            'total_chars': sum(len(child['text']) for child in ordered),
            'score': sum(item['_score'] for item in members) / len(members) + min(3, len(members)),
        })
    return max(choices, key=lambda family: family['score']) if choices else None


@lru_cache(maxsize=65536)
def _stem_word(word: str) -> str:
    # Regulations repeat the same vocabulary across thousands of passages.
    # Cache word morphology, not only whole passages, to bound cold CPU work.
    return _STEMMER.stem(word)


@lru_cache(maxsize=8192)
def term_keys(text: str) -> frozenset[str]:
    text = _normalize_numeric_ordinal_categories(text)
    # Leading section numbers are not topics, but "2 ЦК" is a topic with an
    # essential category number. Never normalize it to the ambiguous "ЦК".
    text = re.sub(r"^\s*\d+(?:\.\d+)*[.)]?\s+(?!цк\b)", "", text, flags=re.I)
    category_words = {"1": "первой", "2": "второй", "3": "третьей", "4": "четвертой", "5": "пятой", "6": "шестой"}
    text = re.sub(r"([1-6])\s+и\s+([1-6])\s*цк", lambda m: f"{category_words[m[1]]} ценовой категории и {category_words[m[2]]} ценовой категории", text, flags=re.I)
    text = re.sub(r"(?<!\d)([1-6])\s*цк", lambda m: f"{category_words[m[1]]} ценовой категории", text, flags=re.I)
    words = re.findall(r"[а-яёa-z]+|\d+", text.casefold().replace("ё", "е"))
    return frozenset(_stem_word(word) for word in words if word not in _STOP) - _GENERIC_STEMS


def source_key(value: object) -> str:
    return Path(str(value or "")).name.casefold()


def requested_source_kind(question: str) -> str | None:
    value = question.casefold()
    if re.search(r"(?:по|согласно|из|смотреть|только)\s+(?:норм\w*\s+)?постановлен\w*", value):
        return "постановлен"
    if re.search(r"(?:по|согласно|из|только)\s+методик\w*", value):
        return "методик"
    return None


def evidence_role(item: dict) -> str:
    leaf = str(item.get("section_title") or item.get("section_path", "").rsplit(">", 1)[-1])
    if _EXCEPTION.search(leaf):
        return "exception"
    prefix = re.sub(r"\[SECTION:[^\]]*\]", "", str(item.get("text", ""))).strip()[:250]
    if re.match(r"(?:В виде исключения|За исключением|В особых случаях|Только для)\b", prefix, re.I):
        return "exception"
    if re.search(r"определени|термин|виды|назначени", leaf, re.I):
        return "definition"
    return "main"


def evidence_id(item: dict) -> str:
    return hashlib.sha256((source_key(item.get("file")) + "\n" + str(item.get("text", ""))).encode()).hexdigest()



def _inventory_subject(positive: str, groups=None, *, enumerate_intent=False):
    """Prepare the inventory profile once, separately from domain subjects."""
    if not enumerate_intent and not re.search(r'какие|перечисл|справочник|перечень|список', positive, re.I):
        return ()
    groups = _query_groups(positive) if groups is None else groups
    kinds = term_keys('виды типы')
    if not enumerate_intent and not any(group & kinds for group in groups):
        return ()
    framing = term_keys('виды типы перечисли перечислить перечень список справочник '
                        'полный весь основные объясни объяснить различия между ними '
                        'включая не только категории')
    return tuple(group for group in groups if not group <= framing)


def _inventory_enum_rows(item: dict, question: str = '', *, prepared_subject=None,
                         enumerate_intent=False) -> list[str]:
    """Atomic matching tables and surrounding source prose, not guessed values.

    The leaf/nearest parent must govern the subject. A condition can follow
    the choices or occupy a separate table row, so rows of a matching table
    are never pruned. All surrounding prose in this section is mandatory.
    Native/display math is never split by this plain-table path. This is a
    structural closure, not a proof that external dependencies are complete.
    """
    subject = prepared_subject
    if subject is None:
        subject = _inventory_subject(_positive_query(question)[0])
    if not subject:
        return []
    context = ' > '.join(str(item.get('section_path', '')).split(' > ')[-2:])
    context_keys = term_keys(context)
    if not enumerate_intent and _group_coverage(subject, context_keys) != 1:
        return []
    kinds, found, matched = term_keys('виды типы'), [], False
    for block in _content_blocks(str(item.get('text', ''))):
        if '[TABLE]' not in block:
            found.append(block)
            continue
        if re.search(r'\[MATH:|\$\$|\\\[', block):
            continue
        starts, offset = [], 0
        for line in block.splitlines(keepends=True):
            # Only explicit row separators are boundaries. Wrapped lines and
            # their source conditions remain in the same row.
            if '|' in line and line.strip().strip('|').strip():
                starts.append(offset)
            offset += len(line)
        rows = [block[start:end].rstrip('\r\n')
                for start, end in zip(starts, [*starts[1:], len(block)])]
        matching = []
        for row in rows:
            label, separator, value = row.lstrip(' \t|').partition('|')
            if not separator:
                continue
            if enumerate_intent:
                # A subject can be split between a governing heading and the
                # field label. This is still a source-anchored finite list,
                # never an inventory inferred from incidental body mentions.
                if _group_coverage(subject, context_keys | term_keys(label)) != 1:
                    continue
            elif not term_keys(label) & kinds:
                continue
            marker = re.search(r'\b(?:выбор\s+из\s+списка|допустим\w*\s+значени\w*)\s*:', value, re.I)
            if not marker or re.search(r'\bнапример\b|\b(?:и|возможны)\s+други\w*\b|и\s+т\.\s*д\.', value, re.I):
                continue
            options = [part.strip() for part in value[marker.end():].split(';') if part.strip()]
            if len(options) >= 2:  # No expected count or item vocabulary.
                matching.append(row)
        if matching:
            # A different named or empty-label row may constrain these same
            # choices. No keyword guess can establish that it is disposable.
            found.append(block)
            matched = True
    # Identical wording can govern different following tables. Deduplicating
    # it would change the source's scope/order, not merely save characters.
    return found if matched else []

class SectionEvidenceIndex:
    """Group split paragraphs by physical document and complete section path."""

    def __init__(self, nodes: list):
        # An inverted body index also covers documents with no headings.
        # Russian stemming avoids losing a fact when ящик becomes ящика.
        self.body_items = []
        self.body_postings: dict[str, set[int]] = {}
        for node in nodes:
            metadata = node.metadata or {}
            text = node.text or ''
            file = str(metadata.get('file_name', metadata.get('file_path', '')))
            if not file or not text.strip() or metadata.get('status') == 'deleted':
                continue
            position = len(self.body_items)
            self.body_items.append({**metadata, 'file': file, 'text': text})
            for key in term_keys(re.sub(r'\[SECTION:[^\]]*\]', '', text)):
                self.body_postings.setdefault(key, set()).add(position)
        groups: OrderedDict[tuple[str, str], dict] = OrderedDict()
        for position, node in enumerate(nodes):
            metadata = node.metadata or {}
            path = str(metadata.get("section_path", "")).strip()
            marker = re.search(r"\[SECTION:\s*([^\]]+)\]", node.text or "")
            if marker and path.endswith("..."):
                path = marker.group(1).strip()
            file = str(metadata.get("file_name", metadata.get("file_path", "")))
            if not path or not file or metadata.get("status") == "deleted":
                continue
            key = (source_key(file), path)
            group = groups.setdefault(key, {
                **metadata, "file": file, "section_path": path,
                "section_title": metadata.get("section_title") or path.rsplit(">", 1)[-1].strip(),
                "position": position, "parts": [], "seen": set(),
            })
            body = re.sub(r"\[SECTION:[^\]]*\]\s*", "", node.text or "").strip()
            if body and body not in group["seen"]:
                group["parts"].append(body)
                group["seen"].add(body)
        self.sections = []
        for group in groups.values():
            parts = group.pop("parts")
            group.pop("seen")
            if not parts:
                continue
            group["text"] = "\n\n".join(parts)
            group["heading_keys"] = term_keys(str(group["section_title"]))
            # Normative point numbers identify locations, not their subject.
            # The first paragraph of a point carries its topic; later text may
            # contain rare exceptions and must not override that main topic.
            if re.fullmatch(r"Пункт \d+(?:\.\d+)*", group["section_path"].rsplit(" > ", 1)[-1]):
                group["heading_keys"] = term_keys(group["text"].split("\n\n", 1)[0][:500])
            self.sections.append(group)

    def body_seeds(self, question: str, limit: int = 24, *, mode: str = 'legacy',
                   search_focus: SearchFocus | None = None,
                   constraint_question: str | None = None) -> list[dict]:
        """Find positive subject matches, independently of vector similarity.

        Generic price/action words cannot anchor a match. Require at least two
        subject words and a rare anchor, so incidental mentions in a large
        manual do not swamp an exact fact in a small document.
        """
        if mode == 'coverage':
            return self._body_seeds_v2(question, limit, search_focus=search_focus,
                                       constraint_question=constraint_question)
        keys = term_keys(question) - _LOOKUP_WORDS
        if len(keys) < 2:
            return []
        count = max(1, len(self.body_items))
        weights = {key: math.log1p(count / (1 + len(self.body_postings.get(key, ())))) for key in keys}
        positions = set().union(*(self.body_postings.get(key, set()) for key in keys))
        results = []
        for position in positions:
            item = self.body_items[position]
            matched = {key for key in keys if position in self.body_postings.get(key, ())}
            rare_anchor = any(len(self.body_postings[key]) <= max(2, count * 0.1) for key in matched)
            if len(matched) < 2 or len(matched) / len(keys) < 0.75 or not rare_anchor:
                continue
            coverage = len(matched) / len(keys)
            score = 3.0 + coverage + min(2.0, sum(weights[key] for key in matched) / len(keys))
            results.append({**item, 'score': round(score, 4),
                            'matched_queries': ['body-lexical'],
                            'lexical_subject_keys': sorted(keys), 'lexical_coverage': coverage})
        return sorted(results, key=lambda item: (-item['score'], len(item['text']), source_key(item['file'])))[:limit]

    def _body_seeds_v2(self, question: str, limit: int, *, search_focus: SearchFocus | None = None,
                       constraint_question: str | None = None) -> list[dict]:
        profiles = _subject_profiles(question, search_focus)
        if not any(profiles) or limit <= 0:
            return []
        authority = constraint_question if constraint_question is not None else question
        requested = _category_ids(_positive_query(authority)[0])
        count = max(1, len(self.body_items))
        results = {}
        for groups in profiles:
            if not groups:
                continue
            postings = [set().union(*(self.body_postings.get(key, ()) for key in group)) for group in groups]
            for position in set().union(*postings):
                item = self.body_items[position]
                scope = _scope_categories(item)
                if requested and scope and not requested & scope:
                    continue
                applicable = _applicable_groups(groups, requested, scope)
                eligible = {number for number, group in enumerate(groups) if group in applicable}
                matched = [number for number, posting in enumerate(postings) if position in posting]
                coverage = len(set(matched) & eligible) / max(1, len(eligible))
                # Keep the same rare-anchor gate per independent subject.
                rare = any(len(postings[number]) <= max(2, count * 0.1) for number in matched)
                if coverage < 0.65 or not rare:
                    continue
                if position not in results or coverage > results[position]['lexical_coverage']:
                    results[position] = {**item, 'score': round(3 + coverage, 4),
                                         'matched_queries': ['body-coverage'],
                                         'lexical_subject_groups': [sorted(group) for group in groups],
                                         'lexical_coverage': coverage}
        return sorted(results.values(), key=lambda item: (-item['score'], len(item['text']), source_key(item['file'])))[:limit]

    def _coverage_sections(self) -> list[dict]:
        """Keep legacy assembly, repairing only actual revision collisions.

        Most indexed filenames already identify a physical upload. Older
        indexes can carry two revisions under the same filename and section;
        do not concatenate those versions in the new evidence path.
        """
        if hasattr(self, '_coverage_section_cache'):
            return self._coverage_section_cache
        versions = {}
        positions = {id(item): position for position, item in enumerate(self.body_items)}
        for item in self.body_items:
            path = str(item.get('section_path', '')).strip()
            if path:
                key = source_key(item['file']), path
                signature = tuple(str(item.get(field) or '') for field in _VERSION_FIELDS)
                versions.setdefault(key, {}).setdefault(signature, []).append(item)
        collisions = {key: values for key, values in versions.items() if len(values) > 1}
        sections = [item for item in self.sections
                    if (source_key(item['file']), item['section_path']) not in collisions]
        for values in collisions.values():
            for items in values.values():
                first = items[0]
                parts = list(dict.fromkeys(re.sub(r'\[SECTION:[^\]]*\]\s*', '', str(item['text'])).strip()
                                           for item in items))
                section = {**first, 'text': '\n\n'.join(part for part in parts if part),
                           'section_title': first.get('section_title') or first['section_path'].rsplit('>', 1)[-1].strip(),
                           'position': positions[id(first)]}
                section['heading_keys'] = term_keys(section['section_title'])
                if re.fullmatch(r'Пункт \d+(?:\.\d+)*', section['section_title']):
                    section['heading_keys'] = term_keys(section['text'].split('\n\n', 1)[0][:500])
                sections.append(section)
        self._coverage_section_cache = sections
        return sections

    def select_v2(
        self, question: str, ranked: list[dict], *, excluded_sources: set[str] | None = None,
        allowed_sources: set[str] | None = None, top_k: int = 10, max_chars: int = 36000,
        facets: list[str] | None = None,
        resolve_references: bool | None = None,
        selection_meta: dict | None = None,
        search_focus: SearchFocus | None = None,
        constraint_question: str | None = None,
    ) -> list[dict]:
        """Coverage-aware evidence packing; legacy ``select`` remains unchanged.

        Rankings are evidence, not a hard gate. Heading hits, positive body
        matches and already ranked semantic hits compete in the same pool.
        Explicit facets and overview siblings get coverage reservations instead
        of a document-diversity quota. Conditions/exceptions are retained but
        cannot displace a primary rule unless the question asks for them.
        """
        if selection_meta is not None:
            selection_meta.pop('inventory_enumeration_incomplete', None)
            selection_meta.pop('inventory_enumeration_scoped_warnings', None)
        if top_k <= 0 or max_chars <= 0:
            return []
        reference_enabled = (os.getenv('RAG_REFERENCE_EXPANSION', 'off').casefold() in {'on', 'true', '1'}
                             if resolve_references is None else resolve_references)
        excluded = {source_key(value) for value in (excluded_sources or ())}
        allowed = {source_key(value) for value in (allowed_sources or ())}
        authoritative = constraint_question if constraint_question is not None else question
        kind = requested_source_kind(authoritative)
        positive, forbidden = _positive_query(authoritative)
        profiles = _subject_profiles(question, search_focus)
        groups = profiles[0]
        requested = _category_ids(positive)
        positive_keys = frozenset().union(*(group for profile in profiles for group in profile))
        constraint_groups = _query_groups(positive)
        constraint_keys = frozenset().union(*constraint_groups) if constraint_groups else frozenset()
        # Preserve literal named subjects such as a device/model name. Only
        # enforce them when at least one actual candidate carries the anchor;
        # otherwise semantic retrieval is still allowed to resolve a synonym.
        literal_anchors = term_keys(' '.join(match[0] for match in re.finditer(
            r'\b[А-ЯЁA-Z][а-яёa-z]{2,}\b', positive,
        ) if match.start() > 0)) - _V2_FILLER - _LOOKUP_WORDS
        forbidden_keys = frozenset().union(*(term_keys(value) for value in forbidden)) - constraint_keys
        contrast_keys = term_keys(' '.join(re.findall(
            r'\b[а-яё]+(?:ую|ая|ое|ые|ый|ий|ыми|ими)\b', positive, re.I,
        ))) - _CATEGORY_STEMS - _V2_FILLER
        enumerate_intent = search_focus is not None and search_focus.operation == SearchOperation.ENUMERATE
        overview = (enumerate_intent or bool(re.search(
            r'какие|обзор|перечисл|из\s+чего|состав|структур|что\s+ведется', positive, re.I)))
        wants_exception = bool(_EXCEPTION.search(positive))
        historical_intent = bool(re.search(r'истори|раньше|ранее|изменил|изменен|предыдущ|стар\w*\s+редакц|по\s+состоянию', positive, re.I))
        # Full requirements stay with coverage/writer. Only typed subjects may
        # reserve lexical payload slots when the planner supplied this contract.
        focus_facets = ({f'subject:{number}': _subject_profiles('', SearchFocus(
            operation=search_focus.operation, subjects=(subject,)))
                         for number, subject in enumerate(search_focus.subjects)}
                        if search_focus is not None else {})
        facet_groups = ({label: values[0] for label, values in focus_facets.items()}
                        if search_focus is not None else
                        {label: _query_groups(_positive_query(label)[0])
                         for label in (facets or ()) if str(label).strip()})
        if len(requested) > 1:
            facet_groups.update({f'{number} ЦК': _query_groups(f'{number} ЦК') for number in sorted(requested)})

        def facet_coverage(label, keys):
            return _profile_coverage(focus_facets.get(label, (facet_groups[label],)), keys)

        def visible(item):
            key = source_key(item.get('file'))
            return bool(key) and key not in excluded and (allowed_sources is None or key in allowed) and (
                not kind or kind in str(item.get('file', '')).casefold()
                or (kind == 'постановлен' and bool(re.search(r'(?:^|[\W_])пп[\W_]*\d', key)))
            ) and item.get('status') != 'deleted'

        sections = self._coverage_sections()
        section_lookup = {}
        for item in sections:
            section_lookup.setdefault((source_key(item['file']), item['section_path']), []).append(item)
        version_preferences = {}
        for item in ranked:
            if any(item.get(field) for field in _VERSION_FIELDS):
                key = source_key(item.get('file')), str(item.get('section_path', ''))
                if not section_lookup.get(key) or any(_version_compatible(item, known) for known in section_lookup[key]):
                    version_preferences.setdefault(key, []).append(item)
        pool, analyses = {}, {}

        def offer(candidate, reason, rank=None, structural_facet=None, reference=None):
            if not visible(candidate) or not str(candidate.get('text', '')).strip():
                return
            location = source_key(candidate['file']), str(candidate.get('section_path', ''))
            versions = section_lookup.get(location, [])
            compatible = [item for item in versions if _version_compatible(candidate, item)]
            if versions and not compatible:
                # Never reconstruct an old revision using a new revision's
                # neighboring paragraphs, or carry stale hits into the answer.
                return
            if version_preferences.get(location) and not any(
                _version_compatible(candidate, preferred) for preferred in version_preferences[location]
            ):
                return
            section = compatible[0] if len(compatible) == 1 else None
            item = {**candidate, **section} if section else dict(candidate)
            if section is not None:
                # A former excerpt is now a fresh complete native section.
                # Earlier budget/family annotations describe another packing
                # snapshot and must not survive this reconstruction.
                for transient in ('evidence_excerpt', 'evidence_truncated', 'evidence_block_indices',
                                  'selection_signals', 'coverage_facets', 'evidence_family', 'evidence_family_id'):
                    item.pop(transient, None)
            if not _substantive_text(str(item.get('text', '')), str(item.get('section_title', ''))):
                return
            scope = _scope_categories(item)
            if requested and scope and not requested & scope and reference is None:
                return
            identity = evidence_id(item)
            analysis = analyses.get(identity)
            if analysis is None:
                body = term_keys(re.sub(r'\[SECTION:[^\]]*\]', '', str(item.get('text', ''))))
                heading = item.get('heading_keys') or term_keys(str(item.get('section_title', '')))
                blocks = _content_blocks(str(item.get('text', '')))
                block_keys = [term_keys(block) for block in blocks]
                analysis = body, heading, blocks, block_keys
                analyses[identity] = analysis
            body, heading, blocks, block_keys = analysis
            body_coverage = _profile_coverage(profiles, body, requested, scope)
            heading_coverage = _profile_coverage(profiles, heading, requested, scope)
            local_coverage = max((_profile_coverage(profiles, keys, requested, scope) for keys in block_keys), default=0)
            if forbidden_keys & (body | heading) and not constraint_keys & (body | heading) - forbidden_keys:
                return
            # A forbidden-only comparator commonly shares a noun ("мощность")
            # with the desired topic. Require its positive distinguishing term.
            discriminators = constraint_keys - frozenset().union(*(term_keys(value) for value in forbidden)) if forbidden else constraint_keys
            discriminators = contrast_keys or (discriminators - _CATEGORY_STEMS - term_keys('ценовая категория'))
            if forbidden_keys and forbidden_keys <= heading and not discriminators & heading:
                return  # An explicitly excluded child heading is not a mixed-topic source paragraph.
            if forbidden_keys & (body | heading) and discriminators and not discriminators & (body | heading):
                return
            if reason not in {'subject-child', 'inventory-enumeration'} and rank is None and reference is None and max(body_coverage, heading_coverage) < 0.65:
                return
            support = pool.get(identity)
            covered = {label for label, keys in facet_groups.items()
                       if keys and any(facet_coverage(label, value | heading) >= 0.7 for value in block_keys)}
            if structural_facet:
                covered.add(structural_facet)
            # Whole-section word unions reward collapsed books: unrelated
            # paragraphs can jointly match every term. Prefer local cooccurrence
            # and a focused heading; raw backend scores remain incomparable.
            specificity = len(positive_keys & heading) / max(1, len(heading))
            score = 1.0 * body_coverage + 1.4 * local_coverage + 1.8 * heading_coverage + 1.2 * specificity
            if reason == 'body-coverage':
                score += 1.1 * body_coverage
            if rank is not None:
                # Bounded rank support avoids making incomparable vector,
                # lexical and reranker score scales additive by accident.
                score += 1.8 / (1 + rank / 6)
            if search_focus is not None and literal_anchors:
                # USER-named objects remain a soft discriminator even if a
                # model over-generalizes the subject. Generated names never
                # enter this signal, nor veto semantic/synonym evidence.
                score += 2.0 * len(literal_anchors & (body | heading)) / len(literal_anchors)
            if reason == 'subject-child':
                score += 0.8
            if reference is not None:
                score += 1.3
            role = evidence_role(item)
            if role == 'exception' and not wants_exception:
                score -= 2.0
            if support:
                support['_facets'].update(covered)
                support['_score'] = max(support['_score'], score)
                support['_rank'] = min(support['_rank'], rank if rank is not None else 100000)
                if reason == 'subject-heading':
                    support['selection_reason'] = reason
                if reference is not None:
                    support.setdefault('reference_from', []).append(reference['from'])
                    support['reference_target'] = reference['target']
                return
            pool[identity] = {**item, 'selection_reason': reason, 'evidence_role': role,
                              'section_role': _section_role(item), 'section_role_basis': 'heading-pattern-only',
                              'scope_categories': sorted(scope), '_facets': covered,
                              '_body_coverage': body_coverage, '_heading_coverage': heading_coverage,
                              '_local_coverage': local_coverage, '_heading_specificity': specificity,
                              '_anchors': literal_anchors & (body | heading),
                              '_score': score, '_rank': rank if rank is not None else 100000}
            if reference is not None:
                pool[identity]['reference_from'] = [reference['from']]
                pool[identity]['reference_target'] = reference['target']

        # Ranked evidence is never discarded merely because another heading
        # matched. Reassemble its own section, not an arbitrary chosen document.
        for rank, item in enumerate(ranked):
            offer(item, 'retrieval-rank', rank=rank)
        for item in self._body_seeds_v2(question, max(24, top_k * 3), search_focus=search_focus,
                                       constraint_question=authoritative):
            offer(item, 'body-coverage')
        roots = []
        for section in sections:
            if not visible(section):
                continue
            coverage = _profile_coverage(profiles, section['heading_keys'], requested, _scope_categories(section))
            if coverage >= 0.65:
                scope = _scope_categories(section)
                if requested and scope and not requested & scope:
                    continue
                roots.append((coverage, section))
                offer(section, 'subject-heading')
        roots.sort(key=lambda pair: (-pair[0], len(pair[1]['section_path'].split('>')), pair[1]['position']))
        # Do not expand every repeated heading in a large corpus. Prefer the
        # most complete topic roots, retaining requested-category alternatives.
        roots = [item for coverage, item in roots[:8]
                 if coverage >= roots[0][0] - 0.15 and evidence_id(item) in pool] if roots else []
        for root in roots:
            if overview and evidence_id(root) in pool:
                label = f"{source_key(root['file'])}::{root['section_path']}"
                facet_groups.setdefault(label, ())
                pool[evidence_id(root)]['_facets'].add(label)
            prefix = root['section_path'] + ' > '
            for child in sections:
                if source_key(child['file']) != source_key(root['file']) or not _version_compatible(root, child):
                    continue
                path = str(child.get('section_path', ''))
                if not path.startswith(prefix) or ' > ' in path[len(prefix):]:
                    continue
                # An overview needs sibling breadth. A specific query can
                # still reach a pertinent child through ordinary retrieval.
                if overview:
                    label = f"{source_key(child['file'])}::{path}"
                    if evidence_role(child) != 'exception':
                        facet_groups.setdefault(label, ())
                    offer(child, 'subject-child', structural_facet=label)

        # Finite UI field choices can be stronger inventory evidence than a
        # family of examples. Prepare intent once; all offers retain existing
        # visibility/version/category/negative constraints and source wording.
        inventory_subjects = tuple(dict.fromkeys(
            subject for profile in profiles
            if (subject := _inventory_subject(positive, profile, enumerate_intent=enumerate_intent))
        ))
        for section in (sections if inventory_subjects else ()):
            if not visible(section):
                continue
            rows = next((rows for subject in inventory_subjects
                         if (rows := _inventory_enum_rows(section, prepared_subject=subject,
                                                         enumerate_intent=enumerate_intent))), [])
            if not rows:
                continue
            label = 'inventory:' + source_key(section['file']) + '::' + section['section_path']
            # A structural reservation must not add requested facets and
            # thereby increase the existing derived payload-slot limit.
            offer(section, 'inventory-enumeration', structural_facet=label)
            if evidence_id(section) in pool:
                pool[evidence_id(section)]['_inventory_rows'] = rows

        # Narrow / negative requests are intentionally not expanded into a
        # complete sibling family. Ordinary subject children remain unchanged.
        sibling_family = None
        family_intent = enumerate_intent or bool(re.search(
            r'какие|перечисл|обзор|сколько|из\s+чего|состав|структур|порядок|процедур|'
            r'как\s+(?:\w+\s+){0,2}(?:рассчит|рассчитыв|выполн|провод|начисл|организ)', positive, re.I,
        ))
        if family_intent and not forbidden and not wants_exception:
            families = [family for profile in profiles
                        if (family := _sibling_family(sections, list(pool.values()), profile, visible, requested))]
            sibling_family = max(families, key=lambda family: family['score']) if families else None
            if sibling_family:
                for child in sibling_family['children'][:12]:
                    offer(child, 'subject-child')
                    if evidence_id(child) in pool:
                        pool[evidence_id(child)]['_family_id'] = sibling_family['id']

        if reference_enabled:
            # One-hop only: origins are frozen before targets are appended.
            # Category applicability can be inherited only through a verbatim,
            # explicit same-source reference, not a loose semantic resemblance.
            origins = sorted(pool.values(), key=lambda item: -item['_score'])[:8]
            subject_profiles = tuple(tuple(group for group in profile
                                           if not group <= (_CATEGORY_STEMS | term_keys('ценовая категория')))
                                     for profile in profiles)
            expanded = set()
            for origin in origins:
                for link in _reference_clauses(str(origin.get('text', ''))):
                    quote_keys = term_keys(link['quote'])
                    origin_heading = term_keys(str(origin.get('section_title', '')))
                    if any(subject_profiles) and _profile_coverage(subject_profiles, quote_keys | origin_heading) < 0.5:
                        continue
                    targets = []
                    for target in sections:
                        if not visible(target) or source_key(target['file']) != source_key(origin['file']):
                            continue
                        if not _version_compatible(origin, target) or evidence_id(target) == evidence_id(origin):
                            continue
                        target_scope = _scope_categories(target)
                        if link['kind'] == 'section':
                            match = _section_locator(target) == link['locator']
                        else:
                            categories = set(link['categories'])
                            match = bool(target_scope & categories) and target_scope <= categories
                        if not match:
                            continue
                        target_keys = term_keys(str(target.get('text', ''))) | target['heading_keys']
                        relevance = _profile_coverage(subject_profiles, target_keys)
                        # An exact numbered locator is a narrow structural edge
                        # even when its equation uses symbols rather than the
                        # user's words. A category analogy is broader, so its
                        # target still needs positive subject agreement.
                        if link['kind'] == 'category' and any(subject_profiles) and relevance < 0.5:
                            continue
                        targets.append((relevance, target))
                    targets.sort(key=lambda pair: (-pair[0], len(pair[1]['text']), pair[1]['position']))
                    for _, target in targets:
                        identity = evidence_id(target)
                        if identity in expanded or len(expanded) >= 3:
                            continue
                        provenance = {'from': {'file': origin['file'], 'section_path': origin.get('section_path', ''),
                                               'quote': link['quote']},
                                      'target': {key: value for key, value in link.items() if key != 'quote'}}
                        label = 'reference:' + identity
                        facet_groups.setdefault(label, ())
                        offer(target, 'explicit-reference', structural_facet=label, reference=provenance)
                        if identity in pool:
                            expanded.add(identity)
                    if len(expanded) >= 3:
                        break
                if len(expanded) >= 3:
                    break

        candidates = list(pool.values())
        if not historical_intent:
            operative = [item for item in candidates if item['section_role'] == 'operative'
                         and item['evidence_role'] != 'exception'
                         and max(item['_local_coverage'], item['_heading_coverage']) >= 0.65]
            for item in candidates:
                quality = max(item['_local_coverage'], item['_heading_coverage'])
                if item['section_role'] != 'operative' and quality > 0 and any(
                    max(primary['_local_coverage'], primary['_heading_coverage']) >= quality - 0.1
                    for primary in operative
                ):
                    item['_score'] -= 1.2
                    item['section_role_penalty'] = -1.2
        if search_focus is None and literal_anchors and any(literal_anchors <= item['_anchors'] for item in candidates):
            candidates = [item for item in candidates if literal_anchors <= item['_anchors']
                          or item['selection_reason'] == 'subject-child' or item.get('reference_from')]
        # Do not veto semantic evidence solely because a literal/acronym hit
        # exists elsewhere. A definition may spell the entire term without its
        # acronym; the bounded semantic-rank contribution and later applicability
        # review decide its utility. Literal named-entity constraints above are
        # separate, and category/source/negative restrictions remain enforced.
        selected, covered, used, families = [], set(), 0, {}
        family_used = 0
        incomplete_inventory = []
        limit = max(top_k, min(24, len(facet_groups) + min(2, len(roots))))
        complete_family_fits = bool(sibling_family and len(sibling_family['children']) <= min(12, limit)
                                   and sibling_family['total_chars'] <= min(6000, max_chars))
        while candidates and len(selected) < limit:
            pending_subjects = set(focus_facets) - covered

            def priority(item):
                family = (source_key(item.get('file')), str(item.get('section_path', '')).rsplit(' > ', 1)[0])
                return (not wants_exception and item['evidence_role'] == 'exception',
                        not bool(item['_facets'] & pending_subjects),
                        not bool(item.get('_inventory_rows')),
                        not bool(item.get('_family_id')),
                        -(item['_score'] + 2.2 * len(item['_facets'] - covered)
                          - 0.25 * families.get(family, 0)), item['_rank'], item.get('position', 100000))

            candidate = min(candidates, key=priority)
            candidates.remove(candidate)
            item = {key: value for key, value in candidate.items() if not key.startswith('_')
                    and key not in {'heading_keys', 'position'}}
            text, remaining = str(item.get('text', '')), max_chars - used
            # Every still-selectable slot needs a useful payload, not only the
            # first four long hits. Short complete sections release their spare
            # allowance to subsequent candidates. This is facet/item fairness,
            # not a quota for different physical documents or a larger top_k.
            remaining_slots = min(limit - len(selected), len(candidates) + 1)
            budget = max(0, remaining // max(1, remaining_slots))
            other_subjects = pending_subjects - candidate['_facets']
            if candidate['_facets'] & pending_subjects:
                # Before optional sibling completion, retain space for a native
                # payload of each independent subject that is still available.
                reserve_others = sum(min((len(str(item.get('text', ''))) for item in candidates
                                          if label in item['_facets']), default=0)
                                     for label in other_subjects)
                if len(text) <= remaining - reserve_others:
                    budget = max(budget, len(text))
            if complete_family_fits and candidate.get('_family_id') and not other_subjects:
                # Reserve each complete child's native payload as a bundle,
                # while retaining separate evidence identities and locators.
                budget = min(remaining, len(text))
            if candidate.get('_family_id'):
                budget = min(budget, max(0, 6000 - family_used))
            excerpt_signals = {}
            if candidate.get('_inventory_rows'):
                excerpt_signals['inventory_enumeration_reserved'] = True
                if len(text) > budget:
                    packet = '\n\n'.join(candidate['_inventory_rows'])
                    if len(packet) > budget:
                        # Whole matching tables and surrounding source prose
                        # are atomic: never drop conditions or enum values.
                        incomplete_inventory.append({
                            'file': item.get('file'), 'section_path': item.get('section_path'),
                            '_revision': {key: item[key] for key in _VERSION_FIELDS if item.get(key)},
                            'reason': 'atomic-inventory-packet-budget',
                            'required_chars': len(packet), 'available_chars': budget})
                        continue
                    item['text'] = packet
                    item['evidence_truncated'] = True
                    item['evidence_excerpt'] = True
                    text = packet
                    packet_keys = term_keys(packet) | term_keys(str(item.get('section_title', '')))
                    candidate['_facets'] = {label for label in candidate['_facets']
                                            if not facet_groups.get(label)
                                            or facet_coverage(label, packet_keys) >= 0.7}
            if len(text) > budget:
                excerpt, indices = _focused_excerpt(text, budget, groups, facet_groups,
                                                    question=positive, diagnostics=excerpt_signals,
                                                    subject_profiles=profiles if search_focus is not None else None,
                                                    formula_subjects=tuple(phrase for subject in search_focus.subjects
                                                        for phrase in (subject.text, *subject.alternatives))
                                                    if search_focus is not None else None)
                if not excerpt or not _substantive_text(excerpt, str(item.get('section_title', ''))):
                    continue
                item['text'] = excerpt
                item['evidence_truncated'] = True
                item['evidence_excerpt'] = True
                item['evidence_block_indices'] = indices
                actual = term_keys(item['text']) | term_keys(str(item.get('section_title', '')))
                actual_facets = {label for label in candidate['_facets'] if not facet_groups.get(label)
                                 or facet_coverage(label, actual) >= 0.7}
            else:
                actual_facets = candidate['_facets']
            item['coverage_facets'] = sorted(actual_facets)
            if candidate.get('_family_id'):
                item['evidence_family_id'] = candidate['_family_id']
            item['selection_score'] = round(candidate['_score'], 4)
            item['selection_signals'] = {
                'heading_coverage': round(candidate['_heading_coverage'], 4),
                'heading_specificity': round(candidate['_heading_specificity'], 4),
                'body_coverage': round(candidate['_body_coverage'], 4),
                'local_paragraph_coverage': round(candidate['_local_coverage'], 4),
                'fair_budget_chars': budget,
                'reserved_payload_slots': remaining_slots,
                'section_role_penalty': candidate.get('section_role_penalty', 0),
                'search_focus_used': search_focus is not None,
                **excerpt_signals,
            }
            item['applicability'] = ('referenced' if candidate.get('reference_from') else
                                     ('mixed' if len(candidate['scope_categories']) > 1 else
                                      ('specific' if candidate['scope_categories'] else 'shared')))
            if forbidden:
                item['excluded_topics'] = forbidden
            selected.append(item)
            used += len(item['text'])
            if candidate.get('_family_id'):
                family_used += len(item['text'])
            covered.update(actual_facets)
            family = (source_key(item.get('file')), str(item.get('section_path', '')).rsplit(' > ', 1)[0])
            families[family] = families.get(family, 0) + 1
        scoped_inventory_warnings = []
        for warning in incomplete_inventory:
            carrier = next((item for item in selected
                            if source_key(item.get('file')) == source_key(warning['file'])
                            and _version_compatible(item, warning['_revision'])), None)
            if carrier is not None:
                signals = carrier['selection_signals']
                signals['inventory_enumeration_incomplete'] = True
                signals.setdefault('missing_inventory_sections', []).append({
                    key: value for key, value in warning.items() if key not in {'file', '_revision'}})
            else:
                # Out-of-context identities/locators never hitchhike on an
                # unrelated evidence item. Return only source-free metadata.
                scoped_inventory_warnings.append({key: warning[key] for key in
                    ('reason', 'required_chars', 'available_chars')})
        if scoped_inventory_warnings and selection_meta is not None:
            selection_meta.update({'inventory_enumeration_incomplete': True,
                                   'inventory_enumeration_scoped_warnings': scoped_inventory_warnings})
        if sibling_family and selected:
            bounded_children = sibling_family['children'][:12]
            expected = [child['section_path'] for child in bounded_children]
            members = [item for item in selected if item.get('evidence_family_id') == sibling_family['id']]
            present = {item['section_path'] for item in members}
            missing = [path for path in expected if path not in present]
            excerpted = [item['section_path'] for item in members if item.get('evidence_excerpt')]
            complete = len(present) == len(sibling_family['children']) and not excerpted
            anchor = members[0] if members else selected[0]
            anchor['evidence_family'] = {
                'id': sibling_family['id'], 'file': sibling_family['file'],
                'parent_section_path': sibling_family['parent_section_path'],
                'basis': 'eligible-direct-children', 'complete': complete,
                'expected_children': expected, 'selected_children': [item['section_path'] for item in members],
                'expected_members': [{'section_path': child['section_path'],
                                      'revision': {field: child[field] for field in _VERSION_FIELDS if child.get(field)},
                                      'text_sha256': hashlib.sha256(child['text'].strip().encode()).hexdigest()}
                                     for child in bounded_children],
                'missing_children': missing, 'excerpted_children': excerpted,
                'omitted_children_count': max(0, len(sibling_family['children']) - 12),
                'total_children': len(sibling_family['children']), 'max_children': 12, 'max_bundle_chars': 6000,
                'incomplete_reason': (None if complete else 'family-limit' if len(sibling_family['children']) > 12
                                      else 'family-char-limit' if sibling_family['total_chars'] > 6000
                                      else 'payload-or-slot-budget'),
            }
        return selected

    def select(
        self, question: str, ranked: list[dict], *, excluded_sources: set[str] | None = None,
        allowed_sources: set[str] | None = None, top_k: int = 10, max_chars: int = 36000,
    ) -> list[dict]:
        excluded = {source_key(value) for value in (excluded_sources or ())}
        allowed = {source_key(value) for value in (allowed_sources or ())}
        kind = requested_source_kind(question)

        def visible(item: dict) -> bool:
            key = source_key(item.get("file"))
            return key not in excluded and (allowed_sources is None or key in allowed) and (
                not kind or kind in str(item.get("file", "")).casefold()
                or (kind == 'постановлен' and bool(re.search(r'(?:^|[\W_])пп[\W_]*\d', str(item.get('file', '')).casefold())))
            ) and item.get("status") != "deleted"

        keys = term_keys(question)
        categories = keys & _CATEGORY_STEMS
        headings = []
        for section in self.sections:
            if not visible(section):
                continue
            heading_keys = section["heading_keys"]
            if categories and not (categories & heading_keys):
                continue
            overlap = keys & heading_keys
            if not keys or not overlap:
                continue
            # A leaf heading must cover the subject itself. An ancestor match
            # alone would incorrectly promote every nested exceptional case.
            coverage = len(overlap) / len(keys)
            if coverage < 0.65:
                continue
            # Long normative opening paragraphs contain definitions and
            # notation. Extra words must not outweigh matching the subject.
            score = coverage * 8 + len(overlap) * 0.3 - min(6, len(heading_keys - keys)) * 0.15
            if heading_keys == keys:
                score += 2
            if evidence_role(section) == "exception" and not _EXCEPTION.search(question):
                score -= 3
            headings.append((score, section))
        headings.sort(key=lambda pair: (-pair[0], pair[1]["position"]))
        roots = [section for score, section in headings[:3] if score >= headings[0][0] - 0.6] if headings else []
        candidates: list[dict] = []
        for root in roots:
            candidates.append({**root, "score": 1.0, "selection_reason": "subject-heading"})
        # Expand each exact subject into its own children before considering
        # unrelated semantic hits. Preserve document order and sibling breadth.
        children = []
        for root in roots:
            prefix = root["section_path"] + " > "
            for section in self.sections:
                if visible(section) and source_key(section["file"]) == source_key(root["file"]) and section["section_path"].startswith(prefix):
                    children.append({**section, "score": 0.9, "selection_reason": "subject-child"})
        children.sort(key=lambda item: (item["section_path"].count(">"), item["position"]))
        candidates.extend(children)
        # Preserve strong full-subject body matches when no governing heading
        # was found. Do not fill the evidence with unrelated price/cost hits.
        direct = [item for item in ranked if visible(item) and item.get('lexical_coverage') == 1.0]
        if not roots and direct:
            candidates.extend(direct)
        subject_keys = set(direct[0].get('lexical_subject_keys', ())) if direct and not roots else set()
        candidates.extend(
            item for item in ranked if visible(item) and (
                not roots or keys.issubset(term_keys(
                    str(item.get("section_path", "")) + " " + str(item.get("text", ""))
                ))
            ) and (not subject_keys or subject_keys.issubset(term_keys(str(item.get('text', '')))))
        )
        selected, seen, used = [], set(), 0
        limit = max(top_k, min(24, len(roots) + len(children)))
        for candidate in candidates:
            item = {key: value for key, value in candidate.items() if key not in {"heading_keys", "position"}}
            identity = evidence_id(item)
            if identity in seen:
                continue
            text = str(item.get("text", ""))
            # Whole-section packing has a separate input budget. Never cut an
            # equation mid-token or silently turn a partial section into whole.
            remaining = max_chars - used
            if remaining < 300:
                break
            if len(text) > remaining:
                blocks, size = [], 0
                for block in text.split("\n\n"):
                    if size + len(block) + 2 > remaining:
                        break
                    blocks.append(block)
                    size += len(block) + 2
                if not blocks:
                    continue
                item["text"] = "\n\n".join(blocks)
                item["evidence_truncated"] = True
            item["evidence_role"] = evidence_role(item)
            item.setdefault("selection_reason", "retrieval-rank")
            used += len(item["text"])
            selected.append(item)
            seen.add(identity)
            if len(selected) >= limit:
                break
        if not _EXCEPTION.search(question):
            selected.sort(key=lambda item: item["evidence_role"] == "exception")
        return selected


def generation_evidence_prompt(context: list[dict]) -> str:
    roles = {"main": "основное правило", "definition": "определение", "exception": "частный случай или исключение"}
    return "\n".join(
        f"- {item.get('file', '')}, {item.get('section_path', '')}: "
        f"{roles.get(item.get('evidence_role'), 'подтверждающий фрагмент')}"
        for item in context
    )


def overview_sections(question: str, context: list[dict]) -> list[dict]:
    """Plan an overview from immediate children, not from domain-specific facts."""
    value = question.casefold()
    if not re.search(r"что|какие|обзор|опиши|расскажи", value) or re.search(r"что\s+такое", value):
        return []
    roots = [item for item in context if item.get('selection_reason') == 'subject-heading']
    if not roots:
        return []
    root = roots[0]
    prefix = root['section_path'] + ' > '
    children = [item for item in context if source_key(item.get('file')) == source_key(root.get('file'))
                and str(item.get('section_path', '')).startswith(prefix)
                and ' > ' not in item['section_path'][len(prefix):]
                and item.get('evidence_role') != 'exception']
    return children if len(children) >= 2 else []


def missing_overview_sections(answer: str, sections: list[dict]) -> list[dict]:
    headings = re.findall(r'(?m)^\s*(?:#{1,6}\s+|\*\*)([^\n]+)', answer)
    heading_keys = [term_keys(heading.strip('* ')) for heading in headings]
    return [item for item in sections if not any(
        term_keys(str(item.get('section_title', ''))) <= keys for keys in heading_keys
    )]
