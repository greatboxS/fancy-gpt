from pathlib import Path
import pytest
from fancy_gpt.models import RequestMode, RequestState
from fancy_gpt.store import RequestStore
from fancy_gpt.errors import InvalidStateError


def test_store_atomic_state_transition(tmp_path: Path):
    store=RequestStore(tmp_path); store.create_status("r1",route_kind="skill",route_name="technical-review",skill="technical-review",mode=RequestMode.REVIEW,objective="x")
    store.transition("r1",RequestState.NEW,RequestState.WAITING_PLANNER)
    with pytest.raises(InvalidStateError): store.transition("r1",RequestState.NEW,RequestState.COMPLETE)
    assert store.load_status("r1").state==RequestState.WAITING_PLANNER
