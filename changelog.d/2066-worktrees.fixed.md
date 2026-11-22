- Review routing uses recorded product checkout provenance to prevent an unmarked task
from inheriting cockpit evidence. Stale-worktree cleanup also discovers known product
repositories and removes disposable candidates through their owning Git repository.
- Stale-worktree cleanup retains idle checkouts with live task claims and keeps dirty
  or unmerged work. Delegation warnings now describe project-based routing accurately.
