# Phase 25 — Dependency and license readiness

## Status

**Findings documented; distribution is blocked on owner/legal decisions.**
No dependency, parser, or application license was silently selected.

## Repository/package facts

- The tracked repository contains no root `LICENSE`/`COPYING` file and no
  Python or JavaScript dependency manifest/lockfile. The repository's own
  terms for distribution are therefore not established by this checkout.
- README documents Python 3.11+ and a separately obtained `py-ols` checkout
  for `.ols` parsing. The parser is not bundled and is optional for
  streaming hashing/comparison.
- Pylance import analysis found one unresolved external module,
  `ols_reader`, in the selected workspace environment. That environment
  advertises Python 3.13; it is not the Python 3.11 test interpreter and
  does not contain the separate parser checkout.
- No extra Python runtime dependency or JavaScript package was introduced
  by Phases 18–24. The test/CI suite uses Python's `unittest` and Node's
  syntax checker; CI does not install `py-ols`.

## Third-party license decisions

- `TheFlashBold/py-ols` is GPL-3.0. It remains external and optional, but
  using or distributing an application that depends on it can have
  obligations that are not resolved merely by keeping a separate checkout.
  Obtain legal review and choose the intended distribution architecture
  before shipping a parser-dependent product.
- The pinned C parser studied in Phase 19 is 0BSD, but it was not adopted,
  copied, or bundled. Its parsing limitations also prevent its use as a
  verified parser.
- Other reviewed projects with GPL terms, missing license grants, or
  unverified provenance were not reused; see
  [reports/external_repo_review.md](./reports/external_repo_review.md).

## Required human actions before distribution

1. Select and add the repository's own license (or confirm that distribution
   is intentionally restricted); do not infer a license from GitHub hosting.
2. Obtain legal review of the GPL-3.0 parser relationship and any intended
   redistribution or linking.
3. If dependency declarations are introduced, pin and audit them and add a
   lockfile/SBOM appropriate to the chosen deployment model.

Until those decisions are made, this branch is a development/research
artifact, not a cleared distributable release.
