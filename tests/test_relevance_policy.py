from __future__ import annotations

import json

from fancy_gpt.browser import FakeBrowserDriver
from fancy_gpt.focused import FocusedAnswerEngine, FocusedQuestion
from fancy_gpt.models import RawRequest, RequestMode
from fancy_gpt.planner import PreRequestPlanner
from fancy_gpt.providers import ChatGPTWebAutomationProvider
from fancy_gpt.relevance import ExpansionTrigger, ResponseIntent, semantic_policy_text
from fancy_gpt.routing import RequestClassifier


def test_semantic_policy_has_no_numeric_output_ceiling() -> None:
    question = FocusedQuestion(question="Is this pin read-only?", response_intent=ResponseIntent.REFERENCE)
    text = semantic_policy_text(question.principles, question.response_intent)
    assert "Stop when" in text
    assert "actual question" in text
    assert "200" not in text
    assert "max_words" not in text


def test_planner_receives_scope_and_relevance_contract() -> None:
    request = RawRequest(mode=RequestMode.REVIEW, objective="Review only ownership of the ADC input", focus=["ownership"])
    route = RequestClassifier().classify(request, skill_name="technical-review")
    prompt = PreRequestPlanner().build_prompt("req-1", request, route)
    assert "RELEVANCE & SUFFICIENCY POLICY" in prompt
    assert "scope_contract" in prompt
    assert "Do not broaden" in prompt


def test_focused_answer_is_one_pass_and_accepts_material_expansion() -> None:
    engine = FocusedAnswerEngine()
    driver = FakeBrowserDriver([
        lambda turn: json.dumps({
            "request_id": turn.request_id,
            "answer": "No. It is an ADC input and should be read, not driven.",
            "material_context": ["Treating it as an output would be electrically incorrect."],
            "unknowns": [],
            "next_action": None,
            "sources": [],
            "confidence": 0.98,
            "relevance_assessment": {
                "within_requested_scope": True,
                "necessary_expansions": [{
                    "topic": "electrical direction",
                    "trigger": ExpansionTrigger.CORRECTNESS.value,
                    "rationale": "It determines whether software may drive the pin."
                }],
                "omitted_non_material_topics": ["ADC theory"]
            }
        })
    ])
    provider = ChatGPTWebAutomationProvider(driver)
    answer = engine.run(FocusedQuestion(question="Do I only read SARADC_VIN2_DC?"), provider)
    assert answer.answer.startswith("No.")
    assert len(driver.prompts) == 1
    assert driver.prompts[0][1] == "focused"


def test_focused_prompt_explicitly_rejects_broad_review_behavior() -> None:
    request = FocusedAnswerEngine().build_request(FocusedQuestion(question="What owns field X?"))
    assert "NOT a request for a broad review or tutorial" in request.prompt
    assert "Do not manufacture alternatives" in request.prompt


def test_focused_answer_propagates_conversation_binding() -> None:
    engine = FocusedAnswerEngine()
    driver = FakeBrowserDriver([
        lambda turn: json.dumps({
            "request_id": turn.request_id,
            "answer": "Continue from the existing implementation thread.",
            "material_context": [], "unknowns": [], "next_action": None,
            "sources": [], "confidence": 0.9,
            "relevance_assessment": {"within_requested_scope": True, "necessary_expansions": [], "omitted_non_material_topics": []}
        })
    ])
    provider = ChatGPTWebAutomationProvider(driver)
    from fancy_gpt.project_models import ConversationStrategy
    answer = engine.run(FocusedQuestion(
        question="What is the next relevant step?",
        conversation_strategy=ConversationStrategy.RESUME,
        conversation_binding="project:demo:role:implementer",
    ), provider)
    assert answer.conversation_binding and answer.conversation_binding.startswith("fake-conv-")
    # A logical placeholder binding is not a resumable ChatGPT thread, so RESUME
    # opens a new persistent one and reports back the id that can be resumed next time.
    assert ("conversation", "persistent", "") in driver.events
