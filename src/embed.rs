//! Embeddings for the independent Rust vector path, using a 384-dimensional model.
//!
//! These vectors are not interchangeable with Python's configured embedding
//! model. Canonical lexical retrieval deliberately avoids initializing this module.

use anyhow::Result;
use fastembed::{EmbeddingModel, InitOptions, TextEmbedding};

/// Own the FastEmbed multilingual MiniLM model used by the Rust vector collection.
pub struct Embedder {
    model: TextEmbedding,
}

impl Embedder {
    /// Initialize model assets; FastEmbed may download them when not cached.
    pub fn new() -> Result<Self> {
        let model = TextEmbedding::try_new(
            InitOptions::new(EmbeddingModel::ParaphraseMLMiniLML12V2)
                .with_show_download_progress(true),
        )?;
        Ok(Self { model })
    }

    /// Embed inputs in order; model/inference failures propagate to the caller.
    pub fn embed_batch(&self, texts: Vec<String>) -> Result<Vec<Vec<f32>>> {
        let embeddings = self.model.embed(texts, None)?;
        Ok(embeddings)
    }

    /// Embed one query in the same vector space as `embed_batch` documents.
    pub fn embed_one(&self, text: &str) -> Result<Vec<f32>> {
        let mut result = self.embed_batch(vec![text.to_string()])?;
        Ok(result.remove(0))
    }
}
