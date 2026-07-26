"""POST /api/payment/create — создать платёж; GET — редирект на страницу оплаты."""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.http import DispatchHandler  # noqa: E402


class handler(DispatchHandler):  # noqa: N801 - имя требует Vercel
    pass
