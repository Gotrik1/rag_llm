FROM vllm/vllm-openai:v0.17.0
RUN python3 -m pip install --no-cache-dir fastembed-gpu==0.9.0 onnxruntime-gpu==1.23.2
WORKDIR /app
COPY deploy/index-embedding/app.py /app/embedding_app.py
ENTRYPOINT ["python3", "-m", "uvicorn", "embedding_app:app", "--host", "0.0.0.0", "--port", "11434"]
