from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import Base
from autoscholar.agent.records import CitationRecord, ResearchWarningRecord
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.rag import database_models as rag_database_models  # noqa: F401


async def test_repository_persists_task_and_ordered_tool_trace() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repository = AgentTaskRepository(async_sessionmaker(engine, expire_on_commit=False))

    created = await repository.create_task(
        task_id="task-1", objective="Calculate 2 + 2", research_sources=["web"]
    )
    await repository.add_tool_call(
        task_id=created.id,
        sequence=1,
        call_id="call-1",
        tool_name="calculator",
        arguments={"expression": "2 + 2"},
        output="4",
        status="succeeded",
        duration_ms=1.5,
    )
    await repository.update_task(
        created.id,
        status="succeeded",
        plan=["Calculate the expression"],
        answer="4",
        metrics={"iterations": 1, "model_calls": 3},
        mode="research",
        citations=[CitationRecord(claim="LoRA uses low-rank matrices", evidence_ids=("E1",))],
        warnings=[
            ResearchWarningRecord(
                code="web_search_partial",
                message="One web query failed",
                provider="tavily",
            )
        ],
    )
    evidence = await repository.add_evidence(
        task_id=created.id,
        citation_key="E1",
        source_type="paper",
        provider="semantic_scholar",
        title="LoRA",
        url="https://doi.org/10.1/lora",
        authors=("Alice",),
        year=2021,
        external_id="10.1/lora",
        query="LoRA paper",
        topic="LoRA",
        claim="LoRA uses low-rank matrices",
        excerpt="We propose low-rank adaptation.",
        relevance=0.95,
    )

    loaded = await repository.get_task(created.id)
    assert loaded is not None
    assert loaded.status == "succeeded"
    assert loaded.answer == "4"
    assert loaded.plan == ["Calculate the expression"]
    assert loaded.tool_calls[0].call_id == "call-1"
    assert loaded.tool_calls[0].arguments == {"expression": "2 + 2"}
    assert loaded.mode == "research"
    assert loaded.research_sources == ["web"]
    assert loaded.citations[0].evidence_ids == ("E1",)
    assert loaded.warnings[0].provider == "tavily"
    assert loaded.evidence == [evidence]

    listed, total = await repository.list_evidence(created.id, limit=1, offset=0)
    assert total == 1
    assert listed == [evidence]

    await engine.dispose()


async def test_repository_returns_none_for_unknown_task() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repository = AgentTaskRepository(async_sessionmaker(engine, expire_on_commit=False))

    assert await repository.get_task("missing") is None
    await engine.dispose()
