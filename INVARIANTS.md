# Invariants

Rules whose breach is a design change, not a fix; changing one is the owner's decision.

- Nothing in `research/` is canonical product spec, only the basis for it: the folder holds verbatim user voices and dated competitive snapshots that feature copy derives from, so reading a survey or quotebook entry as a commitment inverts the authority direction and turns an observation into a requirement nobody made.
- The version is defined in `pyproject.toml` and mirrored, not independently set, in the package modules: two unrelated version claims ship two answers to "what is installed". Documentation carries no hardcoded version numbers, which every release would falsify.
- The Homebrew formula is not in this repo; it lives in the `overseers-desk/homebrew-od` tap and points at the PyPI sdist. Install metadata kept here as well would give the release two homes that drift.
