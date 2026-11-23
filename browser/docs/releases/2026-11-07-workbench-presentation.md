# Workbench display and first-visit transitions

Removed the connection badge and visible internal task, document, approval,
request and submission identifiers. Plans and execution history use readable
step/entry numbers. Export filenames use the report title or file name.
Internal identifiers, submission deduplication, approval guards and download
integrity checks remain in use.

Page entry now waits for both lazy code and initial query results. Each route
reveals its content once; filters, polling and manual data refreshes do not replay
the animation. Reduced-motion preferences remain supported.

Verification: production build and TypeScript passed; 113 unit tests, 50 mocked
browser tests and six isolated backend/browser resource and approval tests passed.
The first-visit regression holds code and data separately beyond the animation
duration and checks that the actual content animates after release. UUID coverage
includes tasks, plan dependencies, child history, document citations, query errors,
approval dialogs and unconfirmed submissions. The running local workbench was
also inspected read-only. No paid provider calls were made.
