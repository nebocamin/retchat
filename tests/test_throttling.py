"""Batching of announces, chat list refreshes and LXMF stamp cost saves."""

import threading
import types

import pytest

import retchat.reticulum_service as rs
from retchat.database import Database
from retchat.reticulum_service import ReticulumService

PEER_A = "aa" * 16
PEER_B = "bb" * 16


class _FakeRouter:
    def __init__(self):
        self.outbound_stamp_costs = {}
        self.saves = 0

    def update_stamp_cost(self, destination_hash, stamp_cost):
        raise AssertionError("LXMF's own update_stamp_cost must be replaced")

    def save_outbound_stamp_costs(self):
        self.saves += 1


@pytest.fixture
def service(tmp_path):
    svc = ReticulumService.__new__(ReticulumService)
    svc.db = Database(str(tmp_path / "test.db"))
    svc.app = types.SimpleNamespace(message_router=_FakeRouter())
    svc._node_names = {}
    svc._announce_lock = threading.Lock()
    svc._announce_batch = {}
    svc._announce_flush_scheduled = False
    svc._announce_callbacks = []
    svc._conv_changed_lock = threading.Lock()
    svc._conv_changed_scheduled = False
    svc._conversations_changed_callbacks = []
    svc._stamp_cost_router = None
    svc._stamp_costs_dirty = False
    svc._stamp_costs_saved_at = 0.0
    return svc


def test_stamp_costs_saved_at_most_once_per_interval(service, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rs.time, "monotonic", lambda: now[0])
    service._throttle_stamp_cost_saves()
    router = service.app.message_router

    for i in range(50):
        router.update_stamp_cost(bytes([i]) * 16, 8)
    assert len(router.outbound_stamp_costs) == 50
    assert router.saves == 0  # nothing written per announce

    service._save_stamp_costs()
    assert router.saves == 1
    router.update_stamp_cost(b"\x01" * 16, 9)
    service._save_stamp_costs()
    assert router.saves == 1  # within the interval

    now[0] += rs.STAMP_COST_SAVE_INTERVAL
    service._save_stamp_costs()
    assert router.saves == 2
    service._save_stamp_costs(force=True)
    assert router.saves == 2  # nothing changed since


def test_stamp_costs_saved_at_exit(service):
    service._throttle_stamp_cost_saves()
    service.app.message_router.update_stamp_cost(b"\x01" * 16, 8)
    service._save_stamp_costs(force=True)
    assert service.app.message_router.saves == 1
