# Agent Instructions

## Plan First, Then Act

Before making any code changes, always:
1. Explain your understanding of the task
2. Present a detailed plan of changes (which files will be created/modified, what exactly will change)
3. Ask for explicit confirmation before proceeding with implementation

Do NOT write or modify any code until the user explicitly approves the plan.

## Env
If you run into any missing python dependency errors, try running your command with source .venv/bin/activate
to assume the python venv.

## Publishing Documentation

Documentation is published to GitHub Pages **automatically** by `.github/workflows/docs.yml`: on every push to `main` touching `docs/`, `mkdocs.yml`, or `requirements-docs.txt`, it installs `requirements-docs.txt` and runs `mkdocs gh-deploy --force` to update the `gh-pages` branch, which GitHub's `pages-build-deployment` then publishes live.

Do NOT run `mkdocs gh-deploy` (or otherwise deploy docs) from a working/feature branch — that would push unmerged content live. Just commit the `docs/`/`mkdocs.yml` changes as part of your branch; the site updates once they merge to `main`. A manual redeploy is available via the workflow's `workflow_dispatch` trigger.

The site is hosted at: https://docs.oduflow.dev/

## Publishing Docker Image

When asked to publish a Docker image, build and push to Docker Hub:

```bash
# Read version from pyproject.toml, then:
docker build -t oduist/oduflow:<VERSION> -t oduist/oduflow:latest .
docker push oduist/oduflow:<VERSION>
docker push oduist/oduflow:latest
```

Registry: hub.docker.com, repository: `oduist/oduflow`

