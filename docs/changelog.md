# Changelog: 1.0.1 → 1.10.1

## Features

- Refactor Agent Guides into multi-guide system with Odoo version-specific development guides ([8680a18](https://github.com/oduist/oduflow/commit/8680a18b284ba1a7e0e2de9a2d0c5b9af489af69))
- Add `import_template_from_odoo` — import templates from running Odoo instances ([a6d53fc](https://github.com/oduist/oduflow/commit/a6d53fc988e5c387c2d49d98436a530fee3bab19))
- Store `use_overlay` flag in template metadata instead of computing filestore size on every env creation ([e393c44](https://github.com/oduist/oduflow/commit/e393c445fbc040ea451c6876aa82a35b1d5ff392))
- Show `use_overlay` mode in CLI `list-templates` and dashboard UI ([cf85489](https://github.com/oduist/oduflow/commit/cf854894eeb8c33e13b05599652a7e6894c8b4f0))
- Show template name in `get_environment_info` and dashboard UI ([e79a6ae](https://github.com/oduist/oduflow/commit/e79a6ae80917d9d64e5b22d5ca0c61e78ed0019e))
- Add Sync button to environment UI ([66ceed8](https://github.com/oduist/oduflow/commit/66ceed8ef8747b3f679ad1ed7ed62aeab8e779b6))
- Add `ODUFLOW_TRACE=1` trace logging for sync/classify pipeline ([af7b9bc](https://github.com/oduist/oduflow/commit/af7b9bcb55ba43cb4daaaa884cfc9edcfd59a9cb))
- Docker deploy added ([e934bea](https://github.com/oduist/oduflow/commit/e934bea4e9e72e5259c94b6bd54c558e4a0e4daf))
- Append `/web?debug=1` to environment URLs in dashboard ([ff913ad](https://github.com/oduist/oduflow/commit/ff913ada0ff1406b505c5fa103e85977b57d0428))
- Add `ODUFLOW_STATELESS_HTTP` option (enabled by default) ([362e449](https://github.com/oduist/oduflow/commit/362e449335d813c3a3326161f8b6a0c04041e665))
- Add logo to Web UI header ([6fd52ce](https://github.com/oduist/oduflow/commit/6fd52ce17369f30d889aa09fb847bfb136dfeae9))
- Add `run_db_query` MCP tool for executing SQL queries against environment databases ([d7b1b44](https://github.com/oduist/oduflow/commit/d7b1b44d3a378d1357125526860b6a459fc8bbf1))
- Credentials management UI + git auth improvements ([259164e](https://github.com/oduist/oduflow/commit/259164ebcbd74a1216e5585e2d93c1526a6d69ae))
- Per-user git credentials for repo operations ([5c28100](https://github.com/oduist/oduflow/commit/5c28100df0b9072c0b1da85a230ba06e6fd45bf8))
- Add `oduflow cleanup` CLI command to remove orphaned resources ([d1fa09c](https://github.com/oduist/oduflow/commit/d1fa09cf346726892653f724f0ce4a47de19a8b5))
- Add protect/unprotect for extra repos ([1245d6c](https://github.com/oduist/oduflow/commit/1245d6c5f953ac876c52d113ba5b43d82c94a0af))
- Add `update_extra_repo` MCP tool; remove one-off migration SQL ([3c678cc](https://github.com/oduist/oduflow/commit/3c678ccffe26060043a3deb28095ed66150c1108))
- Report elapsed time in `create_environment` response ([90ca9e1](https://github.com/oduist/oduflow/commit/90ca9e19ea1beb6957c30066cbec1a4f78f14927))
- Show filestore and dump sizes in template listing ([a1403a7](https://github.com/oduist/oduflow/commit/a1403a7737d071b42f3052214ae3eef7684c4f65))
- Add Recreate button for environments in web UI ([31afb9e](https://github.com/oduist/oduflow/commit/31afb9e7a28d7e785d05849783db36927f851137))

## Fixes

- Show friendly error when Docker is not available ([5df6df7](https://github.com/oduist/oduflow/commit/5df6df71f6f4886c815f59b1f7469884beb30045))
- Service logs filtering and state isolation from env logs ([9926c2a](https://github.com/oduist/oduflow/commit/9926c2aa6946f80c7e0ef6ac0c51cddaac7a9612))
- Update `test_settings` to match current Settings API ([b92fe80](https://github.com/oduist/oduflow/commit/b92fe800e75c466384cb3f8290543c3b56183f3c))
- Restart container after successful module installation ([b819c65](https://github.com/oduist/oduflow/commit/b819c65c4eb526ba4a51f97682c239b377bc1319))
- Fix tests: expect `ToolError` instead of `ValueError` ([dcf7f23](https://github.com/oduist/oduflow/commit/dcf7f23c7d608603e2fdd589a0471ea594026432))
- License load fix ([8e66e8f](https://github.com/oduist/oduflow/commit/8e66e8f005ac4aa1f8fdd64a50bd84e639459319))
- Stream dump file to container to avoid OOM; save dump to workspace on `reload-template --dump-path` ([8615fe2](https://github.com/oduist/oduflow/commit/8615fe29354943110b3c75def142ad2a9ab3d292))
- Eliminate OOM in all container file extraction (`init_template`, `template_down`, `publish_env_as_template`) ([b40395b](https://github.com/oduist/oduflow/commit/b40395b769480e09cc340c3c68f8dd31217f1cab))
- Surface database drop failures as warnings in `delete_environment` ([02a628b](https://github.com/oduist/oduflow/commit/02a628b4ebc59e142791f8d88539d0a4a6d865c7))
- Use `odoo_image` version as fallback branch for extra addons worktree ([ea297fd](https://github.com/oduist/oduflow/commit/ea297fdf4d81bc06a7bd0c22f89cb1b8427582c0))
- Configure fetch refspec for bare extra repos so `git fetch` updates branches ([54bc937](https://github.com/oduist/oduflow/commit/54bc9373e55569f85db0fe2737691d1a9cb10be3))

## Documentation

- Rework README — add missing config vars, licensing, template metadata, CLI flags ([0b8de7e](https://github.com/oduist/oduflow/commit/0b8de7eea6cb92cffc7678fc9e7c6101f7463df4))
- Restructure README — reorder sections, remove odoo.sh comparison, merge internals ([70af4df](https://github.com/oduist/oduflow/commit/70af4df31e3295db864a034d6afbc43e21218ad2))
- Update README.md ([fe6674f](https://github.com/oduist/oduflow/commit/fe6674ff41ccdcf0a0e4e2f9cab0400f287b2af6))
- Add MkDocs with Material theme, build site to docs/ for GitHub Pages ([bbcf1e9](https://github.com/oduist/oduflow/commit/bbcf1e90691ad96368fd7b6c9270870f64a39311))
- Switch to GitBook theme, use gh-pages branch for deployment ([3cfbbb2](https://github.com/oduist/oduflow/commit/3cfbbb261fabba064e0877430ae5dceeac91d88b))
- Add `requirements-docs.txt` for mkdocs setup ([b620ff5](https://github.com/oduist/oduflow/commit/b620ff57153623f5b47cfcec89b261389ea81936))
- Split README into structured mkdocs documentation ([c52084f](https://github.com/oduist/oduflow/commit/c52084f0772fc0abe1564a384e0341b0907d35ae))
- Add database migrations workflow guide for agents ([63629fd](https://github.com/oduist/oduflow/commit/63629fd267170e1af7a3e5d1d162653da61f99bc))
- Document extra addons pinning behavior in environments ([651f877](https://github.com/oduist/oduflow/commit/651f8778266fd350399bd1605c32ad4ea96e9e77))

## Refactor

- Rename CLI command `promote` to `template-from-env` ([ecaaf84](https://github.com/oduist/oduflow/commit/ecaaf84c1a8bf205ed6b3451ec0d58f0842dce58))
- Rename template DB prefix to `oduflow_template_{id}_{name}`, make `template_name` required ([c3bee0f](https://github.com/oduist/oduflow/commit/c3bee0fbeece89253860389ad144461d4d291861))

## Chore

- Make dev guide hint more assertive in `create_environment` output ([b9933ce](https://github.com/oduist/oduflow/commit/b9933cec0b72aa4832c711a9fbea8ffae9d10b7e))
- Clean up startup logs: use websockets-sansio, suppress docker noise ([a486d87](https://github.com/oduist/oduflow/commit/a486d87da6c7fdf94ae4711d13fe2b12553ad225))
- Add `site/` to `.gitignore`, remove tracked build output ([b36e832](https://github.com/oduist/oduflow/commit/b36e8327eacc900a44f0ea65a0e585b9005a3dc6))
- Add `logo.png` to tracked files for package distribution ([935f5f7](https://github.com/oduist/oduflow/commit/935f5f764d0544d32627c48e046596c0fa441fe8))
