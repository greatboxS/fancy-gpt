import pytest
from pydantic import ValidationError

from fancy_gpt.models import FinalReport
from tests.helpers import final_payload

@pytest.mark.parametrize("mode",["review","design","consult","investigate","verify","write"])
def test_six_mode_contracts_accept_representative_payload(mode):
    assert FinalReport.model_validate(final_payload("r",mode=mode)).mode.value==mode


def test_high_finding_requires_evidence():
    p=final_payload("r"); p["findings"][0]["evidence"]=[]
    with pytest.raises(ValidationError): FinalReport.model_validate(p)


def test_completed_research_requires_url():
    p=final_payload("r"); p["research_trace"][0]["source_urls"]=[]
    with pytest.raises(ValidationError): FinalReport.model_validate(p)
