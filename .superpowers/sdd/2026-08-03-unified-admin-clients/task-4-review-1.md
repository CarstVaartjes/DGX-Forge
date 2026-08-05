# Task 4 review round 1

## Findings

- **Important:** the default LiteLLM Admin link points to `/ui/`, which the current Caddyfile serves from the control SPA, while the pinned LiteLLM configuration explicitly disables its admin UI. The actual Compose/Caddy deployment must expose the native keys/teams/spend UI through a protected route.
- **Important:** CSRF cookie parsing splits on `=` and truncates valid padded values, causing mutation headers not to match the full cookie.
- **Important:** overlapping grant requests and refresh races can overwrite, hide, or resurrect one-time grant tokens.
- **Important:** the page renders unbounded agent/capability collections directly, allowing arbitrarily large DOM output.
- **Important:** approval/rejection/revocation confirmation state survives evidence refresh/action and can be reused against a changed snapshot.

## Verdict

Generated-client reuse, same-origin behavior, allowlisting, navigation, fleet typing, and accessible happy paths are present, but the five findings above require fixes.
