# UTEM checks after repairs — 2026-10-29

Use the logger's 30-second configuration. The supplied helper is 0.4.2, BLOCKING=True.

## Before instrument validation

1. Preserve the installed files and stop the state logger and its camera server.
2. Stop the old helper in DM. Replace the Python helper and reopen its script window;
   a previously open window retains its loaded source. Install only one startup launcher.
3. Start TEMCenter and DM normally, then the helper and logger. Do this during an agreed idle
   instrument period, not during a sensitive acquisition. Do not change microscope settings
   simply to reproduce a failure.
4. Save logger.log and DM Output, and record wall-clock times of any audible beeps.

## Read-only observations

- Confirm logger.log says every 30.0 s and the helper reports version 0.4.2.
- Check --dm-status only when no extra state logger process is being started: the command queries
  the helper, not PyJEM. Expected live=true means the Orius window is open, even if stopped.
- If PyJEM times out, look for keeping worker; no reconnect. Further retries must not produce
  additional connection messages while the previous call is still active.
- A completed late read must be discarded, followed by a fresh observation on a later retry.
- Collect at least 10 minutes of logs to cover more than two of the historical ~245-second cycles.
- Check the NAS state.jsonl and frame metadata for actual state/frame output. Slow rotating getters
  are not refreshed every 30 seconds; their full rotation takes about 6.5 minutes.

## Do not use obsolete diagnostic scripts for this validation

check_frame_listener.py is historical. The supplied helper records that a data-changed listener
crashed this instrument's GMS 3.4.4; do not run that check in this workflow.
check_python_thread.py also makes DM calls from a background Python thread, contrary to the current
helper's default. It is not required for these repairs. Tag inspection is separate from this patch.

No result from the simulator proves microscope compatibility or identifies the sound source.
If a native call stays blocked forever, thread shutdown cannot interrupt it; inspect the logger
process rather than starting repeated copies.
