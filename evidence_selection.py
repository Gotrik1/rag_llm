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


def _expand_ordinal_categories(text: str) -> str:
    def expand(match):
        words = re.findall(r'[а-яё]+', match[1].casefold().replace('ё', 'е'))
        ordinals = [_CATEGORY_IDS.get(_stem_word(word)) for word in words if word != 'и']
        if not ordinals or any(number is None for number in ordinals):
            return match[0]
        return ' '.join(_CATEGORY_WORDS[number] + ' ценовой категории' for number in ordinals)
    return _ORDINAL_ABBREVIATION.sub(expand, text)


def _positive_query(question: str) -> tuple[str, list[str]]:
    """Separate retrieval subjects from explicit negative search instructions."""
    excluded = []

    def remove(match):
        excluded.append(match.group(1).strip())
        return ' '

    value = re.sub(
        r'\b(?:не\s+(?:учитывай|смотри|рассматривай|включай|используй)|кроме|исключая)'
        r'\s+([^;.!?,]+)', remove, question, flags=re.I,
    )
    value = re.sub(r'\b((?:[а-яёa-z]+\s+){0,1}[а-яёa-z]+)\s+не\s+'
                   r'(?:учитывай|смотри|рассматривай|включай|используй)\b',
                   remove, value, flags=re.I)
    return value, excluded


def _query_groups(question: str) -> tuple[frozenset[str], ...]:
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
    keys = term_keys(value) - _V2_FILLER
    subject = keys - _LOOKUP_WORDS
    if subject:
        keys = subject
    groups.extend(frozenset((key,)) for key in sorted(keys))
    return tuple(dict.fromkeys(group for group in groups if group))


def _group_coverage(groups: tuple[frozenset[str], ...], keys: frozenset[str]) -> float:
    return sum(bool(group & keys) for group in groups) / len(groups) if groups else 0.0


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
    blocks, pending, in_display = [], [], False
    for paragraph in str(text).split('\n\n'):
        pending.append(paragraph)
        if paragraph.count('$$') % 2 or paragraph.count('\\[') != paragraph.count('\\]'):
            in_display = not in_display
        if not in_display:
            blocks.append('\n\n'.join(pending))
            pending = []
    if pending:
        blocks.append('\n\n'.join(pending))
    return [block for block in blocks if block.strip()]


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
    return [next(value for value in match.groups() if value is not None)
            for match in re.finditer(r'\[MATH:\s*([^\]]+)\]|\$\$(.*?)\$\$|\\\[(.*?)\\\]',
                                     str(text), re.S)]


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
    for following in range(number + 1, min(len(blocks), number + 3)):
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
    definitions, equations = {}, []
    for number, block in enumerate(blocks):
        for line in block.splitlines():
            expressions = _math_expressions(line)
            if not expressions or any('=' in expression for expression in expressions):
                continue
            # Native table rows / explicit notation sentences, not incidental
            # symbol mentions in arbitrary narrative text.
            if '|' in line:
                description = ' '.join(line.split('|')[1:])
            elif re.search(r'—|\s–\s|\s-\s', line):
                description = re.split(r'—|\s–\s|\s-\s', line, maxsplit=1)[-1]
            else:
                continue
            relevance = len(requested & term_keys(description)) / max(1, len(requested))
            for symbol in frozenset().union(*(_math_symbols(expression) for expression in expressions)):
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
                if len(blocks[preceding]) > 700 or _math_expressions(blocks[preceding]) or '[TABLE]' in blocks[preceding]:
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


def _focused_excerpt(text, budget, groups, facet_groups, *, question='', diagnostics=None):
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
    scores = [2 * _group_coverage(groups, value) + max(
        (_group_coverage(facet, value) for facet in facet_groups.values() if facet), default=0,
    ) for value in keys]
    order = sorted(range(len(blocks)), key=lambda number: (-scores[number], number))
    selected, size = set(), 0
    # Leave room for omission markers, which are metadata-like labels, not
    # invented source content. A prefix-only excerpt needs no extra markers.
    marker = '\n\n[ПРОПУСК АБЗАЦЕВ]\n\n'
    guarded = {number: _equation_guards(blocks, number) for number, block in enumerate(blocks)
               if any('=' in expression for expression in _math_expressions(block))}

    def reserve(positions):
        nonlocal size
        positions = set(positions)
        positions.update(guard for position in list(positions) for guard in guarded.get(position, ()))
        positions -= selected
        cost = sum(len(blocks[position]) + len(marker) for position in positions)
        if size + cost > budget:
            return False
        selected.update(positions)
        size += cost
        return True

    packages, definitions = _formula_dependencies(blocks, question, groups)
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
                reserve([definitions[symbol][1]])
    if diagnostics is not None:
        diagnostics['formula_dependency_blocks'] = formula_blocks
        diagnostics['formula_dependency_max_hops'] = 1
    for number in order:
        # Adjacent notation/conditions are more valuable than an unrelated
        # introduction. The anchor paragraph gets budget before its neighbors.
        for position in (number, number - 1, number + 1, number + 2):
            if position < 0 or position >= len(blocks) or position in selected:
                continue
            reserve([position])
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


@lru_cache(maxsize=65536)
def _stem_word(word: str) -> str:
    # Regulations repeat the same vocabulary across thousands of passages.
    # Cache word morphology, not only whole passages, to bound cold CPU work.
    return _STEMMER.stem(word)


@lru_cache(maxsize=8192)
def term_keys(text: str) -> frozenset[str]:
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

    def body_seeds(self, question: str, limit: int = 24, *, mode: str = 'legacy') -> list[dict]:
        """Find positive subject matches, independently of vector similarity.

        Generic price/action words cannot anchor a match. Require at least two
        subject words and a rare anchor, so incidental mentions in a large
        manual do not swamp an exact fact in a small document.
        """
        if mode == 'coverage':
            return self._body_seeds_v2(question, limit)
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

    def _body_seeds_v2(self, question: str, limit: int) -> list[dict]:
        positive, _ = _positive_query(question)
        groups = _query_groups(positive)
        if not groups or limit <= 0:
            return []
        requested = _category_ids(positive)
        postings = [set().union(*(self.body_postings.get(key, ()) for key in group)) for group in groups]
        positions = set().union(*postings)
        count = max(1, len(self.body_items))
        results = []
        for position in positions:
            item = self.body_items[position]
            scope = _scope_categories(item)
            if requested and scope and not requested & scope:
                continue
            applicable = _applicable_groups(groups, requested, scope)
            eligible = {number for number, group in enumerate(groups) if group in applicable}
            matched = [number for number, posting in enumerate(postings) if position in posting]
            coverage = len(set(matched) & eligible) / max(1, len(eligible))
            # One explicit acronym is a valid subject. A common single word
            # is not enough to bypass semantic retrieval for an entire corpus.
            rare = any(len(postings[number]) <= max(2, count * 0.1) for number in matched)
            if coverage < 0.65 or not rare:
                continue
            results.append({**item, 'score': round(3 + coverage, 4),
                            'matched_queries': ['body-coverage'],
                            'lexical_subject_groups': [sorted(group) for group in groups],
                            'lexical_coverage': coverage})
        return sorted(results, key=lambda item: (-item['score'], len(item['text']), source_key(item['file'])))[:limit]

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
    ) -> list[dict]:
        """Coverage-aware evidence packing; legacy ``select`` remains unchanged.

        Rankings are evidence, not a hard gate. Heading hits, positive body
        matches and already ranked semantic hits compete in the same pool.
        Explicit facets and overview siblings get coverage reservations instead
        of a document-diversity quota. Conditions/exceptions are retained but
        cannot displace a primary rule unless the question asks for them.
        """
        if top_k <= 0 or max_chars <= 0:
            return []
        reference_enabled = (os.getenv('RAG_REFERENCE_EXPANSION', 'off').casefold() in {'on', 'true', '1'}
                             if resolve_references is None else resolve_references)
        excluded = {source_key(value) for value in (excluded_sources or ())}
        allowed = {source_key(value) for value in (allowed_sources or ())}
        kind = requested_source_kind(question)
        positive, forbidden = _positive_query(question)
        groups = _query_groups(positive)
        requested = _category_ids(positive)
        positive_keys = frozenset().union(*groups) if groups else frozenset()
        # Preserve literal named subjects such as a device/model name. Only
        # enforce them when at least one actual candidate carries the anchor;
        # otherwise semantic retrieval is still allowed to resolve a synonym.
        literal_anchors = term_keys(' '.join(match[0] for match in re.finditer(
            r'\b[А-ЯЁA-Z][а-яёa-z]{2,}\b', positive,
        ) if match.start() > 0)) - _V2_FILLER - _LOOKUP_WORDS
        forbidden_keys = frozenset().union(*(term_keys(value) for value in forbidden)) - positive_keys
        contrast_keys = term_keys(' '.join(re.findall(
            r'\b[а-яё]+(?:ую|ая|ое|ые|ый|ий|ыми|ими)\b', positive, re.I,
        ))) - _CATEGORY_STEMS - _V2_FILLER
        overview = bool(re.search(r'какие|обзор|перечисл|из\s+чего|состав|структур|что\s+ведется', positive, re.I))
        wants_exception = bool(_EXCEPTION.search(positive))
        historical_intent = bool(re.search(r'истори|раньше|ранее|изменил|изменен|предыдущ|стар\w*\s+редакц|по\s+состоянию', positive, re.I))
        facet_groups = {label: _query_groups(_positive_query(label)[0])
                        for label in (facets or ()) if str(label).strip()}
        if len(requested) > 1:
            facet_groups.update({f'{number} ЦК': _query_groups(f'{number} ЦК') for number in sorted(requested)})

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
            if not _substantive_text(str(item.get('text', '')), str(item.get('section_title', ''))):
                return
            scope = _scope_categories(item)
            if requested and scope and not requested & scope and reference is None:
                return
            applicable = _applicable_groups(groups, requested, scope)
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
            body_coverage, heading_coverage = _group_coverage(applicable, body), _group_coverage(applicable, heading)
            local_coverage = max((_group_coverage(applicable, keys) for keys in block_keys), default=0)
            if forbidden_keys & (body | heading) and not positive_keys & (body | heading) - forbidden_keys:
                return
            # A forbidden-only comparator commonly shares a noun ("мощность")
            # with the desired topic. Require its positive distinguishing term.
            discriminators = positive_keys - frozenset().union(*(term_keys(value) for value in forbidden)) if forbidden else positive_keys
            discriminators = contrast_keys or (discriminators - _CATEGORY_STEMS - term_keys('ценовая категория'))
            if forbidden_keys & (body | heading) and discriminators and not discriminators & (body | heading):
                return
            if reason != 'subject-child' and rank is None and reference is None and max(body_coverage, heading_coverage) < 0.65:
                return
            support = pool.get(identity)
            covered = {label for label, keys in facet_groups.items()
                       if keys and any(_group_coverage(keys, value | heading) >= 0.7 for value in block_keys)}
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
        for item in self._body_seeds_v2(positive, max(24, top_k * 3)):
            offer(item, 'body-coverage')
        roots = []
        for section in sections:
            if not visible(section):
                continue
            coverage = _group_coverage(_applicable_groups(groups, requested, _scope_categories(section)), section['heading_keys'])
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

        if reference_enabled:
            # One-hop only: origins are frozen before targets are appended.
            # Category applicability can be inherited only through a verbatim,
            # explicit same-source reference, not a loose semantic resemblance.
            origins = sorted(pool.values(), key=lambda item: -item['_score'])[:8]
            subject_groups = tuple(group for group in groups
                                   if not group <= (_CATEGORY_STEMS | term_keys('ценовая категория')))
            expanded = set()
            for origin in origins:
                for link in _reference_clauses(str(origin.get('text', ''))):
                    quote_keys = term_keys(link['quote'])
                    origin_heading = term_keys(str(origin.get('section_title', '')))
                    if subject_groups and _group_coverage(subject_groups, quote_keys | origin_heading) < 0.5:
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
                        relevance = _group_coverage(subject_groups, target_keys)
                        # An exact numbered locator is a narrow structural edge
                        # even when its equation uses symbols rather than the
                        # user's words. A category analogy is broader, so its
                        # target still needs positive subject agreement.
                        if link['kind'] == 'category' and subject_groups and relevance < 0.5:
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
        if literal_anchors and any(literal_anchors <= item['_anchors'] for item in candidates):
            candidates = [item for item in candidates if literal_anchors <= item['_anchors']
                          or item['selection_reason'] == 'subject-child' or item.get('reference_from')]
        # Do not veto semantic evidence solely because a literal/acronym hit
        # exists elsewhere. A definition may spell the entire term without its
        # acronym; the bounded semantic-rank contribution and later applicability
        # review decide its utility. Literal named-entity constraints above are
        # separate, and category/source/negative restrictions remain enforced.
        selected, covered, used, families = [], set(), 0, {}
        limit = max(top_k, min(24, len(facet_groups) + min(2, len(roots))))
        while candidates and len(selected) < limit:
            def priority(item):
                family = (source_key(item.get('file')), str(item.get('section_path', '')).rsplit(' > ', 1)[0])
                return (not wants_exception and item['evidence_role'] == 'exception',
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
            excerpt_signals = {}
            if len(text) > budget:
                excerpt, indices = _focused_excerpt(text, budget, groups, facet_groups,
                                                    question=positive, diagnostics=excerpt_signals)
                if not excerpt or not _substantive_text(excerpt, str(item.get('section_title', ''))):
                    continue
                item['text'] = excerpt
                item['evidence_truncated'] = True
                item['evidence_excerpt'] = True
                item['evidence_block_indices'] = indices
                actual = term_keys(item['text']) | term_keys(str(item.get('section_title', '')))
                actual_facets = {label for label in candidate['_facets'] if not facet_groups.get(label)
                                 or _group_coverage(facet_groups[label], actual) >= 0.7}
            else:
                actual_facets = candidate['_facets']
            item['coverage_facets'] = sorted(actual_facets)
            item['selection_score'] = round(candidate['_score'], 4)
            item['selection_signals'] = {
                'heading_coverage': round(candidate['_heading_coverage'], 4),
                'heading_specificity': round(candidate['_heading_specificity'], 4),
                'body_coverage': round(candidate['_body_coverage'], 4),
                'local_paragraph_coverage': round(candidate['_local_coverage'], 4),
                'fair_budget_chars': budget,
                'reserved_payload_slots': remaining_slots,
                'section_role_penalty': candidate.get('section_role_penalty', 0),
                **excerpt_signals,
            }
            item['applicability'] = ('referenced' if candidate.get('reference_from') else
                                     ('mixed' if len(candidate['scope_categories']) > 1 else
                                      ('specific' if candidate['scope_categories'] else 'shared')))
            if forbidden:
                item['excluded_topics'] = forbidden
            selected.append(item)
            used += len(item['text'])
            covered.update(actual_facets)
            family = (source_key(item.get('file')), str(item.get('section_path', '')).rsplit(' > ', 1)[0])
            families[family] = families.get(family, 0) + 1
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
