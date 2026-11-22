"""Launch Luna's isolated Hindsight service and dedicated PostgreSQL instance."""

import os
from pathlib import Path

from src.paths import HOME, MEMORY_API_PORT, MEMORY_DATABASE, MEMORY_DATABASE_PORT, MEMORY_MODEL as MODEL, OLLAMA_PORT

DATA = HOME / "memory"


def database():
    from pg0 import Pg0
    return Pg0(name=MEMORY_DATABASE, port=MEMORY_DATABASE_PORT, username="hindsight", password="hindsight",
               database="hindsight", data_dir=str(DATA / "postgres"),
               config={"listen_addresses": "127.0.0.1"})


def drop_database():
    postgres = database()
    info = postgres.info()
    expected = DATA / "postgres"
    if info.data_dir is None and not expected.exists():
        return
    if not info.data_dir or Path(info.data_dir).resolve() != expected.resolve():
        raise RuntimeError("Unexpected database directory; no memory data was deleted.")
    postgres.drop()
    if expected.exists():
        raise RuntimeError("Memory database was not removed; reset is incomplete.")


def main():
    os.umask(0o077)
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    postgres = database()
    try:
        info = postgres.info()
    except (ValueError, OSError):
        info = postgres.start()
    if info.data_dir and Path(info.data_dir).resolve() != (DATA / "postgres").resolve():
        raise RuntimeError("The luna-memory database belongs to a different directory.")
    if not info.running:
        info = postgres.start()
    if info.port != MEMORY_DATABASE_PORT:
        raise RuntimeError("The luna-memory database is using an unexpected port.")
    settings = {
        "HINDSIGHT_API_DATABASE_URL": info.uri,
        "HINDSIGHT_API_HOST": "127.0.0.1",
        "HINDSIGHT_API_PORT": str(MEMORY_API_PORT),
        "HINDSIGHT_API_MCP_ENABLED": "false",
        "HINDSIGHT_API_LLM_PROVIDER": "ollama",
        "HINDSIGHT_API_LLM_BASE_URL": f"http://127.0.0.1:{OLLAMA_PORT}/v1",
        "HINDSIGHT_API_LLM_MODEL": MODEL,
        "HINDSIGHT_API_LLM_MAX_CONCURRENT": "1",
        "HINDSIGHT_API_LLM_OLLAMA_NUM_CTX": "32768",
        "HINDSIGHT_API_LLM_TIMEOUT": "600",
        "HINDSIGHT_API_LLM_REASONING_EFFORT": "none",
        "HINDSIGHT_API_RETAIN_MAX_COMPLETION_TOKENS": "8192",
        "HINDSIGHT_API_REFLECT_MAX_CONTEXT_TOKENS": "20000",
        "HINDSIGHT_API_REFLECT_MAX_COMPLETION_TOKENS": "8192",
        "HINDSIGHT_API_REFLECT_WALL_TIMEOUT": "1800",
        "HINDSIGHT_API_CONSOLIDATION_MAX_COMPLETION_TOKENS": "4096",
        "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "onnx",
        "HINDSIGHT_API_RERANKER_PROVIDER": "onnx",
        "HINDSIGHT_API_LOG_LEVEL": "warning",
        "PYTHON_DOTENV_DISABLED": "1",
        "HF_HOME": str(DATA / "models"),
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    os.environ.update(settings)
    from hindsight_api.main import main as serve
    serve()


if __name__ == "__main__":
    main()
