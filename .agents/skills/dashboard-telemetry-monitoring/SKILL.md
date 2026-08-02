---
name: dashboard-telemetry-monitoring
description: Architectural rules and patterns for the unauthenticated status dashboard, telemetry streaming, health metrics, and Sentry integration.
---

# Dashboard Telemetry & Monitoring Skill

This skill governs the front-end dashboard and back-end server telemetry for monitoring job automation pipelines.

## Security & Architecture Rules

1. **Unauthenticated Public Read-Only Access**:
   - As per repository security policies, authentication MUST NEVER be added to the dashboard server.
   - Do not add password configuration, basic auth headers (`WWW-Authenticate`), or login routes.

2. **Real-time Telemetry & Stream Updates**:
   - Stream job application status changes via SSE / Websockets / polling.
   - Display active engine health, success/failure metrics, and submission logs in real time.

3. **Sentry & Error Tracking**:
   - Capture unhandled automation exceptions with `sentry-sdk`.
   - Strip personal identifying information (PII) like user credentials or full contact details from error payloads before sending to Sentry.
