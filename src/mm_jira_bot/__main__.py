from __future__ import annotations

import uvicorn

from mm_jira_bot.web import create_app


def main() -> None:
    app = create_app()
    # ``create_app`` already ran ``configure_logging`` and owns the root handler.
    # ``log_config=None`` stops uvicorn from installing its own (plaintext,
    # non-propagating) loggers, so ``uvicorn.access``/``uvicorn.error`` propagate
    # to root and flow through our JsonFormatter/TextFormatter + ring buffer.
    uvicorn.run(app, host="0.0.0.0", port=8080, log_config=None)


if __name__ == "__main__":
    main()
