FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
RUN mkdir -p src/generated && python -m grpc_tools.protoc -I src/protocols --python_out=src/generated src/protocols/*.proto
ENV REPLIVE_BIND=0.0.0.0 REPLIVE_STATE_DIR=/app/.private PYTHONUNBUFFERED=1
EXPOSE 8769
CMD ["python", "src/webui.py"]
