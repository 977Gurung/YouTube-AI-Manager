"""Loaded automatically by Python when tests/live is on PYTHONPATH. Points the real server's outside
traffic at FakeWeb and swaps the YouTube upload client for FakeYouTube (sandbox only)."""
import os
import sys

if os.environ.get("YTM_FAKEWEB_PORT"):
    root = os.environ["YTM_ROOT_FOR_TESTS"]
    sys.path[:0] = [root, os.path.join(root, "tests")]
    from fakes import FakeYouTube, install_requests_redirect

    install_requests_redirect(int(os.environ["YTM_FAKEWEB_PORT"]))
    import app.agents.upload as _up

    _yt = FakeYouTube()
    _up.service = lambda slug: _yt
