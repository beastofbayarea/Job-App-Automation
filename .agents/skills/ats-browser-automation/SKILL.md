---
name: ats-browser-automation
description: Best practices, selector strategies, stealth mechanisms, and form automation patterns for ATS platforms (Greenhouse, Lever, Workday, Ashby, SmartRecruiters, Taleo) using Playwright.
---

# ATS Browser Automation Skill

This skill provides guidelines and patterns for automated job application submission across major Applicant Tracking Systems (ATS).

## Key Principles & Best Practices

1. **Platform-Specific Detection & Engines**:
   - **Greenhouse**: Standard form controls inside `#application_form` or embedded iframes. Support custom questions (`job_application[answers][...]`).
   - **Lever**: Simple standard forms on `jobs.lever.co`. Handle file upload for resume (`input[type="file"]`).
   - **Workday**: Multi-step wizard layout with dynamic DOM updates. Require explicit wait conditions between step transitions (`nav-button`, `next`).
   - **Ashby**: Modern React components. Use aria labels and role-based locators.
   - **SmartRecruiters / Taleo**: High iframe usage and dynamic modal popups.

2. **Resilient Locators & Fallbacks**:
   - Prefer role/label based locators (`page.get_by_label`, `page.get_by_role`).
   - Fallback to CSS or XPath when dynamic IDs are present.
   - Handle hidden file inputs by setting file paths directly on `input[type="file"]`.

3. **Stealth & Anti-Bot Evasion**:
   - Avoid detectable automated browser flags (`navigator.webdriver`).
   - Introduce subtle human-like delays before typing or clicking.
   - Set browser user-agent and viewport sizes accurately.

4. **Error Recovery & Logging**:
   - Capture screenshots and DOM HTML on automation failure for debugging.
   - Log form field match failures and missing mandatory questions.
