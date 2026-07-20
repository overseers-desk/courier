# Courier Server Development Guide

The invariants live in [`INVARIANTS.md`](INVARIANTS.md): the authority of `research/`, the version's single home, and where the Homebrew formula lives. A change that breaks one is a design change, the owner's to make.

## Versioning and Packaging
- Version is defined in `pyproject.toml` (single source of truth) and mirrored in `courier/__init__.py`, `courier/__main__.py`, and `courier/mcp_server.py`. Do not hardcode version numbers in documentation — use generic references like "latest" or `<version>` placeholders.
- Do not bump the version unless the user asks for it.
- Packaging: `debian/` for .deb, `courier.spec` for .rpm, and PyPI (`pip install courier`) as the primary channel. The Homebrew formula lives in the `overseers-desk/homebrew-od` tap repo at `Formula/courier.rb`; its `url`/`sha256` point at the PyPI sdist (the same tarball pip downloads), not the GitHub archive — see the release steps.

## What "release" means in this project
When the user asks to "do the release", "release X.Y.Z", or "release now", they mean the **complete** end-to-end publication, not source-side prep. Do all of the following without asking for confirmation between steps; ask only if a step actually fails:

1. **Commit** any uncommitted source/test/packaging changes from this session, in logically grouped commits (feature → version bump → packaging metadata sync). Match the commit-message style of the prior release commits (see `git log -- debian/changelog courier.spec`).
2. **Push** to `origin/main`.
3. **Tag** `vX.Y.Z` and **push the tag**.
4. **Create the GitHub release**: `gh release create vX.Y.Z --title "vX.Y.Z" --notes "..."`. Release notes summarise the user-visible changes and copy install plus upgrade commands verbatim from `docs/INSTALLATION.md` (the single source of truth for both). Carry **Install** (first-time users) and **Upgrade** (returning users) as separate sections, not one block: a returning macOS user running just `brew install courier` is told "already installed and up-to-date" and never reaches the new version, because Homebrew's auto-update does not pull third-party taps until `brew update` is run first. If `docs/INSTALLATION.md` does not yet cover a sequence the release needs (e.g. a new platform, a new upgrade caveat), update that file first and propagate to the release notes. The verbatim copy is what makes the duplication safe: divergence between the two is a doc bug to fix in `docs/INSTALLATION.md`. Skip the brew section for any release where macOS install is known broken (e.g. issue #26 was open against v1.1.2); only add it back once a Tahoe install has been verified end-to-end. Do not invent platforms (no `apt install courier` one-liner unless we host an APT repo).
5. **Publish to PyPI** — this is the primary install channel (`pip install courier`). Build the distribution with `uv build` (produces `dist/courier-X.Y.Z.tar.gz` sdist plus the wheel), then upload with twine: `uvx twine upload --non-interactive dist/courier-X.Y.Z.tar.gz dist/courier-X.Y.Z-py3-none-any.whl`. Credentials come from `~/.pypirc`; do not read or print that file. Note that `uv publish` does **not** read `~/.pypirc` (it only checks env vars, keyring, and trusted publishing), which is why twine is used here. Verify the version is live: `curl -sL https://pypi.org/pypi/courier/json` should show `info.version == X.Y.Z`.
6. **Build BOTH `.deb` AND `.rpm`** — never just one. The host has both `dpkg-buildpackage` and `rpmbuild` available; check with `which` if unsure, do not assume.
   - `.deb`: from project root, `dpkg-buildpackage -us -uc -b` → artifact lands at `../courier_X.Y.Z_all.deb`.
   - `.rpm`: download the GitHub tarball to `~/rpmbuild/SOURCES/courier-X.Y.Z.tar.gz`, then `rpmbuild -bb courier.spec` → artifact lands at `~/rpmbuild/RPMS/noarch/courier-X.Y.Z-1.noarch.rpm`.
7. **Upload BOTH artifacts** to the GitHub release: `gh release upload vX.Y.Z <deb> <rpm>`. Verify with `gh release view vX.Y.Z --json assets --jq '[.assets[].name]'` — both filenames must be present before considering the release done.
8. **Bump the Homebrew formula** in the `overseers-desk/homebrew-od` tap repo. The formula's `url` and `sha256` point at the **PyPI sdist** — the same tarball PyPI serves, unified so brew and pip share one file and one hash (do **not** point it at the GitHub `git archive` tarball; that is a different file with a different hash). After step 5 publishes, read both values straight from the PyPI JSON: `curl -sL https://pypi.org/pypi/courier/X.Y.Z/json`, take the object in `.urls` where `packagetype == "sdist"`, and use its `.url` (a `files.pythonhosted.org` link) as `url` and its `.digests.sha256` as `sha256`. Update those fields in `Formula/courier.rb` over in the `overseers-desk/homebrew-od` repo, then commit and push there. The formula is not part of this repo.
9. **Sanity-check** the prior release for parity: `gh release view v<previous> --json assets` — the new release should have at least the same asset types (deb + rpm at minimum) the previous one had. If the new release is missing an asset type, it is incomplete.

Do **not** report the release as done if any of the above is missing. Do not stop at "the .deb is built, want me to upload?" — the user explicitly does not want that hand-off; the upload is part of the release.

## Environment Setup and Build Commands with `uv`
- Create virtual environment: `uv venv`
- Activate virtual environment: `source .venv/bin/activate` (Unix/macOS) or `.venv\Scripts\activate` (Windows)
- Install dependencies: `uv pip install -e ".[dev]"`
- Install specific packages: `uv add package_name`
- Run commands within the environment: `uv run command [args]`

### Package Management
   - ONLY use uv, NEVER pip
   - Installation: `uv add package`
   - Running tools: `uv run tool`
   - Upgrading: `uv add --dev package --upgrade-package package`
   - FORBIDDEN: `uv pip install`, `@latest` syntax
   - FORBIDDEN: `uv run python ...`

## Build and Test Commands
- Install dependencies: `uv pip install -e ".[dev]"`
- Run all tests: `uv run pytest`
- Run single test: `uv run pytest tests/test_file.py::TestClass::test_function -v`
- Run with coverage: `uv run pytest --cov`
- Run MCP server: `courier mcp`
- Development mode: `courier mcp --dev`
- One-line execution with dependencies: `uvx courier mcp`

## Code Style Guidelines
- **CI gate**: the `Code Quality Checks` workflow runs `./scripts/run_checks.sh --ci --skip-integration`, which chains ruff, black, isort (black profile), mypy (non-blocking), and pytest with coverage. Before declaring a branch green, run that script locally; partial substitutes (just black, just pytest) miss real failures. Past incident: an inline import in `courier/__main__.py` was black-clean but isort-dirty, so a session that ran black + pytest only declared the branch ready, the merge to main turned CI red, and a follow-up commit was needed to fix it.
- Use Black with 88 character line length
- Imports: Use isort with Black profile
- Types: All functions must have type hints (mypy enforces this)
- Naming: snake_case for variables/functions, PascalCase for classes
- Error handling: Use specific exceptions and provide helpful messages
- Documentation: Write docstrings for all classes and methods using the existing Google-style convention (summary line, then Args/Returns/Raises sections). When adding or moving code, match the docstring style of neighbouring functions — do not condense or omit sections that peers include.
- Testing: Follow TDD pattern (write tests before implementation)

## Task Workflow
When working on tasks from GitHub Issues, follow this workflow:

1. **Task Analysis**:
   - Read and understand the issue requirements
   - Assess if the issue needs to be broken down into smaller subtasks
   - If needed, create separate issues for subtasks and link them to the parent issue
   - Analyze existing labels and make sure the issue has the correct priority and status labels

2. **Starting Work on an Issue**:
   - Create a branch that references the issue number: `git checkout -b feature/issue-[NUMBER]-[SHORT_DESCRIPTION]`
   - Make an initial commit that references the issue: `git commit -m "refs #[NUMBER]: Start implementing [FEATURE]"`
   - The automated status tracking system will detect this commit and change the issue status to "in-progress"

3. **Test-Driven Development**:
   - Write tests first that verify the desired functionality
   - Implement the feature until all tests pass
   - Refactor code while maintaining test coverage
   - Run full test suite to check for regressions: `uv run pytest --cov=courier`

4. **Completing an Issue**:
   - Create a pull request that references the issue: `gh pr create --title "[TITLE]" --body "Closes #[NUMBER]"`
   - The body should include "Closes #[NUMBER]" or "Fixes #[NUMBER]" to automatically close the issue when merged
   - The automated status tracking system will update the issue status to "completed" when the PR is merged
   - It will also automatically adjust priorities of remaining tasks

5. **GitHub Issue Management Commands**:
   - View all issues: `gh issue list`
   - View specific issue: `gh issue view [NUMBER]`
   - Filter issues by label: `gh issue list --label "priority:1"`
   - Create new issue: `gh issue create` (interactive) or:
     `gh issue create --title "Title" --body "Description" --label "priority:X" --label "status:prioritized"`
   - Edit issue: `gh issue edit [NUMBER] --add-label "priority:1" --remove-label "priority:2"`

6. **Documentation**:
   - Update docstrings in implementation
   - Update README.md or other docs if needed
   - Add new commands or processes to this CLAUDE.md file if relevant

7. **Commit Conventions**:
   - Use these prefixes in commit messages to trigger automatic status changes:
     - `refs #X`: References the issue without changing status
     - `implements #X`: Indicates implementation progress
     - `fixes #X`: Indicates the issue is fixed (used in final commits)
     - `closes #X`: Same as fixes, will close the issue when merged
   - Always include the issue number with the # prefix
   - Add descriptive message after the issue reference

## Issue Status Definitions

GitHub Issues have the following status labels:

- **status:prioritized**: Task has been assigned a priority, not yet started
- **status:in-progress**: Work on the task has begun (automatic when commits reference issue)
- **status:completed**: Implementation is finished (automatic when PR with "fixes/closes" is merged)
- **status:reviewed**: Task has been reviewed (currently manual update)
- **status:archived**: Task has been archived (currently manual update)

Priority labels follow the format `priority:X` where X is a number starting from 1 (highest priority).

## Integration Testing

Integration tests verify that the Courier server works correctly with real email services. These tests require valid credentials and network connectivity to external services.

### Environment Setup for Integration Tests

1. **Required Environment Variables**:
   - `TEST_IMAP_HOST`: IMAP server hostname (e.g., `imap.gmail.com`)
   - `TEST_SMTP_HOST`: SMTP server hostname (e.g., `smtp.gmail.com`)
   - `TEST_EMAIL`: Email address for testing
   - `TEST_PASSWORD`: Email password or app password

2. **Set Up Environment Variables**:
   ```bash
   # For temporary use in current session
   export TEST_IMAP_HOST=imap.gmail.com
   export TEST_SMTP_HOST=smtp.gmail.com
   export TEST_EMAIL=your-test-email@gmail.com
   export TEST_PASSWORD=your-app-password
   
   # Or add to your .env file for persistence (make sure it's in .gitignore)
   echo "TEST_IMAP_HOST=imap.gmail.com" >> .env
   echo "TEST_SMTP_HOST=smtp.gmail.com" >> .env
   echo "TEST_EMAIL=your-test-email@gmail.com" >> .env
   echo "TEST_PASSWORD=your-app-password" >> .env
   ```

### Refreshing OAuth2 Credentials

OAuth2 tokens expire periodically. If integration tests fail with authentication errors, refresh your tokens before running tests:

1. **Check if token refresh is needed**:
   ```bash
   uv run python -m courier.oauth2 check-token --config config.toml
   ```

2. **Refresh the token if expired**:
   ```bash
   uv run python -m courier.auth_setup refresh-token --config config.toml
   ```

3. **Generate a new token if refresh fails**:
   ```bash
   uv run python -m courier.auth_setup generate-token --config config.toml
   ```

### Read-only protocol probes against configured accounts

To verify server-side IMAP behaviour (capability quirks, SEARCH key acceptance, Gmail dialect questions) without test credentials and without reading the credential-guarded config, run a read-only search through the `imap:` escape on the configured blocks:

```bash
courier --all-imap --format json search 'imap:X-GM-RAW before:1784000000' -n 3
courier --imap <block> --format json search 'imap:X-GM-RAW from:noreply ANSWERED' -n 50
```

SEARCH changes no server state, the CLI consumes the config itself, and result counts answer most protocol questions (compare counts with and without a criterion to prove a key filters rather than being ignored). Report counts, not message content.

### Running Integration Tests

1. **Run all tests including integration tests**:
   ```bash
   uv run pytest
   ```

2. **Run only integration tests**:
   ```bash
   uv run pytest tests/integration/
   ```

3. **Skip integration tests when necessary**:
   ```bash
   uv run pytest --skip-integration
   ```

4. **Run specific integration test**:
   ```bash
   uv run pytest tests/integration/test_gmail_integration.py::test_gmail_connect_oauth2
   ```

### Writing New Integration Tests

When writing new integration tests:

1. **Mark tests appropriately**: Use the `@pytest.mark.integration` decorator
2. **Handle authentication errors gracefully**: Tests should fail clearly if credentials are invalid or expired
3. **Clean up after tests**: Restore mailbox state after tests run (delete test messages, reset folders)
4. **Isolate test data**: Use unique identifiers or timestamps for test data to avoid conflicts
5. **Use test fixtures**: Leverage pytest fixtures for setup and teardown
6. **Respect rate limits**: Add delays if necessary to avoid hitting service rate limits

Example integration test structure:
```python
import pytest

@pytest.mark.integration
def test_some_integration_feature(gmail_client):
    # Test implementation
    result = gmail_client.some_operation()
    assert result == expected_value

## Development Efficiency Strategies

When working with AI assistants or development tools that use credit-based systems, follow these practices to maximize efficiency:

### Minimize Tool Use
1. **Batch Commands**: Run fewer, more comprehensive commands rather than many small ones.
   - Run all tests at once: `uv run pytest` instead of testing individual files sequentially
   - Use coverage reports to identify issues in one pass: `uv run pytest --cov=courier`
   
2. **Strategic Command Execution**:
   - Ask the user to run commands that will save many tool calls over time
   - Use more verbose output flags (`-v`, `--verbose`) to get more information in a single command
   - Run commands from the project root to avoid changing directories multiple times

### Optimize Code Changes
1. **Comprehensive Edits**:
   - Make larger batches of related changes rather than incremental edits
   - Fix similar issues across multiple files in a single edit when possible
   - Update both implementation and test code together when they're closely related

2. **Testing Strategy**:
   - Write all tests before implementing features (true TDD approach)
   - Run the full test suite after significant changes rather than testing incrementally
   - Use test fixtures and parameterization to reduce test code duplication

3. **Documentation First**:
   - Document design decisions and architecture before implementation
   - Update documentation immediately after code changes to maintain consistency
   - Use clear, descriptive commit messages that reference issues

These strategies improve development efficiency while maintaining code quality and comprehensive testing.
