FROM vllm/vllm-openai:v0.17.0
RUN python3 -m pip install --no-cache-dir transformers==5.3.0
WORKDIR /app
COPY deploy/document-vision/app.py /app/vision_app.py
ENV HF_HOME=/data/models TOKENIZERS_PARALLELISM=false
ENTRYPOINT ["python3", "-m", "uvicorn", "vision_app:app", "--host", "0.0.0.0", "--port", "8090"]
