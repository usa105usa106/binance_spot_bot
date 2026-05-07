"""Railway fallback entrypoint.

Railway sometimes keeps an old start command pointing to main.py.
This wrapper starts the single-file bot.py application.
"""

from bot import main


if __name__ == "__main__":
    main()
