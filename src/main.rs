//! Standalone Rust CLI and line-oriented compatibility commands for Python.
//!
//! `:retrieve-json` reads the canonical Python docstore without initializing
//! vector/embedding services; `:generate-json` performs a fresh generation call.
//! Interactive `:load` / `:ask-json` instead use the separate Rust stores/cache.
//! This process is not an authenticated HTTP service or a replacement for Python
//! document scope, persistent jobs, answer verification or publication policy.

mod ingest;
mod embed;
mod store;
mod retrieval;
mod bm25;
mod llm;
mod agent;

use anyhow::Result;
use serde::Deserialize;
use std::io::{self, BufRead, Write};
use tracing::info;

#[derive(Deserialize)]
/// Context supplied by the caller; this command does not retrieve or authorize it.
struct GenerateRequest {
    question: String,
    context: String,
    model: Option<String>,
}

#[tokio::main]
/// Dispatch stdin commands; machine JSON responses coexist with CLI banners.
async fn main() -> Result<()> {
    tracing_subscriber::fmt::init();

    println!("=== RAG Agent (Qwen 2.5 + Qdrant) ===");
    println!("Команды:");
    println!("  :load <путь>      — индексировать документ");
    println!("  :sections <путь>  — показать разделы без индексирования");
    println!("  :bm25 <запрос>    — прямой BM25 поиск (диагностика)");
    println!("  :retrieve-json <запрос> — retrieval-кандидаты в JSON");
    println!("  :ask-json <запрос> — ответ и источники в JSON (для UI)");
    println!("  :generate-json <JSON> — свежий ответ по переданному контексту");
    println!("  :exit             — выход");
    println!("  <вопрос>          — задать вопрос базе знаний");
    println!();

    // Machine retrieval/generation uses the canonical corpus and must not
    // initialise an unrelated vector store or download an embedding model.
    let mut agent = None;

    let stdin = io::stdin();
    loop {
        print!("> ");
        io::stdout().flush()?;

        let mut line = String::new();
        if stdin.lock().read_line(&mut line)? == 0 { break; }
        let input = line.trim();

        if input.is_empty() {
            continue;
        }

        if input == ":exit" {
            break;
        }

        // Диагностика: показать разделы из документа
        if let Some(path) = input.strip_prefix(":sections ") {
            match ingest::parse_and_chunk(path) {
                Ok(chunks) => {
                    println!("Найдено {} чанков. Разделы:", chunks.len());
                    let mut seen = std::collections::HashSet::new();
                    for c in &chunks {
                        if !c.section.is_empty() && seen.insert(c.section.clone()) {
                            println!("  [{:04}] {}", c.chunk_id, c.section);
                        }
                    }
                }
                Err(e) => eprintln!("✗ {}", e),
            }
            continue;
        }

        // Диагностика: прямой BM25 поиск
        if let Some(query) = input.strip_prefix(":bm25 ") {
            let agent = initialized_agent(&mut agent).await?;
            match agent.bm25_search(query) {
                Ok(results) => {
                    println!("BM25 топ-10 для «{}»:", query);
                    for (i, r) in results.iter().enumerate() {
                        let preview: String = r.text.chars().take(80).collect();
                        println!("  {}. [score={:.3}] {} — {}", i+1, r.score, r.section, preview);
                    }
                }
                Err(e) => eprintln!("✗ {}", e),
            }
            continue;
        }

        // Машинный retrieval-контракт для Python orchestration
        if let Some(query) = input.strip_prefix(":retrieve-json ") {
            match retrieval::retrieve_canonical(query) {
                Ok(results) => println!("{}", serde_json::to_string(&results)?),
                Err(e) => eprintln!("✗ {}", e),
            }
            continue;
        }

        // Standalone JSON answer command: this uses the local question-only
        // SQLite cache and is not the scoped Python HTTP orchestration path.
        if let Some(query) = input.strip_prefix(":ask-json ") {
            let agent = initialized_agent(&mut agent).await?;
            match agent.ask(query).await {
                Ok(response) => println!("{}", serde_json::to_string(&response)?),
                Err(e) => eprintln!("✗ {}", e),
            }
            continue;
        }

        if let Some(payload) = input.strip_prefix(":generate-json ") {
            match serde_json::from_str::<GenerateRequest>(payload) {
                Ok(request) => {
                    let client = llm::OllamaClient::new();
                    let result = match request.model.as_deref() {
                        Some(model) => client.generate_with_model(&request.context, &request.question, model).await,
                        None => client.generate(&request.context, &request.question).await,
                    };
                    match result {
                    Ok(answer) => println!("{}", serde_json::json!({ "answer": answer })),
                    Err(e) => eprintln!("✗ {}", e),
                    }
                },
                Err(e) => eprintln!("✗ Некорректный generate-json: {}", e),
            }
            continue;
        }

        if let Some(path) = input.strip_prefix(":load ") {
            let agent = initialized_agent(&mut agent).await?;
            match agent.ingest(path).await {
                Ok(n) => println!("✓ Проиндексировано {} чанков из {}", n, path),
                Err(e) => eprintln!("✗ Ошибка: {}", e),
            }
        } else {
            let agent = initialized_agent(&mut agent).await?;
            let t0 = std::time::Instant::now();
            match agent.ask(input).await {
                Ok(response) => {
                    let elapsed = t0.elapsed().as_millis();
                    if response.from_cache {
                        println!("[КЭШ — {}мс]", elapsed);
                    } else {
                        println!("[ЖИВОЙ ЗАПРОС — {}мс]", elapsed);
                    }
                    println!("\nОТВЕТ:\n{}", response.answer);
                    println!("\nИСТОЧНИКИ:");
                    for src in &response.sources {
                        if src.section.is_empty() {
                            println!("  • {} (чанк {})", src.file, src.chunk_id);
                        } else {
                            println!("  • {} — {} (чанк {})", src.file, src.section, src.chunk_id);
                        }
                    }
                    println!();
                }
                Err(e) => eprintln!("✗ Ошибка: {}", e),
            }
        }
    }

    Ok(())
}

/// Initialize the optional CLI stores only for commands that actually need them.
async fn initialized_agent(slot: &mut Option<agent::Agent>) -> Result<&agent::Agent> {
    if slot.is_none() {
        *slot = Some(agent::Agent::new().await?);
        info!("Агент инициализирован");
    }
    Ok(slot.as_ref().expect("agent initialized"))
}
