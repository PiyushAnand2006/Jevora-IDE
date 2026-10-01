# IDE Coder backend

Run locally from the project root:

```powershell
python -m pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --reload
```

Open `http://127.0.0.1:8000`. The service exposes interactive API documentation at `/docs`.

The backend is local-first: provider settings live in `backend/data/`, and each task creates an inspectable Markdown note under `vault/IDE Coder Runs/`. Provider API keys are never returned from the API or written to vault notes. Configure `IDE_WORKSPACE_DIR` and `IDE_VAULT_DIR` before starting if the target workspace or Obsidian vault should be elsewhere.
