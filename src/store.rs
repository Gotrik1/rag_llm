//! Qdrant storage for the independent 384-dimensional Rust embedding space.
//!
//! The fixed `knowledge_base` collection has source/section/text payloads but no
//! tenant or project filtering. Do not point this path at an incompatible Python
//! embedding collection or treat it as the authenticated application registry.

use anyhow::Result;
use qdrant_client::qdrant::{
    CreateCollectionBuilder, Distance, PointStruct, UpsertPointsBuilder,
    VectorParamsBuilder,
};
use qdrant_client::Qdrant;
use serde_json::json;
use std::collections::HashMap;
use std::env;

const COLLECTION: &str = "knowledge_base";
/// Must match `embed::Embedder`'s multilingual MiniLM output dimension.
pub const VECTOR_DIM: u64 = 384;

/// Hold the Qdrant client used by the optional standalone Rust agent.
pub struct VectorStore {
    client: Qdrant,
}

impl VectorStore {
    /// Connect using `QDRANT_GRPC_URL` and create the collection when absent.
    pub async fn new() -> Result<Self> {
        let qdrant_url = env::var("QDRANT_GRPC_URL")
            .unwrap_or_else(|_| "http://localhost:6334".to_string());
        let client = Qdrant::from_url(&qdrant_url).build()?;
        let store = Self { client };
        store.ensure_collection().await?;
        Ok(store)
    }

    /// Ensure existence only; this does not migrate an existing vector schema.
    async fn ensure_collection(&self) -> Result<()> {
        let collections = self.client.list_collections().await?;
        let exists = collections.collections.iter().any(|c| c.name == COLLECTION);
        if !exists {
            self.client
                .create_collection(
                    CreateCollectionBuilder::new(COLLECTION)
                        .vectors_config(VectorParamsBuilder::new(VECTOR_DIM, Distance::Cosine)),
                )
                .await?;
            tracing::info!("Коллекция '{}' создана", COLLECTION);
        }
        Ok(())
    }

    /// Replace a point with the supplied stable id and the source evidence payload.
    pub async fn upsert(
        &self,
        id: u64,
        vector: Vec<f32>,
        source: &str,
        chunk_id: usize,
        text: &str,
        section: &str,
    ) -> Result<()> {
        let mut payload = HashMap::new();
        payload.insert("source".to_string(), json!(source));
        payload.insert("chunk_id".to_string(), json!(chunk_id));
        payload.insert("text".to_string(), json!(text));
        payload.insert("section".to_string(), json!(section));

        let point = PointStruct::new(id, vector, payload);
        self.client
            .upsert_points(UpsertPointsBuilder::new(COLLECTION, vec![point]))
            .await?;
        Ok(())
    }

    /// Return unscoped nearest neighbours with payloads; callers enforce scope.
    pub async fn search(&self, query_vector: Vec<f32>, top_k: u64) -> Result<Vec<SearchResult>> {
        use qdrant_client::qdrant::SearchPointsBuilder;

        let results = self.client
            .search_points(
                SearchPointsBuilder::new(COLLECTION, query_vector, top_k)
                    .with_payload(true),
            )
            .await?;

        let hits = results
            .result
            .into_iter()
            .map(|hit| {
                let payload = hit.payload;
                SearchResult {
                    score: hit.score,
                    text: payload.get("text").and_then(|v| v.as_str()).map_or("", |v| v).to_string(),
                    source: payload.get("source").and_then(|v| v.as_str()).map_or("", |v| v).to_string(),
                    chunk_id: payload.get("chunk_id").and_then(|v| v.as_integer()).unwrap_or(0) as usize,
                    section: payload.get("section").and_then(|v| v.as_str()).map_or("", |v| v).to_string(),
                }
            })
            .collect();

        Ok(hits)
    }
}

/// Candidate evidence; `score` may later be replaced by an RRF fusion weight.
pub struct SearchResult {
    pub score: f32,
    pub text: String,
    pub source: String,
    pub chunk_id: usize,
    pub section: String,
}
