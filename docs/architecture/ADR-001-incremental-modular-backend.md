# ADR-001: Incremental modular backend

**Status:** Accepted
**Date:** 2026-07-17
**Deciders:** Video Masa maintainers

## Context

The stable desktop entry point is a single `app.py` containing configuration,
runtime checks, security validation, job execution, and HTTP routes. Release
3.0.x demonstrated that packaging and runtime behavior are tightly coupled to
that entry point. A big-bang framework rewrite would make regression diagnosis
harder while the desktop distribution is still maturing.

## Decision

Keep `app.py` as the executable compatibility boundary and incrementally move
cohesive, framework-independent responsibilities into a `videomasa` package.
The first slice extracts configuration helpers, dependency health checks, and
security validation. Routes continue to call compatibility wrappers until
later slices introduce blueprints and service objects.

Every extraction must:

1. preserve the desktop launcher and route contracts;
2. add focused unit tests for pure logic;
3. retain integration coverage through the Flask test client;
4. be copied into both macOS and Windows packages; and
5. pass the same repeatable release-validation command used by CI.

## Options considered

### Incremental modular monolith

| Dimension | Assessment |
|---|---|
| Complexity | Medium |
| Regression risk | Low to medium |
| Packaging impact | Small and explicit |
| Migration flexibility | High |

**Pros:** preserves working behavior, supports small reviews, enables pure unit
tests, and keeps rollback straightforward.

**Cons:** temporary compatibility wrappers remain in `app.py`, and dependency
boundaries improve over several releases rather than immediately.

### Big-bang application factory and blueprint rewrite

| Dimension | Assessment |
|---|---|
| Complexity | High |
| Regression risk | High |
| Packaging impact | Broad |
| Migration flexibility | Low |

**Pros:** reaches the target structure faster on paper.

**Cons:** changes startup, imports, globals, routes, tests, and packaging in one
step, making failures harder to isolate.

## Consequences

- Pure backend logic becomes fast to test without Flask or large ML packages.
- `app.py` remains larger than the final target during the migration.
- Packaging scripts must treat `videomasa/` as first-party application code.
- Future slices can extract job state/services, then route blueprints, without
  changing the desktop launcher contract.

## Follow-up

1. Extract typed job state and bounded executor behavior.
2. Extract download and transcription services.
3. Move routes into Flask blueprints after service boundaries stabilize.
4. Extract template JavaScript and CSS with browser interaction coverage.
