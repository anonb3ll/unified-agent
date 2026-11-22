- Keep environment isolation for the pure redactor matrix while refusing database
  access instead of initializing SQLite for every lexical case. Stateful tests
  continue to initialize their real isolated stores by default.
