# Bench machine

The Windows/WSL machine that runs Load Insights scorecards. Use rules are in tests/AGENTS.md.


**Machine:** Ryzen 9 7950X 16C/32T, 64 GB. Windows with WSL2
Ubuntu 24.04 (.wslconfig: 48 GB, 32 processors). Set up 2026-10-04.

**SSH:** `ssh libench` (ssh alias, Windows user `<user>`, admin). Windows OpenSSH with
DefaultShell = C:\Windows\System32\bash.exe, so a session lands in WSL bash. It starts in /mnt/c/Users/<user>: always
use ABSOLUTE paths. Windows' PATH breaks inline quoting: send scripts with `ssh -T libench bash -s <<'EOF' ... EOF`.
Filter the harmless "post-quantum / store now / upgraded" banner lines.

**Bench layout:**
- Python 3.14.7 via uv at ~/.local/share/uv/python/cpython-3.14.7*/bin/python3.14.
- History (no tokens - never copy data/<host> token files) at /home/<user>/li-bench/Load_Insights/data/history (ext4; /mnt/c is
  several times slower).
- A git clone for the owner at /mnt/c/Users/<user>/Documents/Load_Insights (next to Dynamic_OCPP_EVSE), data/history symlinked to the
  ext4 copy.
- Runner /home/<user>/li-bench/card.sh TREE LABEL PAR; Mac-side helper (session scratchpad) remote_card.sh LOCAL_TREE LABEL PAR
  rsyncs a tree to /home/<user>/li-bench/runs/LABEL, runs the card and copies scorecards back.

**Measured:** a full card is ~324 s there vs 10-14 min on the Mac (the longest replay sets the length, so >16 parallel adds
little). Results are identical to the Mac's, digit for digit.

**Memory limit:** a Home replay takes ~2.5 GB; WSL has 47 GB. Keep the SUM of jobrun --slots across all jobs on the
machine <= 24 (two 16-slot jobs OOM-killed the WSL session and every card, 2026-10-04 10:53). Two jobs at once: --slots 12 each.

**LM Studio:** LM Studio serves an OpenAI-compatible API on port 1234 (open on the LAN, no auth). Models include
google/gemma-4-26b-a4b-qat, qwen/qwen3.6-35b-a3b, qwen/qwen3.8-27b, qwen/qwen3.5-9b.

**opencode:** opencode 1.18.34 in WSL (~/.opencode/bin), provider "lmstudio", configured for those models. Use
`opencode run -m lmstudio/google/gemma-4-26b-a4b-qat "..."` to digest logs locally at zero Claude tokens. Gemma read a card
log correctly. Use it for digesting, never for keep/drop decisions or code.

**Also available:** a server-core i7 with 32 GB (2026-10-04) - not set up; only worth it for big batch queues.

**Reaching it (2026-10-09):** when it is off, the owner may say to wake it: a Wake-on-LAN button in Home HA
WSL answers ~35 s later. The Mac may fail to resolve `the bench PC`:
use `ssh` with the bench PC's LAN address as HostName and its host key alias (rsync `-e` the same). The Mac-side
remote_card.sh lived in the session scratchpad and macOS /tmp cleanup removed it - the runner on the machine
(/home/<user>/li-bench/card.sh TREE LABEL PAR) is what persists: rsync a tree to runs/LABEL (data/ excluded; card.sh links
the history) and run it there.
