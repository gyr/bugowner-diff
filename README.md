# bugowner-diff

Reports who owns a package on each side of a SUSE product transition, and where the two sides
disagree.

The **15** side is an OBS project — its package listing and the OBS owner search. The **16** side
is the SLFO `_maintainership.json` document at a git ref. Given a file of package names, the tool
emits one CSV row per name: the owners each side names, and the change that follows from
comparing them.

## Requirements

- Python 3.13 or newer, and [uv](https://docs.astral.sh/uv/).
- **`osc` on `PATH`, already configured for `api.suse.de`.** Both OBS queries are made by running
  `osc -A https://api.suse.de api …` as a subprocess, so the credentials used are the ones `osc`
  already holds: this tool speaks no HTTP itself and never prompts for a password. There is no
  version check, no startup probe and no minimum version — any `osc` that answers `osc api` will
  do, and an `osc` that is not on `PATH` ends the run with exit 127.
- **`git` on `PATH`, with SSH access to `gitea@src.suse.de`.** The maintainership document is
  fetched with `git archive --remote`. HTTPS to that host asks for credentials interactively, so
  SSH is the only transport used and the fetch is pinned to it. Git's own credential prompt is
  disabled, so an unauthenticated remote fails rather than blocking on a prompt nobody is
  watching. SSH itself is deliberately still allowed to prompt on the terminal, so that a
  passphrase-protected key which is not already loaded into an agent keeps working; the 60-second
  timeout is what bounds that wait.

## Install and run

```sh
uv sync
uv run bugowner-diff -i 16.pkg --project SUSE:SLE-15-SP7:GA --ref slfo-main
```

The report goes to stdout unless `-o` names a file.

## Options

| option | required | meaning |
|---|---|---|
| `-i`, `--input PATH` | yes | File listing the packages to diff, one name per line. |
| `-o`, `--output PATH` | no | File to write the CSV report to. Omit it to write to stdout. |
| `--project NAME` | yes | Project whose listing and owners the 15 column reports, e.g. `SUSE:SLE-15-SP7:GA`. |
| `--ref REF` | yes | Git ref of the maintainership document the 16 column reports, e.g. `slfo-main`. |
| `-d`, `--debug` | no | Log at DEBUG level. It sets the log level and nothing else. |
| `-q`, `--quiet` | no | Log at ERROR level, silencing the warnings about owner names that render ambiguously. |

`--project` and `--ref` have no defaults. The values above are examples in the help text, which is
a different thing: an example is read by a person, while a default silently diffs something nobody
asked for. `-d` and `-q` are mutually exclusive; without either, logging is at WARNING.

## The input file

One bare package name per line. Blank lines are ignored and there is no comment syntax. Names are
stripped, deduplicated, and kept in first-seen order — that order is the report's row order.

A name must match `[A-Za-z0-9._+-]` and be at most 200 characters, the file must be valid UTF-8
and at most 64 KiB. Anything else ends the run with exit 64: a rejected name is reported with the
line it was on, a file that is not valid UTF-8 with a byte offset instead — there are no lines to
count until the bytes decode — and an oversized file against the limit it passed. The file is read
and validated before either remote is touched, so a mistyped `-i` costs no network round-trips.

## The report

The header is `package,15,16,change`.

- `package` — the name as read from the input file.
- `15` — owners from the OBS owner search. Empty if the package is absent from the project, or
  present with nobody assigned.
- `16` — maintainers from `_maintainership.json`. Empty if the document has no entry.
- `change` — one of the six values below.

A cell holding more than one owner lists them sorted and space-separated. Group owners carry a
`group:` prefix on both sides, so the two columns compare like for like.

The whole report is rendered before the destination is opened. Either a complete report is written
or nothing is — never a truncated one that reads as finished.

```
package,15,16,change
abseil-cpp,group:team-a,group:team-a,none
blktrace,user-a,group:team-a user-b,changed
catatonit,,user-c,adopted
spice,group:team-b,,removed
SDL3,,group:team-a,added
hiredis,,,added
```

## The change column

| change | meaning |
|---|---|
| `added` | Absent from the project listing. No owner search is made for it, so the 15 cell is empty; the 16 cell still shows whatever the maintainership document says. |
| `removed` | In the listing, and the maintainership document has no entry. |
| `adopted` | In the listing, but the owner search names nobody, while the maintainership document does. |
| `unmaintained` | Owned on the 15 side, but the maintainership document names nobody. |
| `none` | Both sides name owners and the two sets are identical — nothing to report. |
| `changed` | Both sides name owners and the two sets differ. |

They are tested in that order: both absence tests come first, then both emptiness tests. A side with
no entry is a stronger fact than a side that answered and named nobody, so it settles the row first.
That order decides the two cases where more than one value could be read to apply. A package no
source knows is `added` rather than `removed`, because it was never in the project it would have
been removed from. A package the owner search finds nobody for is `removed` if the maintainership
document has no entry either, and `adopted` only when that document does name someone.

`unmaintained` needs the maintainership document to have an entry that names nobody. No measured
entry does — of 2981, 1655 name no users and 1323 no groups, but none is empty on both — so the
value exists for completeness and does not appear in real reports.

## Data sources

Each source answers exactly one question, and no answer is inferred from a source that was not
asked.

| source | question it answers |
|---|---|
| the `-i` file | Which packages to report, and in what order. |
| `osc api '/source/<project>?expand=1'` | Whether a package exists in the project. `expand=1`, so a package inherited from a linked project counts as present. |
| `osc api '/search/owner?package=<name>'` | Who owns a package that already exists in the project. Every `<person>` and `<group>` returned is an owner; an empty `<collection/>` means nobody, which is `adopted` when the maintainership document has an entry for the package and `removed` when it does not. The search is deliberately unscoped and unfiltered — scoping it to the project answers with project-level fallback owners instead of the package's own. |
| `git archive --remote=gitea@src.suse.de:products/SLFO.git <ref> -- _maintainership.json` | Who maintains a package on the 16 side. |

The API host and the SLFO repository are fixed; the project and the ref are what you select.

The owner search runs only for packages the listing holds. A run therefore makes one listing
request, one archive fetch, and one owner request per existing package — measured at about 90
requests and two minutes for a 119-package input file. The requests are sequential because the OBS
API documentation asks callers not to parallelize.

## Exit codes

| code | meaning |
|---|---|
| 0 | The report was written. |
| 1 | A source answered with something unusable, an input or output path could not be used, the output pipe was closed early, or an unexpected error occurred. |
| 64 | Bad command line, or an input file whose contents are unusable. |
| 124 | A subprocess outlived its timeout: 120 seconds per `osc` call, 60 seconds for the archive fetch. |
| 127 | `osc` or `git` is not on `PATH`. |
| 130 | Interrupted with Ctrl-C. |

Every failure prints one line to stderr and no traceback. The single exception is an unexpected
error, which prints a traceback unconditionally and asks for a bug report: a run is two minutes of
network round-trips against sources that change underneath it, so "re-run it with `-d`" is not an
instruction that reliably reproduces anything.
