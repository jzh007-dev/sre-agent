"""srectl — the CLI.

Named `srectl` rather than `alertctl` because alert is one entry mode of three
(see [TRADEOFFS §25](../TRADEOFFS.md#25-trigger-registry-alert-is-one-entry-mode-of-three)).

Subcommands land with the lesson that needs them:

- `smoke`   — W2 L3b: one live call per credentialled provider, verify usage and cost
- `prices`  — W2 L3b: verify the price table against the provider's own invoice
- `trigger` — W2 L7: run a golden case end to end
- `chat`    — post-W5
- `patrol`  — deferred ([§31](../TRADEOFFS.md#31-patrol-stays-a-stub-until-its-value-proposition-is-settled))
- `replay`  — W2 L7: rebuild `messages` from an investigation's JSONL log
"""
from __future__ import annotations

import sys

USAGE = """usage: srectl <command> [options]

commands:
  smoke     one live call per credentialled provider; verify usage + cost   (W2 L3b)
  prices    verify the price table against the provider's billing CSV export (W2 L3b)
  trigger   run a golden case end to end                                   (W2 L7)
  replay    rebuild an investigation from its JSONL log                    (W2 L7)
  chat      interactive session                                            (post-W5)
  patrol    scheduled inspection                                           (deferred)
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    command, rest = args[0], args[1:]

    if command == "smoke":
        from .commands.smoke import main as smoke_main

        return smoke_main(rest)

    if command == "prices":
        from .commands.prices import main as prices_main

        return prices_main(rest)

    if command in ("trigger", "replay", "chat", "patrol"):
        print(f"`srectl {command}` is not implemented yet — see docs/ROADMAP.md")
        return 2

    print(f"unknown command {command!r}\n\n{USAGE}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
