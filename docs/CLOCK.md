# UTC integrity and the WSL host clock

Stage 3 diagnosed an unstable **shared operating-system wall clock**. No host
service was stopped, clock configuration changed, Windows service started, WSL
instance restarted, or machine rebooted during this investigation.

## Measured environment

Private host diagnostics are withheld from this source import. The clock design
assumes that host wall time can step; it requires independently corroborated UTC
and RAW monotonic elapsed time, with explicit uncertainty and fail-closed expiry.

## Application-scoped mitigation

`arbiq.core.clock.TrustedClock` never changes OS time. It:

1. Requests four fixed public HTTPS time endpoints concurrently, with TLS
   verification, a 5s deadline, no redirects, no-cache headers, small response
   limits and explicit rejection of cached responses and round trips above 3s.
2. Preserves source timestamps, original values, local receipt time, raw elapsed
   receipt point, source resolution and round-trip uncertainty.
3. Requires at least two independent overlapping uncertainty intervals. Ambiguous
   equal-size conflicting quorums fail closed. Repeated copies of one source do
   not count as independent evidence.
4. Requires two consistent rounds spanning at least 5s; projects UTC from Linux's
   unadjusted monotonic raw clock, with monotonic fallback on other platforms.
5. Expires external trust after 90s, caps total uncertainty at 2s and restarts the
   stability requirement after an elapsed-clock/reference discontinuity. Projection
   uncertainty grows by 100ppm between verification rounds.
6. Reports external UTC trust separately from OS `host_healthy`. Host health needs
   at least 120s observation, external trust, offset within 2s and no wall-clock
   step above 1s for 120s. It never equates a healthy instantaneous offset with a
   repaired host clock.

Call `await clock.synchronize(session)` every 30s. `clock.utcnow()` supplies the
external projection after an anchor exists, but callers **must check**
`clock.trusted`/`clock.snapshot()` before using it to verify financial freshness.
Without an anchor the UTC value is the explicitly untrusted host fallback. A
failed synchronization immediately removes trust; it does not retain an old
verified flag. Historical records retain source timestamps and clock evidence.

The existing strict book freshness limits remain intact. An externally checked
application clock can support historical attribution; unstable host time, uncertain
source timestamps, fees, inventory and trading eligibility remain separate gates.
There is no claim that an application clock repairs PostgreSQL/server-generated
wall timestamps or other applications on this shared host. Opportunity records
must use explicit calculation/detection UTC plus provenance, not `created_at` alone,
to reconstruct decisions. Live actionability remains gated while host health fails.

HTTPS exchange clocks provide independently corroborated UTC with bounded network
uncertainty. This is not NTS/PTP, submillisecond synchronization, or a long-term
host repair. Reference outages and disagreements disable verified calculations.

## Read-only diagnostics and owner-controlled host repair

Run from the repository with backend dependencies installed:

```bash
PYTHONPATH=backend backend/.venv/bin/python scripts/clock_diagnostics.py --rounds 8 --interval 15
timedatectl show --property=NTPSynchronized --property=NTP --property=Timezone
timedatectl timesync-status
cat /sys/devices/system/clocksource/clocksource0/current_clocksource
cat /proc/cmdline
```

Read-only Windows PowerShell checks:

```powershell
Get-Service W32Time
w32tm /query /status /verbose
w32tm /query /source
wsl --version
wsl --list --verbose
```

An owner/admin should first establish which host-time policy is intended and review
other WSL distributions: they can share kernel clock state. If normal Hyper-V
host synchronization is intended, verify and correct Windows UTC/time service, then
consider Canonical's documented removal of conflicting guest timesyncd. These are
**system-wide changes, not commands executed by QUANTIX**:

```powershell
# Elevated Windows PowerShell, only after reviewing the intended time policy:
Start-Service W32Time
w32tm /resync /rediscover
```

```bash
# In each affected Ubuntu WSL distribution, only after Windows UTC is verified:
sudo systemctl disable --now systemd-timesyncd.service
# Reversal if a guest-managed clock is the intended policy:
sudo systemctl enable --now systemd-timesyncd.service
```

Starting/resynchronizing Windows time can adjust the clock used by other Windows
and WSL applications. Disabling guest NTP changes the distribution's persistent
time policy and relies on verified Windows time/Hyper-V synchronization. Do not
disable synchronization blindly or force time with `date -s`. No WSL shutdown or
reboot is needed for the proposed service-policy review. Repeat external/reference
and monotonic comparisons for at least several minutes afterwards and again after
sleep/resume before declaring the shared host stable.

Technical endpoint references: [Kraken server time](https://docs.kraken.com/api-reference/market-data/get-server-time),
[Coinbase Exchange time](https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_gettime),
[Binance server time](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/general),
[OKX system time](https://www.okx.com/docs-v5/en/#rest-api-public-data-get-system-time),
[systemd-timesyncd](https://www.freedesktop.org/software/systemd/man/248/systemd-timesyncd.service.html).
