# Interactive ChatGPT Web fallback

`prepare` writes `planner-prompt.md`; the user can paste it into a fresh ChatGPT Web chat and import the JSON result with `planner-import`. The engine then creates `ContextPack` + `final-prompt.md`; import the final structured JSON with `final-import`.

This path uses the same validators and state machine as automatic mode and is useful when live browser selectors need repair.
