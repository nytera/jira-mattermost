from __future__ import annotations

import uvicorn

from mm_jira_bot.config import Settings
from mm_jira_bot.web import create_app


def main() -> None:
    settings = Settings.from_env()
    app = create_app(settings)
    # ``create_app`` already ran ``configure_logging`` and owns the root handler.
    # ``log_config=None`` stops uvicorn from installing its own (plaintext,
    # non-propagating) loggers, so ``uvicorn.access``/``uvicorn.error`` propagate
    # to root and flow through our JsonFormatter/TextFormatter.
    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port, log_config=None)


if __name__ == "__main__":
    main()
