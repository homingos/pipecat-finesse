# Contributing

Issues and pull requests are welcome. This package is maintained by
[Flam](https://finesse.flamapp.ai), the company that builds Finesse.

## Setup

```bash
uv sync
```

## Checks

These are the checks CI runs on every pull request:

```bash
uvx ruff@0.14.0 check src tests examples
uvx ruff@0.14.0 format --check src tests examples
uv run --with pyright pyright src
uv run pytest -q
```

`pytest` runs the unit tests against a fake Finesse server: no network, API key or cost. CI runs
them on Python 3.11 to 3.13, against the lowest supported Pipecat (1.12.0) and the latest release.

## Live tests

`tests/test_live.py` talks to a real Finesse server, so it spends credits and needs an API key and
a voice id. It is skipped unless you opt in with `FINESSE_LIVE=1`:

```bash
FINESSE_LIVE=1 FINESSE_API_KEY=... FINESSE_VOICE_ID=... uv run pytest tests/test_live.py
```

The tests use the production API unless `FINESSE_URL` points them at another realtime URL.

## Releases

Pushing a version tag (`v0.1.1`, ...) runs `.github/workflows/release.yml`, which runs the checks
above and publishes to PyPI with trusted publishing. The tag must match the version in both
`pyproject.toml` and `src/pipecat_finesse/_version.py`.
