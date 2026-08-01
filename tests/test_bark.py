import json
from unittest import TestCase
from unittest.mock import MagicMock, patch

from longwatch.bark import BarkNotifier


class BarkNotifierTests(TestCase):
    @patch("longwatch.bark.urlopen")
    def test_push_payload_contains_detail_url(self, urlopen):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"code":200}'
        urlopen.return_value.__enter__.return_value = response
        notifier = BarkNotifier("https://api.day.app", "device-key")
        notifier.send(
            "Intel 告警",
            "现价发生波动",
            "https://watch.example/?symbol=INTC.US",
        )
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["url"], "https://watch.example/?symbol=INTC.US")
