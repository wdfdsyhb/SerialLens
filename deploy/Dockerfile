FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY webapp ./webapp

RUN pip install --no-cache-dir . fastapi "uvicorn[standard]"

ENV PYTHONUTF8=1
EXPOSE 8300

CMD ["python", "-m", "uvicorn", "webapp.app:app", "--host", "0.0.0.0", "--port", "8300"]
