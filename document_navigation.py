"""Request-local navigation over an already authorized native-record snapshot.

The caller owns ACL/applicability/injection filtering and read budgets. Handles
are private navigation capabilities, never citations or evidence IDs. Lexical
matches and native headings guide reading; they establish no factual coverage.
There is no I/O, model call, global cache, or silent truncation of source text.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import secrets
from types import MappingProxyType
from typing import Any


_SOURCE_FIELDS = (
    'file', 'file_path', 'document_id', 'source_id', 'revision_id', 'version_id',
    'document_version', 'content_hash', 'sha256',
)
_SCALAR = (str, int, float, bool, type(None))


class NavigationError(ValueError):
    """Invalid request, source identity, or handle for this navigation snapshot."""


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, UnicodeError) as error:
        raise NavigationError('invalid-native-record') from error


@dataclass(frozen=True)
class _Node:
    node_id: str
    doc_id: str
    record_json: str
    file: str
    section_path: str
    section_title: str
    source_json: str
    text_chars: int
    is_heading_only: bool
    fields: tuple[tuple[str, frozenset[str]], ...]
    search_text: str


@dataclass(frozen=True)
class _Document:
    doc_id: str
    title: str
    file: str
    source_json: str
    node_ids: tuple[str, ...]


class DocumentNavigationProvider:
    """Detached immutable source snapshot with fresh handles for each instance.

    ``records`` must come from the caller's server-side authorized native index,
    never from model output. Deleted, missing-text and empty-text rows are
    excluded here as a defensive check. ``features`` supplies the application's
    lexical normalization (including Russian morphology); ``native_text`` is
    only a search projection. ``read`` always returns the complete original row.
    """

    def __init__(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        features: Callable[[str], Iterable[str]],
        native_text: Callable[[str], str] | None = None,
    ):
        if not callable(features) or (native_text is not None and not callable(native_text)):
            raise NavigationError('invalid-lexical-features')
        self._features = features
        self._secret = secrets.token_bytes(32)
        self._snapshot = secrets.token_hex(16)
        documents: dict[str, dict[str, Any]] = {}
        nodes: dict[str, _Node] = {}
        for supplied in records:
            if not isinstance(supplied, Mapping):
                raise NavigationError('invalid-native-record')
            text = supplied.get('text')
            status = supplied.get('status')
            if ((isinstance(status, str) and status.strip().casefold() == 'deleted')
                    or not isinstance(text, str) or not text.strip()):
                continue
            if not isinstance(supplied.get('file'), str) or not supplied['file'].strip():
                raise NavigationError('missing-native-file')
            if any(key in supplied and not isinstance(supplied[key], _SCALAR) for key in _SOURCE_FIELDS):
                raise NavigationError('invalid-source-identity')
            # Serialization detaches all nested metadata and preserves absent,
            # null, bool, int, float and string revision values distinctly.
            record_json = _json(dict(supplied))
            record = json.loads(record_json)
            text = record['text']
            source = {key: record[key] for key in _SOURCE_FIELDS if key in record}
            source_json = _json(source)
            document = documents.get(source_json)
            if document is None:
                title = record.get('document_title') or record.get('title')
                document = {
                    'doc_id': self._handle('doc'),
                    'title': title if isinstance(title, str) and title.strip() else record['file'],
                    'file': record['file'], 'source_json': source_json, 'node_ids': [],
                }
                documents[source_json] = document
            path = record.get('section_path')
            path = path if isinstance(path, str) else ''
            title = record.get('section_title')
            title = title if isinstance(title, str) else ''
            projected = native_text(text) if native_text else text
            if not isinstance(projected, str):
                raise NavigationError('invalid-search-projection')
            fields = (
                ('text', self._keys(projected)),
                ('section_path', self._keys(path)),
                ('section_title', self._keys(title)),
                ('file', self._keys(record['file'])),
                ('document_title', self._keys(document['title'])),
            )
            node_id = self._handle('node')
            node = _Node(
                node_id, document['doc_id'], record_json, record['file'], path, title,
                source_json, len(text),
                bool(projected.strip() and projected.strip() in {title, path.rsplit('>', 1)[-1].strip()}),
                fields, projected,
            )
            nodes[node_id] = node
            document['node_ids'].append(node_id)
        self._nodes = MappingProxyType(nodes)
        self._documents = tuple(_Document(**{**doc, 'node_ids': tuple(doc['node_ids'])})
                                for doc in documents.values())
        self._by_document = MappingProxyType({doc.doc_id: doc for doc in self._documents})

    def _handle(self, kind: str) -> str:
        return f'nav{kind}:{self._snapshot}:{secrets.token_hex(12)}'

    def _keys(self, text: str) -> frozenset[str]:
        try:
            keys = self._features(text)
            if isinstance(keys, str):
                raise TypeError('features must return individual keys')
            result = frozenset(keys)
            if any(not isinstance(key, str) for key in result):
                raise TypeError('feature keys must be strings')
            # Snowball can produce an empty stem (for example for Russian ь).
            # It is not a search term and must not invalidate a native record.
            return result - {''}
        except (TypeError, ValueError) as error:
            raise NavigationError('invalid-lexical-features') from error

    def _document(self, doc_id: str) -> _Document:
        if not isinstance(doc_id, str) or doc_id not in self._by_document:
            raise NavigationError('unknown-document-handle')
        return self._by_document[doc_id]

    @staticmethod
    def _node_payload(node: _Node) -> dict[str, Any]:
        return {
            'node_id': node.node_id, 'doc_id': node.doc_id, 'file': node.file,
            'section_path': node.section_path, 'section_title': node.section_title,
            'source': json.loads(node.source_json), 'text_chars': node.text_chars,
            'is_heading_only': node.is_heading_only,
        }

    def _page(self, rows: Sequence[dict[str, Any]], operation: str,
              cursor: str | None, limit: int) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise NavigationError('invalid-page-limit')
        scope = hashlib.sha256(operation.encode('utf-8')).hexdigest()
        offset = 0
        if cursor is not None:
            if not isinstance(cursor, str):
                raise NavigationError('invalid-cursor')
            parts = cursor.split(':')
            if len(parts) != 4 or parts[0] != 'navpage' or parts[1] != scope:
                raise NavigationError('invalid-cursor')
            offset_text, signature = parts[2:]
            message = f'{self._snapshot}:{scope}:{offset_text}'.encode('utf-8')
            expected = hmac.new(self._secret, message, hashlib.sha256).hexdigest()
            if (not offset_text.isascii() or not offset_text.isdecimal()
                    or re.fullmatch(r'[0-9a-f]{64}', signature) is None
                    or not hmac.compare_digest(signature, expected)):
                raise NavigationError('invalid-cursor')
            offset = int(offset_text)
            if not 0 < offset < len(rows):
                raise NavigationError('invalid-cursor')
        stop = min(offset + limit, len(rows))
        next_cursor = None
        if stop < len(rows):
            message = f'{self._snapshot}:{scope}:{stop}'.encode('utf-8')
            signature = hmac.new(self._secret, message, hashlib.sha256).hexdigest()
            next_cursor = f'navpage:{scope}:{stop}:{signature}'
        return {'items': list(rows[offset:stop]), 'next_cursor': next_cursor, 'total': len(rows)}

    def list_documents(self, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Page through this immutable authorized snapshot using opaque snapshot-bound document handles."""
        rows = [{
            'doc_id': doc.doc_id, 'title': doc.title, 'file': doc.file,
            'source': json.loads(doc.source_json), 'node_count': len(doc.node_ids),
        } for doc in self._documents]
        return self._page(rows, 'documents', cursor, limit)

    def outline(self, doc_id: str, cursor: str | None = None, limit: int = 40) -> dict[str, Any]:
        """Page through a known document's ordered native nodes without inventing heading semantics."""
        document = self._document(doc_id)
        rows = [self._node_payload(self._nodes[node_id]) for node_id in document.node_ids]
        return self._page(rows, _json(['outline', doc_id]), cursor, limit)

    def search(self, query: str, doc_id: str | None = None,
               cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Return lexical navigation matches and labeled previews; match scores are not evidence confidence."""
        if not isinstance(query, str):
            raise NavigationError('invalid-query')
        selected = (self._document(doc_id).node_ids if doc_id is not None else self._nodes)
        query_keys = self._keys(query)
        ranked = []
        for position, node_id in enumerate(selected):
            node = self._nodes[node_id]
            matched = [(name, query_keys & keys) for name, keys in node.fields if query_keys & keys]
            if not matched:
                continue
            terms = frozenset().union(*(keys for _, keys in matched))
            row = self._node_payload(node)
            row.update({'matched_fields': [name for name, _ in matched],
                        'matched_terms': sorted(terms), 'score': len(terms) / len(query_keys)})
            ranked.append((len(terms), position, row))
        # Match fraction is only lexical navigation metadata, never confidence
        # or evidence. Stable input order breaks ties without losing any row.
        ranked.sort(key=lambda item: (-item[0], item[1]))
        rows = [row for _, _, row in ranked]
        page = self._page(rows, _json(['search', query, doc_id]), cursor, limit)
        # Preview work is limited to the returned page. It is a labeled search
        # aid, not a source passage; only read() supplies the full native row.
        for row in page['items']:
            text = self._nodes[row['node_id']].search_text
            first_match = None
            if 'text' in row['matched_fields']:
                first_match = next((match for match in re.finditer(r'\w+', text)
                                    if self._keys(match.group()) & query_keys), None)
            start = max(0, first_match.start() - 100) if first_match else 0
            stop = min(start + 300, len(text))
            row.update({'preview': text[start:stop], 'preview_kind': 'navigation-only',
                        'preview_truncated': start > 0 or stop < len(text)})
        return page

    def read(self, node_id: str) -> dict[str, Any]:
        """Resolve a snapshot-owned node handle to its full detached native record; reject foreign handles."""
        if not isinstance(node_id, str) or node_id not in self._nodes:
            raise NavigationError('unknown-node-handle')
        return json.loads(self._nodes[node_id].record_json)
