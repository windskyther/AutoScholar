"""Task-specific planning and output contracts."""

TASK_TYPES: dict[str, dict[str, str]] = {
    "research": {
        "label": "资料调研",
        "guidance": (
            "Investigate the exact question using supported sources; explain findings, "
            "evidence gaps and conclusions."
        ),
    },
    "literature_review": {
        "label": "论文综述",
        "guidance": (
            "Build a literature review: organize research themes and chronology, compare "
            "methods and limitations, and identify open questions. Prefer paper sources; "
            "never invent bibliographic details."
        ),
    },
    "comparison": {
        "label": "技术对比",
        "guidance": (
            "Compare the requested methods or technologies against consistent criteria. "
            "Produce an evidence-grounded comparison table, tradeoffs and recommendations "
            "conditional on the user's needs."
        ),
    },
    "research_proposal": {
        "label": "研究方案",
        "guidance": (
            "Develop a research proposal: problem, existing evidence, testable "
            "hypotheses, proposed methods, evaluation plan and risks. Clearly distinguish "
            "proposed experiments from measured results; do not claim to execute them."
        ),
    },
    "document_analysis": {
        "label": "项目文档分析",
        "guidance": (
            "Analyze only the selected project documents. Answer the exact question with "
            "page/source citations, contradictions and gaps; do not substitute web search "
            "for missing documents."
        ),
    },
}


def is_research(kind: str) -> bool:
    return kind in TASK_TYPES


def task_guidance(kind: str) -> str:
    return TASK_TYPES.get(kind, {}).get("guidance", "")
