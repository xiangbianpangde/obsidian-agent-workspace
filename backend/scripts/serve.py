"""M2 服务入口: uvicorn backend.app.main:app --port 8787"""
if __name__ == "__main__":
    import uvicorn

    from app.config import load_config

    cfg = load_config()
    uvicorn.run("backend.app.main:app", host=cfg.bind_host, port=cfg.port, reload=False)
