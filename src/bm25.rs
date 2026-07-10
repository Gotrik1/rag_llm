/// BM25 полнотекстовый поиск через Tantivy.
use anyhow::Result;
use tantivy::collector::TopDocs;
use tantivy::query::QueryParser;
use tantivy::schema::*;
use tantivy::{doc, Index, IndexWriter, ReloadPolicy};
use std::path::Path;

pub struct Bm25Index {
    index: Index,
    chunk_id_field: Field,
    source_field: Field,
    section_field: Field,
    text_field: Field,
}

impl Bm25Index {
    pub fn open_or_create(dir: &str) -> Result<Self> {
        let path = Path::new(dir).join("tantivy_index");
        std::fs::create_dir_all(&path)?;

        let mut schema_builder = Schema::builder();
        let chunk_id_field = schema_builder.add_u64_field("chunk_id", STORED | FAST);
        let source_field   = schema_builder.add_text_field("source",   STORED);
        let section_field  = schema_builder.add_text_field("section",  STORED | TEXT);
        let text_field     = schema_builder.add_text_field("text",     STORED | TEXT);
        let schema = schema_builder.build();

        let index = if path.join("meta.json").exists() {
            Index::open_in_dir(&path)?
        } else {
            Index::create_in_dir(&path, schema.clone())?
        };

        Ok(Self { index, chunk_id_field, source_field, section_field, text_field })
    }

    pub fn writer(&self) -> Result<IndexWriter> {
        Ok(self.index.writer(50_000_000)?)
    }

    pub fn add_doc(
        &self,
        writer: &mut IndexWriter,
        chunk_id: usize,
        source: &str,
        section: &str,
        text: &str,
    ) -> Result<()> {
        writer.add_document(doc!(
            self.chunk_id_field => chunk_id as u64,
            self.source_field   => source,
            self.section_field  => section,
            self.text_field     => text,
        ))?;
        Ok(())
    }

    pub fn commit(writer: &mut IndexWriter) -> Result<()> {
        writer.commit()?;
        Ok(())
    }

    /// Полный поиск по тексту И заголовку раздела
    pub fn search(&self, query_str: &str, top_k: usize) -> Result<Vec<Bm25Result>> {
        let reader = self.reader()?;
        let searcher = reader.searcher();
        let query_parser = QueryParser::for_index(&self.index, vec![self.text_field, self.section_field]);
        let safe = escape_query(query_str);
        let query = query_parser.parse_query(&safe)
            .or_else(|_| {
                let terms = query_str.split_whitespace()
                    .map(|t| escape_query(t))
                    .collect::<Vec<_>>()
                    .join(" ");
                query_parser.parse_query(&terms)
            })?;
        self.collect_results(&searcher, &query, top_k)
    }

    /// Поиск ТОЛЬКО по заголовкам разделов — для точного поиска аббревиатур
    pub fn search_in_sections(&self, query_str: &str, top_k: usize) -> Result<Vec<Bm25Result>> {
        let reader = self.reader()?;
        let searcher = reader.searcher();
        let query_parser = QueryParser::for_index(&self.index, vec![self.section_field]);
        let safe = escape_query(query_str);
        let query = query_parser.parse_query(&safe)
            .or_else(|_| {
                let terms = query_str.split_whitespace()
                    .map(|t| escape_query(t))
                    .collect::<Vec<_>>()
                    .join(" ");
                query_parser.parse_query(&terms)
            })?;
        self.collect_results(&searcher, &query, top_k)
    }

    fn reader(&self) -> Result<tantivy::IndexReader> {
        Ok(self.index
            .reader_builder()
            .reload_policy(ReloadPolicy::OnCommitWithDelay)
            .try_into()?)
    }

    fn collect_results(
        &self,
        searcher: &tantivy::Searcher,
        query: &dyn tantivy::query::Query,
        top_k: usize,
    ) -> Result<Vec<Bm25Result>> {
        let top_docs = searcher.search(query, &TopDocs::with_limit(top_k))?;
        let mut results = Vec::new();
        for (score, doc_address) in top_docs {
            let doc: TantivyDocument = searcher.doc(doc_address)?;
            let chunk_id = doc.get_first(self.chunk_id_field).and_then(|v| v.as_u64()).unwrap_or(0) as usize;
            let source   = doc.get_first(self.source_field).and_then(|v| v.as_str()).unwrap_or("").to_string();
            let section  = doc.get_first(self.section_field).and_then(|v| v.as_str()).unwrap_or("").to_string();
            let text     = doc.get_first(self.text_field).and_then(|v| v.as_str()).unwrap_or("").to_string();
            results.push(Bm25Result { chunk_id, source, section, text, score });
        }
        Ok(results)
    }
}

pub struct Bm25Result {
    pub chunk_id: usize,
    pub source: String,
    pub section: String,
    pub text: String,
    pub score: f32,
}

fn escape_query(s: &str) -> String {
    let special = ['+', '-', '!', '(', ')', '{', '}', '[', ']', '^', '"', '~', '*', '?', ':', '\\', '/'];
    let mut out = String::with_capacity(s.len() * 2);
    for ch in s.chars() {
        if special.contains(&ch) {
            out.push('\\');
        }
        out.push(ch);
    }
    out
}
