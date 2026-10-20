"""``python -m backtester.api`` → uvicorn on configured host/port."""

from backtester.api.app import main

raise SystemExit(main())
