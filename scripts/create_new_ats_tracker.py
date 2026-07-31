"""Create a tracker for Product Management roles on supported browser-form ATSs."""

from pathlib import Path
import openpyxl

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
TRACKER_PATH = DATA_DIR / "new_ats_product_management_jobs.xlsx"

ROLES = [
    # Workable
    (
        "YouTrip",
        "Product Manager (Marketing & Operations Platforms)",
        "https://apply.workable.com/youtrip/",
    ),
    ("NoGood", "Product Manager (Goodie AI)", "https://apply.workable.com/nogood/"),
    ("Fuku", "Product Manager (AI-Powered Workflows)", "https://apply.workable.com/fuku/"),
    # SmartRecruiters
    (
        "Scalable Capital",
        "Product Manager - AI Platform",
        "https://jobs.smartrecruiters.com/ScalableGmbH/",
    ),
    ("Vitol", "GenAI Product Manager", "https://jobs.smartrecruiters.com/Vitol/"),
    ("Grab", "Lead Product Manager, Deliveries", "https://jobs.smartrecruiters.com/Grab/"),
]


def create_tracker() -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Jobs"
    ws.append(["Company", "Role", "URL"])

    for company, role, url in ROLES:
        ws.append([company, role, url])

    TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    wb.save(TRACKER_PATH)
    print(f"Created tracker at {TRACKER_PATH} with {len(ROLES)} jobs.")


if __name__ == "__main__":
    create_tracker()
