# Agent Friction Log

The evidence base for editing `CLAUDE.md`, `docs/agents/*`, and `.claude/`.

The rules in this repo are strict on purpose — results may feed publications, and
most of them exist because something went wrong once. That makes them expensive
to change casually and expensive to leave wrong. This file is how a change gets
made on evidence rather than on the feeling that there is a lot of process.

## Why a log rather than a conversation

Friction is felt in the middle of a task and gone by the end of it. By the time a
session is being summarised, the moment where a rule sent the work sideways has
been smoothed over — either forgotten, or rationalised into "that was fine
actually". Both are lossy in the same direction, so the rule that fired wrongly
looks fine in retrospect and never gets edited.

Nobody can edit a rule from "the reporting rules feel heavy". They can edit one
from "on 2026-09-03 the cosmetic-findings rule left a real defect with nowhere to
go, so it was smuggled into an unrelated issue's acceptance criteria". The first
is a mood; the second names the sentence to change and shows what it cost.

The log also makes the *third* occurrence visible. One entry is an anecdote and
should usually change nothing. Three entries against one sentence is a finding.
Without a written record, every occurrence is the first one.

## Write an entry when

- **`misfire`** — the rule fired on a situation it was not written for, and
  following it produced a worse outcome than ignoring it would have.
- **`gap`** — no rule fired and one should have. A mistake was made that a rule
  could have caught, or a rule turned out to have a hole in it.
- **`ambiguous`** — the rule was followed, but only after reading it more than
  one way, or it was applied more broadly or narrowly than it says. This is
  evidence about the *sentence*, not about the rule.
- **`save`** — the rule fired, following it was inconvenient or you were tempted
  to skip it, and it was right. Log these. A file containing only complaints
  reads as an argument for fewer rules whatever it actually says, and the
  strongest reason to keep a rule is a recorded near-miss.

Write it **in the session it happens**, not at the end of it. If you notice late,
write it anyway and say the entry is reconstructed — a reconstructed entry is
weaker evidence and the reader should know which kind they are holding.

## Do not write an entry when

- The rule cost nothing you can name in a sentence. Disagreeing with a rule you
  followed without incident is not friction.
- The work was simply hard. A rule is not responsible for the difficulty of the
  thing it governs.
- You skipped the rule. That is a deviation to report in the session, not an
  entry — **unless** following it would have been wrong, in which case it is a
  `misfire` and the entry says plainly that the rule was not followed.
- You want a rule relaxed and are looking for grounds. Entries are written from
  incidents, and the incident comes first.

## Entry template

```markdown
### YYYY-MM-DD — one-line title naming the rule and what it did

**Rule:** file and section, with the sentence that fired quoted.
**Mode:** misfire | gap | ambiguous | save
**Situation:** what was being done when it fired. Enough that a reader who was
not there can tell whether their case is the same case.
**What it made me do:**
**What I would have done otherwise:**
**Cost:** concrete — a duplicated computation, a round trip, a wrong artifact, a
decision made on less than the available evidence. If the cost cannot be named,
there is no entry.
**Status:** open
```

## Two things an entry must not do

**It must not propose the fix.** An entry that ends with a suggested rewrite
turns the log into a change queue: the first proposal gets applied, the sentence
moves, and the second and third occurrences are never recorded against it. Keep
the evidence and the remedy in separate places. Proposals belong in the PR that
cites the entries.

**It must not soften.** "This was probably fine, but…" is not evidence. Either
the rule cost something nameable, in which case name it flatly, or it did not, in
which case there is no entry. Hedging is how a log fills up with material nobody
can act on.

## When a rule actually gets edited

The bar, so that the log is not read as a to-do list:

- **three open entries against the same sentence**, or
- **one entry whose cost shipped** — a wrong artifact reached `main`, an issue,
  or a pushed branch.

`ambiguous` entries are answered by rewording, never by removal. An agent reading
a rule two ways is evidence that the sentence is unclear; it says nothing about
whether the rule is right.

The edit is its own PR, scoped `agents`, citing by date the entries it acts on,
and marking each one `Status: resolved by #N` in the same commit. **Entries are
never deleted, and resolved ones stay in place** — a rule that was loosened and
then had to be tightened again is exactly what the next reader needs to see.

## Where the operator's standing signals fit

`CLAUDE.md` ("Reporting work") names two signals from the operator — **"too
long"** and **"why did you stop"**. Those are this instrument driven from the
other side. When one fires, write the entry as well as fixing the report: the
signal says a rule misfired once, and the log is what makes the third time
visible.

## The repo is public

The rule in `docs/agents/issue-tracker.md` binds this file as hard as it binds an
issue body. An entry describes a rule and a workflow, so it should rarely come
near an unpublished result — but a `Situation:` line naming what was being
measured can. No unpublished results, no site coordinates that are not already in
`data/registry/sites.json`, nothing embargoed.

---

## Entries

### 2026-09-03 — the triage/implementation boundary sent an agent work the triage session had already done

**Rule:** the `triage` skill, "Apply the outcome" — a `ready-for-agent` outcome
posts an agent brief and stops there; implementation happens later, on a branch.

**Mode:** misfire

**Situation:** triaging PRD #162, which asked four design questions about the lag
screen's kelp half. Answering two of them well enough to recommend a state meant
loading the comparison table, verifying its digest, and computing the per-bed
shape and transform tables that now sit in the triage comment.

**What it made me do:** file
https://github.com/cweber12/kelp-compare/issues/164 as a specification for an
agent to compute, inside the notebook, the table that had just been computed ad
hoc in order to write the specification.

**What I would have done otherwise:** carried straight on into the notebook
change on a branch, since the measurement was done and only needed to be made
reproducible.

**Cost:** the six-bed shape table is computed twice, and the second computation
is by an agent working from a description of the first rather than from the first
itself. Not pure waste — the notebook version has to run against a digest and the
scratch version does not — but the boundary fell in the middle of one piece of
work rather than between two.

**Status:** open

### 2026-09-03 — docs/04 §5 read as forbidding a computation it only forbids choosing on

**Rule:** `docs/04-analysis-methods.md` §5 — "The rule is not restated on that
evidence, and could not honestly be. The list each scale returns is now visible,
so picking the scale from it would be picking a rule by the answer it gives".

**Mode:** ambiguous

**Situation:** PRD #162 asks whether §4.1 should rank on Spearman rather than
Pearson. The table was loaded and the alternative ranking was one query away.

**What it made me do:** decline to compute it, and tell the operator the question
could not be settled during triage.

**What I would have done otherwise:** computed it and put it on the issue. §5
forbids *restating the rule* on such evidence; it does not forbid producing the
evidence, and `01-lag-screen.ipynb` §7 already prints two alternative-scale
rankings as a standing sensitivity for exactly that reason.

**Cost:** the operator is deciding the Spearman question with less on the table
than the documented rule permits. There is a real second reason not to have put
it in a triage comment — a ranking printed outside the notebook is not
reproducible against a digest — but that is a different reason than the one
given, and it points at extending §7 rather than at silence.

**Status:** open

### 2026-09-03 — the cosmetic-findings rule left a real defect with nowhere to go

**Rule:** `CLAUDE.md`, "Reporting work" — "If it is cosmetic, or you are not
confident it is real, say so in prose and file nothing."

**Mode:** misfire

**Situation:** `01-lag-screen.ipynb` carries two sections numbered `## 8`, and
`notebooks/README.md` indexes the reading figures under §8. Real, checkable, and
about two minutes of work — but cosmetic by any reading of the word.

**What it made me do:** mention it in prose, file nothing, and then put it into
the acceptance criteria of
https://github.com/cweber12/kelp-compare/issues/164, an issue about something
else.

**What I would have done otherwise:** filed a two-line issue and left #164 alone.

**Cost:** #164 now carries a criterion outside its own concern — the one-concern
rule bent to route around the no-filing rule. Small, but it is the shape of a
defect getting lost: the next cosmetic finding has the same two bad options.

**Status:** open

### 2026-09-03 — the plan-mode gate held on a transform that would have moved every published coefficient

**Rule:** `CLAUDE.md` — "Any change that touches the observation schema, a
storage zone, or a feature definition: propose in plan mode first."

**Mode:** save

**Situation:** measuring PRD #162 showed that a square-root transform applied to
`kelp_area_m2` before the climatology improves the anomaly's shape on all six
beds and overshoots on none, while `log1p` over-corrects badly where beds are
rarely empty. The measurement was convincing and the change is a few lines.

**What it made me do:** stop, and hand the decision to the operator.

**What I would have done otherwise:** implemented it on the strength of the
measurement.

**Cost:** none, and the gate was right for a reason the measurement could not
see. The transform changes the anomaly definition in `docs/03` §3, moves every
anomaly in the features zone, and moves every coefficient in the pre-registered
list — including three signals registered against a named digest. It also turns
on a question that is ecological rather than statistical: whether a zero-canopy
quarter and a heavily-thinned one should sit as far apart as the linear scale
puts them.

**Status:** open

### 2026-09-03 — the plan-mode gate caught a table that would have mislabelled what it measured

**Rule:** `CLAUDE.md` — "Any change that touches the observation schema, a
storage zone, or a feature definition: propose in plan mode first and update the
matching doc in the same PR."

**Mode:** save

**Situation:** designing how to get more out of a single logger deployment. The
plan proposed three new tables in the features zone, one of them an hour-of-day
"diel composite" over `deployment_daily`, on the reasoning that a 600 s logger
must carry a solar signal the deployment mean erases.

**What it made me do:** write the design down and hand it over before building
any of it, which is what produced a verification step that read the ten-minute
series once before the schema was fixed.

**What I would have done otherwise:** built all three tables. I was ready to
leave plan mode twice before that verification step existed; it came out of the
operator asking what would strengthen the implementation, not out of my own
discipline. That is the part worth recording — the gate held because it is a
gate, not because I used it well.

**Cost:** none, and it caught a real error. An hour-of-day table would have
mislabelled its own contents in both directions: at 8.23 m the composite peaks at
20:00 and troughs at noon local, which solar heating cannot do, and at 16.76 m
the record is dominated by the semidiurnal band, so folding it onto a 24-hour
axis is an aliasing artifact rather than a measurement. A column named for a diel
cycle would have reached the features zone and `docs/03` before anything
contradicted it, and the reading it invited is the kind nothing downstream
questions.

**Status:** open
