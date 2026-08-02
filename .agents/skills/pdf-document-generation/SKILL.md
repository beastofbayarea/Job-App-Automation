---
name: pdf-document-generation
description: Standards for generating ATS-compliant, single-page PDF resumes and cover letters using ReportLab and PyMuPDF.
---

# PDF Document Generation Skill

This skill defines rules for producing clean, ATS-scannable PDF documents programmatically.

## PDF Generation Standards

1. **ATS Parsability**:
   - Store content as clean, searchable vector text (not image scans).
   - Use standard system fonts or clean Helvetica/Times-Roman fonts embedded directly.
   - Avoid complex multi-column tables or floating frames that confuse ATS text extractors.

2. **ReportLab Canvas Math**:
   - Dynamically compute height and spacing to guarantee single-page constraint when requested.
   - Set consistent page margins (0.5 to 0.75 inches).

3. **Validation with PyMuPDF / PyPDF**:
   - Extract raw text from generated PDFs to verify parseability.
   - Verify page count (`doc.page_count == 1`) prior to job application submission.
