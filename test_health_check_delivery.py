import unittest
from unittest.mock import call, patch

import health_check_delivery
import monitor


class HealthCheckDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.args = ("session", "private prompt")
        self.kwargs = {"request_id": "ci-agent-health-2026102403"}
        self.receipt = {"session_id": "session", "via": "turn", "turn_id": 17}

    @patch("health_check_delivery.monitor.log")
    @patch("health_check_delivery.time.sleep")
    @patch("health_check_delivery.monitor.send_session_message")
    def test_upgrade_refusals_recover_without_changing_identity(self, send, sleep, log):
        send.side_effect = [
            monitor.MjError("mj message request refused (HTTP 409)",
                            reason="mj_supervision_failed"),
            monitor.MjError("mj message request refused (HTTP 503)",
                            reason="mj_supervision_failed"),
            self.receipt,
        ]
        self.assertEqual(health_check_delivery.send_check(*self.args, **self.kwargs),
                         self.receipt)
        self.assertEqual(send.call_args_list, [call(*self.args, **self.kwargs)] * 3)
        self.assertEqual(sleep.call_args_list, [call(5), call(10)])
        self.assertNotIn("private prompt", str(log.call_args_list))

    @patch("health_check_delivery.monitor.log")
    @patch("health_check_delivery.time.sleep")
    @patch("health_check_delivery.monitor.send_session_message")
    def test_lost_response_retries_the_accepted_identity_once(self, send, sleep, log):
        send.side_effect = [monitor.MjError("mj message API is unavailable",
                                            reason="daemon_unreachable"), self.receipt]
        self.assertEqual(health_check_delivery.send_check(*self.args, **self.kwargs),
                         self.receipt)
        self.assertEqual(send.call_args_list, [call(*self.args, **self.kwargs)] * 2)
        sleep.assert_called_once_with(5)

    @patch("health_check_delivery.monitor.log")
    @patch("health_check_delivery.time.sleep")
    @patch("health_check_delivery.monitor.send_session_message")
    def test_unavailable_session_stops_after_bounded_delivery_retries(self, send, sleep, log):
        refusal = monitor.MjError("mj message request refused (HTTP 409)",
                                  reason="mj_supervision_failed")
        send.side_effect = refusal
        with self.assertRaises(monitor.MjError) as raised:
            health_check_delivery.send_check(*self.args, **self.kwargs)
        self.assertIs(raised.exception, refusal)
        self.assertEqual(send.call_count, 4)
        self.assertEqual(sleep.call_args_list, [call(v) for v in [5, 10, 20]])
        self.assertTrue(all(v == call(*self.args, **self.kwargs)
                            for v in send.call_args_list))

    @patch("health_check_delivery.time.sleep")
    @patch("health_check_delivery.monitor.send_session_message")
    def test_invalid_and_unauthorized_submissions_are_not_retried(self, send, sleep):
        for message, reason in [
            ("invalid request ID", "mj_message_invalid"),
            ("mj message request refused (HTTP 401)", "mj_supervision_failed"),
            ("mj message API returned an invalid receipt", "mj_supervision_failed"),
        ]:
            with self.subTest(reason=reason, message=message):
                send.reset_mock()
                send.side_effect = monitor.MjError(message, reason=reason)
                with self.assertRaises(monitor.MjError):
                    health_check_delivery.send_check(*self.args, **self.kwargs)
                send.assert_called_once_with(*self.args, **self.kwargs)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
