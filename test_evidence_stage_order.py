from rag_agent import apply_document_version_preference


def test_version_annotation_does_not_rerank_complete_evidence_packets():
    governing = {'file': 'manual.md', 'section_path': 'Main procedure',
                 'selection_reason': 'subject-heading', 'score': 1.0}
    incidental = {'file': 'other.md', 'section_path': 'Special case',
                  'selection_reason': 'retrieval-rank', 'score': 100.0}
    result = apply_document_version_preference(
        [governing, incidental], {'manual.md': 'latest', 'other.md': 'latest'},
        preserve_order=True,
    )
    assert result[0]['section_path'] == 'Main procedure'
    assert result[0]['version_status'] == 'latest'
    assert governing['score'] == 1.0


def test_candidate_stage_still_softly_prefers_latest_revision():
    candidates = [{'file': 'old.md', 'score': 1.0}, {'file': 'new.md', 'score': 1.0}]
    result = apply_document_version_preference(candidates, {'old.md': 'superseded', 'new.md': 'latest'})
    assert [item['file'] for item in result] == ['new.md', 'old.md']
