"""业务服务层。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.actuals import ActualEvent, ActualsReplayError, replay as replay_actuals
from app.schemas import (
    ActualEventCorrectionIn,
    ActualEventRegisterIn,
    ManifestIn,
    PlanConfirmIn,
    RestowCreateIn,
    ScheduleCreateIn,
    SlotDataIn,
    UnloadingCreateIn,
)
from app.scheduler import InfeasibleSchedule, Job, solve_loading
from app.solver import (
    Container,
    InfeasibleStowage,
    Slot,
    solve_restow,
    solve_stowage,
)
from app.unloader import (
    EPS as UNLOAD_EPS,
    InfeasibleUnloading,
    PlacedContainer,
    PortWindow,
    solve_unloading,
)


class BusinessError(Exception):
    def __init__(self, message: str, status_code: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        # 附加结构化字段（如超限箱与偏差），随 {"detail": ..., **extra} 一并返回
        self.extra = extra


class ConflictError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class BlockedError(Exception):
    """所选箱合法但新状态下不存在可卸完全部剩余箱的后续（进度不变，422）。"""

    def __init__(self, message: str, extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.extra = extra


# ---------- 资料修订 ----------


def create_slot_revision(db: Session, data: SlotDataIn) -> models.SlotRevision:
    next_rev = (db.scalar(select(func.max(models.SlotRevision.revision))) or 0) + 1
    rev = models.SlotRevision(
        revision=next_rev,
        min_moment=data.min_moment,
        max_moment=data.max_moment,
    )
    for s in data.slots:
        rev.slots.append(
            models.SlotRow(
                code=s.code, stack=s.stack, tier=s.tier,
                capacity=s.capacity, arm=s.arm,
            )
        )
    for a, b in {tuple(sorted(p)) for p in data.adjacency}:
        rev.adjacencies.append(models.AdjacencyRow(slot_a=a, slot_b=b))
    for a, b in {
        tuple(sorted((p[0].strip(), p[1].strip())))
        for p in data.incompatible_classes
    }:
        rev.incompatibilities.append(models.IncompatRow(class_a=a, class_b=b))
    db.add(rev)
    db.commit()
    db.refresh(rev)
    return rev


def create_manifest_revision(db: Session, data: ManifestIn) -> models.ManifestRevision:
    next_rev = (db.scalar(select(func.max(models.ManifestRevision.revision))) or 0) + 1
    rev = models.ManifestRevision(revision=next_rev)
    for c in data.containers:
        rev.containers.append(
            models.ContainerRow(
                no=c.no,
                weight=c.weight,
                hazmat=c.hazmat,
                discharge_order=c.discharge_order,
            )
        )
    db.add(rev)
    db.commit()
    db.refresh(rev)
    return rev


def get_slot_revision(db: Session, revision: int | None) -> models.SlotRevision:
    if revision is None:
        rev = db.scalar(
            select(models.SlotRevision).order_by(models.SlotRevision.revision.desc())
        )
    else:
        rev = db.scalar(
            select(models.SlotRevision).where(models.SlotRevision.revision == revision)
        )
    if rev is None:
        raise BusinessError(f"槽位资料修订 {revision} 不存在", 404)
    return rev


def get_manifest_revision(db: Session, revision: int | None) -> models.ManifestRevision:
    if revision is None:
        rev = db.scalar(
            select(models.ManifestRevision).order_by(
                models.ManifestRevision.revision.desc()
            )
        )
    else:
        rev = db.scalar(
            select(models.ManifestRevision).where(
                models.ManifestRevision.revision == revision
            )
        )
    if rev is None:
        raise BusinessError(f"箱清单修订 {revision} 不存在", 404)
    return rev


def _to_solver_inputs(slot_rev, manifest_rev):
    slots = [
        Slot(
            code=r.code, stack=r.stack, tier=r.tier,
            capacity=r.capacity, arm=r.arm,
        )
        for r in slot_rev.slots
    ]
    containers = [
        Container(
            no=r.no, weight=r.weight, hazmat=r.hazmat,
            discharge_order=r.discharge_order,
        )
        for r in manifest_rev.containers
    ]
    adjacency = [(r.slot_a, r.slot_b) for r in slot_rev.adjacencies]
    incompat = [(r.class_a, r.class_b) for r in slot_rev.incompatibilities]
    return slots, containers, adjacency, incompat


# ---------- 方案 ----------


def _save_plan(
    db: Session,
    *,
    slots_rev,
    manifest_rev,
    result,
    parent: models.Plan | None,
    status: str = "draft",
) -> models.Plan:
    new_id = models._new_id()
    plan = models.Plan(
        id=new_id,
        root_id=parent.root_id if parent else new_id,
        revision_no=(parent.revision_no + 1) if parent else 1,
        status=status,
        slots_revision=slots_rev.revision,
        manifest_revision=manifest_rev.revision,
        total_moment=result.total_moment,
        max_tier=result.max_occupied_tier,
        parent_id=parent.id if parent else None,
    )
    for a in result.assignments:
        plan.items.append(
            models.PlanItem(
                container_no=a.container_no,
                slot_code=a.slot_code,
                stack=a.stack,
                tier=a.tier,
            )
        )
    db.add(plan)
    db.flush()
    return plan


def compute_plan(
    db: Session,
    *,
    slots_revision: int | None,
    manifest_revision: int | None,
    parent_id: str | None = None,
) -> models.Plan:
    slot_rev = get_slot_revision(db, slots_revision)
    manifest_rev = get_manifest_revision(db, manifest_revision)
    parent = None
    if parent_id is not None:
        parent = db.get(models.Plan, parent_id)
        if parent is None:
            raise BusinessError(f"方案 {parent_id} 不存在", 404)

    slots, containers, adjacency, incompat = _to_solver_inputs(slot_rev, manifest_rev)
    try:
        result = solve_stowage(
            containers, slots, adjacency, incompat,
            slot_rev.min_moment, slot_rev.max_moment,
        )
    except InfeasibleStowage as exc:
        raise BusinessError(f"无解：{exc.reason}", 422)

    plan = _save_plan(
        db, slots_rev=slot_rev, manifest_rev=manifest_rev,
        result=result, parent=parent,
    )
    db.commit()
    db.refresh(plan)
    return plan


def list_plans(db: Session, root_id: str | None = None) -> list[models.Plan]:
    stmt = select(models.Plan).order_by(
        models.Plan.root_id, models.Plan.revision_no
    )
    if root_id:
        stmt = stmt.where(models.Plan.root_id == root_id)
    return list(db.scalars(stmt))


def get_plan(db: Session, plan_id: str) -> models.Plan:
    plan = db.get(models.Plan, plan_id)
    if plan is None:
        raise BusinessError(f"方案 {plan_id} 不存在", 404)
    return plan


def confirm_plan(
    db: Session,
    plan_id: str,
    payload: PlanConfirmIn,
) -> models.Plan:
    plan = get_plan(db, plan_id)

    if plan.status == "confirmed":
        # 幂等：同一方案以其自身资料修订重复确认，直接返回
        if (
            payload.slots_revision == plan.slots_revision
            and payload.manifest_revision == plan.manifest_revision
        ):
            return plan
        raise ConflictError("方案已确认，不能重复确认（如资料已更新请修订方案后重新确认）")

    # 核对参与方案的箱与槽位资料修订；过期则拒绝，原方案保持草稿不变
    latest_slots = _latest_slot_revision(db)
    latest_manifest = _latest_manifest_revision(db)
    if payload.slots_revision != plan.slots_revision:
        raise ConflictError(
            f"提交的槽位资料修订 {payload.slots_revision} 与方案修订 "
            f"{plan.slots_revision} 不一致，拒绝确认"
        )
    if payload.manifest_revision != plan.manifest_revision:
        raise ConflictError(
            f"提交的箱清单修订 {payload.manifest_revision} 与方案修订 "
            f"{plan.manifest_revision} 不一致，拒绝确认"
        )
    if plan.slots_revision != latest_slots:
        raise ConflictError(
            f"槽位资料已过期：方案基于修订 {plan.slots_revision}，"
            f"当前最新修订 {latest_slots}，原方案保持不变"
        )
    if plan.manifest_revision != latest_manifest:
        raise ConflictError(
            f"箱清单已过期：方案基于修订 {plan.manifest_revision}，"
            f"当前最新修订 {latest_manifest}，原方案保持不变"
        )

    # 读检查完成后快照本方案占用槽位，并释放只读事务；
    # 随后在独立连接上以 IMMEDIATE 立即拿写锁，串行化并发确认；
    # 槽位唯一索引兜底冲突占用。锁内不使用 ORM 会话，避免隐式事务干扰。
    plan_slots = tuple(i.slot_code for i in plan.items)
    db.rollback()

    from app.database import engine

    conn = engine.pool.connect()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute(
            "SELECT status, revision_no, root_id FROM plans WHERE id = ?",
            (plan.id,),
        )
        row = cursor.fetchone()
        if row is None:
            conn.rollback()
            raise ConflictError("方案在确认期间被删除，确认被拒绝")
        status_locked, rev_no_locked, root_locked = row
        if status_locked != "draft":
            conn.rollback()
            raise ConflictError(
                f"方案状态已为 {status_locked}（并发操作），本次确认被拒绝，原方案不变"
            )

        cursor.execute(
            "SELECT id FROM plans WHERE root_id = ? AND status = 'confirmed' "
            "AND revision_no > ? LIMIT 1",
            (root_locked, rev_no_locked),
        )
        if cursor.fetchone() is not None:
            conn.rollback()
            raise ConflictError(
                "同一方案已存在更新的已确认修订，本次确认被拒绝，原方案不变"
            )

        # 同根旧已确认修订释放槽位占用并标记为被取代
        cursor.execute(
            "SELECT id FROM plans WHERE root_id = ? AND status = 'confirmed'",
            (root_locked,),
        )
        old_ids = [r[0] for r in cursor.fetchall()]
        for old_id in old_ids:
            cursor.execute("DELETE FROM confirmed_slots WHERE plan_id = ?", (old_id,))
            cursor.execute(
                "UPDATE plans SET status = 'superseded' WHERE id = ?", (old_id,)
            )
            # 旧修订的装船排程保留可查，但不再列为当前排程
            cursor.execute(
                "UPDATE loading_schedules SET status = 'historical' "
                "WHERE plan_id = ? AND status = 'current'",
                (old_id,),
            )
            # 旧修订的逐港卸箱顺序同样保留可查，但不再列为当前顺序
            cursor.execute(
                "UPDATE unloading_sequences SET status = 'historical' "
                "WHERE plan_id = ? AND status = 'current'",
                (old_id,),
            )

        # 冲突预检：本方案占用槽位是否已被其他方案的确认占用
        conflict_slot = None
        if plan_slots:
            placeholders = ",".join("?" for _ in plan_slots)
            cursor.execute(
                f"SELECT slot_code FROM confirmed_slots WHERE slot_code IN "
                f"({placeholders})",
                plan_slots,
            )
            row_conflict = cursor.fetchone()
            conflict_slot = row_conflict[0] if row_conflict else None
        if conflict_slot is not None:
            conn.rollback()
            raise ConflictError(
                f"并发确认冲突：槽位 {conflict_slot} 已被其他已确认方案占用，"
                "本次确认被拒绝，原方案不变"
            )

        now = models._utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
        cursor.execute(
            "UPDATE plans SET status = 'confirmed', confirmed_at = ? WHERE id = ?",
            (now, plan.id),
        )
        cursor.executemany(
            "INSERT INTO confirmed_slots (plan_id, slot_code) VALUES (?, ?)",
            [(plan.id, i.slot_code) for i in plan.items],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    db.expire_all()
    return get_plan(db, plan_id)


def _latest_slot_revision(db: Session) -> int:
    return db.scalar(select(func.max(models.SlotRevision.revision))) or 0


def _latest_manifest_revision(db: Session) -> int:
    return db.scalar(select(func.max(models.ManifestRevision.revision))) or 0


# ---------- 应急改配 ----------


def create_emergency_restow(
    db: Session,
    plan_id: str,
    payload: RestowCreateIn,
) -> dict:
    """码头临时封锁槽位：基于当前已确认方案计算应急改配草稿。

    方案所用槽位资料与箱清单须均为当前最新修订；封锁编号须属于该槽位修订；
    保持全部箱及其资料、封锁槽位不得占用，并满足全部既有配载硬约束。
    先最小化换槽箱数，再最小化最高占用层，最后按箱号升序取槽位编号序列
    字典序最小。无完整可行改配返回 422 且不保存；求解后保存前若基础方案
    被取代、同根出现更新修订或基础资料变化，返回 409 且不保存。
    """
    plan = get_plan(db, plan_id)
    if plan.status != "confirmed":
        raise ConflictError(
            f"方案当前状态为 {plan.status}，应急改配仅可基于当前已确认方案"
        )

    latest_slots = _latest_slot_revision(db)
    latest_manifest = _latest_manifest_revision(db)
    if plan.slots_revision != latest_slots:
        raise ConflictError(
            f"槽位资料已更新：当前已确认方案基于修订 {plan.slots_revision}，"
            f"最新修订 {latest_slots}，请先按最新资料修订并确认方案后再申请应急改配"
        )
    if plan.manifest_revision != latest_manifest:
        raise ConflictError(
            f"箱清单已更新：当前已确认方案基于修订 {plan.manifest_revision}，"
            f"最新修订 {latest_manifest}，请先按最新资料修订并确认方案后再申请应急改配"
        )

    slot_rev = get_slot_revision(db, latest_slots)
    manifest_rev = get_manifest_revision(db, latest_manifest)

    valid_codes = {s.code for s in slot_rev.slots}
    unknown = sorted({c for c in payload.blocked_slots if c not in valid_codes})
    if unknown:
        raise BusinessError(
            f"封锁槽位编号不属于槽位资料修订 {latest_slots}：{', '.join(unknown)}（无效槽位）",
            422,
        )

    # 改配前后箱清单必须与基础方案完全一致（资料须为最新修订）
    plan_nos = {i.container_no for i in plan.items}
    manifest_nos = {c.no for c in manifest_rev.containers}
    if plan_nos != manifest_nos:
        missing = sorted(plan_nos - manifest_nos)
        extra = sorted(manifest_nos - plan_nos)
        problems = []
        if missing:
            problems.append(f"方案箱在最新清单中缺失：{', '.join(missing)}")
        if extra:
            problems.append(f"最新清单存在方案外箱：{', '.join(extra)}")
        raise BusinessError(
            "当前已确认方案箱项与最新箱清单不一致（" + "；".join(problems) + "）",
            500,
        )

    slots, containers, adjacency, incompat = _to_solver_inputs(slot_rev, manifest_rev)
    original = {i.container_no: i.slot_code for i in plan.items}
    blocked = tuple(sorted(set(payload.blocked_slots)))

    try:
        result = solve_restow(
            containers, slots, adjacency, incompat,
            slot_rev.min_moment, slot_rev.max_moment,
            original=original, blocked=blocked,
        )
    except InfeasibleStowage as exc:
        raise BusinessError(f"无完整可行改配：{exc.reason}", 422)

    # 求解完成后快照结果并结束只读事务；在独立连接的写锁内复核后落库
    parent_id = plan.id
    root_id = plan.root_id
    parent_rev = plan.revision_no
    base_slots_rev = latest_slots
    base_manifest_rev = latest_manifest
    original_items = tuple(
        (i.container_no, i.slot_code, i.stack, i.tier)
        for i in sorted(plan.items, key=lambda x: x.container_no)
    )
    item_rows = tuple(
        (a.container_no, a.slot_code, a.stack, a.tier)
        for a in result.assignments
    )
    total_moment = result.total_moment
    max_tier = result.max_occupied_tier
    db.rollback()

    from app.database import engine

    conn = engine.pool.connect()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")

        # 保存前复核：基础方案仍存在且仍为当前已确认修订
        cursor.execute(
            "SELECT status, slots_revision, manifest_revision, revision_no, root_id "
            "FROM plans WHERE id = ?",
            (parent_id,),
        )
        row = cursor.fetchone()
        if row is None:
            conn.rollback()
            raise ConflictError("基础方案在求解期间被删除，应急改配草稿被拒绝且不保存")
        status_locked, slots_rev_locked, manifest_rev_locked, rev_locked, root_locked = row
        if status_locked != "confirmed":
            conn.rollback()
            raise ConflictError(
                f"保存前复核发现基础方案状态已为 {status_locked}（可能已被新修订取代），"
                "应急改配草稿被拒绝且不保存，原确认方案仍有效"
            )
        if slots_rev_locked != base_slots_rev or manifest_rev_locked != base_manifest_rev:
            conn.rollback()
            raise ConflictError(
                "保存前复核发现基础方案所用资料已变化（槽位资料修订 "
                f"{slots_rev_locked}、箱清单修订 {manifest_rev_locked}），"
                "应急改配草稿被拒绝且不保存，原确认方案仍有效"
            )

        # 同根出现更新修订（新草稿/新确认）-> 拒绝，由调用方基于最新修订重试
        cursor.execute(
            "SELECT id FROM plans WHERE root_id = ? AND revision_no > ? LIMIT 1",
            (root_locked, rev_locked),
        )
        if cursor.fetchone() is not None:
            conn.rollback()
            raise ConflictError(
                f"保存前复核发现同方案链（root={root_locked}）已出现更新修订，"
                "应急改配草稿被拒绝且不保存，原确认方案仍有效"
            )

        # 资料在求解期间是否产生新修订
        cursor.execute("SELECT COALESCE(MAX(revision), 0) FROM slot_revisions")
        if cursor.fetchone()[0] != base_slots_rev:
            conn.rollback()
            raise ConflictError(
                "保存前复核发现槽位资料已产生新修订，应急改配草稿被拒绝且不保存，"
                "原确认方案仍有效"
            )
        cursor.execute("SELECT COALESCE(MAX(revision), 0) FROM manifest_revisions")
        if cursor.fetchone()[0] != base_manifest_rev:
            conn.rollback()
            raise ConflictError(
                "保存前复核发现箱清单已产生新修订，应急改配草稿被拒绝且不保存，"
                "原确认方案仍有效"
            )

        new_id = models._new_id()
        new_revision_no = parent_rev + 1
        now = models._utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
        # 草稿不写 confirmed_slots，不影响原确认方案的槽位占用
        cursor.execute(
            "INSERT INTO plans "
            "(id, revision_no, status, slots_revision, manifest_revision, "
            "total_moment, max_tier, created_at, confirmed_at, parent_id, root_id) "
            "VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, NULL, ?, ?)",
            (new_id, new_revision_no, base_slots_rev, base_manifest_rev,
             total_moment, max_tier, now, parent_id, root_id),
        )
        cursor.executemany(
            "INSERT INTO plan_items (plan_id, container_no, slot_code, stack, tier) "
            "VALUES (?, ?, ?, ?, ?)",
            [(new_id, *r4) for r4 in item_rows],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    db.expire_all()
    new_plan = get_plan(db, new_id)

    orig_by_no = {cno: (scode, stack, tier) for cno, scode, stack, tier in original_items}
    moves = []
    for item in sorted(new_plan.items, key=lambda x: x.container_no):
        from_code, from_stack, from_tier = orig_by_no[item.container_no]
        if from_code != item.slot_code:
            moves.append({
                "container_no": item.container_no,
                "from_slot_code": from_code,
                "to_slot_code": item.slot_code,
                "from_stack": from_stack,
                "to_stack": item.stack,
                "from_tier": from_tier,
                "to_tier": item.tier,
            })

    return {
        "plan": new_plan,
        "blocked_slots": list(blocked),
        "moves": moves,
    }


# ---------- 装船时间表 ----------


def create_loading_schedule(
    db: Session,
    plan_id: str,
    payload: ScheduleCreateIn,
) -> models.LoadingSchedule:
    """为已确认方案修订求解并保存装船时间表。

    箱项缺失或多余、时间窗无解、求解不可行均拒绝且不保存；
    保存前在 IMMEDIATE 写锁内再次核对方案仍为已确认状态。
    """
    plan = get_plan(db, plan_id)
    if plan.status != "confirmed":
        raise ConflictError(
            f"方案当前状态为 {plan.status}，仅已确认方案可生成装船时间表"
        )

    # 箱项必须与方案修订完全一致（不多不少）
    plan_items = {i.container_no: i for i in plan.items}
    submitted = {e.no: e for e in payload.entries}
    missing = sorted(set(plan_items) - set(submitted))
    extra = sorted(set(submitted) - set(plan_items))
    if missing or extra:
        problems = []
        if missing:
            problems.append(f"缺少箱项：{', '.join(missing)}")
        if extra:
            problems.append(f"多余箱项：{', '.join(extra)}")
        raise BusinessError(
            "提交箱项与方案修订不一致（" + "；".join(problems) + "），拒绝排程",
            422,
        )

    jobs = [
        Job(
            no=no,
            stack=plan_items[no].stack,
            stack_tier=plan_items[no].tier,
            duration=e.duration,
            release=e.release_minute,
            deadline=e.deadline_minute,
        )
        for no, e in submitted.items()
    ]
    try:
        result = solve_loading(jobs, payload.crane_count)
    except InfeasibleSchedule as exc:
        raise BusinessError(f"无解：{exc.reason}", 422)

    # 求解完成后快照结果，结束只读事务；在独立连接的写锁内复核方案状态后落库
    snapshot = tuple(
        (
            sc.container_no,
            plan_items[sc.container_no].stack,
            plan_items[sc.container_no].tier,
            sc.start_minute,
            sc.finish_minute,
            sc.crane_no,
        )
        for sc in result.jobs
    )
    crane_count = payload.crane_count
    makespan = result.makespan
    db.rollback()

    from app.database import engine

    conn = engine.pool.connect()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            "SELECT status FROM plans WHERE id = ?", (plan_id,)
        )
        row = cursor.fetchone()
        if row is None:
            conn.rollback()
            raise ConflictError("方案在排程保存期间被删除，排程被拒绝且不保存")
        if row[0] != "confirmed":
            conn.rollback()
            raise ConflictError(
                f"保存前复核发现方案状态已为 {row[0]}（方案可能已被新修订取代），"
                "本次排程被拒绝且不保存"
            )

        # 同一方案修订重新提交：以新排程原子替换旧排程
        cursor.execute(
            "SELECT id FROM loading_schedules WHERE plan_id = ?", (plan_id,)
        )
        old = cursor.fetchone()
        if old is not None:
            cursor.execute(
                "DELETE FROM loading_schedule_items WHERE schedule_id = ?",
                (old[0],),
            )
            cursor.execute("DELETE FROM loading_schedules WHERE id = ?", (old[0],))

        now = models._utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
        cursor.execute(
            "INSERT INTO loading_schedules "
            "(plan_id, crane_count, makespan, status, created_at) "
            "VALUES (?, ?, ?, 'current', ?)",
            (plan_id, crane_count, makespan, now),
        )
        schedule_id = cursor.lastrowid
        cursor.executemany(
            "INSERT INTO loading_schedule_items "
            "(schedule_id, container_no, stack, tier, start_minute, "
            "finish_minute, crane_no) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(schedule_id, *row7) for row7 in snapshot],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    db.expire_all()
    return get_loading_schedule(db, plan_id)


def list_loading_schedules(
    db: Session, include_historical: bool = False
) -> list[models.LoadingSchedule]:
    stmt = select(models.LoadingSchedule)
    if not include_historical:
        stmt = stmt.where(models.LoadingSchedule.status == "current")
    stmt = stmt.order_by(models.LoadingSchedule.id)
    return list(db.scalars(stmt))


def get_loading_schedule(db: Session, plan_id: str) -> models.LoadingSchedule:
    schedule = db.scalar(
        select(models.LoadingSchedule).where(
            models.LoadingSchedule.plan_id == plan_id
        )
    )
    if schedule is None:
        raise BusinessError(f"方案 {plan_id} 不存在装船时间表", 404)
    return schedule


# ---------- 逐港卸箱顺序 ----------


def _placed_containers(db: Session, plan: models.Plan):
    """按方案修订所用箱重（箱清单修订）与槽位力臂（槽位资料修订）组装箱快照。"""
    manifest_rev = get_manifest_revision(db, plan.manifest_revision)
    slot_rev = get_slot_revision(db, plan.slots_revision)
    container_by_no = {c.no: c for c in manifest_rev.containers}
    slot_by_code = {s.code: s for s in slot_rev.slots}

    placed = []
    for item in plan.items:
        crow = container_by_no.get(item.container_no)
        if crow is None:
            raise BusinessError(
                f"方案箱 {item.container_no} 在箱清单修订 "
                f"{plan.manifest_revision} 中缺失，数据不一致", 500
            )
        srow = slot_by_code.get(item.slot_code)
        if srow is None:
            raise BusinessError(
                f"方案槽位 {item.slot_code} 在槽位资料修订 "
                f"{plan.slots_revision} 中缺失，数据不一致", 500
            )
        placed.append(
            PlacedContainer(
                no=item.container_no,
                weight=crow.weight,
                arm=srow.arm,
                stack=item.stack,
                tier=item.tier,
                discharge_order=crow.discharge_order,
            )
        )
    return placed


def create_unloading_sequence(
    db: Session,
    plan_id: str,
    payload: UnloadingCreateIn,
) -> models.UnloadingSequence:
    """为已确认方案修订求解并保存逐港卸箱顺序。

    卸港序号与方案箱清单不一致、区间倒置、无可行顺序、求解无法确定均拒绝且不保存；
    保存前在 IMMEDIATE 写锁内再次核对方案仍为已确认状态。
    """
    plan = get_plan(db, plan_id)
    if plan.status != "confirmed":
        raise ConflictError(
            f"方案当前状态为 {plan.status}，仅已确认方案可规划逐港卸箱顺序"
        )

    boxes = _placed_containers(db, plan)
    slot_by_item = {i.container_no: i.slot_code for i in plan.items}

    windows = [
        PortWindow(
            discharge_order=p.discharge_order,
            min_remaining_moment=p.min_remaining_moment,
            max_remaining_moment=p.max_remaining_moment,
        )
        for p in payload.ports
    ]

    try:
        result = solve_unloading(boxes, windows)
    except InfeasibleUnloading as exc:
        # 入参一致性问题、无可行顺序、求解无法确定均归 422 且不保存
        raise BusinessError(f"无可行卸箱顺序：{exc.reason}", 422)

    # 求解完成后快照结果，结束只读事务；在独立连接的写锁内复核方案状态后落库
    step_rows = tuple(
        (
            st.step_no,
            st.discharge_order,
            st.container_no,
            slot_by_item[st.container_no],
            st.stack,
            st.tier,
            st.weight,
            st.arm,
            st.remaining_moment,
        )
        for st in result.steps
    )
    port_rows = tuple(
        sorted(
            ((w.discharge_order, w.min_remaining_moment, w.max_remaining_moment)
             for w in windows),
            key=lambda r: r[0],
        )
    )
    initial_moment = result.initial_moment
    final_moment = result.final_moment
    db.rollback()

    from app.database import engine

    conn = engine.pool.connect()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT status FROM plans WHERE id = ?", (plan_id,))
        row = cursor.fetchone()
        if row is None:
            conn.rollback()
            raise ConflictError("方案在卸箱顺序保存期间被删除，顺序被拒绝且不保存")
        if row[0] != "confirmed":
            conn.rollback()
            raise ConflictError(
                f"保存前复核发现方案状态已为 {row[0]}（方案可能已被新修订取代），"
                "本次卸箱顺序被拒绝且不保存"
            )

        # 同一方案修订重新提交：以新顺序原子替换旧顺序
        cursor.execute(
            "SELECT id FROM unloading_sequences WHERE plan_id = ?", (plan_id,)
        )
        old = cursor.fetchone()
        if old is not None:
            cursor.execute(
                "DELETE FROM unloading_steps WHERE sequence_id = ?", (old[0],)
            )
            cursor.execute(
                "DELETE FROM unloading_ports WHERE sequence_id = ?", (old[0],)
            )
            cursor.execute(
                "DELETE FROM unloading_sequences WHERE id = ?", (old[0],)
            )

        now = models._utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
        cursor.execute(
            "INSERT INTO unloading_sequences "
            "(plan_id, initial_moment, final_moment, status, created_at) "
            "VALUES (?, ?, ?, 'current', ?)",
            (plan_id, initial_moment, final_moment, now),
        )
        sequence_id = cursor.lastrowid
        cursor.executemany(
            "INSERT INTO unloading_ports "
            "(sequence_id, discharge_order, min_remaining_moment, "
            "max_remaining_moment) VALUES (?, ?, ?, ?)",
            [(sequence_id, *row3) for row3 in port_rows],
        )
        cursor.executemany(
            "INSERT INTO unloading_steps "
            "(sequence_id, step_no, discharge_order, container_no, slot_code, "
            "stack, tier, weight, arm, remaining_moment) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(sequence_id, *row9) for row9 in step_rows],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    db.expire_all()
    return get_unloading_sequence(db, plan_id)


def list_unloading_sequences(
    db: Session, include_historical: bool = False
) -> list[models.UnloadingSequence]:
    stmt = select(models.UnloadingSequence)
    if not include_historical:
        stmt = stmt.where(models.UnloadingSequence.status == "current")
    stmt = stmt.order_by(models.UnloadingSequence.id)
    return list(db.scalars(stmt))


def get_unloading_sequence(db: Session, plan_id: str) -> models.UnloadingSequence:
    sequence = db.scalar(
        select(models.UnloadingSequence).where(
            models.UnloadingSequence.plan_id == plan_id
        )
    )
    if sequence is None:
        raise BusinessError(f"方案 {plan_id} 不存在卸箱顺序", 404)
    return sequence


# ---------- 逐港卸箱执行（现场逐步登记） ----------


def _exec_content_hash(operation_no: str, container_no: str, expected_version: int) -> str:
    """登记操作内容指纹：操作号 + 箱号 + 期望进度版本的规范 JSON 的 SHA-256。"""
    payload = json.dumps(
        {
            "operation_no": operation_no,
            "container_no": container_no,
            "expected_version": expected_version,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _remaining_recommendation(remaining, windows_by_order):
    """从当前剩余箱状态求可行的剩余推荐顺序（全程箱号字典序最小）。

    remaining 为箱元组 (no, slot, stack, tier, weight, arm, order)；
    初始总力矩按剩余箱重×力臂求和（等于已卸前缀后的当前剩余总力矩）。
    返回 (recommendation_steps, infeasible_reason_or_None)。
    """
    if not remaining:
        return [], None
    boxes = [
        PlacedContainer(
            no=r[0], weight=r[4], arm=r[5], stack=r[2], tier=r[3],
            discharge_order=r[6],
        )
        for r in remaining
    ]
    remaining_orders = sorted({r[6] for r in remaining})
    windows = [windows_by_order[d] for d in remaining_orders]
    initial_moment = sum(r[4] * r[5] for r in remaining)
    try:
        result = solve_unloading(boxes, windows)
    except InfeasibleUnloading as exc:
        return None, exc.reason
    rec = [
        {
            "discharge_order": st.discharge_order,
            "container_no": st.container_no,
            "slot_code": next(
                r[1] for r in remaining if r[0] == st.container_no
            ),
            "stack": st.stack,
            "tier": st.tier,
            "weight": st.weight,
            "arm": st.arm,
            "remaining_moment": st.remaining_moment,
        }
        for st in result.steps
    ]
    return rec, None


def _execution_view_from_rows(
    plan_id: str,
    revision_no: int,
    plan_status: str,
    *,
    frozen,
    boxes,
    ports,
    steps,
    remaining_moment,
    progress_version,
    frozen_at,
    planned_steps,
    precomputed_recommendation=None,
):
    """组装逐港卸箱执行进度视图（GET / 登记成功响应共用）。

    * frozen=True：由首次登记冻结的 boxes/ports/steps 组装（重排/取代均不影响）；
    * frozen=False：首次登记前，直接取当前卸箱顺序（planned_steps 含全步快照），
      此时 progress_version=0、无已卸步骤。
    每个 planned/exec step 为 9 元组：
      (step_no, order, no, slot, stack, tier, weight, arm, remaining_moment[, at])
    box 元组：(no, slot, stack, tier, weight, arm, order)
    port 元组：(order, min, max)
    """
    windows_by_order = {
        p[0]: PortWindow(
            discharge_order=p[0],
            min_remaining_moment=p[1],
            max_remaining_moment=p[2],
        )
        for p in ports
    }

    if frozen:
        unloaded_nos = {s[2] for s in steps}
        remaining = tuple(b for b in boxes if b[0] not in unloaded_nos)
        step_views = [
            {
                "step_no": s[0],
                "discharge_order": s[1],
                "container_no": s[2],
                "slot_code": s[3],
                "stack": s[4],
                "tier": s[5],
                "weight": s[6],
                "arm": s[7],
                "remaining_moment": s[8],
                "unloaded_at": utc_iso(s[9]) if len(s) > 9 else None,
            }
            for s in sorted(steps, key=lambda x: x[0])
        ]
    else:
        # 首次登记前：无已卸步骤，全部箱均剩余
        unloaded_nos = set()
        remaining = tuple(boxes)
        remaining_moment = sum(b[4] * b[5] for b in boxes)
        progress_version = 0
        step_views = []

    current_port = min((b[6] for b in remaining), default=None)

    # 原计划下一步：按冻结的原计划箱号顺序，取第一个尚未卸下的箱
    # （现场可偏离原计划，故不能简单地以“已卸数量+1”定位）
    planned_next = None
    for s in planned_steps:
        if frozen and s[2] in unloaded_nos:
            continue
        planned_next = s[2]
        break

    if precomputed_recommendation is not None:
        # 首次登记前：推荐顺序即已求解并保存的原计划，直接复用，不再求解
        recommendation = precomputed_recommendation
        blocked_reason = None
    else:
        recommendation, blocked_reason = _remaining_recommendation(
            remaining, windows_by_order
        )
    completed = len(remaining) == 0
    read_only = plan_status == "superseded" or completed

    return {
        "plan_id": plan_id,
        "revision_no": revision_no,
        "status": "historical" if plan_status == "superseded" else "current",
        "read_only": read_only,
        "completed": completed,
        "progress_version": progress_version,
        "initial_moment": round(sum(b[4] * b[5] for b in boxes), 6),
        "remaining_moment": round(remaining_moment, 6),
        "current_port": current_port,
        "planned_next_container": planned_next,
        "frozen": frozen,
        "frozen_at": utc_iso(frozen_at) if frozen_at is not None else None,
        "steps": step_views,
        "recommendation": recommendation if recommendation is not None else [],
        # 仅供服务内部在“被阻碍”场景下读取原因，不随响应输出
        "_blocked_reason": blocked_reason,
    }


def get_unloading_execution(db: Session, plan_id: str) -> dict:
    """查询逐港卸箱执行进度：已卸步骤、当前港、剩余力矩、进度版本与推荐顺序。

    首次登记前且存在当前卸箱顺序：返回 version=0 的未冻结预览（原计划推荐）；
    无卸箱顺序（或方案不存在）：404。首次登记后一律以冻结快照核算，方案被
    取代仍可查询（status=historical、read_only=true）。
    """
    plan = get_plan(db, plan_id)
    execution = db.scalar(
        select(models.UnloadingExecution).where(
            models.UnloadingExecution.plan_id == plan_id
        )
    )

    if execution is None:
        sequence = db.scalar(
            select(models.UnloadingSequence).where(
                models.UnloadingSequence.plan_id == plan_id
            )
        )
        if sequence is None:
            raise BusinessError(f"方案 {plan_id} 不存在卸箱顺序，无执行进度", 404)
        boxes = tuple(
            (
                st.container_no,
                st.slot_code,
                st.stack,
                st.tier,
                st.weight,
                st.arm,
                st.discharge_order,
            )
            for st in sequence.steps
        )
        ports = tuple(
            (p.discharge_order, p.min_remaining_moment, p.max_remaining_moment)
            for p in sequence.ports
        )
        planned_steps = tuple(
            (
                st.step_no,
                st.discharge_order,
                st.container_no,
                st.slot_code,
                st.stack,
                st.tier,
                st.weight,
                st.arm,
                st.remaining_moment,
            )
            for st in sorted(sequence.steps, key=lambda x: x.step_no)
        )
        precomputed = [
            {
                "discharge_order": s[1],
                "container_no": s[2],
                "slot_code": s[3],
                "stack": s[4],
                "tier": s[5],
                "weight": s[6],
                "arm": s[7],
                "remaining_moment": s[8],
            }
            for s in planned_steps
        ]
        return _execution_view_from_rows(
            plan_id,
            plan.revision_no,
            plan.status,
            frozen=False,
            boxes=boxes,
            ports=ports,
            steps=(),
            remaining_moment=sequence.initial_moment,
            progress_version=0,
            frozen_at=None,
            planned_steps=planned_steps,
            precomputed_recommendation=precomputed,
        )

    boxes, ports, steps, planned_steps = _frozen_state_rows(execution)
    return _execution_view_from_rows(
        plan.id,
        plan.revision_no,
        plan.status,
        frozen=True,
        boxes=boxes,
        ports=ports,
        steps=steps,
        remaining_moment=execution.remaining_moment,
        progress_version=execution.progress_version,
        frozen_at=execution.frozen_at,
        planned_steps=planned_steps,
    )


def _frozen_state_rows(execution):
    """从冻结快照读取 (boxes, ports, steps, planned_steps) 元组集合。

    元组结构见 _execution_view_from_rows；planned_steps 的剩余力矩列对原计划
    无对照意义，置 0。
    """
    boxes = tuple(
        (
            b.container_no,
            b.slot_code,
            b.stack,
            b.tier,
            b.weight,
            b.arm,
            b.discharge_order,
        )
        for b in sorted(execution.boxes, key=lambda x: x.container_no)
    )
    ports = tuple(
        (p.discharge_order, p.min_remaining_moment, p.max_remaining_moment)
        for p in sorted(execution.ports, key=lambda x: x.discharge_order)
    )
    steps = tuple(
        (
            s.step_no,
            s.discharge_order,
            s.container_no,
            s.slot_code,
            s.stack,
            s.tier,
            s.weight,
            s.arm,
            s.remaining_moment,
            s.unloaded_at,
        )
        for s in sorted(execution.steps, key=lambda x: x.step_no)
    )
    box_by_no = {b[0]: b for b in boxes}
    planned_steps = tuple(
        (
            idx,
            box_by_no[no][6],
            no,
            box_by_no[no][1],
            box_by_no[no][2],
            box_by_no[no][3],
            box_by_no[no][4],
            box_by_no[no][5],
            0.0,
        )
        for idx, no in enumerate(json.loads(execution.frozen_plan_order), start=1)
    )
    return boxes, ports, steps, planned_steps


def execute_unloading_step(
    db: Session,
    plan_id: str,
    payload,
) -> tuple[dict, bool]:
    """现场登记卸下一箱，返回 (执行进度视图, 是否为同操作号重试)。

    每次提交方案号（路径）、箱号、全局唯一操作号与期望进度版本。当前港取
    剩余箱中最小卸港序号；所选箱须属于该港且位于同堆栈最上层；卸后剩余
    力矩须落入该港原定闭区间；且只有从新状态仍能按港序、堆栈先后和各港
    力矩区间卸完全部剩余箱时才登记（否则说明阻碍且进度不变，422）。
    首次登记冻结当时卸箱计划及所用箱重、力臂和港区间；此后重排原计划不改
    已卸记录与后续核算。方案被新修订取代后只读（409），历史进度仍可查。
    同操作号同内容重试优先返回首次结果（200），异内容冲突（409）。
    """
    plan = get_plan(db, plan_id)
    container_no = payload.container_no
    operation_no = payload.operation_no
    expected_version = payload.expected_version
    content_hash = _exec_content_hash(operation_no, container_no, expected_version)
    db.rollback()

    from app.database import engine

    conn = engine.pool.connect()
    cursor = conn.cursor()
    idempotent_hit = False
    snapshot = None
    try:
        # 可行性复核可能调用 MILP（最长 TIME_LIMIT 秒）且发生在写锁内，
        # 放宽该连接的锁等待，避免并发登记立刻 SQLITE_BUSY
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.execute("BEGIN IMMEDIATE")

        # 锁内复核方案状态：方案被新修订取代后执行进度只读
        cursor.execute("SELECT status, revision_no FROM plans WHERE id = ?", (plan_id,))
        prow = cursor.fetchone()
        if prow is None:
            conn.rollback()
            raise BusinessError(f"方案 {plan_id} 不存在", 404)
        plan_status_locked, revision_no_locked = prow
        if plan_status_locked == "draft":
            conn.rollback()
            raise ConflictError("方案尚未确认（draft），尚无卸箱计划，拒绝登记卸箱")
        read_only_plan = plan_status_locked == "superseded"

        # 同操作号全局唯一：同内容重试即使方案已取代/已全部卸完也返回首次结果；
        # 异内容（含复用至另一方案）一律冲突。
        cursor.execute(
            "SELECT execution_id, content_hash, response_snapshot "
            "FROM unloading_exec_operations WHERE operation_no = ?",
            (operation_no,),
        )
        op = cursor.fetchone()
        if op is not None:
            op_exec_id, op_hash, op_snapshot = op
            if op_hash != content_hash:
                conn.rollback()
                raise ConflictError(
                    f"操作号 {operation_no} 已用于不同内容的卸箱登记，"
                    "同操作号异内容冲突，本次登记被拒绝且进度不变"
                )
            if op_exec_id is not None:
                cursor.execute(
                    "SELECT plan_id FROM unloading_executions WHERE id = ?",
                    (op_exec_id,),
                )
                erow = cursor.fetchone()
                if erow is None or erow[0] != plan_id:
                    conn.rollback()
                    raise ConflictError(
                        f"操作号 {operation_no} 已用于另一方案的卸箱登记，"
                        "同操作号异内容冲突，本次登记被拒绝且进度不变"
                    )
            idempotent_hit = True
            conn.commit()
            snapshot = json.loads(op_snapshot)
        else:
            snapshot = _register_unloading_step(
                cursor,
                plan_id=plan_id,
                revision_no=revision_no_locked,
                plan_superseded=read_only_plan,
                container_no=container_no,
                operation_no=operation_no,
                expected_version=expected_version,
                content_hash=content_hash,
            )
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    db.expire_all()
    return snapshot, idempotent_hit


def _register_unloading_step(
    cursor,
    *,
    plan_id,
    revision_no,
    plan_superseded,
    container_no,
    operation_no,
    expected_version,
    content_hash,
):
    """写锁内完成单次卸箱登记的全部核对与写入；任何拒绝均在调用方回滚。"""
    if plan_superseded:
        raise ConflictError(
            "方案修订已被新修订取代，其卸箱执行进度已冻结为只读，"
            "不再接受登记；历史进度仍可查询"
        )

    cursor.execute(
        "SELECT id, remaining_moment, progress_version "
        "FROM unloading_executions WHERE plan_id = ?",
        (plan_id,),
    )
    head = cursor.fetchone()

    if head is None:
        # 首次登记：必须存在当前卸箱计划，无则拒绝；并冻结计划/箱重/力臂/港区间
        cursor.execute(
            "SELECT id, initial_moment FROM unloading_sequences "
            "WHERE plan_id = ? AND status = 'current'",
            (plan_id,),
        )
        seq_row = cursor.fetchone()
        if seq_row is None:
            raise ConflictError(
                f"方案 {plan_id} 没有当前卸箱计划，须先生成逐港卸箱顺序，"
                "方可登记现场卸箱"
            )
        if expected_version != 0:
            raise ConflictError(
                f"尚无执行进度（首次进度版本为 0），提交的期望进度版本 "
                f"{expected_version} 已过期，本次登记被拒绝且进度不变"
            )
        sequence_id, initial_moment = seq_row
        plan_steps_present = _sequence_step_tuples(cursor, sequence_id)
        if not plan_steps_present:
            # 空箱方案：无箱可卸，执行进度天然完结，不创建冻结头
            raise ConflictError("该卸箱计划为空箱方案（无箱可卸），不接受卸箱登记")
        plan_order = [s[2] for s in plan_steps_present]
        now = models._utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
        cursor.execute(
            "INSERT INTO unloading_executions "
            "(plan_id, frozen_sequence_id, frozen_plan_order, initial_moment, "
            "remaining_moment, progress_version, frozen_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            (
                plan_id, sequence_id,
                json.dumps(plan_order, ensure_ascii=False),
                initial_moment, initial_moment, now, now,
            ),
        )
        exec_id = cursor.lastrowid
        # 冻结当时计划所用箱重、槽位力臂、堆栈/层与卸港序号
        cursor.execute(
            "INSERT INTO unloading_frozen_boxes "
            "(execution_id, container_no, slot_code, stack, tier, weight, "
            "arm, discharge_order) "
            "SELECT ?, container_no, slot_code, stack, tier, weight, arm, "
            "discharge_order FROM unloading_steps WHERE sequence_id = ?",
            (exec_id, sequence_id),
        )
        # 冻结各卸港原定剩余力矩闭区间
        cursor.execute(
            "INSERT INTO unloading_frozen_ports "
            "(execution_id, discharge_order, min_remaining_moment, "
            "max_remaining_moment) "
            "SELECT ?, discharge_order, min_remaining_moment, "
            "max_remaining_moment FROM unloading_ports WHERE sequence_id = ?",
            (exec_id, sequence_id),
        )
        remaining_moment = initial_moment
        progress_version = 0
    else:
        exec_id, remaining_moment, progress_version = head
        if expected_version != progress_version:
            raise ConflictError(
                f"期望进度版本 {expected_version} 与当前进度版本 "
                f"{progress_version} 不一致（版本过期或并发登记已推进），"
                "本次登记被拒绝且进度不变"
            )

    boxes_all = _exec_box_tuples(cursor, exec_id)
    ports_all = _exec_port_tuples(cursor, exec_id)
    steps_all = _exec_step_tuples(cursor, exec_id)
    box_by_no = {b[0]: b for b in boxes_all}
    window_by_order = {p[0]: p for p in ports_all}
    unloaded_nos = {s[2] for s in steps_all}
    remaining_boxes = [b for b in boxes_all if b[0] not in unloaded_nos]

    if not remaining_boxes:
        raise ConflictError("全部箱已卸完，执行进度已完结，不再接受登记")

    # 非法箱项（422，进度不变）
    chosen = box_by_no.get(container_no)
    if chosen is None:
        raise BusinessError(
            f"箱 {container_no} 不在冻结卸箱计划中（非法箱项），拒绝登记", 422
        )
    if chosen[0] in unloaded_nos:
        raise BusinessError(
            f"箱 {container_no} 已经卸下，不得重复卸箱，拒绝登记", 422
        )
    current_port = min(b[6] for b in remaining_boxes)
    if chosen[6] != current_port:
        port_boxes = ", ".join(
            sorted(b[0] for b in remaining_boxes if b[6] == current_port)
        )
        raise BusinessError(
            f"当前港为卸港序号 {current_port}，所选箱 {container_no} 属于卸港 "
            f"{chosen[6]}，不属当前港，拒绝登记；当前港剩余箱：{port_boxes}",
            422,
        )
    # 同堆栈最上层：剩余箱中同栈最大 tier 即栈顶（已卸箱不参与）
    stack_top = max(
        (b for b in remaining_boxes if b[2] == chosen[2]), key=lambda b: b[3]
    )
    if chosen[0] != stack_top[0]:
        raise BusinessError(
            f"箱 {container_no} 不是堆栈 {chosen[2]} 的最上层剩余箱"
            f"（栈顶为 {stack_top[0]}），上层箱未卸不得取下下层箱，拒绝登记",
            422,
        )

    # 卸后剩余总力矩须落入该港原定闭区间（容差随量级缩放，与求解器一致）
    new_remaining_moment = round(remaining_moment - chosen[4] * chosen[5], 6)
    wmin, wmax = window_by_order[current_port][1], window_by_order[current_port][2]
    scale = max(1.0, abs(remaining_moment), abs(wmin), abs(wmax))
    if not (wmin - UNLOAD_EPS * scale <= new_remaining_moment
            <= wmax + UNLOAD_EPS * scale):
        raise BusinessError(
            f"卸下 {container_no} 后剩余总力矩 {new_remaining_moment} 不在当前港"
            f"（卸港序号 {current_port}）原定允许区间 [{wmin}, {wmax}] 内，"
            "拒绝登记",
            422,
        )

    # 可行性：从新状态仍须能按港序、堆栈先后与各港力矩区间卸完全部剩余箱
    next_remaining = tuple(b for b in remaining_boxes if b[0] != container_no)
    next_windows = {
        d: PortWindow(
            discharge_order=d,
            min_remaining_moment=p[1],
            max_remaining_moment=p[2],
        )
        for d, p in window_by_order.items()
    }
    recommendation, blocked_reason = _remaining_recommendation(
        next_remaining, next_windows
    )
    if recommendation is None:
        raise BlockedError(
            f"卸下 {container_no} 虽合法，但从新状态无法再按卸港顺序、堆栈"
            "先后与各港剩余力矩区间卸完全部剩余箱，本次登记被拒绝且进度不变；"
            f"阻碍原因：{blocked_reason}",
            extra={
                "container_no": container_no,
                "current_port": current_port,
                "remaining_moment_after": new_remaining_moment,
                "blocked_reason": blocked_reason,
            },
        )

    # 登记本步并推进版本（原子提交；任何异常由调用方整批回滚，不留部分进度）
    new_step_no = len(steps_all) + 1
    ts = models._utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
    cursor.execute(
        "INSERT INTO unloading_exec_steps "
        "(execution_id, step_no, discharge_order, container_no, slot_code, "
        "stack, tier, weight, arm, remaining_moment, unloaded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            exec_id, new_step_no, current_port, chosen[0], chosen[1], chosen[2],
            chosen[3], chosen[4], chosen[5], new_remaining_moment, ts,
        ),
    )
    cursor.execute(
        "UPDATE unloading_executions SET remaining_moment = ?, "
        "progress_version = ? WHERE id = ?",
        (new_remaining_moment, progress_version + 1, exec_id),
    )

    # 同一锁内组装首次结果快照并登记操作台账
    snapshot = _locked_execution_view(
        cursor, exec_id, plan_id, revision_no, plan_status="confirmed"
    )
    cursor.execute(
        "INSERT INTO unloading_exec_operations "
        "(execution_id, operation_no, container_no, expected_version, "
        "content_hash, response_snapshot, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            exec_id, operation_no, container_no, expected_version,
            content_hash, json.dumps(snapshot, ensure_ascii=False), ts,
        ),
    )
    return snapshot


def _sequence_step_tuples(cursor, sequence_id):
    cursor.execute(
        "SELECT step_no, discharge_order, container_no, slot_code, stack, tier, "
        "weight, arm, remaining_moment FROM unloading_steps "
        "WHERE sequence_id = ? ORDER BY step_no",
        (sequence_id,),
    )
    return [tuple(r) for r in cursor.fetchall()]


def _exec_box_tuples(cursor, exec_id):
    cursor.execute(
        "SELECT container_no, slot_code, stack, tier, weight, arm, discharge_order "
        "FROM unloading_frozen_boxes WHERE execution_id = ? ORDER BY container_no",
        (exec_id,),
    )
    return [tuple(r) for r in cursor.fetchall()]


def _exec_port_tuples(cursor, exec_id):
    cursor.execute(
        "SELECT discharge_order, min_remaining_moment, max_remaining_moment "
        "FROM unloading_frozen_ports WHERE execution_id = ? ORDER BY discharge_order",
        (exec_id,),
    )
    return [tuple(r) for r in cursor.fetchall()]


def _exec_step_tuples(cursor, exec_id):
    cursor.execute(
        "SELECT step_no, discharge_order, container_no, slot_code, stack, tier, "
        "weight, arm, remaining_moment, unloaded_at FROM unloading_exec_steps "
        "WHERE execution_id = ? ORDER BY step_no",
        (exec_id,),
    )
    return [
        (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], _raw_dt(r[9]))
        for r in cursor.fetchall()
    ]


def _locked_execution_view(cursor, exec_id, plan_id, revision_no, plan_status):
    """写锁内组装执行进度视图（与 GET 视图同构），用于操作成功快照。"""
    cursor.execute(
        "SELECT remaining_moment, progress_version, frozen_at, frozen_plan_order "
        "FROM unloading_executions WHERE id = ?",
        (exec_id,),
    )
    rem, ver, frozen_at, plan_order_json = cursor.fetchone()
    frozen_at = _raw_dt(frozen_at)
    boxes = tuple(_exec_box_tuples(cursor, exec_id))
    ports = tuple(_exec_port_tuples(cursor, exec_id))
    steps = tuple(_exec_step_tuples(cursor, exec_id))
    box_by_no = {b[0]: b for b in boxes}
    planned_steps = tuple(
        (
            idx,
            box_by_no[no][6],
            no,
            box_by_no[no][1],
            box_by_no[no][2],
            box_by_no[no][3],
            box_by_no[no][4],
            box_by_no[no][5],
            0.0,
        )
        for idx, no in enumerate(json.loads(plan_order_json), start=1)
    )
    view = _execution_view_from_rows(
        plan_id,
        revision_no,
        plan_status,
        frozen=True,
        boxes=boxes,
        ports=ports,
        steps=steps,
        remaining_moment=rem,
        progress_version=ver,
        frozen_at=frozen_at,
        planned_steps=planned_steps,
    )
    view.pop("_blocked_reason", None)
    return view





# ---------- 装船实绩登记（事件重放） ----------


def _load_actuals_view(db: Session, plan_id: str) -> models.LoadingActuals:
    actuals = db.scalar(
        select(models.LoadingActuals).where(models.LoadingActuals.plan_id == plan_id)
    )
    if actuals is None:
        raise BusinessError(f"方案 {plan_id} 尚无装船实绩登记", 404)
    return actuals


def register_actual_events(
    db: Session,
    plan_id: str,
    payload: ActualEventRegisterIn,
) -> tuple[models.LoadingActuals, bool]:
    """登记逐箱实际开工/完工事件，返回 (实缋头, 是否为纯重试幂等命中)。

    首次登记时在写锁内原子冻结当时排程作为偏差基线；后续排程替换不影响已登记
    实绩与基线。方案被新修订取代后实绩只读（409）。事件允许晚到：合并全部已
    登记事件按实际分钟重放（同分钟完工先于开工）；任一事件使重放链非法则整次
    拒绝、记录不变（422）。同方案内流水号同内容重试返回原结果，异内容冲突（409）。
    """
    plan = get_plan(db, plan_id)
    plan_nos = {i.container_no for i in plan.items}

    # 锁前快速预检（锁内还会再次核对）：箱必须属于方案
    for e in payload.events:
        if e.container_no not in plan_nos:
            raise BusinessError(
                f"箱 {e.container_no} 不在方案 {plan_id} 中，拒绝登记实绩",
                422,
            )

    incoming = tuple(
        (e.seq, e.event_type, e.container_no, e.crane_no, e.minute)
        for e in payload.events
    )
    db.rollback()

    from app.database import engine

    conn = engine.pool.connect()
    cursor = conn.cursor()
    idempotent_hit = False
    try:
        cursor.execute("BEGIN IMMEDIATE")

        # 锁内复核方案状态：方案被新修订取代后实绩只读
        cursor.execute(
            "SELECT status FROM plans WHERE id = ?", (plan_id,)
        )
        row = cursor.fetchone()
        if row is None:
            conn.rollback()
            raise BusinessError(f"方案 {plan_id} 不存在", 404)
        plan_status_locked = row[0]
        if plan_status_locked == "draft":
            conn.rollback()
            raise ConflictError(
                "方案尚未确认（draft），无已确认装船排程，拒绝登记装船实绩"
            )
        if plan_status_locked == "superseded":
            conn.rollback()
            raise ConflictError(
                "方案修订已被新修订取代，其装船实绩已冻结为只读，不再接受登记"
            )

        cursor.execute(
            "SELECT id, baseline_crane_count FROM loading_actuals WHERE plan_id = ?",
            (plan_id,),
        )
        head = cursor.fetchone()

        if head is None:
            # 首次登记：原子绑定并冻结当时排程作为偏差基线
            cursor.execute(
                "SELECT id, crane_count FROM loading_schedules "
                "WHERE plan_id = ? AND status = 'current'",
                (plan_id,),
            )
            sch = cursor.fetchone()
            if sch is None:
                conn.rollback()
                raise ConflictError(
                    f"方案 {plan_id} 没有当前装船排程，须先生成并确认装船时间表，"
                    "方可登记装船实绩"
                )
            schedule_id, crane_count = sch
            now = models._utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
            cursor.execute(
                "INSERT INTO loading_actuals "
                "(plan_id, baseline_crane_count, baseline_schedule_id, "
                "baseline_frozen_at, version, created_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (plan_id, crane_count, schedule_id, now, now),
            )
            actuals_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO loading_actual_baseline_items "
                "(actuals_id, container_no, stack, tier, start_minute, "
                "finish_minute, crane_no) "
                "SELECT ?, container_no, stack, tier, start_minute, finish_minute, "
                "crane_no FROM loading_schedule_items "
                "WHERE schedule_id = ?",
                (actuals_id, schedule_id),
            )
        else:
            actuals_id, crane_count = head

        # 已登记事件（seq -> 内容），用于幂等与冲突判定。
        # 原登记接口始终按**首次登记内容**识别同流水号重试与冲突：事件类型与箱号
        # 不可更正（现行值即原始值），分钟/岸桥取 orig_* 原始列，不受更正影响。
        cursor.execute(
            "SELECT seq, event_type, container_no, orig_crane_no, orig_minute, id, "
            "crane_no, minute "
            "FROM loading_actual_events WHERE actuals_id = ? ORDER BY id",
            (actuals_id,),
        )
        existing_rows = cursor.fetchall()
        existing_by_seq = {r[0]: r for r in existing_rows}

        new_rows: list[tuple] = []
        for seq, etype, cno, crane_no, minute in incoming:
            old = existing_by_seq.get(seq)
            if old is not None:
                # r[1..4] = 首次登记的原始 类型/箱/桥/分钟
                if (old[1], old[2], old[3], old[4]) == (etype, cno, crane_no, minute):
                    continue  # 同流水号同原始内容：重试，原结果幂等返回（不改写更正）
                conn.rollback()
                raise ConflictError(
                    f"事件流水号 {seq} 已登记且内容不同（首次登记："
                    f"{old[1]} 箱 {old[2]} 桥 {old[3]} 第 {old[4]} 分钟），"
                    "异内容冲突，本次登记被拒绝且记录不变；如需修正分钟或岸桥，"
                    "请调用实绩更正接口"
                )
            if crane_no > crane_count:
                conn.rollback()
                raise BusinessError(
                    f"事件流水号 {seq}：岸桥编号 {crane_no} 超出冻结排程岸桥数量 "
                    f"{crane_count}（合法编号 1..{crane_count}），拒绝登记",
                    422,
                )
            new_rows.append((seq, etype, cno, crane_no, minute))

        if not new_rows:
            # 全部为同内容重试：不写入，直接返回原结果
            idempotent_hit = True
            conn.commit()
        else:
            # 合并旧事件与新事件，按实际分钟重放（同分钟完工先于开工）。
            # 重放使用各事件**当前生效**的 minute/crane_no（r[8]/r[9]）；
            # 新事件尚未落库、无自增 id：同分钟同类型时以已落库事件优先，
            # 新事件之间按本次提交顺序排（以自增序占位保证排在既有事件之后）。
            events: list[ActualEvent] = [
                ActualEvent(
                    seq=r[0], event_type=r[1], container_no=r[2],
                    crane_no=r[6], minute=r[7], arrival_id=r[5],
                )
                for r in existing_rows
            ]
            next_arrival = max((r[5] for r in existing_rows), default=0)
            for offset, (seq, etype, cno, crane_no, minute) in enumerate(new_rows, start=1):
                events.append(ActualEvent(
                    seq=seq, event_type=etype, container_no=cno,
                    crane_no=crane_no, minute=minute,
                    arrival_id=next_arrival + offset,
                ))

            cursor.execute(
                "SELECT container_no, stack, tier FROM plan_items WHERE plan_id = ?",
                (plan_id,),
            )
            plan_items = [(r[0], r[1], r[2]) for r in cursor.fetchall()]
            try:
                replay_actuals(events, plan_items, crane_count)
            except ActualsReplayError as exc:
                conn.rollback()
                raise BusinessError(f"实绩事件重放被拒绝：{exc.reason}", 422)

            cursor.executemany(
                "INSERT INTO loading_actual_events "
                "(actuals_id, seq, event_type, container_no, crane_no, minute, "
                "orig_minute, orig_crane_no, corrected, registered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                [
                    (actuals_id, seq, etype, cno, crane_no, minute,
                     minute, crane_no,
                     models._utcnow().strftime("%Y-%m-%d %H:%M:%S.%f"))
                    for seq, etype, cno, crane_no, minute in new_rows
                ],
            )
            conn.commit()
    except BusinessError:
        raise
    except ConflictError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    db.expire_all()
    return _load_actuals_view(db, plan_id), idempotent_hit


def correct_actual_event(
    db: Session,
    plan_id: str,
    payload: ActualEventCorrectionIn,
) -> tuple[dict, bool]:
    """更正一条已登记实绩事件的分钟或岸桥，返回 (ActualsOut 字典, 是否幂等重试)。

    凭方案编号、原事件流水号、期望实绩版本、更正请求号与替代的完整事件内容更正；
    替代事件沿用原流水号、事件类型与箱号，只改 minute 或 crane_no。更正后按现有
    规则重放全部有效事件：整条链非法则整次拒绝，实绩、版本与更正历史均不变（422）。
    成功则 version+1 并留档原值与更正历史（201）。同 request_id 同内容重试，即使
    版本已被后续更正推进，也返回首次更正的结果快照、不重复生效（200）；同 request_id
    异内容或期望版本过期返回 409。方案被取代（只读）或原事件不存在分别返回 409/404。
    """
    plan = get_plan(db, plan_id)
    plan_nos = {i.container_no for i in plan.items}

    seq = payload.seq
    expected_version = payload.expected_version
    request_id = payload.request_id
    repl = payload.event
    # 锁前快速预检（锁内还会再次核对）：替代箱仍须属于方案
    if repl.container_no not in plan_nos:
        raise BusinessError(
            f"箱 {repl.container_no} 不在方案 {plan_id} 中，拒绝更正实绩事件",
            422,
        )

    db.rollback()

    from app.database import engine

    conn = engine.pool.connect()
    cursor = conn.cursor()
    idempotent_hit = False
    try:
        cursor.execute("BEGIN IMMEDIATE")

        # 锁内复核方案状态
        cursor.execute("SELECT status FROM plans WHERE id = ?", (plan_id,))
        prow = cursor.fetchone()
        if prow is None:
            conn.rollback()
            raise BusinessError(f"方案 {plan_id} 不存在", 404)
        plan_status_locked = prow[0]
        if plan_status_locked == "draft":
            conn.rollback()
            raise ConflictError("方案尚未确认（draft），尚无装船实绩可更正")
        if plan_status_locked == "superseded":
            conn.rollback()
            raise ConflictError(
                "方案修订已被新修订取代，其装船实绩已冻结为只读，不再接受更正"
            )

        cursor.execute(
            "SELECT id, version, baseline_crane_count FROM loading_actuals "
            "WHERE plan_id = ?",
            (plan_id,),
        )
        head = cursor.fetchone()
        if head is None:
            conn.rollback()
            raise BusinessError(
                f"方案 {plan_id} 尚无装船实绩登记，原事件流水号 {seq} 未找到", 404
            )
        actuals_id, current_version, crane_count = head

        # 幂等：同方案内 request_id 唯一。先查该请求号是否已生效过。
        # 以请求号为幂等键：若整次请求（期望版本/流水号/替代内容）与首次提交完全
        # 一致，则无论版本后来是否被其它更正推进，都返回首次更正的结果快照。
        cursor.execute(
            "SELECT response_snapshot, seq, expected_version, event_type, container_no, "
            "new_minute, new_crane_no FROM loading_actual_corrections "
            "WHERE actuals_id = ? AND request_id = ?",
            (actuals_id, request_id),
        )
        snap_row = cursor.fetchone()

        # 原事件（目标流水号）
        cursor.execute(
            "SELECT seq, event_type, container_no, crane_no, minute, id "
            "FROM loading_actual_events WHERE actuals_id = ? AND seq = ?",
            (actuals_id, seq),
        )
        target = cursor.fetchone()
        if target is None:
            conn.rollback()
            raise BusinessError(
                f"原事件流水号 {seq} 未找到（方案 {plan_id} 未登记该流水号），"
                "无法更正",
                404,
            )
        t_seq, t_type, t_cno, t_crane, t_minute, t_id = target

        if snap_row is not None:
            (snapshot_json, s_seq, s_expected, s_type, s_cno,
             s_new_minute, s_new_crane) = snap_row
            same_request = (
                seq == s_seq
                and expected_version == s_expected
                and repl.event_type == s_type
                and repl.container_no == s_cno
                and repl.minute == s_new_minute
                and repl.crane_no == s_new_crane
            )
            if same_request:
                # 同请求号同内容重试 -> 返回首次更正结果快照，不重复生效
                idempotent_hit = True
                conn.commit()
                return json.loads(snapshot_json), idempotent_hit
            conn.rollback()
            raise ConflictError(
                f"更正请求号 {request_id} 已用于一次不同内容的更正，"
                "同请求号不得提交异内容（如需再次更正请使用新的更正请求号），"
                "本次更正被拒绝且记录不变"
            )

        # 全新请求号：先做期望版本（乐观并发）校验
        if expected_version != current_version:
            conn.rollback()
            raise ConflictError(
                f"期望实绩版本 {expected_version} 已过期：当前版本为 "
                f"{current_version}，请基于最新实绩重新提交更正，本次更正被拒绝"
                "且记录不变"
            )

        # 替代事件只能沿用原流水号、类型与箱号，只改分钟或岸桥
        if repl.event_type != t_type:
            conn.rollback()
            raise BusinessError(
                f"替代事件类型 {repl.event_type} 与原事件类型 {t_type} 不一致："
                "更正沿用原事件类型，只允许修改分钟或岸桥",
                422,
            )
        if repl.container_no != t_cno:
            conn.rollback()
            raise BusinessError(
                f"替代事件箱号 {repl.container_no} 与原箱号 {t_cno} 不一致："
                "更正沿用原箱号，只允许修改分钟或岸桥",
                422,
            )
        if repl.crane_no == t_crane and repl.minute == t_minute:
            conn.rollback()
            raise BusinessError(
                f"更正请求号 {request_id}：替代事件与原事件（桥 {t_crane}、"
                f"第 {t_minute} 分钟）完全相同，未修改分钟或岸桥，拒绝空更正",
                422,
            )

        # 用替代值重放全部有效事件（沿用原到达 id，保持到达先后）
        cursor.execute(
            "SELECT seq, event_type, container_no, crane_no, minute, id "
            "FROM loading_actual_events WHERE actuals_id = ? ORDER BY id",
            (actuals_id,),
        )
        all_rows = cursor.fetchall()
        events = []
        for r in all_rows:
            if r[0] == seq:
                events.append(ActualEvent(
                    seq=t_seq, event_type=t_type, container_no=t_cno,
                    crane_no=repl.crane_no, minute=repl.minute, arrival_id=r[5],
                ))
            else:
                events.append(ActualEvent(
                    seq=r[0], event_type=r[1], container_no=r[2],
                    crane_no=r[3], minute=r[4], arrival_id=r[5],
                ))
        cursor.execute(
            "SELECT container_no, stack, tier FROM plan_items WHERE plan_id = ?",
            (plan_id,),
        )
        plan_items = [(r[0], r[1], r[2]) for r in cursor.fetchall()]
        try:
            replay_result = replay_actuals(events, plan_items, crane_count)
        except ActualsReplayError as exc:
            conn.rollback()
            raise BusinessError(
                f"更正后实绩重放链非法：{exc.reason}；整次更正被拒绝，"
                "实绩、版本与更正历史均不变",
                422,
            )

        # 合法：原子更新目标事件现行值、版本 +1，并写更正历史
        new_version = current_version + 1
        now = models._utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
        cursor.execute(
            "UPDATE loading_actual_events SET crane_no = ?, minute = ?, corrected = 1 "
            "WHERE id = ?",
            (repl.crane_no, repl.minute, t_id),
        )
        # 先插入更正历史（snapshot 在同事务内组装后回填），以拿到自增 id
        cursor.execute(
            "INSERT INTO loading_actual_corrections "
            "(actuals_id, request_id, seq, expected_version, from_version, to_version, "
            "event_type, container_no, old_minute, new_minute, old_crane_no, "
            "new_crane_no, response_snapshot, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)",
            (actuals_id, request_id, seq, expected_version, current_version,
             new_version, t_type, t_cno, t_minute, repl.minute, t_crane,
             repl.crane_no, now),
        )
        correction_id = cursor.lastrowid
        cursor.execute(
            "UPDATE loading_actuals SET version = ? WHERE id = ?",
            (new_version, actuals_id),
        )

        # ---- 在同一事务/快照下组装首次更正结果（与 GET /actuals 严格一致）----
        cursor.execute(
            "SELECT revision_no FROM plans WHERE id = ?", (plan_id,)
        )
        revision_no = cursor.fetchone()[0]
        cursor.execute(
            "SELECT baseline_frozen_at, created_at FROM loading_actuals WHERE id = ?",
            (actuals_id,),
        )
        bf, ca = cursor.fetchone()
        cursor.execute(
            "SELECT container_no, stack, tier, start_minute, finish_minute, crane_no "
            "FROM loading_actual_baseline_items WHERE actuals_id = ?",
            (actuals_id,),
        )
        baseline_rows = list(cursor.fetchall())
        cursor.execute(
            "SELECT seq, event_type, container_no, crane_no, minute, orig_crane_no, "
            "orig_minute, corrected, registered_at "
            "FROM loading_actual_events WHERE actuals_id = ? ORDER BY seq",
            (actuals_id,),
        )
        event_rows = []
        for r in cursor.fetchall():
            event_rows.append((
                r[0], r[1], r[2], r[3], r[4], r[5], r[6], bool(r[7]),
                utc_iso(_raw_dt(r[8])),
            ))
        cursor.execute(
            "SELECT id, request_id, seq, expected_version, from_version, to_version, "
            "event_type, container_no, old_minute, new_minute, old_crane_no, "
            "new_crane_no, created_at FROM loading_actual_corrections "
            "WHERE actuals_id = ? ORDER BY id",
            (actuals_id,),
        )
        correction_rows = []
        for r in cursor.fetchall():
            correction_rows.append((
                r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9],
                r[10], r[11], utc_iso(_raw_dt(r[12])),
            ))

        out = assemble_actuals_out(
            plan_id=plan_id,
            revision_no=revision_no,
            version=new_version,
            plan_status=plan_status_locked,
            baseline_frozen_at=utc_iso(_raw_dt(bf)),
            baseline_crane_count=crane_count,
            created_at=utc_iso(_raw_dt(ca)),
            replay_result=replay_result,
            baseline_rows=baseline_rows,
            event_rows=event_rows,
            correction_rows=correction_rows,
        )
        # 回填幂等快照（含本次更正，version=新值）
        cursor.execute(
            "UPDATE loading_actual_corrections SET response_snapshot = ? WHERE id = ?",
            (json.dumps(out, ensure_ascii=False), correction_id),
        )
        conn.commit()
        return out, idempotent_hit
    except BusinessError:
        raise
    except ConflictError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def _raw_dt(value):
    """裸游标读出的时间列归一化为 datetime（SQLite 驱动通常直接返回 datetime）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.rstrip()
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return value


def get_actuals(db: Session, plan_id: str) -> models.LoadingActuals:
    # 方案须存在（不存在给 404 方案错误），实绩头不存在给 404 实绩错误
    get_plan(db, plan_id)
    return _load_actuals_view(db, plan_id)


def build_actuals_out(db: Session, actuals: models.LoadingActuals) -> dict:
    """重放已登记事件，组装每箱状态、实际起止、相对冻结基线的偏差与未完箱。"""
    events = [
        ActualEvent(
            seq=e.seq, event_type=e.event_type, container_no=e.container_no,
            crane_no=e.crane_no, minute=e.minute, arrival_id=e.id,
        )
        for e in sorted(actuals.events, key=lambda x: x.id)
    ]
    plan_items = [(i.container_no, i.stack, i.tier) for i in actuals.plan.items]
    # 已落库事件必然通过重放校验（防御性仍调用以得到状态）
    result = replay_actuals(events, plan_items, actuals.baseline_crane_count)
    baseline_rows = [
        (b.container_no, b.stack, b.tier, b.start_minute, b.finish_minute, b.crane_no)
        for b in sorted(actuals.baseline_items, key=lambda x: x.container_no)
    ]
    event_rows = [
        (e.seq, e.event_type, e.container_no, e.crane_no, e.minute,
         e.orig_crane_no, e.orig_minute, bool(e.corrected),
         utc_iso(e.registered_at))
        for e in sorted(actuals.events, key=lambda x: x.seq)
    ]
    correction_rows = [
        (c.id, c.request_id, c.seq, c.expected_version, c.from_version, c.to_version,
         c.event_type, c.container_no, c.old_minute, c.new_minute,
         c.old_crane_no, c.new_crane_no, utc_iso(c.created_at))
        for c in sorted(actuals.corrections, key=lambda x: x.id)
    ]
    return assemble_actuals_out(
        plan_id=actuals.plan_id,
        revision_no=actuals.plan.revision_no,
        version=actuals.version,
        plan_status=actuals.plan.status,
        baseline_frozen_at=utc_iso(actuals.baseline_frozen_at),
        baseline_crane_count=actuals.baseline_crane_count,
        created_at=utc_iso(actuals.created_at),
        replay_result=result,
        baseline_rows=baseline_rows,
        event_rows=event_rows,
        correction_rows=correction_rows,
    )


def assemble_actuals_out(
    *,
    plan_id: str,
    revision_no: int,
    version: int,
    plan_status: str,
    baseline_frozen_at: str,
    baseline_crane_count: int,
    created_at: str,
    replay_result: dict,
    # (container_no, stack, tier, start, finish, crane)
    baseline_rows,
    # (seq, type, cno, crane, minute, orig_crane, orig_minute, corrected, registered_at)
    event_rows,
    # (id, request_id, seq, expected, from_v, to_v, type, cno, old_min, new_min,
    #  old_crane, new_crane, created_at)
    correction_rows,
) -> dict:
    """纯组装：输入重放结果与各表行元组，产出 ActualsOut 字典。

    ORM 路径与更正写锁内的裸游标路径共用，保证幂等快照与查询呈现严格一致。
    """
    boxes = replay_result["boxes"]
    baseline_by_no = {row[0]: row for row in baseline_rows}

    containers = []
    for no in sorted(boxes):
        box = boxes[no]
        base = baseline_by_no.get(no)
        # base: (cno, stack, tier, start, finish, crane)
        containers.append({
            "container_no": no,
            "status": box.status,
            "actual_start_minute": box.start_minute,
            "actual_finish_minute": box.finish_minute,
            "actual_crane_no": box.crane_no,
            "baseline_start_minute": base[3] if base else None,
            "baseline_finish_minute": base[4] if base else None,
            "baseline_crane_no": base[5] if base else None,
            "start_delta_minutes": (
                box.start_minute - base[3]
                if box.start_minute is not None and base is not None else None
            ),
            "finish_delta_minutes": (
                box.finish_minute - base[4]
                if box.finish_minute is not None and base is not None else None
            ),
            "crane_changed": (
                box.crane_no != base[5]
                if box.crane_no is not None and base is not None else None
            ),
            "stack": box.stack,
            "tier": box.tier,
        })

    return {
        "plan_id": plan_id,
        "revision_no": revision_no,
        "version": version,
        "plan_status": plan_status,
        "read_only": plan_status == "superseded",
        "baseline_frozen_at": baseline_frozen_at,
        "baseline_crane_count": baseline_crane_count,
        "baseline": [
            {
                "container_no": r[0],
                "start_minute": r[3],
                "finish_minute": r[4],
                "crane_no": r[5],
                "stack": r[1],
                "tier": r[2],
            }
            for r in baseline_rows
        ],
        "containers": containers,
        "unfinished_containers": replay_result["unfinished"],
        "replay_order": replay_result["order"],
        "events": [
            {
                "seq": r[0],
                "event_type": r[1],
                "container_no": r[2],
                "crane_no": r[3],
                "minute": r[4],
                "orig_crane_no": r[5],
                "orig_minute": r[6],
                "corrected": bool(r[7]),
                "registered_at": r[8],
            }
            for r in sorted(event_rows, key=lambda x: x[0])
        ],
        "corrections": [
            {
                "request_id": r[1],
                "seq": r[2],
                "expected_version": r[3],
                "from_version": r[4],
                "to_version": r[5],
                "event_type": r[6],
                "container_no": r[7],
                "old_minute": r[8],
                "new_minute": r[9],
                "old_crane_no": r[10],
                "new_crane_no": r[11],
                "created_at": r[12],
            }
            for r in sorted(correction_rows, key=lambda x: x[0])
        ],
        "created_at": created_at,
    }


def utc_iso(dt) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).isoformat()
    return dt.astimezone(timezone.utc).isoformat()


# ---------- 装船完结交接（不可变凭证） ----------


FINGERPRINT_ALGORITHM = "sha256"


def _actuals_fingerprint(version: int, event_rows) -> str:
    """对当前有效实绩计算规范指纹：版本 + 各事件现行（流水号, 类型, 箱, 桥, 分钟）。

    event_rows 为 (seq, event_type, container_no, crane_no, minute) 可迭代对象，
    取事件**当前生效**值（更正后为替代值）；按流水号升序规范化后 SHA-256。
    """
    payload = {
        "version": version,
        "events": sorted(
            ((r[0], r[1], r[2], r[3], r[4]) for r in event_rows),
            key=lambda x: x[0],
        ),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def issue_loading_handover(
    db: Session,
    plan_id: str,
    payload,
) -> tuple[models.LoadingHandover, bool]:
    """装船完结交接：为已确认方案的已登记实绩签发不可变凭证。

    仅接受方案仍 confirmed、实绩版本等于期望版本、有效流水号集合与登记一致、
    每箱均在截止分钟前（含）完工且截止后无有效事件、逐箱按冻结排程基线计算的
    完工延误均不超过阈值的申请；任一业务条件不满足均不生成凭证。生成前在
    BEGIN IMMEDIATE 写事务内复核方案仍已确认、实绩版本及有效事件未变（指纹）。
    同交接号同内容重试（即使其后实绩版本已被更正推进）返回原凭证；同交接号
    异内容冲突（409）。返回 (凭证 ORM 对象, 是否幂等重试命中)。
    """
    expected_version = payload.expected_actual_version
    cutoff = payload.cutoff_minute
    threshold = payload.max_finish_delay_minutes
    requested_seqs = sorted(payload.valid_seqs)
    handover_no = payload.handover_no

    # 锁前仅做存在性预检（404）；方案状态等业务判定全部在写锁内完成，
    # 保证“同号同内容重试优先返回原凭证”且不读到并发更正/补登的中间快照。
    get_plan(db, plan_id)
    db.rollback()

    from app.database import engine

    conn = engine.pool.connect()
    cursor = conn.cursor()
    idempotent_hit = False
    handover_pk: str | None = None
    try:
        cursor.execute("BEGIN IMMEDIATE")

        # 幂等/冲突优先：交接号全局唯一，先判定是否已有同号凭证
        cursor.execute(
            "SELECT id, plan_id, cutoff_minute, expected_actual_version, valid_seqs, "
            "max_finish_delay_minutes FROM loading_handovers WHERE handover_no = ?",
            (handover_no,),
        )
        existing = cursor.fetchone()

        # 锁内复核：方案仍存在且仍已确认
        cursor.execute("SELECT status, revision_no FROM plans WHERE id = ?", (plan_id,))
        prow = cursor.fetchone()
        if prow is None:
            conn.rollback()
            raise BusinessError(f"方案 {plan_id} 不存在", 404)
        plan_status_locked, revision_no_locked = prow
        if plan_status_locked != "confirmed":
            conn.rollback()
            raise ConflictError(
                f"方案当前状态为 {plan_status_locked}：未确认或已被取代的方案"
                "拒绝装船完结交接"
            )

        if existing is not None:
            same = (
                existing[1] == plan_id
                and existing[2] == cutoff
                and existing[3] == expected_version
                and json.loads(existing[4]) == requested_seqs
                and existing[5] == threshold
            )
            if not same:
                conn.rollback()
                raise ConflictError(
                    f"交接号 {handover_no} 已用于一次不同的交接申请"
                    + ("（属于另一方案）" if existing[1] != plan_id else "（内容不同）")
                    + "，同交接号异内容冲突，本次申请被拒绝且不生成新凭证"
                )
            # 同交接号同内容：重试，直接返回原凭证，不重新评估后续条件
            idempotent_hit = True
            handover_pk = existing[0]
            conn.commit()
        else:
            cursor.execute(
                "SELECT id, version, baseline_crane_count FROM loading_actuals "
                "WHERE plan_id = ?",
                (plan_id,),
            )
            hrow = cursor.fetchone()
            if hrow is None:
                conn.rollback()
                raise ConflictError(
                    f"方案 {plan_id} 尚无已登记装船实绩，拒绝装船完结交接"
                )
            actuals_id_locked, version_locked, crane_count_locked = hrow

            cursor.execute(
                "SELECT seq, event_type, container_no, crane_no, minute, id "
                "FROM loading_actual_events WHERE actuals_id = ? ORDER BY id",
                (actuals_id_locked,),
            )
            event_rows = cursor.fetchall()
            seqs_locked = sorted(r[0] for r in event_rows)
            if set(seqs_locked) != set(requested_seqs):
                unknown = sorted(set(requested_seqs) - set(seqs_locked))
                missing = sorted(set(seqs_locked) - set(requested_seqs))
                problems = []
                if unknown:
                    problems.append(f"提交含未登记流水号：{', '.join(map(str, unknown))}")
                if missing:
                    problems.append(f"遗漏已登记有效流水号：{', '.join(map(str, missing))}")
                conn.rollback()
                raise ConflictError(
                    "有效流水号集合与当前已登记实绩不一致（"
                    + "；".join(problems) + "），拒绝装船完结交接"
                )

            if version_locked != expected_version:
                conn.rollback()
                raise ConflictError(
                    f"期望实绩版本 {expected_version} 已过期：当前版本为 "
                    f"{version_locked}，请基于最新实绩重新申请交接凭证"
                )

            cursor.execute(
                "SELECT container_no, stack, tier FROM plan_items WHERE plan_id = ?",
                (plan_id,),
            )
            plan_items = [(r[0], r[1], r[2]) for r in cursor.fetchall()]
            events = [
                ActualEvent(
                    seq=r[0], event_type=r[1], container_no=r[2],
                    crane_no=r[3], minute=r[4], arrival_id=r[5],
                )
                for r in event_rows
            ]
            replay_result = replay_actuals(events, plan_items, crane_count_locked)
            boxes = replay_result["boxes"]
            fingerprint_locked = _actuals_fingerprint(version_locked, event_rows)

            # 截止条件：每箱在截止前（含）完工，且截止后无有效事件
            unfinished = [no for no in sorted(boxes) if boxes[no].status != "finished"]
            finished_after = sorted(
                no for no, b in boxes.items()
                if b.status == "finished" and b.finish_minute > cutoff
            )
            events_after = sorted(
                (e for e in events if e.minute > cutoff),
                key=lambda e: (e.minute, 0 if e.event_type == "finish" else 1,
                               e.arrival_id),
            )
            if unfinished or finished_after or events_after:
                conn.rollback()
                extra = {
                    "unfinished_containers": unfinished,
                    "finished_after_cutoff": [
                        {"container_no": no,
                         "actual_finish_minute": boxes[no].finish_minute}
                        for no in finished_after
                    ],
                    "events_after_cutoff": [
                        {"seq": e.seq, "event_type": e.event_type,
                         "container_no": e.container_no, "minute": e.minute}
                        for e in events_after
                    ],
                    "cutoff_minute": cutoff,
                }
                raise BusinessError(
                    "不满足截止条件：每箱均须在截止分钟前（含）完工且截止后无有效"
                    "事件，本次交接被拒绝且不生成凭证",
                    422,
                    extra=extra,
                )

            # 逐箱以冻结排程基线计算完工延误（仅延后计为延误，提前不计）
            cursor.execute(
                "SELECT container_no, stack, tier, start_minute, finish_minute, "
                "crane_no FROM loading_actual_baseline_items WHERE actuals_id = ?",
                (actuals_id_locked,),
            )
            baseline_by_no = {r[0]: r for r in cursor.fetchall()}
            item_snapshot = []
            exceeded = []
            max_delay = 0
            for no in sorted(boxes):
                box = boxes[no]
                base = baseline_by_no.get(no)
                if base is None:  # 防御：首次登记冻结基线覆盖排程全部箱
                    conn.rollback()
                    raise BusinessError(
                        f"箱 {no} 缺少冻结排程基线，数据不一致，拒绝交接", 500
                    )
                # base: (cno, stack, tier, base_start, base_finish, base_crane)
                start_delta = box.start_minute - base[3]
                finish_delta = box.finish_minute - base[4]
                delay = max(0, finish_delta)
                max_delay = max(max_delay, delay)
                crane_changed = box.crane_no != base[5]
                item_snapshot.append((
                    no, box.stack, box.tier,
                    box.start_minute, box.finish_minute, box.crane_no,
                    base[3], base[4], base[5],
                    start_delta, finish_delta, 1 if crane_changed else 0,
                ))
                if delay > threshold:
                    exceeded.append({
                        "container_no": no,
                        "baseline_finish_minute": base[4],
                        "actual_finish_minute": box.finish_minute,
                        "finish_delta_minutes": finish_delta,
                        "delay_minutes": delay,
                        "allowed_max_delay_minutes": threshold,
                    })
            if exceeded:
                conn.rollback()
                raise BusinessError(
                    f"存在 {len(exceeded)} 箱完工延误超过允许阈值 {threshold} 分钟，"
                    "本次交接被拒绝且不生成凭证",
                    422,
                    extra={
                        "cutoff_minute": cutoff,
                        "allowed_max_delay_minutes": threshold,
                        "exceeded_containers": exceeded,
                    },
                )

            # 全部条件满足：固化凭证（方案修订号、各箱实际起止与岸桥、冻结基线、
            # 偏差、截止分钟与实绩指纹均随凭证不可变）
            handover_pk = models._new_id()
            now = models._utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
            cursor.execute(
                "INSERT INTO loading_handovers "
                "(id, plan_id, handover_no, cutoff_minute, expected_actual_version, "
                "actual_version, max_finish_delay_minutes, max_observed_finish_delay, "
                "revision_no, container_count, valid_seqs, actuals_fingerprint, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (handover_pk, plan_id, handover_no, cutoff, expected_version,
                 version_locked, threshold, max_delay, revision_no_locked,
                 len(item_snapshot), json.dumps(requested_seqs),
                 fingerprint_locked, now),
            )
            cursor.executemany(
                "INSERT INTO loading_handover_items "
                "(handover_id, container_no, stack, tier, actual_start_minute, "
                "actual_finish_minute, actual_crane_no, baseline_start_minute, "
                "baseline_finish_minute, baseline_crane_no, start_delta_minutes, "
                "finish_delta_minutes, crane_changed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(handover_pk, *row12) for row12 in item_snapshot],
            )
            conn.commit()
    except BusinessError:
        raise
    except ConflictError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    db.expire_all()
    return get_handover_by_pk(db, handover_pk), idempotent_hit


def get_handover_by_pk(db: Session, handover_id: str) -> models.LoadingHandover:
    handover = db.get(models.LoadingHandover, handover_id)
    if handover is None:
        raise BusinessError(f"交接凭证 {handover_id} 不存在", 404)
    return handover


def list_plan_handovers(db: Session, plan_id: str) -> list[models.LoadingHandover]:
    get_plan(db, plan_id)  # 方案不存在 -> 404
    return list(db.scalars(
        select(models.LoadingHandover)
        .where(models.LoadingHandover.plan_id == plan_id)
        .order_by(models.LoadingHandover.created_at, models.LoadingHandover.id)
    ))


def get_plan_handover(
    db: Session, plan_id: str, handover_id: str
) -> models.LoadingHandover:
    handover = get_handover_by_pk(db, handover_id)
    if handover.plan_id != plan_id:
        raise BusinessError(f"交接凭证 {handover_id} 不存在", 404)
    return handover


def build_handover_out(db: Session, handover: models.LoadingHandover) -> dict:
    """组装凭证响应：冻结内容 + 当前实绩匹配标示（指纹比对）。"""
    actuals = db.scalar(
        select(models.LoadingActuals).where(
            models.LoadingActuals.plan_id == handover.plan_id
        )
    )
    current_version = None
    current_fingerprint = None
    matches = False
    if actuals is not None:
        current_version = actuals.version
        current_fingerprint = _actuals_fingerprint(
            actuals.version,
            [(e.seq, e.event_type, e.container_no, e.crane_no, e.minute)
             for e in actuals.events],
        )
        matches = current_fingerprint == handover.actuals_fingerprint

    return assemble_handover_out(
        handover,
        matches_current=matches,
        current_version=current_version,
        current_fingerprint=current_fingerprint,
    )


def assemble_handover_out(
    handover: models.LoadingHandover,
    *,
    matches_current: bool,
    current_version: int | None,
    current_fingerprint: str | None,
) -> dict:
    containers = [
        {
            "container_no": i.container_no,
            "stack": i.stack,
            "tier": i.tier,
            "actual_start_minute": i.actual_start_minute,
            "actual_finish_minute": i.actual_finish_minute,
            "actual_crane_no": i.actual_crane_no,
            "baseline_start_minute": i.baseline_start_minute,
            "baseline_finish_minute": i.baseline_finish_minute,
            "baseline_crane_no": i.baseline_crane_no,
            "start_delta_minutes": i.start_delta_minutes,
            "finish_delta_minutes": i.finish_delta_minutes,
            "crane_changed": bool(i.crane_changed),
        }
        for i in sorted(handover.items, key=lambda x: x.container_no)
    ]
    return {
        "handover_id": handover.id,
        "handover_no": handover.handover_no,
        "plan_id": handover.plan_id,
        "revision_no": handover.revision_no,
        "cutoff_minute": handover.cutoff_minute,
        "expected_actual_version": handover.expected_actual_version,
        "actual_version": handover.actual_version,
        "max_finish_delay_minutes": handover.max_finish_delay_minutes,
        "max_observed_finish_delay": handover.max_observed_finish_delay,
        "valid_seqs": json.loads(handover.valid_seqs),
        "actuals_fingerprint": handover.actuals_fingerprint,
        "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
        "containers": containers,
        "summary": {
            "container_count": handover.container_count,
            "cutoff_minute": handover.cutoff_minute,
            "actual_version": handover.actual_version,
            "max_finish_delay_minutes": handover.max_finish_delay_minutes,
            "max_observed_finish_delay": handover.max_observed_finish_delay,
            "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
        },
        "matches_current_actuals": matches_current,
        "current_actual_version": current_version,
        "current_fingerprint": current_fingerprint,
        "created_at": utc_iso(handover.created_at),
    }


# ---------- 离港前封签核对与放行凭证 ----------


def _seal_manifest_content_hash(items) -> str:
    """预期清单内容指纹：逐箱 (箱号, 预期封签号) 按箱号升序规范 JSON 的 SHA-256。"""
    payload = json.dumps(
        {"items": [[c, s] for c, s in sorted(items)]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scan_content_hash(
    plan_id: str, scan_no: str, container_no: str, seal_no: str, check_minute: int
) -> str:
    """扫描内容指纹：方案+扫描号+箱号+实见封签号+检查分钟 规范 JSON 的 SHA-256。"""
    payload = json.dumps(
        {
            "plan_id": plan_id,
            "scan_no": scan_no,
            "container_no": container_no,
            "seal_no": seal_no,
            "check_minute": check_minute,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scans_fingerprint(version: int, effective_rows) -> str:
    """扫描状态指纹：扫描版本 + 各箱当前有效扫描（按箱号升序规范化）。

    effective_rows 为 (container_no, scan_no, observed_seal_no, check_minute,
    matched) 五元组，覆盖清单全部箱（未扫描箱以 ("", "", -1, False) 占位）。
    任何新扫描都会推进扫描版本，指纹随之改变。
    """
    payload = {
        "version": version,
        "effective": sorted(
            ((r[0], r[1], r[2], r[3], bool(r[4])) for r in effective_rows),
            key=lambda x: x[0],
        ),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def register_seal_manifest(
    db: Session, plan_id: str, payload
) -> tuple[models.SealManifest, bool]:
    """登记预期封签清单，返回 (清单, 是否为同内容重试)。

    清单须覆盖方案全部箱且封签号互异；首次登记在写锁内原子冻结，冻结后不
    能改写：同方案同内容重复登记幂等返回原清单（200），异内容冲突（409）。
    方案须为当前已确认（draft/superseded → 409）。
    """
    items = tuple(sorted((i.container_no, i.seal_no) for i in payload.items))
    content_hash = _seal_manifest_content_hash(items)
    get_plan(db, plan_id)  # 锁前存在性预检（404）
    db.rollback()

    from app.database import engine

    conn = engine.pool.connect()
    cursor = conn.cursor()
    idempotent_hit = False
    try:
        cursor.execute("BEGIN IMMEDIATE")

        # 幂等/冲突优先：每方案至多一份清单；同内容重试即使方案已被取代也返回原清单
        cursor.execute(
            "SELECT id, content_hash FROM seal_manifests WHERE plan_id = ?",
            (plan_id,),
        )
        existing = cursor.fetchone()
        if existing is not None:
            if existing[1] != content_hash:
                conn.rollback()
                raise ConflictError(
                    f"方案 {plan_id} 已登记预期封签清单且内容不同：清单冻结后不能"
                    "改写，异内容冲突，本次登记被拒绝，原清单不变"
                )
            idempotent_hit = True
            conn.commit()
        else:
            cursor.execute(
                "SELECT status, revision_no FROM plans WHERE id = ?", (plan_id,)
            )
            prow = cursor.fetchone()
            if prow is None:
                conn.rollback()
                raise BusinessError(f"方案 {plan_id} 不存在", 404)
            plan_status_locked, revision_no_locked = prow
            if plan_status_locked == "draft":
                conn.rollback()
                raise ConflictError("方案尚未确认（draft），拒绝登记预期封签清单")
            if plan_status_locked == "superseded":
                conn.rollback()
                raise ConflictError(
                    "方案修订已被新修订取代，拒绝登记预期封签清单"
                )
            # 覆盖全部箱：提交箱号集合须与方案箱项完全一致
            cursor.execute(
                "SELECT container_no FROM plan_items WHERE plan_id = ?", (plan_id,)
            )
            plan_nos = {r[0] for r in cursor.fetchall()}
            submitted_nos = {c for c, _ in items}
            missing = sorted(plan_nos - submitted_nos)
            extra = sorted(submitted_nos - plan_nos)
            if missing or extra:
                problems = []
                if missing:
                    problems.append(f"缺少箱：{', '.join(missing)}")
                if extra:
                    problems.append(f"方案外箱：{', '.join(extra)}")
                conn.rollback()
                raise BusinessError(
                    "预期封签清单须覆盖方案全部箱（" + "；".join(problems) + "），"
                    "拒绝登记",
                    422,
                )
            now = models._utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
            cursor.execute(
                "INSERT INTO seal_manifests "
                "(plan_id, revision_no, container_count, content_hash, "
                "scan_version, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                (plan_id, revision_no_locked, len(items), content_hash, now),
            )
            manifest_id = cursor.lastrowid
            cursor.executemany(
                "INSERT INTO seal_manifest_items "
                "(manifest_id, container_no, expected_seal_no) VALUES (?, ?, ?)",
                [(manifest_id, c, s) for c, s in items],
            )
            conn.commit()
    except BusinessError:
        raise
    except ConflictError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    db.expire_all()
    return get_seal_manifest(db, plan_id), idempotent_hit


def get_seal_manifest(db: Session, plan_id: str) -> models.SealManifest:
    get_plan(db, plan_id)  # 方案不存在 -> 404
    manifest = db.scalar(
        select(models.SealManifest).where(models.SealManifest.plan_id == plan_id)
    )
    if manifest is None:
        raise BusinessError(f"方案 {plan_id} 尚未登记预期封签清单", 404)
    return manifest


def build_seal_manifest_out(manifest: models.SealManifest) -> dict:
    return {
        "plan_id": manifest.plan_id,
        "revision_no": manifest.revision_no,
        "container_count": manifest.container_count,
        "scan_version": manifest.scan_version,
        "fingerprint": manifest.content_hash,
        "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
        "items": [
            {"container_no": i.container_no, "expected_seal_no": i.expected_seal_no}
            for i in sorted(manifest.items, key=lambda x: x.container_no)
        ],
        "created_at": utc_iso(manifest.created_at),
    }


def register_seal_scan(db: Session, plan_id: str, payload) -> tuple[dict, bool]:
    """现场登记一条封签扫描，返回 (扫描记录视图, 是否为同扫描号重试)。

    扫描号全局唯一：同号同内容重试返回首次登记快照（200），异内容（含复用
    至另一方案）冲突（409）。未知箱号拒绝登记（422）；封签不符的扫描留痕
    （matched=false）但不计为通过。每登记一条新扫描，扫描版本 +1。方案被
    新修订取代后扫描只读（409），历史记录仍可查询。
    """
    scan_no = payload.scan_no
    container_no = payload.container_no
    seal_no = payload.seal_no
    check_minute = payload.check_minute
    content_hash = _scan_content_hash(
        plan_id, scan_no, container_no, seal_no, check_minute
    )
    get_plan(db, plan_id)  # 锁前存在性预检（404）
    db.rollback()

    from app.database import engine

    conn = engine.pool.connect()
    cursor = conn.cursor()
    idempotent_hit = False
    snapshot = None
    try:
        cursor.execute("BEGIN IMMEDIATE")

        # 幂等/冲突优先：扫描号全局唯一，同号同内容重试返回首次登记快照
        cursor.execute(
            "SELECT plan_id, content_hash, response_snapshot FROM seal_scans "
            "WHERE scan_no = ?",
            (scan_no,),
        )
        existing = cursor.fetchone()
        if existing is not None:
            if existing[1] != content_hash:
                conn.rollback()
                suffix = "（该扫描号已用于另一方案）" if existing[0] != plan_id else ""
                raise ConflictError(
                    f"扫描号 {scan_no} 已用于不同内容的扫描登记{suffix}，"
                    "同扫描号异内容冲突，本次登记被拒绝"
                )
            idempotent_hit = True
            snapshot = json.loads(existing[2])
            conn.commit()
        else:
            cursor.execute(
                "SELECT status, revision_no FROM plans WHERE id = ?", (plan_id,)
            )
            prow = cursor.fetchone()
            if prow is None:
                conn.rollback()
                raise BusinessError(f"方案 {plan_id} 不存在", 404)
            plan_status_locked = prow[0]
            if plan_status_locked == "draft":
                conn.rollback()
                raise ConflictError("方案尚未确认（draft），拒绝登记封签扫描")
            if plan_status_locked == "superseded":
                conn.rollback()
                raise ConflictError(
                    "方案修订已被新修订取代，其封签扫描已冻结为只读，不再接受登记"
                )
            cursor.execute(
                "SELECT id, scan_version FROM seal_manifests WHERE plan_id = ?",
                (plan_id,),
            )
            mrow = cursor.fetchone()
            if mrow is None:
                conn.rollback()
                raise ConflictError(
                    f"方案 {plan_id} 尚未登记预期封签清单，须先登记清单方可扫描"
                )
            manifest_id, scan_version = mrow
            cursor.execute(
                "SELECT expected_seal_no FROM seal_manifest_items "
                "WHERE manifest_id = ? AND container_no = ?",
                (manifest_id, container_no),
            )
            irow = cursor.fetchone()
            if irow is None:
                conn.rollback()
                raise BusinessError(
                    f"箱 {container_no} 不在方案 {plan_id} 的预期封签清单中"
                    "（未知箱号），拒绝登记",
                    422,
                )
            expected_seal = irow[0]
            matched = seal_no == expected_seal
            new_version = scan_version + 1
            now = models._utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
            cursor.execute(
                "INSERT INTO seal_scans "
                "(plan_id, manifest_id, scan_no, container_no, expected_seal_no, "
                "seal_no, check_minute, matched, content_hash, response_snapshot, "
                "registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)",
                (plan_id, manifest_id, scan_no, container_no, expected_seal,
                 seal_no, check_minute, 1 if matched else 0, content_hash, now),
            )
            scan_id = cursor.lastrowid
            cursor.execute(
                "UPDATE seal_manifests SET scan_version = ? WHERE id = ?",
                (new_version, manifest_id),
            )
            # 同箱有效扫描：检查分钟较晚者优先，同分钟登记顺序（自增 id）较晚者优先
            cursor.execute(
                "SELECT id FROM seal_scans "
                "WHERE manifest_id = ? AND container_no = ? "
                "ORDER BY check_minute DESC, id DESC LIMIT 1",
                (manifest_id, container_no),
            )
            effective = cursor.fetchone()[0] == scan_id
            snapshot = {
                "scan_no": scan_no,
                "plan_id": plan_id,
                "container_no": container_no,
                "expected_seal_no": expected_seal,
                "seal_no": seal_no,
                "check_minute": check_minute,
                "matched": matched,
                "effective": effective,
                "scan_version": new_version,
                "registered_at": utc_iso(_raw_dt(now)),
            }
            cursor.execute(
                "UPDATE seal_scans SET response_snapshot = ? WHERE id = ?",
                (json.dumps(snapshot, ensure_ascii=False), scan_id),
            )
            conn.commit()
    except BusinessError:
        raise
    except ConflictError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    db.expire_all()
    return snapshot, idempotent_hit


def _effective_scan_map(scans):
    """各箱当前有效扫描：检查分钟较晚者优先，同分钟登记顺序（id）较晚者优先。"""
    latest: dict = {}
    for s in scans:
        cur = latest.get(s.container_no)
        if cur is None or (s.check_minute, s.id) > (cur.check_minute, cur.id):
            latest[s.container_no] = s
    return latest


def get_seal_scan_status(db: Session, plan_id: str) -> dict:
    """封签扫描状态：扫描版本、逐箱最新有效扫描（是否通过）与全部留痕记录。"""
    plan = get_plan(db, plan_id)  # 方案不存在 -> 404
    manifest = db.scalar(
        select(models.SealManifest).where(models.SealManifest.plan_id == plan_id)
    )
    if manifest is None:
        raise BusinessError(
            f"方案 {plan_id} 尚未登记预期封签清单，无封签扫描状态", 404
        )
    scans = list(db.scalars(
        select(models.SealScan)
        .where(models.SealScan.manifest_id == manifest.id)
        .order_by(models.SealScan.id)
    ))
    latest = _effective_scan_map(scans)
    effective_ids = {s.id for s in latest.values()}

    containers = []
    for item in sorted(manifest.items, key=lambda x: x.container_no):
        eff = latest.get(item.container_no)
        containers.append({
            "container_no": item.container_no,
            "expected_seal_no": item.expected_seal_no,
            "scanned": eff is not None,
            "passed": bool(eff.matched) if eff is not None else False,
            "latest_scan_no": eff.scan_no if eff is not None else None,
            "observed_seal_no": eff.seal_no if eff is not None else None,
            "check_minute": eff.check_minute if eff is not None else None,
        })
    return {
        "plan_id": plan_id,
        "revision_no": plan.revision_no,
        "plan_status": plan.status,
        "read_only": plan.status == "superseded",
        "scan_version": manifest.scan_version,
        "container_count": manifest.container_count,
        "containers": containers,
        "scans": [
            {
                "scan_no": s.scan_no,
                "container_no": s.container_no,
                "expected_seal_no": s.expected_seal_no,
                "seal_no": s.seal_no,
                "check_minute": s.check_minute,
                "matched": bool(s.matched),
                "effective": s.id in effective_ids,
                "registered_at": utc_iso(s.registered_at),
            }
            for s in scans
        ],
        "created_at": utc_iso(manifest.created_at),
    }


def issue_seal_release(
    db: Session, plan_id: str, payload
) -> tuple[models.SealRelease, bool]:
    """申请离港放行凭证，返回 (凭证, 是否为同申请号重试)。

    仅当方案仍已确认、所给交接凭证仍对应当前实绩、每箱最新扫描均匹配预期
    且不晚于截止分钟时，在写事务内原子签发含逐箱结果的不可变凭证；缺扫、
    不符或版本过期拒绝签发（缺扫/不符/超截止 → 422，版本过期/方案未确认或
    已取代/交接凭证失效 → 409）。同申请号同内容重试返回原凭证（200），
    异内容冲突（409）。
    """
    release_no = payload.release_no
    handover_id = payload.handover_id
    expected_scan_version = payload.expected_scan_version
    cutoff = payload.cutoff_minute
    get_plan(db, plan_id)  # 锁前存在性预检（404）
    db.rollback()

    from app.database import engine

    conn = engine.pool.connect()
    cursor = conn.cursor()
    idempotent_hit = False
    release_pk: str | None = None
    try:
        cursor.execute("BEGIN IMMEDIATE")

        # 幂等/冲突优先：申请号全局唯一，同号同内容重试返回原凭证
        cursor.execute(
            "SELECT id, plan_id, handover_id, expected_scan_version, cutoff_minute "
            "FROM seal_releases WHERE release_no = ?",
            (release_no,),
        )
        existing = cursor.fetchone()
        if existing is not None:
            same = (
                existing[1] == plan_id
                and existing[2] == handover_id
                and existing[3] == expected_scan_version
                and existing[4] == cutoff
            )
            if not same:
                conn.rollback()
                raise ConflictError(
                    f"申请号 {release_no} 已用于一次不同的放行申请"
                    + ("（属于另一方案）" if existing[1] != plan_id else "（内容不同）")
                    + "，同申请号异内容冲突，本次申请被拒绝且不生成新凭证"
                )
            idempotent_hit = True
            release_pk = existing[0]
            conn.commit()
        else:
            # 锁内复核：方案仍已确认
            cursor.execute(
                "SELECT status, revision_no FROM plans WHERE id = ?", (plan_id,)
            )
            prow = cursor.fetchone()
            if prow is None:
                conn.rollback()
                raise BusinessError(f"方案 {plan_id} 不存在", 404)
            plan_status_locked, revision_no_locked = prow
            if plan_status_locked != "confirmed":
                conn.rollback()
                raise ConflictError(
                    f"方案当前状态为 {plan_status_locked}：未确认或已被取代的方案"
                    "拒绝离港放行"
                )

            # 交接凭证须存在、属于本方案且仍对应当前实绩
            cursor.execute(
                "SELECT plan_id, handover_no, actuals_fingerprint "
                "FROM loading_handovers WHERE id = ?",
                (handover_id,),
            )
            hrow = cursor.fetchone()
            if hrow is None:
                conn.rollback()
                raise BusinessError(f"交接凭证 {handover_id} 不存在", 404)
            if hrow[0] != plan_id:
                conn.rollback()
                raise BusinessError(
                    f"交接凭证 {handover_id} 不属于方案 {plan_id}，拒绝离港放行",
                    422,
                )
            cursor.execute(
                "SELECT id, version FROM loading_actuals WHERE plan_id = ?",
                (plan_id,),
            )
            arow = cursor.fetchone()
            current_actuals_fp = None
            if arow is not None:
                cursor.execute(
                    "SELECT seq, event_type, container_no, crane_no, minute "
                    "FROM loading_actual_events WHERE actuals_id = ? ORDER BY id",
                    (arow[0],),
                )
                current_actuals_fp = _actuals_fingerprint(arow[1], cursor.fetchall())
            if current_actuals_fp is None or current_actuals_fp != hrow[2]:
                conn.rollback()
                raise ConflictError(
                    f"交接凭证 {handover_id} 已不对应当前实绩（实绩在凭证签发后"
                    "已更正或补登），拒绝离港放行"
                )

            # 预期清单与期望扫描版本
            cursor.execute(
                "SELECT id, scan_version FROM seal_manifests WHERE plan_id = ?",
                (plan_id,),
            )
            mrow = cursor.fetchone()
            if mrow is None:
                conn.rollback()
                raise ConflictError(
                    f"方案 {plan_id} 尚未登记预期封签清单，拒绝离港放行"
                )
            manifest_id, scan_version = mrow
            if expected_scan_version != scan_version:
                conn.rollback()
                raise ConflictError(
                    f"期望扫描版本 {expected_scan_version} 已过期：当前扫描版本为 "
                    f"{scan_version}，请基于最新扫描状态重新申请放行"
                )

            # 逐箱最新有效扫描核对：缺扫、最新扫描封签不符、检查晚于截止均拒绝
            cursor.execute(
                "SELECT container_no, expected_seal_no FROM seal_manifest_items "
                "WHERE manifest_id = ?",
                (manifest_id,),
            )
            manifest_items = cursor.fetchall()
            cursor.execute(
                "SELECT id, scan_no, container_no, seal_no, check_minute, matched "
                "FROM seal_scans WHERE manifest_id = ? ORDER BY id",
                (manifest_id,),
            )
            latest: dict = {}
            for r in cursor.fetchall():
                cur = latest.get(r[2])
                if cur is None or (r[4], r[0]) > (cur[4], cur[0]):
                    latest[r[2]] = r

            missing = []
            mismatched = []
            after_cutoff = []
            item_snapshot = []
            fingerprint_rows = []
            max_check = 0
            for cno, expected_seal in sorted(manifest_items):
                s = latest.get(cno)
                if s is None:
                    missing.append(cno)
                    fingerprint_rows.append((cno, "", "", -1, False))
                    continue
                fingerprint_rows.append((cno, s[1], s[3], s[4], bool(s[5])))
                if not s[5]:
                    mismatched.append({
                        "container_no": cno,
                        "expected_seal_no": expected_seal,
                        "observed_seal_no": s[3],
                        "check_minute": s[4],
                        "scan_no": s[1],
                    })
                    continue
                if s[4] > cutoff:
                    after_cutoff.append({
                        "container_no": cno,
                        "check_minute": s[4],
                        "scan_no": s[1],
                    })
                    continue
                item_snapshot.append((cno, expected_seal, s[3], s[4], s[1], 1))
                max_check = max(max_check, s[4])
            if missing or mismatched or after_cutoff:
                conn.rollback()
                problems = []
                if missing:
                    problems.append(f"{len(missing)} 箱缺扫描")
                if mismatched:
                    problems.append(f"{len(mismatched)} 箱最新扫描封签不符")
                if after_cutoff:
                    problems.append(f"{len(after_cutoff)} 箱最新扫描晚于截止分钟")
                raise BusinessError(
                    "不满足放行条件：" + "、".join(problems)
                    + "，本次放行被拒绝且不生成凭证",
                    422,
                    extra={
                        "missing_scans": missing,
                        "mismatched_containers": mismatched,
                        "scans_after_cutoff": after_cutoff,
                        "cutoff_minute": cutoff,
                    },
                )

            # 全部条件满足：固化凭证（方案修订号、所用交接凭证、截止分钟、
            # 扫描版本、扫描/实绩指纹与逐箱核对结果均随凭证不可变）
            scans_fp = _scans_fingerprint(scan_version, fingerprint_rows)
            release_pk = models._new_id()
            now = models._utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
            cursor.execute(
                "INSERT INTO seal_releases "
                "(id, plan_id, release_no, handover_id, handover_no, cutoff_minute, "
                "expected_scan_version, scan_version, revision_no, container_count, "
                "max_check_minute, scans_fingerprint, actuals_fingerprint, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (release_pk, plan_id, release_no, handover_id, hrow[1], cutoff,
                 expected_scan_version, scan_version, revision_no_locked,
                 len(item_snapshot), max_check, scans_fp, current_actuals_fp, now),
            )
            cursor.executemany(
                "INSERT INTO seal_release_items "
                "(release_id, container_no, expected_seal_no, observed_seal_no, "
                "check_minute, scan_no, matched) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(release_pk, *row6) for row6 in item_snapshot],
            )
            conn.commit()
    except BusinessError:
        raise
    except ConflictError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    db.expire_all()
    return get_seal_release_by_pk(db, release_pk), idempotent_hit


def get_seal_release_by_pk(db: Session, release_id: str) -> models.SealRelease:
    release = db.get(models.SealRelease, release_id)
    if release is None:
        raise BusinessError(f"放行凭证 {release_id} 不存在", 404)
    return release


def list_plan_seal_releases(db: Session, plan_id: str) -> list[models.SealRelease]:
    get_plan(db, plan_id)  # 方案不存在 -> 404
    return list(db.scalars(
        select(models.SealRelease)
        .where(models.SealRelease.plan_id == plan_id)
        .order_by(models.SealRelease.created_at, models.SealRelease.id)
    ))


def get_plan_seal_release(
    db: Session, plan_id: str, release_id: str
) -> models.SealRelease:
    release = get_seal_release_by_pk(db, release_id)
    if release.plan_id != plan_id:
        raise BusinessError(f"放行凭证 {release_id} 不存在", 404)
    return release


def _current_scans_state(db: Session, plan_id: str):
    """当前扫描状态 (扫描版本, 指纹)；无预期清单时 (None, None)。"""
    manifest = db.scalar(
        select(models.SealManifest).where(models.SealManifest.plan_id == plan_id)
    )
    if manifest is None:
        return None, None
    scans = list(db.scalars(
        select(models.SealScan)
        .where(models.SealScan.manifest_id == manifest.id)
        .order_by(models.SealScan.id)
    ))
    latest = _effective_scan_map(scans)
    rows = []
    for item in sorted(manifest.items, key=lambda x: x.container_no):
        s = latest.get(item.container_no)
        if s is None:
            rows.append((item.container_no, "", "", -1, False))
        else:
            rows.append(
                (item.container_no, s.scan_no, s.seal_no, s.check_minute,
                 bool(s.matched))
            )
    return manifest.scan_version, _scans_fingerprint(manifest.scan_version, rows)


def build_seal_release_out(db: Session, release: models.SealRelease) -> dict:
    """组装放行凭证响应：冻结内容 + 当前有效性标示。

    后续扫描、实绩更正或方案取代只使凭证显示失效（is_current=false 且
    invalid_reasons 非空），不改签发时固化的历史内容。
    """
    plan = db.get(models.Plan, release.plan_id)
    plan_status = plan.status if plan is not None else None

    current_scan_version, current_scans_fp = _current_scans_state(
        db, release.plan_id
    )

    actuals = db.scalar(
        select(models.LoadingActuals).where(
            models.LoadingActuals.plan_id == release.plan_id
        )
    )
    current_actuals_fp = None
    if actuals is not None:
        current_actuals_fp = _actuals_fingerprint(
            actuals.version,
            [(e.seq, e.event_type, e.container_no, e.crane_no, e.minute)
             for e in actuals.events],
        )

    invalid_reasons = []
    if plan_status != "confirmed":
        invalid_reasons.append("plan_not_confirmed")
    if current_scans_fp != release.scans_fingerprint:
        invalid_reasons.append("scans_changed")
    if current_actuals_fp != release.actuals_fingerprint:
        invalid_reasons.append("actuals_changed")

    containers = [
        {
            "container_no": i.container_no,
            "expected_seal_no": i.expected_seal_no,
            "observed_seal_no": i.observed_seal_no,
            "check_minute": i.check_minute,
            "scan_no": i.scan_no,
            "matched": bool(i.matched),
        }
        for i in sorted(release.items, key=lambda x: x.container_no)
    ]
    return {
        "release_id": release.id,
        "release_no": release.release_no,
        "plan_id": release.plan_id,
        "revision_no": release.revision_no,
        "handover_id": release.handover_id,
        "handover_no": release.handover_no,
        "cutoff_minute": release.cutoff_minute,
        "expected_scan_version": release.expected_scan_version,
        "scan_version": release.scan_version,
        "container_count": release.container_count,
        "max_check_minute": release.max_check_minute,
        "scans_fingerprint": release.scans_fingerprint,
        "actuals_fingerprint": release.actuals_fingerprint,
        "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
        "containers": containers,
        "summary": {
            "container_count": release.container_count,
            "cutoff_minute": release.cutoff_minute,
            "scan_version": release.scan_version,
            "max_check_minute": release.max_check_minute,
            "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
        },
        "is_current": not invalid_reasons,
        "invalid_reasons": invalid_reasons,
        "plan_status": plan_status,
        "current_scan_version": current_scan_version,
        "current_scans_fingerprint": current_scans_fp,
        "current_actuals_fingerprint": current_actuals_fp,
        "created_at": utc_iso(release.created_at),
    }
