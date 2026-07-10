use anyhow::Result;
use fastembed::{EmbeddingModel, InitOptions, TextEmbedding};

pub struct Embedder {
    model: TextEmbedding,
}

impl Embedder {
    pub fn new() -> Result<Self> {
        let model = TextEmbedding::try_new(
            InitOptions::new(EmbeddingModel::ParaphraseMLMiniLML12V2)
                .with_show_download_progress(true),
        )?;
        Ok(Self { model })
    }

    pub fn embed_batch(&self, texts: Vec<String>) -> Result<Vec<Vec<f32>>> {
        let embeddings = self.model.embed(texts, None)?;
        Ok(embeddings)
    }

    pub fn embed_one(&self, text: &str) -> Result<Vec<f32>> {
        let mut result = self.embed_batch(vec![text.to_string()])?;
        Ok(result.remove(0))
    }
}
