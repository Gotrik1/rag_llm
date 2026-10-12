use crate::bm25::Bm25Index;
use crate::embed::Embedder;
use crate::store::{SearchResult, VectorStore};
use anyhow::Result;
use std::collections::HashMap;

const TOP_K: usize = 10;
const VECTOR_CANDIDATES: u64 = 30;
const BM25_FULLTEXT_CANDIDATES: usize = 15;
const BM25_SECTION_CANDIDATES: usize = 5; // per abbreviation
const RRF_K: f32 = 60.0;

/// Lexical candidates from the same persisted docstore used by Python.
/// This machine path requires neither a second embedding model nor Qdrant.
pub fn retrieve_canonical(question: &str) -> Result<Vec<crate::agent::RetrievalEvidence>> {
    use std::{env, path::PathBuf};
    let data = env::var("RAG_DATA_DIR").unwrap_or_else(|_| ".data".to_string());
    let directory = env::var("BM25_PERSIST_DIR").unwrap_or_else(|_| format!("{}/bm25_v12", data));
    let content = std::fs::read_to_string(PathBuf::from(directory).join("docstore.json"))?;
    parse_canonical(&content, question)
}

fn parse_canonical(content: &str, question: &str) -> Result<Vec<crate::agent::RetrievalEvidence>> {
    let corpus: serde_json::Value = serde_json::from_str(content)?;
    let nodes = corpus.get("docstore/data").and_then(|v| v.as_object())
        .ok_or_else(|| anyhow::anyhow!("Canonical docstore has no nodes"))?;
    let keys = lexical_keys(question);
    let mut results = Vec::new();
    for (index, (_, stored)) in nodes.iter().enumerate() {
        let node = stored.get("__data__").unwrap_or(stored);
        let metadata = &node["metadata"];
        if metadata["status"].as_str() == Some("deleted") { continue; }
        let text = node["text"].as_str().unwrap_or("");
        let section = metadata["section_path"].as_str().unwrap_or("");
        let body_keys = lexical_keys(text);
        let heading_keys = lexical_keys(section);
        let score = keys.iter().filter(|key| body_keys.contains(*key)).count() as f32
            + 3.0 * keys.iter().filter(|key| heading_keys.contains(*key)).count() as f32;
        if score <= 0.0 { continue; }
        results.push(crate::agent::RetrievalEvidence {
            file: metadata["file_name"].as_str().unwrap_or("").to_string(),
            chunk_id: index, score,
            section_path: section.to_string(),
            section_title: metadata["section_title"].as_str().unwrap_or(section).to_string(),
            status: metadata["status"].as_str().unwrap_or("active").to_string(),
            chunk_type: metadata["chunk_type"].as_str().unwrap_or("unknown").to_string(),
            text: text.to_string(),
        });
    }
    results.sort_by(|left, right| right.score.total_cmp(&left.score).then(left.chunk_id.cmp(&right.chunk_id)));
    results.truncate(40);
    Ok(results)
}

fn lexical_keys(value: &str) -> std::collections::HashSet<String> {
    value.to_lowercase().split(|c: char| !c.is_alphanumeric())
        .filter(|word| word.chars().count() >= 2 && !["на", "по", "из", "со", "во", "как", "что", "для"].contains(word))
        .map(|word| word.chars().take(5).collect()).collect()
}

pub struct Retriever<'a> {
    store: &'a VectorStore,
    embedder: &'a Embedder,
    bm25: &'a Bm25Index,
}

impl<'a> Retriever<'a> {
    pub fn new(store: &'a VectorStore, embedder: &'a Embedder, bm25: &'a Bm25Index) -> Self {
        Self { store, embedder, bm25 }
    }

    pub async fn retrieve(&self, question: &str) -> Result<Vec<SearchResult>> {
        // chunk_id -> (rrf_score, SearchResult)
        let mut scores: HashMap<(String, usize), (f32, Option<SearchResult>)> = HashMap::new();

        // 1. Vector search — семантическое сходство
        let query_vec = self.embedder.embed_one(question)?;
        let vector_results = self.store.search(query_vec, VECTOR_CANDIDATES).await?;
        for (rank, r) in vector_results.into_iter().enumerate() {
            let rrf = 1.0 / (RRF_K + rank as f32 + 1.0);
            let entry = scores.entry((r.source.clone(), r.chunk_id)).or_insert((0.0, None));
            entry.0 += rrf;
            if entry.1.is_none() { entry.1 = Some(r); }
        }

        // 2. BM25 по полному тексту — для слов из вопроса
        if let Ok(bm25_full) = self.bm25.search(question, BM25_FULLTEXT_CANDIDATES) {
            for (rank, b) in bm25_full.into_iter().enumerate() {
                let rrf = 1.5 / (RRF_K + rank as f32 + 1.0); // x1.5 буст
                let entry = scores.entry((b.source.clone(), b.chunk_id)).or_insert((0.0, None));
                entry.0 += rrf;
                if entry.1.is_none() {
                    entry.1 = Some(SearchResult {
                        score: b.score, text: b.text,
                        source: b.source, chunk_id: b.chunk_id, section: b.section,
                    });
                }
            }
        }

        // 3. BM25 по заголовкам разделов — per abbreviation, МАКСИМАЛЬНЫЙ БУСТ x5
        // Ищем каждую аббревиатуру отдельно в поле section
        // Это находит "Внешняя инициатива ИВ1" для запроса "ИВ1" и т.д.
        for abbr in extract_abbreviations(question) {
            if let Ok(section_results) = self.bm25.search_in_sections(&abbr, BM25_SECTION_CANDIDATES) {
                for (rank, b) in section_results.into_iter().enumerate() {
                    let rrf = 5.0 / (RRF_K + rank as f32 + 1.0); // x5 буст — section title match
                    let entry = scores.entry((b.source.clone(), b.chunk_id)).or_insert((0.0, None));
                    entry.0 += rrf;
                    if entry.1.is_none() {
                        entry.1 = Some(SearchResult {
                            score: b.score, text: b.text,
                            source: b.source, chunk_id: b.chunk_id, section: b.section,
                        });
                    }
                }
            }
        }

        // Сортируем и берём топ
        let mut merged: Vec<(f32, SearchResult)> = scores
            .into_values()
            .filter_map(|(score, r)| r.map(|r| (score, r)))
            .collect();

        merged.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
        merged.truncate(TOP_K);

        Ok(merged.into_iter().map(|(score, mut r)| { r.score = score; r }).collect())
    }
}

/// Извлекаем аббревиатуры из вопроса.
/// Критерии: содержит заглавные буквы + цифры или просто заглавные буквы, длина 2-8 символов.
/// Примеры: ИВ0-1, ИВ0, ИВ1, ИС, ИВА, ИВК, ГТП, ПБР, УДГ
fn extract_abbreviations(question: &str) -> Vec<String> {
    let mut result = Vec::new();
    for word in question.split_whitespace() {
        // Убираем знаки препинания
        let clean: String = word
            .trim_matches(|c: char| !c.is_alphanumeric() && c != '-')
            .to_string();
        if clean.chars().count() < 2 || clean.chars().count() > 8 {
            continue;
        }
        let has_upper = clean.chars().any(|c| c.is_uppercase());
        let has_letter = clean.chars().any(|c| c.is_alphabetic());
        // Считаем аббревиатурой если есть заглавные буквы и не полностью строчное слово
        if has_upper && has_letter {
            result.push(clean);
        }
    }
    result.dedup();
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_retrieval_reads_both_sources_without_vector_initialization() {
        let corpus = r#"{"docstore/data":{"a":{"__data__":{"text":"ИС — собственная инициатива","metadata":{"file_name":"one.docx","section_path":"ИС"}}},"b":{"__data__":{"text":"ИС определяется правилом","metadata":{"file_name":"two.docx","section_path":"ИС"}}}}}"#;
        let hits = parse_canonical(corpus, "ИС").unwrap();
        assert_eq!(hits.len(), 2);
        assert_ne!(hits[0].file, hits[1].file);
    }

    #[test]
    fn deleted_source_cannot_be_returned() {
        let corpus = r#"{"docstore/data":{"a":{"__data__":{"text":"ХОП","metadata":{"file_name":"deleted.docx","status":"deleted"}}}}}"#;
        assert!(parse_canonical(corpus, "ХОП").unwrap().is_empty());
    }

    #[test]
    fn unicode_abbreviation_length_counts_characters() {
        assert!(extract_abbreviations("ОГРАНИЧ").contains(&"ОГРАНИЧ".to_string()));
    }
}
