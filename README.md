# RAG assistant

Проектный RAG-бот на Python/Rust с гибридным поиском, Qdrant, BM25 и локальной/облачной LLM.

## Быстрый запуск

1. Скопировать `.env.example` в `.env` и при необходимости изменить порты и модель.
2. Запустить инфраструктуру:

```bash
docker compose up --build
```

3. Открыть frontend на `http://localhost:5173`. Backend доступен на `http://localhost:8080`.

Для локальной Ollama можно использовать `docker compose --profile ollama up --build`.
Подробности находятся в [DEPLOYMENT.md](DEPLOYMENT.md).

## Учебный FAISS-режим

Для выполнения учебного сценария без Qdrant:

```powershell
pip install -r requirements.educational.txt
python faiss_rag.py build --source knowledge_base
python faiss_rag.py search --question "Как создать параметр?"
python faiss_rag.py ask --question "Как создать параметр?"
```

Индекс и метаданные появятся в `.faiss_edu/`: `faiss.index`, `metadata.json`, `config.json`. Для текущей архитектурной подборки собран `.faiss_edu_architecture/`.

## Учебная сдача

Описание решений и результаты экспериментов собраны в [Project_template.md](Project_template.md).
Пока не заполнены папка `knowledge_base/`, `terms_map.json`, golden set и материалы демонстрации, проект нельзя считать полностью готовым к сдаче.
