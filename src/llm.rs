//! Ollama-compatible generation client for the optional Rust commands.
//!
//! The caller supplies authorized context. This module formats a prompt but
//! does not perform source selection, grounding checks or durable publication.
//! Endpoint, default model and budgets come from the Rust process environment.

use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::env;

const DEFAULT_OLLAMA_BASE_URL: &str = "http://localhost:11434";
const DEFAULT_MODEL: &str = "qwen2.5:14b";

#[derive(Serialize)]
struct OllamaRequest<'a> {
    model: &'a str,
    prompt: String,
    stream: bool,
    think: bool,
    options: OllamaOptions,
}

#[derive(Serialize)]
struct OllamaOptions {
    temperature: f32,
    seed: u32,
    top_k: u32,
    top_p: f32,
    num_ctx: u32,
    num_predict: i32,
}

#[derive(Deserialize)]
struct OllamaResponse {
    response: String,
}

/// Reuse an HTTP client across non-streaming `/api/generate` requests.
pub struct OllamaClient {
    client: reqwest::Client,
}

impl OllamaClient {
    /// Construct the client without performing a model or network health check.
    pub fn new() -> Self {
        Self {
            client: reqwest::Client::new(),
        }
    }

    /// Use `LLM_MODEL`, falling back to the standalone CLI model name.
    pub async fn generate(&self, context: &str, question: &str) -> Result<String> {
        let model = env::var("LLM_MODEL").unwrap_or_else(|_| DEFAULT_MODEL.to_string());
        self.generate_with_model(context, question, &model).await
    }

    /// Generate a fresh answer from supplied evidence without an answer cache.
    /// HTTP/model errors propagate; a successful response is not a grounding proof.
    pub async fn generate_with_model(&self, context: &str, question: &str, model: &str) -> Result<String> {
        let prompt = format!(
            "<|im_start|>system\n\
            Ты — строгий аналитик нормативных документов. Отвечай ТОЛЬКО на русском языке.\n\
            \n\
            КРИТИЧЕСКИЕ ПРАВИЛА (нарушение недопустимо):\n\
            - ЗАПРЕЩЕНО расшифровывать аббревиатуры иначе, чем они определены в тексте контекста.\n\
              Если в тексте написано «ИС — собственная инициатива», используй именно это.\n\
              Если аббревиатура не расшифрована в контексте — оставь её как есть.\n\
            - ЗАПРЕЩЕНО выдумывать формулы, пункты, ссылки, которых нет в контексте.\n\
            - Сопоставляй относящиеся к вопросу документы и ставь цитату [Источник: имя файла — раздел] после подтверждённого утверждения.\n\
            - Сначала дай основное правило, затем частные случаи с условиями. Не подменяй основной раздел похожим исключением.\n\
            - Если нужная информация есть в контексте — дай полный структурированный ответ с формулами.\n\
            - Если информации недостаточно — скажи конкретно чего не хватает, не додумывай.\n\
            - Для инструкции дай 4–7 нумерованных действий от начала до сохранения/подтверждения.\n\
            - Не смешивай создание обычного параметра с документами, макросами, копированием или удалением.\n\
            - Игнорируй нерелевантные фрагменты, даже если они находятся в переданном контексте.\n\
            - Показывай финальный ответ с ссылками на документы. Не упоминай внутреннюю проверку.\n\
            - Не добавляй формулы, версии и примечания, если пользователь их не спрашивал.\n\
            - Форматируй ответ строго в CommonMark Markdown: абзацы разделяй одной пустой строкой, используй корректные списки и таблицы GFM только для сравнения однотипных данных с короткими значениями. Если в ячейке нужен перечень, не создавай таблицу — используй подзаголовок и маркированный список. Не используй HTML и не оставляй незакрытые Markdown-маркеры.\n\
            <|im_end|>\n\
            <|im_start|>user\n\
            КОНТЕКСТ ИЗ ДОКУМЕНТА:\n\
            {}\n\n\
            ВОПРОС: {}\n\
            <|im_end|>\n\
            <|im_start|>assistant\n",
            context, question
        );

        let request = OllamaRequest {
            model,
            prompt,
            stream: false,
            think: false,
            options: OllamaOptions {
                temperature: 0.0,  // детерминизм
                seed: 42,          // фиксированный seed
                top_k: 1,
                top_p: 1.0,
                num_ctx: env::var("RAG_CONTEXT_WINDOW").ok().and_then(|v| v.parse().ok()).unwrap_or(32768),
                num_predict: env::var("RAG_MAX_OUTPUT_TOKENS").ok().and_then(|v| v.parse().ok()).unwrap_or(-1),
            },
        };

        let resp = self
            .client
            .post(ollama_generate_url())
            .json(&request)
            .send()
            .await?
            .error_for_status()?
            .json::<OllamaResponse>()
            .await?;

        Ok(resp.response.trim().to_string())
    }
}

/// Accept either an Ollama base URL or its complete `/api/generate` endpoint.
fn ollama_generate_url() -> String {
    let base_url = env::var("OLLAMA_BASE_URL")
        .unwrap_or_else(|_| DEFAULT_OLLAMA_BASE_URL.to_string());
    if base_url.ends_with("/api/generate") {
        base_url
    } else {
        format!("{}/api/generate", base_url.trim_end_matches('/'))
    }
}
