# ChatGPT Web automation

`ChatGPTWebAutomationProvider` talks only to the `BrowserDriver` contract.

Implementations:

- `FakeBrowserDriver`: deterministic offline UT/integration.
- `PlaywrightChatGPTDriver`: real local browser adapter prepared for workstation verification.

The real driver uses a tool-owned persistent profile, fresh Temporary Chat surfaces, logical `data-turn-id` response binding, single-submit behavior, bounded prompt size, timeout, and fail-closed UI drift handling. Cross-process profile ownership uses an OS file lock.

It does not import external browser cookies/tokens and does not call undocumented ChatGPT endpoints.

Offline simulation:

```bash
uv run fancy-gpt simulate-auto examples/requests/qos-review.yaml \
  --skill technical-review \
  --planner-response examples/planner-result.example.json \
  --final-response examples/final-result.template.json \
  --workdir /tmp/fancy-gpt-sim --allowed-root .
```

Live selectors/login/session compatibility must be smoke-tested locally with the user's own account.
