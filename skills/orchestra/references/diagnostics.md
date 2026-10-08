# Activation and review evidence

Use the skill's bundled launcher for these commands. An activation audit checks
public tool events for a read of the specified `SKILL.md` and its returned
Orchestra header. Assistant prose mentioning the skill does not prove activation.

```text
activation SESSION.jsonl --skill-path /absolute/path/to/orchestra/SKILL.md
fingerprint PROJECT --path src/app.py --path tests/test_app.py
fingerprint PROJECT --check REVIEW-SNAPSHOT.json
```

Activation reports distinguish attempted reads, observed content, and reads
with a successful process exit. Missing outputs, truncated logs, and unsupported
formats limit the conclusion. The command exits zero only for exit-attested
activation. It also reports repeated reads for investigation; overlap can be
intentional after a truncated output or a changed file. It omits command bodies,
chat content, and reasoning. Model metadata describes configured models.

Save the fingerprint JSON beside review evidence. It hashes the exact listed
files and records missing files. Recheck immediately before accepting a review;
any content change, creation, removal, or unreadable file invalidates that
snapshot. A fingerprint proves which files the evidence covers. The root still
checks the review verdict, scope, and required verification.

These diagnostics run locally and submit no inference requests. They neither
force skill selection in a host nor certify a model's identity or billing.


The global bootstrap contains the canonical workflow body. When that body is
already present in host instructions, do not reread unchanged `SKILL.md` just to
produce a file-read audit. That audit does not measure host context delivery.

For native hooks, use `install-hooks` and `install-hooks --check`. The latter
checks local configuration only and reports runtime trust as unknown. Inspect
Codex `hooks/list` or `/hooks` for actual trust. `UserPromptSubmit` delivers the
body on each submitted prompt; a compact `SessionStart` restores it before the
next model request. These handlers verify the pinned skill hash and stop their
supported continuation on invalid source. Installations and upgrades can need
a new exact-definition review through `/hooks`; never forge trust state or use
a trust-bypass flag to describe unattended automatic activation as verified.

Host context delivery and model compliance are separate claims. Preserve higher
and nearer instructions. The native executor's leaf environment marker adds a
bounded-role hint; it never suppresses the canonical context or proves identity.
