"""LittleMatrix — fast Quotex client with premium features.

Built on the fixed pyquotex core (see PYQUOTEX_FIXES.md — 108/108 tests
pass, default host market-qx.info). LittleMatrix adds on top:

* Multilogin — several Quotex accounts at once, with failover.
* Advanced sentiment — pressure, trend and spike detection, not just
  the raw buy/sell percentages.
* Proxy / DNS — per-account proxy, round-robin rotation, custom DNS.
* Fast execution — pre-warmed price stream + latency measurement
  around every buy, so you can see the real speed in ms.

"Ultra fast" in marketing means nothing without a number — every
buy through FastTrader returns its latency in milliseconds, so you
can measure it yourself. Real speed still depends on Quotex's
server and your network, no library can beat that.
"""

from .client import LittleMatrix
from .fast import FastTradeResult, FastTrader
from .multilogin import AccountConfig, MultiLoginManager
from .proxy import ProxyManager
from .sentiment import SentimentAnalyzer, SentimentSignal

__version__ = "1.0.0"
__all__ = [
    "LittleMatrix",
    "AccountConfig",
    "MultiLoginManager",
    "SentimentAnalyzer",
    "SentimentSignal",
    "ProxyManager",
    "FastTrader",
    "FastTradeResult",
    "__version__",
]
