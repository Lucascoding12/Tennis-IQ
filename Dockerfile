# Tennis IQ backend (also serves the website). Built and run by Render via render.yaml.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TIQ_DATA_DIR=/var/data
WORKDIR /app
COPY backend/requirements.txt backend/
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend backend
COPY frontend frontend
WORKDIR /app/backend
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
