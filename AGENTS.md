# Agent Instructions

## Plan First, Then Act

Before making any code changes, always:
1. Explain your understanding of the task
2. Present a detailed plan of changes (which files will be created/modified, what exactly will change)
3. Ask for explicit confirmation before proceeding with implementation

Do NOT write or modify any code until the user explicitly approves the plan.

## Testing
If you run into any missing python dependency errors, try running your command with source .venv/bin/activate
to assume the python venv (or .venv312 if .venv is missing).

## Publishing Documentation

After committing and pushing changes to `docs/` or `mkdocs.yml`, always publish the documentation to GitHub Pages automatically — do not ask the user:

```bash
pip install -r requirements-docs.txt
source .venv/bin/activate && mkdocs gh-deploy --force
```

Documentation dependencies are listed in `requirements-docs.txt`.

The site is hosted at: https://oduist.github.io/oduflow/

## Publishing Docker Image

When asked to publish a Docker image, build and push to Docker Hub:

```bash
# Read version from pyproject.toml, then:
docker build -t oduist/oduflow:<VERSION> -t oduist/oduflow:latest .
docker push oduist/oduflow:<VERSION>
docker push oduist/oduflow:latest
```

Registry: hub.docker.com, repository: `oduist/oduflow`

