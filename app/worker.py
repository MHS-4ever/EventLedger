import argparse
import logging
import signal
from threading import Event
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings, settings
from app.db.compatibility import check_schema_compatibility
from app.db.session import SessionLocal, engine
from app.services.order_handler import HandlerOptions
from app.services.processing import run_worker_cycle
from app.services.processing_errors import ProcessingStateError
from app.services.processing_types import ProcessingPolicy, WorkerCycleResult

logger = logging.getLogger(__name__)

WORKER_SCHEMA_PREFLIGHT_EXIT_CODE = 2


def build_policy(worker_settings: Settings) -> ProcessingPolicy:
    return ProcessingPolicy(
        lease_duration_seconds=worker_settings.worker_lease_duration_seconds,
        max_attempts=worker_settings.worker_max_attempts,
        retry_base_seconds=worker_settings.worker_retry_base_seconds,
        retry_max_seconds=worker_settings.worker_retry_max_seconds,
    )


def build_handler_options(worker_settings: Settings) -> HandlerOptions:
    return HandlerOptions(
        failure_simulation_enabled=(
            worker_settings.worker_failure_simulation_enabled
        ),
        simulation_max_slow_seconds=(
            worker_settings.worker_simulation_max_slow_seconds
        ),
    )


def _log_cycle(result: WorkerCycleResult, worker_id: str) -> None:
    if result.recovered is not None:
        logger.info(
            "lease_recovered worker_id=%s event_id=%s attempt=%s status=%s",
            worker_id,
            result.recovered.event_id,
            result.recovered.attempt_number,
            result.recovered.status,
        )
    if result.exhausted_event_id is not None:
        logger.info(
            "exhausted_pending_failed worker_id=%s event_id=%s",
            worker_id,
            result.exhausted_event_id,
        )
    if result.processing is not None:
        logger.info(
            "processing_finished worker_id=%s event_id=%s attempt=%s status=%s",
            worker_id,
            result.processing.event_id,
            result.processing.attempt_number,
            result.processing.status,
        )


def _schema_preflight(worker_id: str) -> bool:
    try:
        with SessionLocal() as session:
            compatibility = check_schema_compatibility(session)
    except Exception as error:
        logger.error(
            "worker_schema_preflight_failed worker_id=%s error_type=%s",
            worker_id,
            type(error).__name__,
        )
        return False

    if not compatibility.compatible:
        logger.error(
            "worker_schema_incompatible worker_id=%s mode=%s reason=%s",
            worker_id,
            compatibility.mode,
            compatibility.reason,
        )
        return False

    logger.info(
        "worker_schema_compatible worker_id=%s mode=%s",
        worker_id,
        compatibility.mode,
    )
    return True


def run_worker(*, once: bool = False) -> int:
    worker_id = settings.worker_identifier or f"worker-{uuid4().hex[:12]}"
    policy = build_policy(settings)
    handler_options = build_handler_options(settings)
    stop_requested = Event()

    def request_stop(signum: int, frame: object) -> None:
        del frame
        logger.info(
            "worker_stop_requested worker_id=%s signal=%s",
            worker_id,
            signum,
        )
        stop_requested.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    try:
        if not _schema_preflight(worker_id):
            return WORKER_SCHEMA_PREFLIGHT_EXIT_CODE

        logger.info(
            "worker_started worker_id=%s once=%s max_attempts=%s "
            "lease_seconds=%s",
            worker_id,
            once,
            policy.max_attempts,
            policy.lease_duration_seconds,
        )
        if handler_options.failure_simulation_enabled:
            logger.warning(
                "worker_failure_simulation_enabled worker_id=%s "
                "production_safe=false message='Failure simulation must not be "
                "enabled in production'",
                worker_id,
            )

        while not stop_requested.is_set():
            try:
                result = run_worker_cycle(SessionLocal, policy, handler_options)
                _log_cycle(result, worker_id)
            except SQLAlchemyError as error:
                logger.error(
                    "worker_database_error worker_id=%s error_type=%s",
                    worker_id,
                    type(error).__name__,
                )
                if once:
                    return 1
            except ProcessingStateError as error:
                logger.error(
                    "worker_state_error worker_id=%s message=%s",
                    worker_id,
                    str(error),
                )
                if once:
                    return 1

            if once:
                return 0
            stop_requested.wait(settings.worker_poll_interval_seconds)
    finally:
        engine.dispose()
        logger.info("worker_stopped worker_id=%s", worker_id)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Process EventLedger events from PostgreSQL.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Run one maintenance cycle and process at most one claimed event, "
            "then exit."
        ),
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, arguments.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return run_worker(once=arguments.once)


if __name__ == "__main__":
    raise SystemExit(main())
