import asyncio
import pytest
from tests.remote.test_opencode_l1_qualification import test_10_opencode_text_transport_smoke

if __name__ == "__main__":
    pytest.main(["-s", "tests/remote/test_opencode_l1_qualification.py::test_10_opencode_text_transport_smoke"])
