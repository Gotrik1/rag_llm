use anyhow::Result;
use serde::{Deserialize, Serialize};

const OLLAMA_URL: &str = "http://localhost:11434/api/generate";
const MODEL: &str = "qwen2.5:14b";

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
    num_predict: u32,
}

#[derive(Deserialize)]
struct OllamaResponse {
    response: String,
}

pub struct OllamaClient {
    client: reqwest::Client,
}

impl OllamaClient {
    pub fn new() -> Self {
        Self {
            client: reqwest::Client::new(),
        }
    }

    pub async fn generate(&self, context: &str, question: &str) -> Result<String> {
        self.generate_with_model(context, question, MODEL).await
    }

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
            - ЗАПРЕЩЕНО смешивать информацию из разных документов — указывай из какого раздела берёшь.\n\
            - Если нужная информация есть в контексте — дай полный структурированный ответ с формулами.\n\
            - Если информации недостаточно — скажи конкретно чего не хватает, не додумывай.\n\
            - Для инструкции дай 4–7 нумерованных действий от начала до сохранения/подтверждения.\n\
            - Не смешивай создание обычного параметра с документами, макросами, копированием или удалением.\n\
            - Игнорируй нерелевантные фрагменты, даже если они находятся в переданном контексте.\n\
            - Показывай только финальный ответ. Не упоминай контекст, retrieval, источники или внутреннюю проверку.\n\
            - Не добавляй формулы, версии и примечания, если пользователь их не спрашивал.\n\
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
                num_ctx: 8192,
                num_predict: 768,
            },
        };

        let resp = self
            .client
            .post(OLLAMA_URL)
            .json(&request)
            .send()
            .await?
            .json::<OllamaResponse>()
            .await?;

        Ok(resp.response.trim().to_string())
    }
}
