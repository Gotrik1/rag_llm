# Архитектура решения

Проект представляет собой корпоративный RAG-ассистент с веб-интерфейсом и тремя режимами обработки:

- `python` — LlamaIndex, Qdrant, BM25 и Python-генерация;
- `rust` — Python/Rust retrieval и Rust-генерация;
- `hybrid` — объединённый контекст с Python-генерацией и валидацией.

Пользователь работает через React/Vite UI. Backend на Python принимает вопрос, получает контекст из векторного и полнотекстового индекса, передаёт ограниченный контекст локальной или подключённой LLM и возвращает ответ с источниками. Rust CLI используется через subprocess bridge для дополнительного retrieval и генерации.

Для учебного сценария добавлен отдельный offline-контур FAISS:

```text
knowledge_base/architecture/*.md
        ↓
faiss_rag.py
        ↓
Ollama nomic-embed-text, 768 dimensions
        ↓
.faiss_edu_architecture/faiss.index + metadata.json
```

Полное описание потоков, API и Docker-инфраструктуры находится в корневом [`architecture.md`](../architecture.md).

