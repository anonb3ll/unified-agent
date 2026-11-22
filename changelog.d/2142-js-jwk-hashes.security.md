- Preserve JavaScript JWK ownership when regexp or template literals contain
hash characters. Private fields after those literals remain redacted and
blocked by the secret audit and public relay. Inspect typed literal contents
independently, retain Python comment isolation, and bound nested-template scans.

- Share lexical delimiter and trivia state across control headers, statement
  blocks, function/class bodies and template expressions so regexp contents
  cannot swallow following JWK fields.

- Keep ambiguous identifier calls and property names out of primary comment
  ownership. Alternate private evidence respects primary public-role proofs,
  including literal unpacks. Share statement starts, labels, optional catch,
  for-await headers and method-body delimiters with template interpolation.

- Bind public-role vetoes to the same source member, preserving independently
  typed private descendants inside public expressions. Retain alternate-only
  regexp evidence after line-completed debugger statements, including trivia
  and template interpolation, without replacing primary Python type ownership.

- Retain line completion across break/continue transfers, optional labels and
  JavaScript whitespace including U+FEFF in source and template walks. Keep
  this completion evidence private-only and preserve primary Python trivia
  authority separately.

- Raw text has no trustworthy source-language selector. A source-local alternate
lexical view adds only positive private spans, preserving baseline conservative
privacy for ambiguous, unsupported and malformed excerpts. This can also mask
an otherwise generic Python value; JavaScript syntax validity is not a privacy
gate. Alternate evidence does not change primary Python types, public-role
proofs, comment/source fences or oct-block ownership.

- The paired 83-case compatibility matrix retains 32 baseline conservative masks
and five unchanged public cases while protecting all 46 private cases. This
precision tradeoff is deliberate for text without trusted language metadata.

- Consume complete Unicode and escaped identifier spellings through one shared
  source/template helper, including combining marks and joiner continuations.
  Optional transfer labels retain alternate private regexp evidence without
  expanding primary Python label authority. Incomplete escape extents stay
  bounded to local identifier text; no semantic validity check or decode layer
  is added.

- Keep UNKNOWN identifier tails and malformed escape braces source-owned in
  both lexical walks. An escape's uncertain punctuation stays opaque and its
  leftover close cannot discard the containing private candidate. Actual
  source delimiters still advance normally; private recovery ends at the
  containing owner or physical template fence. No UNKNOWN token grants
  primary comment, keyword or public-role authority.

- Keep nested escape braces separate from their containing private owner, and
  retain escape-owned quote/backtick punctuation across discovery and private
  role scanning. A literal interruption cannot erase local private evidence or
  pair with a real template/source fence; primary public/unpack and comment
  authority remain unchanged.

- Preserve each unfinished escape opener, including nested openers consumed in
  one identifier extent or encountered after opaque quotes. Each close releases
  only its escape owner in both source and template walks, retaining the real
  mapping and physical source fences for later private members.

- Bind template fences to the preceding delimiter event and its interpolation
  owner. A synthetic escape close inside a real source frame leaves enclosing
  escapes pending across opaque quotes; ragged physical template boundaries
  still release only their own interpolation before repeated or nested content.

- Keep after-backtick closes with their pending escape owners before proving a
  physical template fence. Following punctuation cannot turn a synthetic close
  into a source boundary. Local unknown-token evidence also survives an opaque
  transfer expression, without publishing primary comment or public authority.

Retain pending escape ownership across trivia and punctuation after opaque
quotes before accepting a physical template fence. The source and template
walks share local closer classification, preserving private continuations and
real source cleanup without changing primary public proofs or decode limits.

- Keep quoted-expression braces and backticks inside their own lexical extent
  after punctuation and comments, including real call frames. Pending escape
  punctuation stays opaque until its own close; source and template discovery
  share the same bounded continuation frontier.
