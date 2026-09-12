"""新進場次的主題／AI／實踐強度自動判讀。

只用在「官方目錄新增、頁面上還沒有人工標註」的場次；
已有標註的場次一律沿用原值，不會被這裡覆寫。
"""
from __future__ import annotations

import re

_I = re.IGNORECASE

# 依序比對，第一個命中的主題勝出（順序＝特異性由高到低）
TOPIC_RULES: list[tuple[str, str]] = [
    ("Water / InfoWorks", r"InfoWorks|InfoDrainage|\bICM\b|drainage|stormwater|wastewater|\bwater\b|flood|hydraul|sewer"),
    ("Media & Entertainment", r"\bMaya\b|3ds Max|\bFlame\b|Flow Production|\bVFX\b|animat|\bfilm\b|\bgame|MotionBuilder|\bArnold\b|lookdev|\bmedia\b|entertainment|creators?\b"),
    ("Industrialized Construction", r"Industrialized Construction|prefab|modular|offsite|off-site|productization|Informed Design"),
    ("Sustainability / Carbon", r"carbon|sustainab|embodied|biodiversity|net zero|decarboni|climate|energy analysis"),
    ("Digital Twin / Tandem / Ops", r"Tandem|digital twin|operational twin|\boperations?\b|facility|facilities|asset (data|management|strategy)|handover|\bEAM\b|owner"),
    ("Vault / PLM / Data", r"\bVault\b|\bPLM\b|Fusion Manage|product data|bill of materials|\bBOM\b|change management"),
    ("Manufacturing / Fusion / CAM", r"\bFusion\b|Inventor|\bCAM\b|manufactur|factory|machining|product (design|development)|Nastran|fabricat|\bCNC\b|physical product"),
    ("Reality Capture / XR", r"reality capture|ReCap|\bscan|point cloud|\bXR\b|\bVR\b|\bAR\b|immersive|Workshop XR|lidar|laser|CloudWorx|FARO|mesh"),
    ("Platform Services / APS Dev", r"\bAPS\b|Platform Services|\bAPI\b|\bMCP\b|developer|\bSDK\b|plug-?in|add-?in|\bcode\b|coding|app\b|automation ecosystem"),
    ("Civil 3D / Infrastructure", r"Civil 3D|InfraWorks|\broads?\b|\brail|bridge|\bGIS\b|infrastructure|transportation|highway|airport|street|geospatial|ArcGIS|Esri"),
    ("AutoCAD / Drafting", r"AutoCAD|Plant 3D|drafting|\bDWG\b|AutoLISP"),
    ("Revit / BIM", r"\bRevit\b|\bBIM\b|Dynamo|famil(y|ies)|\bMEP\b|structural|electrical design"),
    ("Forma / Construction Cloud", r"\bForma\b|Construction Cloud|\bACC\b|Autodesk Build|\bBuild\b|\bDocs\b|construction|jobsite|Takeoff|estimat|procurement|preconstruction|schedul|field"),
    ("AI Strategy & Trust", r"\bAI\b|\bagent|agentic|machine learning|\bLLM|copilot|generative|workstation|GPU|CPU|hardware"),
    ("Community / Networking", r"community|meetup|networking|watch party"),
]

AI_RE = re.compile(
    r"\bAI\b|\bA\.I\.|agent|agentic|assistant|machine learning|\bML\b|\bLLM|\bMCP\b|generative|GenAI|copilot|neural|intelligen",
    _I,
)

# 實踐強度：課名裡「真的有人做過」的訊號
_PRAC_STRONG = re.compile(
    r"how (we|i|our)\b|lessons?\b|case stud|journey|we built|real[- ]world|in practice|in action|playbook|"
    r"from pilot|at scale|replaced|migrat|deliver(ed|ing)|transform(ed|ing)",
    _I,
)
_PRAC_NUMBER = re.compile(r"\b\d{2,}\b|\d+%|\d+\+")
_PRAC_WEAK = re.compile(
    r"workflow|hands[- ]on|practical|tips|build(ing)?|automat|implement|deploy|integrat|step[- ]by[- ]step|guide",
    _I,
)
NO_PRAC_FORMATS = {"Hands-on Lab", "Meetup", "Watch Party", "Design Slam", "Roundtable", "Lunch & Learn"}
ZERO_PRAC_FORMATS = {"Roadmap", "Keynote"}


def topic_of(title: str, fmt: str, text: str = "") -> str:
    if fmt in ("Keynote", "Roadmap"):
        return "Keynote / Roadmap"
    if fmt in ("Watch Party", "Meetup"):
        return "Community / Networking"
    for topic, pat in TOPIC_RULES:
        if re.search(pat, title, _I):
            return topic
    for topic, pat in TOPIC_RULES:
        if text and re.search(pat, text, _I):
            return topic
    return "Leadership / Practice"


def ai_of(title: str, abstract: str = "") -> bool:
    if AI_RE.search(title):
        return True
    return len(AI_RE.findall(abstract or "")) >= 3


def prac_of(title: str, fmt: str) -> int:
    if fmt in NO_PRAC_FORMATS:
        return -1
    if fmt in ZERO_PRAC_FORMATS:
        return 0
    base = 0 if fmt in ("Spark Session", "Solution Spotlight") else 1
    score = base
    if _PRAC_STRONG.search(title):
        score += 1
    if _PRAC_NUMBER.search(title):
        score += 1
    if score == 0 and _PRAC_WEAK.search(title):
        score = 1
    return min(score, 3)
