# UTC integrity and the WSL host clock

Stage 3 diagnosed an unstable **shared operating-system wall clock**. No host
service was stopped, clock configuration changed, Windows service started, WSL
instance restarted, or machine rebooted during this investigation.

## Measured environment

Private host diagnostics are withheld from this source import. The clock design
assumes that host wall time can step; it requires independently corroborated UTC
and RAW monotonic elapsed time, with explicit uncertainty and fail-closed expiry.

