"""
Run the VYOM REST API with uvicorn.

  python -m vyom.api                 # serve on http://127.0.0.1:8000
  python -m vyom.api --port 9000 --reload

Interactive docs at /docs once running. /query needs a real GEMINI_API_KEY in .env;
/health, /events and /scenes work without one.
"""

import argparse

import uvicorn


def main():
    ap = argparse.ArgumentParser(description="Serve the VYOM REST API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    args = ap.parse_args()

    # Import string form is required for --reload to work.
    uvicorn.run("vyom.api.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
