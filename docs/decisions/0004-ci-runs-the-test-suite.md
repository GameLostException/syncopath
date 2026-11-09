# ADR-0004 — CI runs the test suite on every pull request

- **Status:** Accepted
- **Date:** 2026-11-09

## Context

The repository had no CI: merging relied on someone remembering to run `pytest`
locally. The owner's rule is to merge a pull request only once CI is green, so a
check has to exist that anyone can see on the pull request.

## Decision

1. A GitHub Actions workflow (`.github/workflows/tests.yml`) runs `pytest` with
   coverage on every pull request and on every push to `master`.
2. It runs on Ubuntu with Python 3.11 (the minimum in `pyproject.toml`) and 3.14
   (the version in use), and installs the system libraries PyGObject needs to build
   and import (GObject introspection, GTK 3, cloud-providers). The tests run under
   `xvfb-run`, because importing GTK needs a display.
3. Test tools are declared once, as the `dev` extra in `pyproject.toml`; CI and
   the README install `.[dev]`.
4. No coverage threshold is enforced; raising one is a separate decision.

## Consequences

- Each pull request shows a pass/fail check; merging waits for it.
- Optional desktop libraries (AppIndicator) are absent on the runner, so the code
  must import without them — which is what users without them need too.
- Runs take a few minutes, mostly building PyGObject.
