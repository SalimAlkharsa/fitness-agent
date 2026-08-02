"""Entry point staged to packages/fitness/app/__main__.py by `make stage`.

The function directory contains a copy of src/app as `app/`, so the import
below resolves inside the deployed bundle.
"""

from app.serverless import main  # noqa: F401  (OpenWhisk calls main(event))
