"""Retry short daemon interruptions in the existing workstation health timer."""

import time

import monitor


def send_check(session_id: str, text: str, *, request_id: str) -> dict:
    """Keep one submission identity through refusals and ambiguous responses.

    These waits bound delivery only. They neither resume a stopped session nor
    impose a deadline on the agent's work. The next scheduled tick still owns
    its own request ID; this helper does not replay historical missed checks.
    """
    # Keep the existing submitter's 50-second process timeout. In particular,
    # quick refusals during an upgrade get another attempt after 35 seconds.
    delays = iter((5, 10, 20))
    while True:
        try:
            return monitor.send_session_message(
                session_id, text, request_id=request_id)
        except monitor.MjError as exc:
            retryable = (
                exc.reason == "daemon_unreachable"
                or str(exc) in {
                    "mj message request refused (HTTP 409)",
                    "mj message request refused (HTTP 503)",
                }
            )
            if not retryable:
                raise
            delay = next(delays, None)
            if delay is None:
                raise
            # Do not log the prompt, connection data, or the response body.
            monitor.log(f"health-check delivery deferred; retry in {delay}s "
                        f"with the original request ID ({exc.reason})")
            time.sleep(delay)
