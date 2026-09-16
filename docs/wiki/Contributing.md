# Contributing / keeping docs in sync

- **This wiki mirrors feature changes.** Any change to endpoints, config, import behavior, providers, or the dashboard must update the matching doc page in the same commit: `docs/wiki/*.md` + `README.md`.
- Page naming: GitHub-wiki compatible (`Home`, `Installation`, `Configuration`, `Providers`, `API Reference`, `Dashboard`, `Credential Import (OmniRoute and 9Router)`, `Playground and Live Testing`, `Architecture`, `Troubleshooting`) so they can be pushed straight to the GitHub wiki via `kiterouter.wiki.git` once the wiki is enabled in repo settings.
- Cross-links use `[[Page Name]]` wiki syntax; inside the repo they resolve to the sibling file.
- Keep the API Reference table in sync with `@app.get/@app.post` routes in `src/kiterouter/server.py`.
- Truthfulness rule docs must obey: no doc may claim a provider/model works without a real completion proving it.
