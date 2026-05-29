from __future__ import annotations

import uvicorn

from mm_jira_bot.web import create_app


def main() -> None:
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
