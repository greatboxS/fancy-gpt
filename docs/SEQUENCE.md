# Sequence

```text
Caller
  ↓ skill OR workflow + domains + focus
RequestClassifier / CapabilityResolver
  ↓ RoutingDecision
Online planner
  ↓ ResearchManifest
ContextBuilder
  ├─ allowed-root security
  ├─ independence filtering
  ├─ symlink/generated-tree pruning
  ├─ budget/provenance
  └─ required-context gate
  ↓ ContextPack
Final prompt compiler
  ↓
Final online model
  ├─ execute/cross-check research
  ├─ evaluate untrusted evidence
  └─ produce structured report
  ↓
Final validators
  ├─ mode contract
  ├─ required sections
  ├─ research trace
  └─ evidence coverage
  ↓
COMPLETE
```
