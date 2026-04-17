"""Web app entry point in reorganized src layout.

This module re-exports the legacy root web UI app so existing behavior
is preserved while new package paths become available.
"""

from web_ui import app  # noqa: F401


if __name__ == "__main__":
    app.run(debug=False)
