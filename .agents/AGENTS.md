# Antigravity Rules

## File Access & Permissions Policy
- Always access all file paths directly without asking for permission.
- Accept all file edits and changes by default without requiring manual review or diff approvals.
- Execute requested modifications immediately.
## Dashboard Security Policy
- Password authentication MUST NEVER be added to the front-end dashboard or back-end server.
- The dashboard server is strictly unauthenticated by design for public read-only access.
- Do not introduce login routes, basic authentication headers (WWW-Authenticate), or password configuration environment variables for dashboard access.
