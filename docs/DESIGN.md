# QUANTIX terminal visual system

Visual thesis: a calm navy trading workspace with cyan actions, violet research
context, tabular financial figures and precise separation between work areas.
The supplied QUANTIX symbol, wordmark, login lockup and favicon assets are retained.
No old identity or acronym expansion appears in the product.

Content plan: persistent navigation and authentication; live operational context;
primary market or portfolio workspace; secondary quantitative evidence inspector.
Each section supports an operator action or explains the scope of its records.
The Command Center uses a continuous KPI strip and a dominant historical equity
plane, not a mosaic of decorative cards. Analytics and System Health use the same
surface, spacing, type and state tokens.

Interaction thesis: short workspace entrance, restrained navigation hover, smooth
mobile evidence drawers and explicit connection/reconnect states. Reduced motion
is respected; financial traces do not animate fictitious activity. Charts render
recorded values only and show an honest empty state when history is insufficient.

## Financial presentation

Financial API amounts remain exact strings. Display abbreviation uses an explicit
approximation marker and the original value in a tooltip. Chart floats are used
only for plotting coordinates; exact source strings supply financial tooltips.
No chart performs accounting, changes records or manufactures a missing mark.
Simulated legs display their native market, venue, base/quote denomination, fee
asset, observation timestamps and quality; USDT and EUR prices are never presented
as comparable unlabeled numbers.
Positive/negative figures use reserved green/coral states; cyan is the action color.
Research classification stays visible and never joins validated capital or metrics.
Switching portfolio/request scope immediately hides the previous scope's amounts;
late asynchronous trade details cannot appear under another selected session.
Zero completed trades produces unavailable win-rate statistics, with sample counts
and small-sample caveats where applicable.

## Workspaces

- Command Center combines actual connectivity, coverage, confidence, coordinator
  state, selected portfolio results, benchmark history and recent recorded activity.
- Radar prioritizes current rows. Main filters are compact; extra profit/liquidity/
  custom budget/quality controls are collapsed by default. Mobile uses summary
  cards and a full-height evidence inspector with large recheck controls. Official
  market links are displayed only when supplied by the verified backend registry.
- Paper Trading and Portfolio retain scope-separated history and a structured
  lifecycle/leg/depth/fee/ledger inspector. Funding proposals account for each asset's
  initial CAD basis, show paired inventory constraints and create new experiments.
  Opening the planner scrolls to its working surface, including on mobile. Manual
  non-CAD starting rates are required research assumptions and clear on asset change;
  CAD uses a read-only identity rate.
- Analytics supports explicit UTC period and strategy filters, recorded history,
  equity/benchmark/P&L/drawdown/outcome/expense/group views and authenticated ZIP
  downloads. Whole-session equity remains clearly separate from filtered trade cohorts.
- Settings exposes the actual versioned financial controls, market preferences
  and inactive Stage 6 notification policy. Protected financial boundaries cannot
  be switched off through an editable display control.
- System Health shows actual process/database/feed/clock/queue measurements and
  unavailable telemetry honestly. No estimated container/VPS numbers enter live UI.

## Live update boundary

The terminal uses bounded authenticated polling, not a second market-data stream.
Command Center and System Health normally refresh at15s; analytics30s; session/
configuration metadata60s. Only one poll per resource is in flight. Hidden tabs
pause work, disposed views abort requests, and reusable hooks back off after errors
with a60s maximum retry interval. Retained data remains accompanied by a reconnect
warning. Existing explorer/scanner polls are also guarded against overlapping work.
No browser performs exchange requests or replays finance calculations.

The BFF allowlists methods and paths, preserves session authentication and same-origin
writes, bounds request bodies and streams ZIP bytes directly. Downloads do not make
an extra full-size text/blob copy. Safe attachment filenames and SHA-256 headers
are retained; report credentials and private operational secrets are excluded.

Tokens and responsive rules live in frontend/src/app/globals.css. Local shadcn/ui
Button follows Radix Slot/CVA composition. Recharts is the existing plotting library;
no dependency or paid service was added for this stage.
