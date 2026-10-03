"""HTTP API 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app import services
from app.database import get_db
from app.schemas import (
    ActualEventCorrectionIn,
    ActualEventRegisterIn,
    ActualsOut,
    AssignmentOut,
    HandoverCreateIn,
    HandoverOut,
    ManifestIn,
    ManifestRevisionOut,
    MessageOut,
    PlanAmendIn,
    PlanComputeIn,
    PlanConfirmIn,
    PlanOut,
    RestowCreateIn,
    RestowMoveOut,
    RestowOut,
    ScheduleCreateIn,
    ScheduleEntryOut,
    ScheduleOut,
    SlotDataIn,
    SlotRevisionOut,
    UnloadingCreateIn,
    UnloadingExecuteIn,
    UnloadingExecutionOut,
    UnloadingOut,
    UnloadingPortOut,
    UnloadingStepOut,
)

router = APIRouter()


def _plan_out(p) -> PlanOut:
    return PlanOut(
        plan_id=p.id,
        revision_no=p.revision_no,
        status=p.status,
        slots_revision=p.slots_revision,
        manifest_revision=p.manifest_revision,
        total_moment=p.total_moment,
        max_occupied_tier=p.max_tier,
        assignments=[
            AssignmentOut(
                container_no=i.container_no, slot_code=i.slot_code,
                stack=i.stack, tier=i.tier,
            )
            for i in sorted(p.items, key=lambda x: x.container_no)
        ],
        created_at=services.utc_iso(p.created_at),
        confirmed_at=services.utc_iso(p.confirmed_at),
        parent_id=p.parent_id,
        root_id=p.root_id,
    )


# ---------- 资料修订 ----------


@router.post(
    "/slot-revisions",
    response_model=SlotRevisionOut,
    status_code=201,
    summary="录入槽位资料（生成新修订）",
)
def create_slot_revision(data: SlotDataIn, db: Session = Depends(get_db)):
    rev = services.create_slot_revision(db, data)
    return SlotRevisionOut(
        slots_revision=rev.revision,
        slot_count=len(rev.slots),
        min_moment=rev.min_moment,
        max_moment=rev.max_moment,
        created_at=services.utc_iso(rev.created_at),
    )


@router.post(
    "/manifest-revisions",
    response_model=ManifestRevisionOut,
    status_code=201,
    summary="录入箱清单（生成新修订）",
)
def create_manifest_revision(data: ManifestIn, db: Session = Depends(get_db)):
    rev = services.create_manifest_revision(db, data)
    return ManifestRevisionOut(
        manifest_revision=rev.revision,
        container_count=len(rev.containers),
        created_at=services.utc_iso(rev.created_at),
    )


@router.get("/slot-revisions/{revision}", summary="查询槽位资料修订")
def get_slot_revision(revision: int, db: Session = Depends(get_db)):
    rev = services.get_slot_revision(db, revision)
    return {
        "slots_revision": rev.revision,
        "min_moment": rev.min_moment,
        "max_moment": rev.max_moment,
        "created_at": services.utc_iso(rev.created_at),
        "slots": [
            {
                "code": s.code, "stack": s.stack, "tier": s.tier,
                "capacity": s.capacity, "arm": s.arm,
            }
            for s in sorted(rev.slots, key=lambda x: (x.stack, x.tier))
        ],
        "adjacency": [[a.slot_a, a.slot_b] for a in rev.adjacencies],
        "incompatible_classes": [
            [i.class_a, i.class_b] for i in rev.incompatibilities
        ],
    }


@router.get("/manifest-revisions/{revision}", summary="查询箱清单修订")
def get_manifest_revision(revision: int, db: Session = Depends(get_db)):
    rev = services.get_manifest_revision(db, revision)
    return {
        "manifest_revision": rev.revision,
        "created_at": services.utc_iso(rev.created_at),
        "containers": [
            {
                "no": c.no, "weight": c.weight, "hazmat": c.hazmat,
                "discharge_order": c.discharge_order,
            }
            for c in sorted(rev.containers, key=lambda x: x.no)
        ],
    }


# ---------- 方案核算 ----------


@router.post(
    "/plans/compute",
    response_model=PlanOut,
    status_code=201,
    responses={422: {"model": MessageOut}},
    summary="核算新配载方案（无解时不保存）",
)
def compute_plan(body: PlanComputeIn, db: Session = Depends(get_db)):
    plan = services.compute_plan(
        db,
        slots_revision=body.slots_revision,
        manifest_revision=body.manifest_revision,
    )
    return _plan_out(plan)


@router.post(
    "/plans/{plan_id}/amend",
    response_model=PlanOut,
    status_code=201,
    responses={404: {"model": MessageOut}, 422: {"model": MessageOut}},
    summary="基于现有方案修订并重新核算（生成方案修订号）",
)
def amend_plan(plan_id: str, body: PlanAmendIn, db: Session = Depends(get_db)):
    plan = services.compute_plan(
        db,
        slots_revision=body.slots_revision,
        manifest_revision=body.manifest_revision,
        parent_id=plan_id,
    )
    return _plan_out(plan)


@router.post(
    "/plans/{plan_id}/confirm",
    response_model=PlanOut,
    responses={409: {"model": MessageOut}},
    summary="确认方案（核对资料修订；并发安全）",
)
def confirm_plan(plan_id: str, body: PlanConfirmIn, db: Session = Depends(get_db)):
    plan = services.confirm_plan(db, plan_id, body)
    return _plan_out(plan)


# ---------- 应急改配 ----------


def _restow_out(data: dict) -> RestowOut:
    p = data["plan"]
    plan_view = _plan_out(p)
    return RestowOut(
        plan_id=p.id,
        revision_no=p.revision_no,
        status=p.status,
        root_id=p.root_id,
        parent_id=p.parent_id,
        slots_revision=p.slots_revision,
        manifest_revision=p.manifest_revision,
        blocked_slots=data["blocked_slots"],
        moved_boxes=len(data["moves"]),
        max_occupied_tier=p.max_tier,
        total_moment=p.total_moment,
        assignments=plan_view.assignments,
        moves=[RestowMoveOut(**m) for m in data["moves"]],
        created_at=services.utc_iso(p.created_at),
    )


@router.post(
    "/plans/{plan_id}/restow",
    response_model=RestowOut,
    status_code=201,
    responses={404: {"model": MessageOut}, 409: {"model": MessageOut},
               422: {"model": MessageOut}},
    summary="码头临时封锁槽位：计算应急改配草稿（先最少换槽、再最低层级、再字典序）",
)
def create_emergency_restow(
    plan_id: str, body: RestowCreateIn, db: Session = Depends(get_db)
):
    data = services.create_emergency_restow(db, plan_id, body)
    return _restow_out(data)


@router.get("/plans", response_model=list[PlanOut], summary="列出全部方案")
def list_plans(db: Session = Depends(get_db)):
    return [_plan_out(p) for p in services.list_plans(db)]


@router.get(
    "/plans/{plan_id}",
    response_model=PlanOut,
    responses={404: {"model": MessageOut}},
    summary="查询方案（含每箱槽位、总力矩、方案修订号）",
)
def get_plan(plan_id: str, db: Session = Depends(get_db)):
    return _plan_out(services.get_plan(db, plan_id))


# ---------- 装船时间表 ----------


def _schedule_out(s) -> ScheduleOut:
    return ScheduleOut(
        plan_id=s.plan_id,
        revision_no=s.plan.revision_no,
        status=s.status,
        crane_count=s.crane_count,
        makespan=s.makespan,
        entries=[
            ScheduleEntryOut(
                container_no=i.container_no,
                start_minute=i.start_minute,
                finish_minute=i.finish_minute,
                crane_no=i.crane_no,
                stack=i.stack,
                tier=i.tier,
            )
            for i in sorted(s.items, key=lambda x: x.container_no)
        ],
        created_at=services.utc_iso(s.created_at),
    )


@router.post(
    "/plans/{plan_id}/schedules",
    response_model=ScheduleOut,
    status_code=201,
    responses={404: {"model": MessageOut}, 409: {"model": MessageOut},
               422: {"model": MessageOut}},
    summary="为已确认方案修订生成装船时间表（先最短总体完工，再按箱号字典序最小）",
)
def create_loading_schedule(
    plan_id: str, body: ScheduleCreateIn, db: Session = Depends(get_db)
):
    schedule = services.create_loading_schedule(db, plan_id, body)
    return _schedule_out(schedule)


@router.get(
    "/schedules",
    response_model=list[ScheduleOut],
    summary="列出装船时间表（默认仅当前排程；historical=true 含历史排程）",
)
def list_loading_schedules(
    historical: bool = False, db: Session = Depends(get_db)
):
    schedules = services.list_loading_schedules(db, include_historical=historical)
    return [_schedule_out(s) for s in schedules]


@router.get(
    "/plans/{plan_id}/schedules",
    response_model=ScheduleOut,
    responses={404: {"model": MessageOut}},
    summary="查询某方案修订绑定的装船时间表（历史排程可查，含 status）",
)
def get_loading_schedule(plan_id: str, db: Session = Depends(get_db)):
    return _schedule_out(services.get_loading_schedule(db, plan_id))


# ---------- 逐港卸箱顺序 ----------


def _unloading_out(u) -> UnloadingOut:
    return UnloadingOut(
        plan_id=u.plan_id,
        revision_no=u.plan.revision_no,
        status=u.status,
        initial_moment=u.initial_moment,
        final_moment=u.final_moment,
        ports=[
            UnloadingPortOut(
                discharge_order=p.discharge_order,
                min_remaining_moment=p.min_remaining_moment,
                max_remaining_moment=p.max_remaining_moment,
            )
            for p in sorted(u.ports, key=lambda x: x.discharge_order)
        ],
        steps=[
            UnloadingStepOut(
                step_no=s.step_no,
                discharge_order=s.discharge_order,
                container_no=s.container_no,
                slot_code=s.slot_code,
                stack=s.stack,
                tier=s.tier,
                weight=s.weight,
                arm=s.arm,
                remaining_moment=s.remaining_moment,
            )
            for s in sorted(u.steps, key=lambda x: x.step_no)
        ],
        created_at=services.utc_iso(u.created_at),
    )


@router.post(
    "/plans/{plan_id}/unloading",
    response_model=UnloadingOut,
    status_code=201,
    responses={404: {"model": MessageOut}, 409: {"model": MessageOut},
               422: {"model": MessageOut}},
    summary="为已确认方案修订规划逐港卸箱顺序（全程箱号序列字典序最小）",
)
def create_unloading_sequence(
    plan_id: str, body: UnloadingCreateIn, db: Session = Depends(get_db)
):
    sequence = services.create_unloading_sequence(db, plan_id, body)
    return _unloading_out(sequence)


@router.get(
    "/unloading",
    response_model=list[UnloadingOut],
    summary="列出逐港卸箱顺序（默认仅当前；historical=true 含历史顺序）",
)
def list_unloading_sequences(
    historical: bool = False, db: Session = Depends(get_db)
):
    sequences = services.list_unloading_sequences(db, include_historical=historical)
    return [_unloading_out(u) for u in sequences]


@router.get(
    "/plans/{plan_id}/unloading",
    response_model=UnloadingOut,
    responses={404: {"model": MessageOut}},
    summary="查询某方案修订绑定的逐港卸箱顺序（历史顺序可查，含 status）",
)
def get_unloading_sequence(plan_id: str, db: Session = Depends(get_db)):
    return _unloading_out(services.get_unloading_sequence(db, plan_id))


# ---------- 逐港卸箱执行（现场逐步登记） ----------


def _strip_internal(view: dict) -> dict:
    """去掉服务内部字段，得到对外执行进度视图。"""
    return {k: v for k, v in view.items() if not k.startswith("_")}


@router.post(
    "/plans/{plan_id}/unloading/execute",
    response_model=UnloadingExecutionOut,
    responses={404: {"model": MessageOut}, 409: {"model": MessageOut},
               422: {"model": MessageOut}},
    summary="现场登记卸下一箱（可偏离原计划；原子核对期望版本，成功后返回剩余推荐顺序）",
)
def execute_unloading_step(
    plan_id: str,
    body: UnloadingExecuteIn,
    response: Response,
    db: Session = Depends(get_db),
):
    view, idempotent_hit = services.execute_unloading_step(db, plan_id, body)
    # 同操作号同内容重试返回首次结果（200）；新登记成功为 201
    response.status_code = 200 if idempotent_hit else 201
    return _strip_internal(view)


@router.get(
    "/plans/{plan_id}/unloading/execution",
    response_model=UnloadingExecutionOut,
    responses={404: {"model": MessageOut}},
    summary="查询卸箱执行进度：已卸步骤、当前港、剩余力矩、进度版本与推荐顺序",
)
def get_unloading_execution(plan_id: str, db: Session = Depends(get_db)):
    return _strip_internal(services.get_unloading_execution(db, plan_id))


# ---------- 装船实绩登记（事件重放） ----------


@router.post(
    "/plans/{plan_id}/actual-events",
    response_model=ActualsOut,
    responses={404: {"model": MessageOut}, 409: {"model": MessageOut},
               422: {"model": MessageOut}},
    summary="登记逐箱实际开工/完工事件（事件可晚到，按实际分钟重放；首次冻结排程基线）",
)
def register_actual_events(
    plan_id: str,
    body: ActualEventRegisterIn,
    response: Response,
    db: Session = Depends(get_db),
):
    actuals, idempotent_hit = services.register_actual_events(db, plan_id, body)
    # 同流水号同内容重试返回原结果（200），新事件登记成功为 201
    response.status_code = 200 if idempotent_hit else 201
    return services.build_actuals_out(db, actuals)


@router.post(
    "/plans/{plan_id}/actual-events/correction",
    response_model=ActualsOut,
    responses={404: {"model": MessageOut}, 409: {"model": MessageOut},
               422: {"model": MessageOut}},
    summary="更正已登记实绩事件的分钟或岸桥（沿用原流水号/类型/箱号；版本递增、留更正历史）",
)
def correct_actual_event(
    plan_id: str,
    body: ActualEventCorrectionIn,
    response: Response,
    db: Session = Depends(get_db),
):
    out, idempotent_hit = services.correct_actual_event(db, plan_id, body)
    # 同更正请求号同内容重试返回首次更正结果（200）；新更正生效为 201
    response.status_code = 200 if idempotent_hit else 201
    return out


@router.get(
    "/plans/{plan_id}/actuals",
    response_model=ActualsOut,
    responses={404: {"model": MessageOut}},
    summary="查询装船实绩：每箱状态、实际起止、相对冻结排程偏差与未完箱",
)
def get_actuals(plan_id: str, db: Session = Depends(get_db)):
    actuals = services.get_actuals(db, plan_id)
    return services.build_actuals_out(db, actuals)


# ---------- 装船完结交接（不可变凭证） ----------


@router.post(
    "/plans/{plan_id}/handovers",
    response_model=HandoverOut,
    responses={404: {"model": MessageOut}, 409: {"model": MessageOut},
               422: {"model": MessageOut}},
    summary="装船完结交接：申请不可变凭证（截止前全完工、无截止后事件、完工延误不超阈值）",
)
def issue_loading_handover(
    plan_id: str,
    body: HandoverCreateIn,
    response: Response,
    db: Session = Depends(get_db),
):
    handover, idempotent_hit = services.issue_loading_handover(db, plan_id, body)
    # 同交接号同内容重试返回原凭证（200）；新凭证签发为 201
    response.status_code = 200 if idempotent_hit else 201
    return services.build_handover_out(db, handover)


@router.get(
    "/plans/{plan_id}/handovers",
    response_model=list[HandoverOut],
    responses={404: {"model": MessageOut}},
    summary="列出某方案的装船完结交接凭证（历史凭证仍可读）",
)
def list_plan_handovers(plan_id: str, db: Session = Depends(get_db)):
    handovers = services.list_plan_handovers(db, plan_id)
    return [services.build_handover_out(db, h) for h in handovers]


@router.get(
    "/plans/{plan_id}/handovers/{handover_id}",
    response_model=HandoverOut,
    responses={404: {"model": MessageOut}},
    summary="查询单个交接凭证（内容不可变；标示是否仍对应当前实绩）",
)
def get_plan_handover(
    plan_id: str, handover_id: str, db: Session = Depends(get_db)
):
    handover = services.get_plan_handover(db, plan_id, handover_id)
    return services.build_handover_out(db, handover)


@router.get(
    "/handovers/{handover_id}",
    response_model=HandoverOut,
    responses={404: {"model": MessageOut}},
    summary="按凭证号查询交接凭证（全局，历史凭证仍可读）",
)
def get_handover(handover_id: str, db: Session = Depends(get_db)):
    handover = services.get_handover_by_pk(db, handover_id)
    return services.build_handover_out(db, handover)
