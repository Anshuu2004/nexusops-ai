FROM python:3.11-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 HF_HOME=/srv/.hf
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Pre-download the embedding model so the container starts offline-ready.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"
COPY app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
