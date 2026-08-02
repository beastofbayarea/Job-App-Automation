---
name: llm-resume-tailoring
description: Guidelines for utilizing Gemini AI (google-genai) to analyze job descriptions, extract ATS keywords, dynamically reword resume bullet points, and generate targeted cover letters.
---

# LLM Resume & Cover Letter Tailoring Skill

This skill outlines the standard workflow for AI-assisted resume adaptation and cover letter synthesis using `google-genai`.

## Workflow & Guidelines

1. **Job Posting Parsing**:
   - Extract required technical skills, qualifications, years of experience, and key responsibility keywords.
   - Categorize requirements into "Must-have" vs "Nice-to-have".

2. **Structured Output Generation**:
   - Use Pydantic schemas or JSON schema mode to enforce deterministic LLM outputs.
   - Validate response objects before injecting into ReportLab or PDF generator pipelines.

3. **Resume Alignment**:
   - Highlight candidate experience matching the job posting without fabricating credentials.
   - Quantify achievements (e.g. percentages, scale, throughput) where applicable.

4. **Cover Letter Synthesis**:
   - Keep cover letters concise (3-4 paragraphs max).
   - Tailor the opening hook to the target company and position title.
