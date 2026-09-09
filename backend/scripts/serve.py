"""M2 服务入口: uvicorn backend.app.main:app --port 8787"""
from pathlib import Path

if __name__ == "__main__":
    import sys
    import uvicorn

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from backend.app.config import load_config

    cfg = load_config()
    uvicorn.run("backend.app.main:app", host=cfg.bind_host, port=cfg.port, reload=False)
