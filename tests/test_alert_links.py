from unittest import TestCase

from longwatch.alert_links import AlertLinkSigner


class AlertLinkSignerTests(TestCase):
    def test_each_token_is_random_and_expires_after_one_day(self):
        now = [1_700_000_000.0]
        signer = AlertLinkSigner("test-signing-secret", clock=lambda: now[0])

        first = signer.issue("INTC.US")
        second = signer.issue("INTC.US")

        self.assertNotEqual(first, second)
        self.assertTrue(signer.verify(first, "INTC.US"))
        self.assertFalse(signer.verify(first, "AAPL.US"))

        now[0] += 24 * 60 * 60 + 1
        self.assertFalse(signer.verify(first, "INTC.US"))

    def test_rejects_tampered_token(self):
        signer = AlertLinkSigner("test-signing-secret")
        token = signer.issue("INTC.US")
        self.assertFalse(signer.verify(f"{token}x", "INTC.US"))
