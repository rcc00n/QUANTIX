# QUANTIX paper accounting and simulation

Stage4 is strictly virtual. No order-placement client, exchange trading key, paid
provider or real-trade writer exists. ActualTrade remains entirely separate.

## Capital and scenario isolation

The default validated portfolio contributes C$10,000 cash, split equally between
Kraken and NDAX virtual accounts. It cannot trade merely because public data works:
fees, provincial/account eligibility, funding and host-clock conditions remain
independent gates. Current Stage3 actionable=false routes therefore cannot become
validated trades.

A separate, explicitly hypothetical research portfolio has its own C$10,000 initial
capital. It is not added to the validated portfolio. When trusted observed BTC/CAD
marks are available, half its capital is contributed as priced BTC inventory and
half as CAD across Kraken/NDAX. Crypto quantities times the initialization rate
consume that capital; remaining fractional CAD stays cash. These are initial
inventory contributions, not fabricated exchange purchases. If evidence is missing,
research initialization waits. Other assets and allocations can be supplied through
the private portfolio API; assumed prices require research classification.

Classification cannot be changed after funding, including by direct database
updates. To change starting equity or switch between validated and research modes,
create another portfolio. Existing history is preserved. Each simulation pins its
immutable StrategyVersion and original settings; editing a strategy affects later
candidates, not already reserved trades.

