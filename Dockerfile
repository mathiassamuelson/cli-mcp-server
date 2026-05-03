FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY cli_mcp/ cli_mcp/

EXPOSE 8100

CMD ["uvicorn", "cli_mcp.server:app", "--host", "0.0.0.0", "--port", "8100"]
