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
        let mut scores: HashMap<usize, (f32, Option<SearchResult>)> = HashMap::new();

        // 1. Vector search — семантическое сходство
        let query_vec = self.embedder.embed_one(question)?;
        let vector_results = self.store.search(query_vec, VECTOR_CANDIDATES).await?;
        for (rank, r) in vector_results.into_iter().enumerate() {
            let rrf = 1.0 / (RRF_K + rank as f32 + 1.0);
            let entry = scores.entry(r.chunk_id).or_insert((0.0, None));
            entry.0 += rrf;
            if entry.1.is_none() { entry.1 = Some(r); }
        }

        // 2. BM25 по полному тексту — для слов из вопроса
        if let Ok(bm25_full) = self.bm25.search(question, BM25_FULLTEXT_CANDIDATES) {
            for (rank, b) in bm25_full.into_iter().enumerate() {
                let rrf = 1.5 / (RRF_K + rank as f32 + 1.0); // x1.5 буст
                let entry = scores.entry(b.chunk_id).or_insert((0.0, None));
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
                    let entry = scores.entry(b.chunk_id).or_insert((0.0, None));
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

        Ok(merged.into_iter().map(|(_, r)| r).collect())
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
        if clean.len() < 2 || clean.len() > 8 {
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
