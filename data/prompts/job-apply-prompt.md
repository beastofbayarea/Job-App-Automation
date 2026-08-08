# Job Apply Prompt

Process every job URL in this file for the specific ATS platform:

Run locally (not VPS or cloud) with up to 3 parallel sub-agents, one job/tab each. Leave completed tabs open and continue until the queue is exhausted.

Reuse one Chrome debug session, open tabs in the background, skip submitted roles, and never click Submit.

For each job, use a personalized resume, random email from C:\Users\Nagarro\Downloads\job-flow-ai\config\candidate_email_pool.json and LLM-generated answers.

Fill all fields with any possible assumptions or estimates, keep essays concise and MECE, and verify the correct resume is attached; retry its upload once.

Use short render timeouts: on a hang, reload the same tab once, then reopen once in a new background tab before skipping. On errors/timeouts, clean up only the affected helper; never kill Chrome or close unrelated tabs.
