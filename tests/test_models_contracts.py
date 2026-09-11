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


def test_consult_requires_real_distinct_tradeoffs():
    p=final_payload("r",mode="consult"); p["options"]=p["options"][:1]
    with pytest.raises(ValidationError, match="at least two"):
        FinalReport.model_validate(p)
    p=final_payload("r",mode="consult"); p["options"][1]["advantages"]=[]
    with pytest.raises(ValidationError, match="advantages"):
        FinalReport.model_validate(p)


def test_verify_verdict_is_bounded():
    p=final_payload("r",mode="verify"); p["verdict"]="probably"
    with pytest.raises(ValidationError, match="pass, fail, or insufficient"):
        FinalReport.model_validate(p)


def test_deliverable_and_validation_plan_cannot_be_placeholders():
    p=final_payload("r",mode="write"); p["deliverables"][0]["content"]="x"
    with pytest.raises(ValidationError):
        FinalReport.model_validate(p)
    p=final_payload("r"); p["validation_plan"]=[]
    with pytest.raises(ValidationError, match="validation plan"):
        FinalReport.model_validate(p)
