# SDD ledger — plan: docs/superpowers/plans/2026-08-03-unified-admin-clients.md

Task 1: active (typed routine-operation API resources and reproducible admin-only OpenAPI/client generation; base 2437fe2f8d68f5759ca54fb894ecc427575b5f76)
Task 1: initial implementation delivered (commits 99bbbb7..c5ddad5; 684 control tests, 17 Compose tests, 3 generator/drift tests, web build, and pinned Ruff green; independent review pending)
Task 1: review round 1 submitted (1 Critical + 5 Important — certificate-bearing approval escaped through an opaque generated response; enrollment mutations lacked audits; endpoint validation duplicated only part of the authoritative route reader; generated errors were untyped; plan/job nested objects remained unrestricted; missing-observation metrics called float(None); fix base c5ddad5)
Task 1: fix round 1/5 (5 addressed, 1 open — profile planning omitted typed 409 and job-log content omitted typed 403/404/503; commits c5ddad5..7d9eea2)
Task 1: fix round 2/5 (1 addressed, 0 open — every raised plan and job-log-content status now has a strict bounded generated response; commits 7d9eea2..8f1a092)
Task 1: complete (commits 2437fe2..8f1a092, task review clean after two fix rounds; publication acceptance pending merge and hosted CI)
Task 1: hosted CI run 31007916192 failed both matrix jobs because root pytest collected generator tests before `tools/openapi-client` was installed; dedicated generated-client and Ruff jobs passed
Task 1: publication fix delivered and independently approved (commits 7b1bd09..5c50fd8; exact locked generator install now precedes root pytest on Ubuntu/macOS; workflow regression, platform boundaries, generator/drift, Ruff, and diff checks green; hosted rerun pending)
Task 1: hosted CI publication fix (run 31007916192 failed Ubuntu/macOS root pytest because the matrix omitted the pinned TypeScript-generator install; exact locked step and ordering regression committed as 2ee85da; focused workflow 5 tests, YAML parse, generator/drift 5 tests, and pinned Ruff green; hosted rerun pending)
