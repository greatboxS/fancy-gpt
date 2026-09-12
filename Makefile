.PHONY: test schemas compile functional release-gate simulate-auto

test:
	PYTHONPATH=src pytest -ra

schemas:
	PYTHONPATH=src python scripts/export_schemas.py

compile:
	python -m compileall -q src tests scripts

functional:
	PYTHONPATH=src python scripts/functional_review.py

release-gate:
	PYTHONPATH=src python scripts/release_gate.py

simulate-auto:
	PYTHONPATH=src python -m fancy_gpt.cli simulate-auto examples/requests/qos-review.yaml \
		--skill technical-review \
		--planner-response examples/planner-result.example.json \
		--final-response examples/final-result.template.json \
		--workdir /tmp/fancy-gpt-sim \
		--allowed-root .
