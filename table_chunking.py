"""LlamaIndex integration for source-bound, self-contained table rows."""
from __future__ import annotations

import re
import uuid

from llama_index.core import Document
from llama_index.core.schema import MetadataMode, NodeRelationship, TextNode
from llama_index.core.utils import get_tokenizer

from table_context import TableContextError, build_table_units, parse_table_unit


_SLOT_PATTERN = re.compile(r'\[RAG_TABLE_SLOT_[0-9a-f]{64}\]')


def chunk_documents_with_tables(documents, slots: dict, splitter, *, source_identity: str):
    """Replace each uniquely bound table slot with source-bound row units within the splitter budget.

    Prose uses the configured splitter; table units retain native grid metadata and
    section-local previous/next links. Missing, repeated or unbound slots fail closed."""
    if not slots:
        return splitter.get_nodes_from_documents(documents)
    occurrences = {marker: 0 for marker in slots}
    for document in documents:
        for match in _SLOT_PATTERN.finditer(document.text):
            if match.group() not in slots:
                raise TableContextError('Unbound table placeholder')
            occurrences[match.group()] += 1
    if any(count != 1 for count in occurrences.values()):
        raise TableContextError('Table placeholder was lost or duplicated during section parsing')

    tokenizer = get_tokenizer()
    result = []
    for document in documents:
        section_nodes, position = [], 0
        metadata_tokens = max(len(tokenizer(document.get_metadata_str(mode=mode)))
                              for mode in (MetadataMode.EMBED, MetadataMode.LLM))
        budget = int(splitter.chunk_size) - metadata_tokens
        if budget < 1:
            raise TableContextError('Section metadata leaves no table text budget')

        def prose(value):
            if value.strip():
                original = Document(text=value, doc_id=document.id_, metadata=dict(document.metadata),
                                    excluded_embed_metadata_keys=list(document.excluded_embed_metadata_keys),
                                    excluded_llm_metadata_keys=list(document.excluded_llm_metadata_keys))
                section_nodes.extend(splitter.get_nodes_from_documents([original]))

        for match in _SLOT_PATTERN.finditer(document.text):
            prose(document.text[position:match.start()])
            record = slots[match.group()]
            texts = build_table_units(record, source_identity=f'{source_identity}\0{document.id_}',
                                      tokenizer=tokenizer, max_tokens=budget)
            for ordinal, text in enumerate(texts):
                unit = parse_table_unit(text)
                metadata = {**document.metadata, 'table_schema': 'table-grid-v1',
                            'source_table_id': unit['table_id'], 'table_row': unit['row'],
                            'table_fragment': ordinal, 'table_header_status': unit['header_status'],
                            'table_source_start': record['start_char'], 'table_source_end': record['end_char'],
                            'chunk_type': 'table'}
                structural_keys = [key for key in metadata if key not in document.metadata]
                section_nodes.append(TextNode(
                    id_=str(uuid.uuid5(uuid.NAMESPACE_URL, f'{unit["table_id"]}:{ordinal}')),
                    text=text, metadata=metadata,
                    excluded_embed_metadata_keys=[*document.excluded_embed_metadata_keys, *structural_keys],
                    excluded_llm_metadata_keys=[*document.excluded_llm_metadata_keys, *structural_keys],
                    relationships={NodeRelationship.SOURCE: document.as_related_node_info()},
                ))
            position = match.end()
        prose(document.text[position:])
        for index, node in enumerate(section_nodes):
            if index:
                node.relationships[NodeRelationship.PREVIOUS] = section_nodes[index - 1].as_related_node_info()
            if index + 1 < len(section_nodes):
                node.relationships[NodeRelationship.NEXT] = section_nodes[index + 1].as_related_node_info()
        result.extend(section_nodes)
    return result
