"""Script to create Excel tracker for the 3 Product Management roles per new ATS."""

from pathlib import Path
import openpyxl

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
TRACKER_PATH = DATA_DIR / "new_ats_product_management_jobs.xlsx"

ROLES = [
    # Workable
    ("YouTrip", "Product Manager (Marketing & Operations Platforms)", "https://apply.workable.com/youtrip/"),
    ("NoGood", "Product Manager (Goodie AI)", "https://apply.workable.com/nogood/"),
    ("Fuku", "Product Manager (AI-Powered Workflows)", "https://apply.workable.com/fuku/"),
    # SmartRecruiters
    ("Scalable Capital", "Product Manager - AI Platform", "https://jobs.smartrecruiters.com/ScalableGmbH/"),
    ("Vitol", "GenAI Product Manager", "https://jobs.smartrecruiters.com/Vitol/"),
    ("Grab", "Lead Product Manager, Deliveries", "https://jobs.smartrecruiters.com/Grab/"),
    # Recruitee
    ("Aikido Security", "Product Manager (Enterprise)", "https://aikidosecurity.recruitee.com/"),
    ("Bitfinex", "Product Manager (Trading & Wallets)", "https://bitfinex.recruitee.com/"),
    ("Better Collective", "Product Manager, Partnerships", "https://bettercollective.recruitee.com/"),
    # BambooHR
    ("BambooHR", "Product Manager II - HRIS Compensation", "https://bamboohr.bamboohr.com/careers/"),
    ("BambooHR", "Product Manager II - HRIS Training", "https://bamboohr.bamboohr.com/careers/"),
    ("BambooHR", "Product Manager - Core Data Platform", "https://bamboohr.bamboohr.com/careers/"),
    # Breezy HR
    ("Beli", "Product Manager (Consumer Products)", "https://beli.breezy.hr/"),
    ("Aidaptive", "Product Manager (0 to 1 AI/Data Platform)", "https://aidaptive.breezy.hr/"),
    ("Nexo", "Product Manager (FinTech / Crypto)", "https://nexo.breezy.hr/"),
    # JazzHR
    ("Codekeeper", "Product Manager (Security & UX)", "https://codekeeper.applytojob.com/apply/"),
    ("Instinct Science", "Product Manager (Veterinary Software)", "https://instinctscience.applytojob.com/apply/"),
    ("Wealth Access", "Product Manager (Wealth Management Tech)", "https://wealthaccess.applytojob.com/apply/"),
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
