# Cache usage accounting

New model calls retain reported cached/uncached input and reasoning token details,
plus their research stage. Cached input stays part of the existing total budget;
it is never added again or treated as a free allowance. Unsupported, inconsistent
and historical cache reports remain unknown. The usage page shows that coverage
and calculates cache hit rate using only inputs with known cache details.

The additive migration preserves past outcomes and token totals. Existing API
consumers remain compatible. Offline SDK/ledger, migration and browser regression
checks cover both reported and missing details, stage isolation, unchanged budgets,
filters and first-page transitions. No paid API calls or live schema updates were
performed. This module supplies accounting; the new research quality pipeline is
a separate implementation and is not declared complete by this integration.
