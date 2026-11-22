import inspect
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Generic, Optional, TypeVar
from uuid import UUID

from app.core.database import get_db_connection
from app.models.job_tracking import (
    DuplicateJobRunError,
    JobExecutionContext,
    JobRunContext,
    JobRunOutcome,
    JobRunSpecification,
    JobTrackingFinalizeError,
    JobTrackingNotificationError,
    JobTrackingStartError,
    NotificationStatus,
)
from app.repositories.job_tracking_repository import (
    JobTrackingRepository,
    sanitize_recursive,
    sanitize_tracking_error,
)
from app.services.email_service import EmailService
from app.services.job_tracking.adapters.base import JobResultAdapter
from app.services.job_tracking.notification.policy import (
    JobNotificationRule,
    NotificationDecision,
    NotificationPolicy,
)
from app.services.job_tracking.notification.renderer import JobEmailRenderer

logger = logging.getLogger(__name__)

TResult = TypeVar("TResult")


def _inspect_takes_ctx(fn: Callable[..., Any]) -> bool:
    """Analyze callable signature to determine whether it accepts JobExecutionContext."""
    try:
        sig = inspect.signature(fn)
        params = [
            p
            for p in sig.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        return len(params) > 0 or any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    except (ValueError, TypeError):
        return True


def build_job_run_context(
    started_at: Optional[datetime] = None,
    dry_run: bool = False,
    no_email: bool = False,
) -> JobRunContext:
    """Construct JobRunContext by inspecting system environment and GitHub Actions metadata."""
    now_utc = started_at or datetime.now(timezone.utc)
    event_name = os.environ.get("GITHUB_EVENT_NAME", "").strip().lower()

    trigger_type = "scheduled" if event_name == "schedule" else "manual"

    attempt_str = os.environ.get("GITHUB_RUN_ATTEMPT")
    run_attempt: Optional[int] = None
    if attempt_str:
        try:
            val = int(attempt_str)
            if val > 0:
                run_attempt = val
        except ValueError:
            pass

    return JobRunContext(
        started_at=now_utc,
        trigger_type=trigger_type,
        github_run_id=os.environ.get("GITHUB_RUN_ID"),
        github_run_attempt=run_attempt,
        github_workflow=os.environ.get("GITHUB_WORKFLOW"),
        github_sha=os.environ.get("GITHUB_SHA"),
        github_ref=os.environ.get("GITHUB_REF_NAME") or os.environ.get("GITHUB_REF"),
        dry_run=dry_run,
        no_email=no_email,
    )


class TrackedJobRunner(Generic[TResult]):
    """Decorator / Wrapper runner coordinating job execution, database tracking, and notification policy."""

    def __init__(
        self,
        spec: JobRunSpecification,
        adapter: JobResultAdapter[TResult],
        renderer: Optional[JobEmailRenderer[TResult]] = None,
        notification_rule: JobNotificationRule = JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        email_service: Optional[EmailService] = None,
        db_connection_factory: Optional[Callable] = None,
    ) -> None:
        self.spec = spec
        self.adapter = adapter
        self.renderer = renderer
        self.notification_rule = notification_rule
        self.email_service = email_service or EmailService()
        self.db_connection_factory = db_connection_factory or get_db_connection

    def _call_business_fn(
        self,
        execute_fn: Callable[..., Any],
        exec_ctx: JobExecutionContext,
    ) -> Any:
        """Invoke synchronous execute_fn exactly once with appropriate arguments."""
        if _inspect_takes_ctx(execute_fn):
            return execute_fn(exec_ctx)
        return execute_fn()

    async def _call_business_fn_async(
        self,
        execute_fn: Callable[..., Any],
        exec_ctx: JobExecutionContext,
    ) -> Any:
        """Invoke asynchronous execute_fn exactly once with appropriate arguments."""
        if _inspect_takes_ctx(execute_fn):
            res = execute_fn(exec_ctx)
        else:
            res = execute_fn()

        if inspect.isawaitable(res):
            return await res
        return res

    def _finalize_and_notify_lifecycle(
        self,
        run_id: UUID,
        ctx: JobRunContext,
        job_result: Optional[TResult],
        unhandled_exc: Optional[Exception],
    ) -> TResult:
        """Common lifecycle handler coordinating Tx C (finalize) and Tx D (notification)."""
        finished_at = datetime.now(timezone.utc)
        if finished_at < ctx.started_at:
            finished_at = ctx.started_at
        duration = max(0.0, (finished_at - ctx.started_at).total_seconds())

        outcome: JobRunOutcome
        adapter_exc: Optional[Exception] = None
        if unhandled_exc is not None:
            clean_err = sanitize_tracking_error(str(unhandled_exc))
            outcome = JobRunOutcome(
                status="failed",
                sync_action="failed_no_write",
                finished_at=finished_at,
                duration_seconds=duration,
                warnings=[f"Unhandled exception: {type(unhandled_exc).__name__}"],
                metrics={"unhandled_exception": type(unhandled_exc).__name__},
                error=clean_err or "Unhandled exception during execution",
            )
        elif job_result is not None:
            try:
                outcome = self.adapter.to_outcome(job_result)
                if not outcome.finished_at or outcome.finished_at < ctx.started_at:
                    outcome = outcome.model_copy(update={"finished_at": finished_at})
                if outcome.duration_seconds is None:
                    outcome = outcome.model_copy(update={"duration_seconds": duration})
            except Exception as exc:
                adapter_exc = exc
                logger.error(
                    "Adapter failed to convert result for '%s' (error_type=%s)",
                    self.spec.job_name,
                    type(exc).__name__,
                )
                clean_err = sanitize_tracking_error(str(exc))
                outcome = JobRunOutcome(
                    status="failed",
                    sync_action="failed_no_write",
                    finished_at=finished_at,
                    duration_seconds=duration,
                    warnings=[f"Adapter exception: {type(exc).__name__}"],
                    metrics={"adapter_exception": type(exc).__name__},
                    error=clean_err or "Adapter processing failed",
                )
        else:
            outcome = JobRunOutcome(
                status="failed",
                sync_action="failed_no_write",
                finished_at=finished_at,
                duration_seconds=duration,
                error="Job returned None without exception",
            )

        # Sanitize outcome before persistence & notification
        outcome = outcome.model_copy(
            update={
                "error": sanitize_tracking_error(outcome.error),
                "warnings": [sanitize_tracking_error(w) or w for w in outcome.warnings],
                "metrics": sanitize_recursive(outcome.metrics),
            }
        )

        # Evaluate notification decision safely
        notif_eval_exc: Optional[Exception] = None
        try:
            is_email_ready = self.email_service.is_configured()
            decision = NotificationPolicy.evaluate(
                rule=self.notification_rule,
                status=outcome.status,
                dry_run=ctx.dry_run,
                no_email=ctx.no_email,
                is_email_configured=is_email_ready,
            )
        except Exception as exc:
            notif_eval_exc = exc
            logger.error(
                "Failed to evaluate notification policy for '%s' (error_type=%s)",
                self.spec.job_name,
                type(exc).__name__,
            )
            decision = NotificationDecision(
                should_send=False,
                initial_notification_status="failed",
                reason=f"Notification policy evaluation failed: {type(exc).__name__}",
            )

        # Transaction C: Finalize run in refresh_runs & write provider_run_results if multi-provider
        try:
            with self.db_connection_factory() as conn:
                with conn.transaction():
                    finalized = JobTrackingRepository.finalize_run(
                        conn,
                        run_id=run_id,
                        outcome=outcome,
                        notification_status=decision.initial_notification_status,
                    )
                    if not finalized:
                        raise JobTrackingFinalizeError(
                            f"refresh_run record {run_id} could not be finalized: record not in 'running' status"
                        )

                    if outcome.provider_outcomes:
                        JobTrackingRepository.upsert_provider_results(
                            conn,
                            run_id=run_id,
                            outcomes=outcome.provider_outcomes,
                        )
            logger.info(
                "Finalized tracked job '%s' (status=%s, sync_action=%s, notif_status=%s)",
                self.spec.job_name,
                outcome.status,
                outcome.sync_action,
                decision.initial_notification_status,
            )
        except Exception as exc:
            logger.error(
                "Failed to finalize job tracking record in database (error_type=%s)",
                type(exc).__name__,
            )
            # Evaluate notification policy and dispatch central tracking failure notification (best-effort)
            is_email_ready = False
            try:
                is_email_ready = self.email_service.is_configured() if self.email_service else False
            except Exception:
                is_email_ready = False

            try:
                comp_decision = NotificationPolicy.evaluate(
                    rule=self.notification_rule,
                    status="failed",
                    dry_run=ctx.dry_run,
                    no_email=ctx.no_email,
                    is_email_configured=is_email_ready,
                )
            except Exception as n_err:
                logger.error(
                    "Failed to evaluate notification policy during compensation for '%s' (error_type=%s)",
                    self.spec.job_name,
                    type(n_err).__name__,
                )
                comp_decision = NotificationDecision(
                    should_send=False,
                    reason="compensation_policy_eval_failed",
                    initial_notification_status="failed",
                )

            comp_notif_status: NotificationStatus = comp_decision.initial_notification_status
            comp_notified_at: Optional[datetime] = None
            comp_notif_error: Optional[str] = None

            if comp_decision.should_send:
                scope_label = self.spec.scope or "-"
                comp_subject = f"[FinBot UYARI] {self.spec.job_name} ({scope_label}) — FAILED"
                comp_body_text = (
                    f"FinBot İş Bildirimi (Merkezi Takip Hatası)\n"
                    f"İş Adı: {self.spec.job_name}\n"
                    f"Kapsam: {scope_label}\n"
                    f"Durum: FAILED\n"
                    f"Hata: Merkezi iş takibi veya finalization sırasında operasyonel hata oluştu."
                )
                try:
                    send_ok = self.email_service.send_email(
                        subject=comp_subject,
                        body_text=comp_body_text,
                        body_html=None,
                    )
                    if send_ok:
                        comp_notif_status = "sent"
                        comp_notified_at = datetime.now(timezone.utc)
                        comp_notif_error = None
                        logger.info("Compensation notification email sent successfully for job '%s'.", self.spec.job_name)
                    else:
                        comp_notif_status = "failed"
                        comp_notif_error = "E-posta gönderimi başarısız oldu (SMTP teslimat hatası)."
                        logger.error("Failed to send compensation notification email for job '%s'", self.spec.job_name)
                except Exception as send_exc:
                    comp_notif_status = "failed"
                    comp_notif_error = "E-posta servisi istisna fırlattı."
                    logger.error(
                        "Exception sending compensation notification email for '%s' (error_type=%s)",
                        self.spec.job_name,
                        type(send_exc).__name__,
                    )

            # Compensation transaction: attempt to mark running refresh_run as failed on a fresh connection
            try:
                with self.db_connection_factory() as comp_conn:
                    with comp_conn.transaction():
                        compensation_outcome = JobRunOutcome(
                            status="failed",
                            sync_action="failed_no_write",
                            finished_at=finished_at,
                            duration_seconds=duration,
                            warnings=[f"Finalize transaction failed: {type(exc).__name__}"],
                            error=sanitize_tracking_error(f"Finalize transaction failed: {type(exc).__name__}"),
                        )
                        finalized = JobTrackingRepository.finalize_run(
                            comp_conn,
                            run_id=run_id,
                            outcome=compensation_outcome,
                            notification_status=comp_notif_status,
                            notification_error=comp_notif_error,
                            notified_at=comp_notified_at,
                        )
                        if finalized:
                            logger.info(
                                "Applied compensation transaction to mark refresh_run %s as failed after Tx C error",
                                run_id,
                            )
                        else:
                            logger.warning(
                                "Failed to apply compensation transaction: refresh_run %s not found or not in 'running' status",
                                run_id,
                            )
            except Exception as comp_exc:
                logger.error(
                    "Compensation transaction DB write failed for '%s' (error_type=%s). Run record may remain unfinalized.",
                    self.spec.job_name,
                    type(comp_exc).__name__,
                )

            raise JobTrackingFinalizeError(
                f"Failed to finalize tracked job '{self.spec.job_name}': {type(exc).__name__}"
            ) from exc

        if notif_eval_exc is not None:
            raise JobTrackingNotificationError(
                f"Notification policy evaluation failed for '{self.spec.job_name}': {type(notif_eval_exc).__name__}"
            ) from notif_eval_exc

        # Transaction D: Notification Dispatch
        notif_status = decision.initial_notification_status
        notif_error: Optional[str] = None
        display_email_error: Optional[str] = None
        notified_at: Optional[datetime] = None

        if decision.should_send:
            try:
                subject: str
                body_text: str
                body_html: Optional[str] = None

                if self.renderer and job_result is not None and adapter_exc is None:
                    try:
                        subject, body_text, body_html = self.renderer.render(job_result)
                    except Exception as render_exc:
                        logger.error(
                            "Failed to render notification template for '%s' (error_type=%s)",
                            self.spec.job_name,
                            type(render_exc).__name__,
                        )
                        subject = f"[FinBot UYARI] {self.spec.job_name} çalışması tamamlandı (Rapor Hatası)"
                        body_text = (
                            f"FinBot İş Bildirimi\n"
                            f"İş Adı: {self.spec.job_name}\n"
                            f"Kapsam: {self.spec.scope or '-'}\n"
                            f"Durum: {outcome.status.upper()}\n"
                            f"Hata: Bildirim şablonu oluşturulamadı ({type(render_exc).__name__})."
                        )
                        body_html = None
                else:
                    subject = f"[FinBot UYARI] {self.spec.job_name} çalışması başarısız oldu"
                    body_text = (
                        f"FinBot İş Bildirimi\n"
                        f"İş Adı: {self.spec.job_name}\n"
                        f"Kapsam: {self.spec.scope or '-'}\n"
                        f"Durum: {outcome.status.upper()}\n"
                        f"Hata: {outcome.error or 'Operasyonel hata oluştu.'}"
                    )
                    body_html = None

                subject = sanitize_tracking_error(subject) or subject
                body_text = sanitize_tracking_error(body_text) or body_text
                if body_html:
                    body_html = sanitize_tracking_error(body_html) or body_html

                ok = self.email_service.send_email(
                    subject=subject,
                    body_text=body_text,
                    body_html=body_html,
                )
                if ok:
                    notif_status = "sent"
                    notified_at = datetime.now(timezone.utc)
                    logger.info("Notification email sent successfully for job '%s'.", self.spec.job_name)
                else:
                    notif_status = "failed"
                    notif_error = "email_service.send_email returned False"
                    display_email_error = "E-posta gönderimi başarısız oldu (SMTP teslimat hatası)."
                    logger.warning("Email service failed to send notification for job '%s'.", self.spec.job_name)
            except Exception as email_exc:
                logger.error(
                    "Failed to send notification email for '%s' (error_type=%s)",
                    self.spec.job_name,
                    type(email_exc).__name__,
                )
                notif_status = "failed"
                notif_error = sanitize_tracking_error(str(email_exc))
                display_email_error = "E-posta gönderimi sırasında hata oluştu."

            # Record notification outcome in refresh_runs (Fail-closed on DB failure)
            try:
                with self.db_connection_factory() as conn:
                    with conn.transaction():
                        ok_notif = JobTrackingRepository.update_notification_status(
                            conn,
                            run_id=run_id,
                            status=notif_status,
                            error=notif_error,
                            notified_at=notified_at,
                        )
                        if not ok_notif:
                            raise JobTrackingNotificationError(
                                f"Failed to record notification status '{notif_status}' for job '{self.spec.job_name}'"
                            )
            except JobTrackingNotificationError:
                raise
            except Exception as notif_db_exc:
                logger.error(
                    "Database error updating notification status for '%s' (error_type=%s)",
                    self.spec.job_name,
                    type(notif_db_exc).__name__,
                )
                raise JobTrackingNotificationError(
                    f"Database error recording notification status: {type(notif_db_exc).__name__}"
                ) from notif_db_exc

        # Map concrete result email_status and email_error deterministically
        if job_result is not None:
            mapped_model_status: Optional[str] = notif_status
            if mapped_model_status == "disabled":
                mapped_model_status = "skipped_no_email"
            elif mapped_model_status == "not_configured":
                mapped_model_status = "skipped_not_configured"
            elif mapped_model_status == "not_required":
                mapped_model_status = None
            elif mapped_model_status == "skipped_dry_run":
                mapped_model_status = "skipped_dry_run"
            elif mapped_model_status in ("sent", "failed"):
                mapped_model_status = mapped_model_status

            if hasattr(job_result, "email_status"):
                job_result.email_status = mapped_model_status
            if hasattr(job_result, "email_error"):
                job_result.email_error = display_email_error if notif_error else None

        if unhandled_exc is not None:
            raise unhandled_exc

        if adapter_exc is not None:
            raise JobTrackingFinalizeError(
                f"Adapter failed to convert result for '{self.spec.job_name}': {type(adapter_exc).__name__}"
            ) from adapter_exc

        return job_result  # type: ignore

    def run(
        self,
        execute_fn: Callable[..., TResult],
        context: Optional[JobRunContext] = None,
        dry_run: bool = False,
        no_email: bool = False,
    ) -> TResult:
        """Execute the job synchronously with full tracking lifecycle and separated transaction boundaries."""
        ctx = context or build_job_run_context(dry_run=dry_run, no_email=no_email)

        # 1. Dry-run execution: zero DB writes, zero emails
        if ctx.dry_run:
            logger.info("Executing job '%s' in DRY-RUN mode (tracking disabled).", self.spec.job_name)
            exec_ctx = JobExecutionContext(run_id=None, dry_run=True, no_email=ctx.no_email, started_at=ctx.started_at)
            result = self._call_business_fn(execute_fn, exec_ctx)
            if hasattr(result, "email_status"):
                result.email_status = "skipped_dry_run"
            return result

        # 2. Production Apply-DB execution: Transaction A (Start tracking - Fail Closed)
        run_id: UUID
        try:
            with self.db_connection_factory() as conn:
                with conn.transaction():
                    run_id = JobTrackingRepository.start_run(conn, self.spec, ctx)
            logger.info("Started tracked job '%s' (refresh_run_id=%s)", self.spec.job_name, run_id)
        except (DuplicateJobRunError, JobTrackingStartError):
            raise
        except Exception as exc:
            logger.error(
                "Failed to start job tracking for '%s' (error_type=%s). Failing closed.",
                self.spec.job_name,
                type(exc).__name__,
            )
            raise JobTrackingStartError(f"Failed to start tracked job '{self.spec.job_name}': {type(exc).__name__}") from exc

        # Execute business logic (Transaction B is managed inside business logic)
        job_result: Optional[TResult] = None
        unhandled_exc: Optional[Exception] = None

        exec_ctx = JobExecutionContext(run_id=run_id, dry_run=False, no_email=ctx.no_email, started_at=ctx.started_at)
        try:
            job_result = self._call_business_fn(execute_fn, exec_ctx)
        except Exception as exc:
            unhandled_exc = exc
            logger.error(
                "Unhandled exception during job execution of '%s' (error_type=%s)",
                self.spec.job_name,
                type(exc).__name__,
            )

        return self._finalize_and_notify_lifecycle(
            run_id=run_id,
            ctx=ctx,
            job_result=job_result,
            unhandled_exc=unhandled_exc,
        )

    async def run_async(
        self,
        execute_fn: Callable[..., Any],
        context: Optional[JobRunContext] = None,
        dry_run: bool = False,
        no_email: bool = False,
    ) -> TResult:
        """Execute the job asynchronously with full tracking lifecycle and separated transaction boundaries."""
        ctx = context or build_job_run_context(dry_run=dry_run, no_email=no_email)

        # 1. Dry-run execution: zero DB writes, zero emails
        if ctx.dry_run:
            logger.info("Executing async job '%s' in DRY-RUN mode (tracking disabled).", self.spec.job_name)
            exec_ctx = JobExecutionContext(run_id=None, dry_run=True, no_email=ctx.no_email, started_at=ctx.started_at)
            result = await self._call_business_fn_async(execute_fn, exec_ctx)
            if hasattr(result, "email_status"):
                result.email_status = "skipped_dry_run"
            return result

        # 2. Production Apply-DB execution: Transaction A (Start tracking - Fail Closed)
        run_id: UUID
        try:
            with self.db_connection_factory() as conn:
                with conn.transaction():
                    run_id = JobTrackingRepository.start_run(conn, self.spec, ctx)
            logger.info("Started tracked async job '%s' (refresh_run_id=%s)", self.spec.job_name, run_id)
        except (DuplicateJobRunError, JobTrackingStartError):
            raise
        except Exception as exc:
            logger.error(
                "Failed to start job tracking for '%s' (error_type=%s). Failing closed.",
                self.spec.job_name,
                type(exc).__name__,
            )
            raise JobTrackingStartError(f"Failed to start tracked job '{self.spec.job_name}': {type(exc).__name__}") from exc

        # Execute business logic (Transaction B is managed inside business logic)
        job_result: Optional[TResult] = None
        unhandled_exc: Optional[Exception] = None

        exec_ctx = JobExecutionContext(run_id=run_id, dry_run=False, no_email=ctx.no_email, started_at=ctx.started_at)
        try:
            job_result = await self._call_business_fn_async(execute_fn, exec_ctx)
        except Exception as exc:
            unhandled_exc = exc
            logger.error(
                "Unhandled exception during async job execution of '%s' (error_type=%s)",
                self.spec.job_name,
                type(exc).__name__,
            )

        return self._finalize_and_notify_lifecycle(
            run_id=run_id,
            ctx=ctx,
            job_result=job_result,
            unhandled_exc=unhandled_exc,
        )

