"""FastAPI 应用入口。

启动：``uvicorn app.main:app --host 0.0.0.0 --port 8000``
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.config import get_settings
from app.database import init_db
from app.routers import router
from app.services import BlockedError, BusinessError, ConflictError

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    lifespan=lifespan,
    description=(
        "集装箱船配载方案核算服务。录入槽位资料（堆栈/层/承重/纵向力臂/"
        "相邻关系/力矩区间）与箱清单（箱号/重量/危险类别/卸港先后），"
        "核算满足全部约束的最优完整配载方案，并支持修订核对与并发安全确认；"
        "方案确认后可按岸桥数量与每箱吊装时长/时间窗生成两阶段优选的装船时间表，"
        "并按该排程逐箱登记实际开工/完工事件（事件可晚到、按实际分钟重放，"
        "首次登记原子冻结排程基线用于偏差核算，方案被取代后实绩只读），"
        "或按各卸港允许剩余总力矩区间生成全程箱号序列字典序最小的逐港卸箱顺序；"
        "码头临时封锁槽位时，可基于当前已确认方案、最新资料修订生成应急改配草稿"
        "（先最少换槽箱数，再最低最高占用层，最后箱号升序槽位序列字典序最小）；"
        "装船完结交接时可凭交接号、截止分钟、期望实绩版本、有效流水号集合与最大"
        "完工延误阈值申请不可变交接凭证（每箱截止前完工、截止后无有效事件、按冻结"
        "基线逐箱核算延误；凭证签发后实绩变化不改内容，仅标示是否仍对应当前实绩）。"
        "已完成装船交接的当前方案离港前，可按方案编号登记覆盖全部箱且封签号互异的"
        "预期封签清单（冻结后不可改写，同内容重试幂等、异内容冲突），现场按箱号、"
        "实见封签号、检查分钟与全局唯一扫描号逐箱登记封签扫描（不符留痕但不算通过，"
        "同箱以检查分钟较晚者为准、同分钟以登记顺序较晚者为准），并凭当前交接凭证、"
        "期望扫描版本、截止分钟与唯一申请号申请离港放行：仅当方案仍已确认、交接凭证"
        "仍对应当前实绩、每箱最新扫描匹配预期且不晚于截止分钟时，原子签发含逐箱结果"
        "的不可变放行凭证（后续扫描、实绩更正或方案取代只使凭证显示失效，不改历史内容）。"
    ),
)


@app.exception_handler(BusinessError)
def _business_handler(_request: Request, exc: BusinessError) -> JSONResponse:
    content = {"detail": exc.message}
    if getattr(exc, "extra", None):
        content.update(exc.extra)
    return JSONResponse(status_code=exc.status_code, content=content)


@app.exception_handler(ConflictError)
def _conflict_handler(_request: Request, exc: ConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": exc.message})


@app.exception_handler(BlockedError)
def _blocked_handler(_request: Request, exc: BlockedError) -> JSONResponse:
    # 所选箱合法但新状态无可行后续：422（业务不可处理），进度保持不变
    content = {"detail": exc.message}
    if getattr(exc, "extra", None):
        content.update(exc.extra)
    return JSONResponse(status_code=422, content=content)


@app.get("/health", tags=["系统"], summary="健康检查")
def health() -> dict:
    return {"status": "ok", "version": __version__}


app.include_router(router, prefix=settings.api_prefix)
