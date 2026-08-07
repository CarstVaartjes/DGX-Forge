# README and MIT License Design

## Goal

Give Vonk Forge a concise public landing page and an explicit open-source
license without duplicating its detailed operational documentation.

## Files

### `LICENSE`

Add the canonical MIT License text with this copyright notice:

```text
Copyright (c) 2026 Carst Vaartjes
```

### `README.md`

Add a concise, operator-facing project overview containing:

- a one-paragraph description of Vonk Forge;
- a short capability summary grounded in the checked-in implementation;
- prerequisites, including Python 3.12 or newer, `uv`, SSH access to the Vonk Forge
  GPU nodes, and Docker on those nodes;
- a quick start that installs the locked development environment, runs the
  tests, and invokes safe read-only `vonkctl` commands;
- a compact repository map;
- links to the existing architecture overview and relevant runbooks;
- a security note directing users away from committing credentials; and
- a license section linking to `LICENSE`.

The README will link to detailed documentation instead of reproducing its
operational procedures. It will not add badges, contribution policy, support
promises, or claims that unaccepted model definitions are production-ready.

## Accuracy and Verification

Commands and prerequisites will be derived from `pyproject.toml`, the
repository launchers, and the existing runbooks. Relative documentation links
will be checked against the filesystem. The Markdown will be reviewed for
placeholders, ambiguous maturity claims, and unnecessary duplication. Since
the change is documentation-only, verification does not require exercising
the remote Vonk Forge GPU nodes.
