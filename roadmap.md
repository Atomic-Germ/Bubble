Updated todo list

I’ll patch the status-label logic so `chatting` shows a friendlier `Second Voice` label during consult/integration phases, then run a quick test/lint check for regressions. After that I’ll share a short list of psych/neuro-inspired ideas you can evaluate.

Read [](file:///home/atomic-germ/Projects/Code/Bubble/gnome-extension/bubble%40bubble/extension.js#770-770), lines 770 to 830

Generating patch (31 lines) in [](file:///home/atomic-germ/Projects/Code/Bubble/gnome-extension/bubble%40bubble/extension.js)


```

```
I’ve applied the label refinement; I’m running a quick error/test check now to ensure this UI-only change is clean.

Ran terminal command: pytest tests/test_dbus_methods.py tests/test_supporting_logic.py -q

Checked [](file:///home/atomic-germ/Projects/Code/Bubble/gnome-extension/bubble%40bubble/extension.js), no problems found

Ran terminal command: rg -n "_setStatus\(|Second Voice|Synthesizing|Connecting" gnome-extension/bubble@bubble/extension.js

Made. The status chip now shows friendlier labels during the second-voice phases.

Change made
- Updated status label mapping in extension.js:
1. chatting + consulting second voice → Second Voice
2. chatting + integrating second voice → Synthesizing
3. chatting + waiting for daemon → Connecting

Validation
- No editor errors in extension.js.
- Tests passed: 149 passed (daemon/supporting suites).

Psychology/neuroscience-inspired ideas worth exploring
1. Cognitive Conflict Monitor:
Use second voice selectively when the primary model shows uncertainty markers or internal contradiction. Inspired by ACC conflict detection and metacognitive control.

2. Working Memory Gating:
Give second voice a strict token budget and role-specific slots (goal, risk, alternative). This mirrors executive-function gating and reduces ruminative drift.

3. Reconsolidation Step:
After second-voice synthesis, require one short “memory update intent” sentence before writing durable memory. Modeled on memory reconsolidation windows to avoid overfitting one exchange.

4. Interoceptive State Signal:
Add a lightweight internal state scalar (load, novelty, confidence) that biases whether to consult second voice. Comparable to arousal/uncertainty regulation in predictive processing.

5. Dual-Process Routing:
Default to fast System-1 style response, then trigger System-2 second voice only on high novelty, high stakes, or identity-relevant turns.

6. Error-Learning Loop:
Track when second-voice revisions materially improve final responses, then adapt trigger thresholds over time (simple reinforcement signal).

