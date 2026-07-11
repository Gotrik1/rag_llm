use anyhow::Result;
use sha2::{Digest, Sha256};
use rusqlite::Connection;
use serde::Serialize;
use std::env;
use std::path::PathBuf;

use crate::bm25::Bm25Index;
use crate::embed::Embedder;
use crate::ingest;
use crate::llm::OllamaClient;
use crate::retrieval::Retriever;
use crate::store::VectorStore;

#[derive(Serialize)]
pub struct AgentResponse {
    pub answer: String,
    pub sources: Vec<Source>,
    pub from_cache: bool,
}

#[derive(Serialize)]
pub struct Source {
    pub file: String,
    pub chunk_id: usize,
    pub section: String,
}

#[derive(Serialize)]
pub struct RetrievalEvidence {
    pub file: String,
    pub chunk_id: usize,
    pub score: f32,
    pub section_path: String,
    pub section_title: String,
    pub status: String,
    pub chunk_type: String,
    pub text: String,
}

pub struct Agent {
    embedder: Embedder,
    store: VectorStore,
    bm25: Bm25Index,
    llm: OllamaClient,
    cache: Connection,
}

impl Agent {
    pub async fn new() -> Result<Self> {
        let data_dir = PathBuf::from(env::var("RUST_DATA_DIR").unwrap_or_else(|_| ".".to_string()));
        std::fs::create_dir_all(&data_dir)?;
        let data_dir_string = data_dir.to_string_lossy().into_owned();
        let embedder = Embedder::new()?;
        let store = VectorStore::new().await?;
        let bm25 = Bm25Index::open_or_create(&data_dir_string)?;
        let llm = OllamaClient::new();
        let cache = Connection::open(data_dir.join("cache.db"))?;

        cache.execute_batch(
            "CREATE TABLE IF NOT EXISTS answer_cache (
                question_hash TEXT PRIMARY KEY,
                question      TEXT,
                answer        TEXT,
                sources       TEXT,
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )",
        )?;

        Ok(Self { embedder, store, bm25, llm, cache })
    }

    pub async fn ingest(&self, path: &str) -> Result<usize> {
        let chunks = ingest::parse_and_chunk(path)?;
        let n = chunks.len();

        // BM25 индекс — перестраиваем при каждой загрузке
        let mut writer = self.bm25.writer()?;
        // Не очищаем полностью — просто добавляем (при повторной загрузке будут дубли,
        // но для MVP это приемлемо)

        let texts: Vec<String> = chunks.iter().map(|c| c.text.clone()).collect();
        let embeddings = self.embedder.embed_batch(texts)?;

        for (chunk, embedding) in chunks.iter().zip(embeddings.iter()) {
            let id = stable_id(&chunk.source_file, chunk.chunk_id);
            self.store.upsert(
                id,
                embedding.clone(),
                &chunk.source_file,
                chunk.chunk_id,
                &chunk.text,
                &chunk.section,
            ).await?;

            self.bm25.add_doc(
                &mut writer,
                chunk.chunk_id,
                &chunk.source_file,
                &chunk.section,
                &chunk.text,
            )?;
        }

        Bm25Index::commit(&mut writer)?;
        tracing::info!("BM25: проиндексировано {} документов", n);

        Ok(n)
    }

    pub async fn ask(&self, question: &str) -> Result<AgentResponse> {
        let hash = question_hash(question);

        // Проверяем кэш
        let cached: Option<(String, String)> = self.cache
            .query_row(
                "SELECT answer, sources FROM answer_cache WHERE question_hash = ?1",
                [&hash],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .ok();

        if let Some((answer, sources_json)) = cached {
            let sources: Vec<serde_json::Value> = serde_json::from_str(&sources_json)?;
            let sources = sources.iter().map(|s| Source {
                file: s["file"].as_str().unwrap_or("").to_string(),
                chunk_id: s["chunk_id"].as_u64().unwrap_or(0) as usize,
                section: s["section"].as_str().unwrap_or("").to_string(),
            }).collect();
            return Ok(AgentResponse { answer, sources, from_cache: true });
        }

        // Hybrid retrieval: vector + BM25
        let retriever = Retriever::new(&self.store, &self.embedder, &self.bm25);
        let results = retriever.retrieve(question).await?;

        if results.is_empty() {
            return Ok(AgentResponse {
                answer: "В базе знаний нет релевантных документов. Загрузите документы командой :load".to_string(),
                sources: vec![],
                from_cache: false,
            });
        }

        // Контекст для LLM
        let context = results.iter().map(|r| {
            if r.section.is_empty() {
                format!("[Источник: {}]\n{}", r.source, r.text)
            } else {
                format!("[Источник: {}, Раздел: {}]\n{}", r.source, r.section, r.text)
            }
        }).collect::<Vec<_>>().join("\n\n---\n\n");

        let answer = self.llm.generate(&context, question).await?;

        let sources: Vec<Source> = results.iter().map(|r| Source {
            file: r.source.clone(),
            chunk_id: r.chunk_id,
            section: r.section.clone(),
        }).collect();

        // Кэш
        let sources_json = serde_json::to_string(
            &sources.iter().map(|s| serde_json::json!({
                "file": s.file,
                "chunk_id": s.chunk_id,
                "section": s.section,
            })).collect::<Vec<_>>()
        )?;

        self.cache.execute(
            "INSERT OR REPLACE INTO answer_cache (question_hash, question, answer, sources)
             VALUES (?1, ?2, ?3, ?4)",
            rusqlite::params![hash, question, answer, sources_json],
        )?;

        Ok(AgentResponse { answer, sources, from_cache: false })
    }

    /// Generate from context selected by the Python/Rust orchestrator.
    /// This intentionally bypasses the answer cache: mode switches must always
    /// produce a fresh answer from the currently selected evidence.
    pub async fn generate_from_context(
        &self,
        question: &str,
        context: &str,
        model: Option<&str>,
    ) -> Result<String> {
        match model.filter(|value| !value.trim().is_empty()) {
            Some(model) => self.llm.generate_with_model(context, question, model).await,
            None => self.llm.generate(context, question).await,
        }
    }

    /// Прямой BM25 поиск для диагностики retrieval
    pub fn bm25_search(&self, query: &str) -> Result<Vec<crate::bm25::Bm25Result>> {
        self.bm25.search(query, 10)
    }

    /// Машинный retrieval-контракт для Python/UI orchestration.
    pub async fn retrieve_evidence(&self, question: &str) -> Result<Vec<RetrievalEvidence>> {
        let retriever = Retriever::new(&self.store, &self.embedder, &self.bm25);
        let results = retriever.retrieve(question).await?;
        Ok(results.into_iter().map(|r| {
            let (section_title, status, chunk_type) = classify_section(&r.section, &r.text);
            RetrievalEvidence {
                file: r.source,
                chunk_id: r.chunk_id,
                score: r.score,
                section_path: r.section,
                section_title,
                status,
                chunk_type,
                text: r.text,
            }
        }).collect())
    }
}

fn classify_section(section: &str, text: &str) -> (String, String, String) {
    let section_title = section
        .split('>')
        .last()
        .map(str::trim)
        .unwrap_or("")
        .to_string();
    let status_scope = format!("{}\n{}", section, text.chars().take(900).collect::<String>()).to_lowercase();
    let text_lower = text.to_lowercase();

    let status = if status_scope.contains("утратил силу")
        || status_scope.contains("пункт удален")
        || status_scope.contains("исключен")
        || status_scope.contains("не применяется")
    {
        "deleted"
    } else {
        "active"
    }.to_string();

    let mut chunk_type = "unknown";
    if text_lower.contains("[с_sub(")
        || text_lower.contains("[т_sub(")
        || text_lower.contains("расчетные показатели стоимости")
        || text_lower.contains("стоимост")
        || text_lower.contains("ставк")
    {
        chunk_type = "cost";
    }
    if text_lower.contains("исходные данные")
        || text_lower.contains("предварительных обязательств")
        || text_lower.contains("предварительных требований")
    {
        chunk_type = "rate";
    }
    if status == "deleted" {
        chunk_type = "history";
    }
    if text_lower.contains("определение величины отклонений")
        || text_lower.contains("виды инициатив")
        || text_lower.contains("составляющая величина отклонения")
        || text_lower.contains("объем отклонения")
    {
        chunk_type = "value";
    }

    (section_title, status, chunk_type.to_string())
}

fn question_hash(question: &str) -> String {
    let normalized: String = question
        .to_lowercase()
        .chars()
        .filter(|c| c.is_alphabetic() || c.is_whitespace())
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");

    let mut hasher = Sha256::new();
    hasher.update(normalized.as_bytes());
    hex::encode(hasher.finalize())
}

fn stable_id(source: &str, chunk_id: usize) -> u64 {
    let mut hasher = Sha256::new();
    hasher.update(format!("{}:{}", source, chunk_id).as_bytes());
    let result = hasher.finalize();
    u64::from_le_bytes(result[..8].try_into().unwrap())
}
