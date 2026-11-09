# Ideas parked for after the dispatcher

Ideas that are worth building but are not part of the current goal. Recorded here so they
are not lost, with enough detail that the interesting problem is visible before anyone
starts.

---

## Controller-side dispatch: "where should I control?"

**The pitch.** A controller opens the page, picks their division (VATSIM UK, VATSIM US, or
anywhere), and the program names the position most worth opening right now — or at a time
they choose: "next Tuesday 19:00Z, open EGTT_S_CTR, there is usually a 340 nm gap between
London and Amsterdam and nobody covers it."

**Why it is not just the dispatcher reversed.** The dispatcher asks "given the network,
where should I fly?". This asks "given the network, where should I *be*?", and the second
question is much harder because of one thing:

> **Pilot demand is endogenous to ATC supply.** Most pilots choose where to fly *because* a
> controller is online. So the historical demand for a position is partly manufactured by
> that position having been staffed. "Nobody flies there" often means "nobody ever staffs
> it", not "nobody wants to go there".

That is the whole problem. Naively ranking positions by "traffic that had no ATC" will
recommend positions that were quiet *because* they are unattractive, and it will
under-recommend positions that would be busy if only someone opened them.

**What makes it tractable anyway.** The data we already collect contains three of the four
things needed:

1. **Filed intent.** Every flight plan declares `departure` and `arrival` before it departs,
   and before any controller acts on it. Intent is recorded *upstream* of staffing. The
   ratio of intent to staffed positions, per FIR-hour, is the closest thing to an
   unconfounded demand signal we have.
2. **Coverage history.** We know exactly which positions were online and when, so we can
   condition demand on supply instead of confusing the two.
3. **Unstaffed traffic.** For any hour we can count aircraft that crossed a FIR with no
   controller on frequency. That is a flow, not a preference, but it is a real lower bound
   on latent demand.

The fourth thing — **where pilots want to go but do not, because it is never staffed** — is
not observable. It has to be inferred, and the honest way is to model demand as a function
of route popularity *conditional on* the coverage of the airports at both ends, so the
unstaffed corners can be extrapolated rather than assumed empty.

**A minimal version that would be worth using tomorrow.**

- Rank *candidate positions* (not airspace) by expected pilots-per-hour-on-frequency:
  `expected_traffic(position, hour) × fraction_of_that_traffic_currently_uncontrolled`.
- Estimate `expected_traffic` from filed flight plans crossing the position's boundary,
  conditioned on the airports at each end, not from past controller presence.
- Show the second-order effect explicitly: "opening this would also serve these three
  airports' departures, and it is the missing piece between two staffed centres" — the
  network effect is the interesting part, not a single position's traffic.
- For a future time, same model at a different hour, with the confidence badge from the
  dispatcher, because a controller deciding next Tuesday needs to know how thin the
  evidence is.

**Open questions.**

- Does a controller want a *position* or a *shift*? "Open EGTT_S_CTR for three hours" is a
  different optimisation from "open it now" — coverage gaps have a duration, and a
  two-hour gap that nobody can fill is worse to recommend than a one-hour gap somebody can.
- Is the right objective *coverage* (minimise aircraft without ATC) or *experience* (maximise
  the number of pilots who get service)? They differ: covering a busy hub for an hour may
  beat covering a whole FIR with the same number of aircraft.
- Cross-border positions make the "domain" choice fuzzy — a UK controller can cover the
  North Sea, and a Eurocontrol-style FIR spans several divisions.
- The dispatcher's fleet/aircraft machinery is irrelevant here; the analogue is the
  controller's **rating and endorsements**, which VATSIM already models but we do not
  collect. Scraping it is out of scope; letting the controller select their endorsements is
  not.

**Why it fits this codebase.** The ingestion, sessionisation, rollup and forecasting layers
are already position-aware: a controller position *is* the unit the whole model is built on.
The model would gain a second query — "who should be online" — over the same materialised
matrix, and the site would gain a second mode over the same bundle.
