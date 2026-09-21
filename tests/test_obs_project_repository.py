"""Tests for the expanded OBS project listing repository."""

import subprocess

import pytest

from bugowner_diff.exceptions import (
    DataSourceError,
    InputError,
    MissingBinaryError,
    NetworkTimeoutError,
)
from bugowner_diff.repositories import obs_project_repository
from bugowner_diff.repositories.obs_project_repository import (
    OSC_TIMEOUT_SECONDS,
    ObsProjectRepository,
    ObsProjectRepositoryImpl,
)

PROJECT = "SUSE:SLE-15-SP7:GA"

LISTING = (
    b"<directory count='3'>"
    b"<entry name='ethtool'/>"
    b"<entry name='spice'/>"
    b"<entry name='SDL3'/>"
    b"</directory>"
)


class _RecordingOsc:
    """Stand in for :func:`subprocess.run`, recording calls and replaying one result."""

    def __init__(
        self,
        stdout: bytes = LISTING,
        returncode: int = 0,
        stderr: bytes = b"",
        raises: BaseException | None = None,
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(
        self,
        argv: list[str],
        *,
        capture_output: bool = False,
        check: bool = False,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((argv, {"capture_output": capture_output, "check": check, **kwargs}))
        if self.raises is not None:
            raise self.raises
        # Honoured, not merely recorded. A fake that accepts both keywords and
        # ignores them leaves the whole suite green when either is dropped from
        # the production call, and both matter: without capture_output the real
        # subprocess.run leaves stdout None and osc's stderr goes straight to the
        # terminal unbounded and unescaped, and with check=True it raises
        # CalledProcessError -- a SubprocessError, outside both BugownerDiffError
        # and OSError, so it escapes the taxonomy to a traceback.
        stdout = self.stdout if capture_output else None
        stderr = self.stderr if capture_output else None
        if check and self.returncode != 0:
            raise subprocess.CalledProcessError(self.returncode, argv, stdout, stderr)
        return subprocess.CompletedProcess(argv, self.returncode, stdout, stderr)


def _install_osc(monkeypatch: pytest.MonkeyPatch, osc: _RecordingOsc) -> _RecordingOsc:
    """Replace the subprocess this module shells out through."""
    monkeypatch.setattr(obs_project_repository.subprocess, "run", osc)
    return osc


def test_list_packages_returns_the_names_of_the_listing_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The one thing the module exists to do. Every `added` row in the report is
    # decided by membership of this set, so a name lost in parsing is a package
    # reported as absent from a project it is in.
    _install_osc(monkeypatch, _RecordingOsc())

    packages = ObsProjectRepositoryImpl().list_packages(PROJECT)

    assert packages == frozenset({"ethtool", "spice", "SDL3"})
    # The type as well as the contents: a mutable set compares equal to a
    # frozenset, so every other assertion in this file stays green if the
    # declared frozenset[str] quietly becomes a set the callers can mutate.
    assert isinstance(packages, frozenset)


def test_list_packages_passes_the_project_to_osc_inside_the_api_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The exact argv, not just "osc was called". The project reaches the child
    # process embedded in an argument that starts with "/source/" and never
    # occupies an argv position of its own, so a name beginning with a dash
    # could not be read as an option by osc's own parser -- defence in depth
    # behind an allowlist that now refuses such a name outright. Move the
    # project into a standalone argv slot and this test is what goes red. A
    # list, never a shell string: no quoting rule is being relied on.
    osc = _install_osc(monkeypatch, _RecordingOsc())

    ObsProjectRepositoryImpl().list_packages(PROJECT)

    argv, _ = osc.calls[0]
    assert argv == [
        "osc",
        "-A",
        "https://api.suse.de",
        "api",
        "/source/SUSE:SLE-15-SP7:GA?expand=1",
    ]


def test_list_packages_bounds_the_osc_call_with_a_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without a timeout an osc that never returns -- a stalled TLS handshake, a
    # black-holed route, a credential helper waiting on a terminal -- wedges the
    # whole run with no output and no exit code, and the 130-on-Ctrl-C path is
    # the only way out. The deadline is what makes NetworkTimeoutError reachable
    # at all.
    osc = _install_osc(monkeypatch, _RecordingOsc())

    ObsProjectRepositoryImpl().list_packages(PROJECT)

    _, kwargs = osc.calls[0]
    assert kwargs["timeout"] == OSC_TIMEOUT_SECONDS
    assert OSC_TIMEOUT_SECONDS > 0


def test_list_packages_denies_osc_an_stdin_to_prompt_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # osc asks for a password on the terminal when its credentials are missing
    # or expired. Inheriting this process's stdin turns that into a prompt
    # buried under the tool's own output, or -- in a CI job or a pipeline -- a
    # wait for input that never comes, until the deadline above expires. With
    # /dev/null it reads EOF and fails immediately with something diagnosable.
    osc = _install_osc(monkeypatch, _RecordingOsc())

    ObsProjectRepositoryImpl().list_packages(PROJECT)

    _, kwargs = osc.calls[0]
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_list_packages_captures_what_osc_prints_rather_than_letting_it_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two things depend on this, and neither is "the tests need the output".
    # Without it the child inherits this process's stdout and stderr, so a
    # 4.8 MB listing lands on the user's terminal instead of being parsed, and
    # osc's stderr bypasses _describe -- the bound and the repr that keep a
    # remote-influenced traceback from rewriting the line the failure is read on.
    osc = _install_osc(monkeypatch, _RecordingOsc())

    ObsProjectRepositoryImpl().list_packages(PROJECT)

    _, kwargs = osc.calls[0]
    assert kwargs["capture_output"] is True


def test_list_packages_reads_oscs_exit_code_rather_than_letting_run_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # check=False is deliberate, not an omission. check=True raises
    # CalledProcessError, which is a SubprocessError: outside BugownerDiffError
    # and outside OSError, so it satisfies no arm of the CLI's ladder and
    # reaches the catch-all as a traceback reading "this is a bug in
    # bugowner-diff" -- for the entirely ordinary case of an expired credential.
    # Reading returncode here is what turns that into DataSourceError.
    osc = _install_osc(monkeypatch, _RecordingOsc())

    ObsProjectRepositoryImpl().list_packages(PROJECT)

    _, kwargs = osc.calls[0]
    assert kwargs["check"] is False


def test_list_packages_reports_a_data_source_error_when_osc_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unreachable API, an expired credential or a project that does not
    # exist all arrive here as a non-zero exit with an empty stdout, and an
    # empty stdout parses into an empty listing -- which would report all 119
    # packages as `added` and look like a successful run. The type matters as much
    # as the raise: RuntimeError, the sibling project's choice, also catches
    # RecursionError, so a crash in the parser would be reported to the user as
    # a bad remote document.
    _install_osc(monkeypatch, _RecordingOsc(stdout=b"", returncode=1, stderr=b"HTTP Error 404"))

    with pytest.raises(DataSourceError):
        ObsProjectRepositoryImpl().list_packages(PROJECT)


def test_the_osc_failure_message_carries_the_exit_code_and_what_osc_said(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This message is the entire diagnosis the user gets: the run stops here,
    # and osc's own stderr is the only thing that distinguishes "no credentials"
    # from "no such project" from "the API is down".
    _install_osc(
        monkeypatch,
        _RecordingOsc(stdout=b"", returncode=2, stderr=b"Server returned an error: HTTP Error 404"),
    )

    with pytest.raises(DataSourceError) as caught:
        ObsProjectRepositoryImpl().list_packages(PROJECT)

    assert "2" in str(caught.value)
    assert "HTTP Error 404" in str(caught.value)


def test_list_packages_reports_a_missing_binary_when_osc_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # osc is not a dependency this package can install for the user, so "it is
    # not on PATH" is a setup mistake with its own exit code (127) and its own
    # remedy. Left raw, FileNotFoundError is indistinguishable from a missing
    # input file.
    _install_osc(monkeypatch, _RecordingOsc(raises=FileNotFoundError(2, "No such file", "osc")))

    with pytest.raises(MissingBinaryError) as caught:
        ObsProjectRepositoryImpl().list_packages(PROJECT)

    assert caught.value.binary == "osc"


def test_list_packages_reports_a_timeout_when_osc_outlives_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A separate exit code (124) from a generic failure, because the remedy is
    # different: wait for the API and re-run. The deadline is reported with it
    # so the user can tell a slow link from a hung one.
    _install_osc(
        monkeypatch,
        _RecordingOsc(raises=subprocess.TimeoutExpired(cmd=["osc"], timeout=OSC_TIMEOUT_SECONDS)),
    )

    with pytest.raises(NetworkTimeoutError) as caught:
        ObsProjectRepositoryImpl().list_packages(PROJECT)

    assert caught.value.timeout == OSC_TIMEOUT_SECONDS


def test_list_packages_lets_an_unattributable_os_error_through_unwrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An osc that is present but not executable, a fork that hits RLIMIT_NPROC,
    # a /dev/null that cannot be opened: errno already says more than any
    # wrapper could add, and mapping these onto MissingBinaryError would tell
    # the user to install something that is already installed. The taxonomy's
    # rule is that OSError escapes by design, and a bare `except OSError` around
    # the subprocess call would silently break it -- FileNotFoundError and
    # PermissionError are both OSError subclasses, so only the narrow catch
    # keeps these two apart.
    _install_osc(monkeypatch, _RecordingOsc(raises=PermissionError(13, "Permission denied", "osc")))

    with pytest.raises(PermissionError):
        ObsProjectRepositoryImpl().list_packages(PROJECT)


def test_list_packages_refuses_a_listing_carrying_a_doctype(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # forbid_dtd is what stops a billion-laughs expansion, and defusedxml
    # signals it with DTDForbidden -- a ValueError, not a ParseError, so the
    # handler for malformed XML does not cover it. Left uncaught it escapes the
    # taxonomy entirely and prints a traceback. A real OBS listing never carries
    # a DOCTYPE, so nothing legitimate is lost by refusing.
    doctype = (
        b"<!DOCTYPE directory [<!ENTITY a 'x'>]><directory><entry name='ethtool'/></directory>"
    )
    _install_osc(monkeypatch, _RecordingOsc(stdout=doctype))

    with pytest.raises(DataSourceError) as caught:
        ObsProjectRepositoryImpl().list_packages(PROJECT)

    assert "DOCTYPE" in str(caught.value)


def test_list_packages_refuses_a_listing_that_is_not_well_formed_xml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A truncated response -- a dropped connection mid-transfer is the usual
    # cause -- raises ElementTree.ParseError, which is a SyntaxError and not a
    # ValueError. Nothing in the CLI's error mapping would recognise it, and a
    # SyntaxError traceback reads as a defect in this tool rather than a bad
    # remote body.
    _install_osc(monkeypatch, _RecordingOsc(stdout=b"<directory><entry name='ethtool'"))

    with pytest.raises(DataSourceError):
        ObsProjectRepositoryImpl().list_packages(PROJECT)


def test_list_packages_refuses_a_listing_naming_an_undefined_entity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The other half of the entity defence. With the DTD forbidden, a reference
    # to an entity that was never declared reaches the parser as a ParseError
    # rather than as a defusedxml exception, so a handler written only for
    # DefusedXmlException misses it.
    undefined_entity = b"<directory><entry name='&boom;'/></directory>"
    _install_osc(monkeypatch, _RecordingOsc(stdout=undefined_entity))

    with pytest.raises(DataSourceError):
        ObsProjectRepositoryImpl().list_packages(PROJECT)


def test_list_packages_refuses_a_document_that_is_not_a_directory_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OBS answers some failures with a well-formed <status> document, and osc
    # does not always exit non-zero when it does. findall("entry") on that
    # document finds nothing and returns an empty set -- which classifies all
    # 119 packages as `added` and writes a full, plausible, entirely wrong CSV.
    # The root element is the only thing that distinguishes the two.
    status = b"<status code='unknown_project'><summary>project not found</summary></status>"
    _install_osc(monkeypatch, _RecordingOsc(stdout=status))

    with pytest.raises(DataSourceError) as caught:
        ObsProjectRepositoryImpl().list_packages(PROJECT)

    assert "status" in str(caught.value)


def test_list_packages_refuses_a_listing_entry_that_has_no_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Skipping it -- commit 7's rule for an <owner> without a package attribute
    # -- is wrong here, and the two cases only look alike. There, a missing
    # attribute identifies a different *kind* of element, project-level
    # fallback, which is meant to be ignored. Here the schema has no such
    # element: an entry without a name is a package whose name was lost, and
    # skipping it reports that package as absent from a project it is in. One
    # nameless entry among 62809 means the document is not the one it claims to
    # be, so the run stops instead of answering from it.
    _install_osc(
        monkeypatch,
        _RecordingOsc(stdout=b"<directory><entry name='ethtool'/><entry/></directory>"),
    )

    with pytest.raises(DataSourceError):
        ObsProjectRepositoryImpl().list_packages(PROJECT)


def test_list_packages_refuses_a_listing_entry_whose_name_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # name='' is not a package, and it is not caught by the check above: the
    # attribute is present, so a `is None` test passes it straight through and
    # the empty string joins the set. It then matches no input name, so the
    # entry is invisible in the report -- but its presence proves the document
    # is not the one it claims to be, exactly as a missing attribute does.
    _install_osc(
        monkeypatch,
        _RecordingOsc(stdout=b"<directory><entry name='ethtool'/><entry name=''/></directory>"),
    )

    with pytest.raises(DataSourceError):
        ObsProjectRepositoryImpl().list_packages(PROJECT)


def test_list_packages_refuses_a_listing_that_nests_an_entry_inside_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # findall("entry") matches direct children only, so the inner entry is
    # silently absent from the result -- verified: this document yields
    # {"ethtool"} and "spice" is simply gone. A lost name is a package reported
    # as `added` against a project it is in, which is the one failure this module
    # cannot be allowed to have. Walking the children instead of searching them
    # is what makes the loss impossible; this is the test that proves it.
    _install_osc(
        monkeypatch,
        _RecordingOsc(
            stdout=b"<directory><entry name='ethtool'><entry name='spice'/></entry></directory>"
        ),
    )

    with pytest.raises(DataSourceError):
        ObsProjectRepositoryImpl().list_packages(PROJECT)


def test_list_packages_refuses_a_listing_entry_qualified_by_a_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same loss by a second route, and the one that rules out the obvious
    # repair: switching findall("entry") to iter("entry") fixes the nesting
    # above but not this. ElementTree renders the tag as "{urn:x}entry", which
    # neither call matches -- verified, both return only "ethtool". Comparing
    # child.tag against "entry" and refusing everything else covers both cases
    # with one rule.
    _install_osc(
        monkeypatch,
        _RecordingOsc(
            stdout=(
                b"<directory><entry name='ethtool'/><entry xmlns='urn:x' name='spice'/></directory>"
            )
        ),
    )

    with pytest.raises(DataSourceError):
        ObsProjectRepositoryImpl().list_packages(PROJECT)


def test_list_packages_refuses_a_listing_holding_an_element_other_than_an_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fail-closed, and the cliff is deliberate: /source/<prj>/<pkg> answers with
    # the same <directory> root and carries <linkinfo> and <serviceinfo>
    # siblings, so an OBS that started adding siblings to the *project* listing
    # would stop this tool rather than degrade it. That is the trade -- a loud
    # failure naming the tag it did not expect, over quietly reading names out
    # of a document whose shape it does not recognise.
    _install_osc(
        monkeypatch,
        _RecordingOsc(
            stdout=b"<directory><entry name='ethtool'/><linkinfo project='X'/></directory>"
        ),
    )

    with pytest.raises(DataSourceError) as caught:
        ObsProjectRepositoryImpl().list_packages(PROJECT)

    assert "linkinfo" in str(caught.value)


def test_list_packages_refuses_a_listing_holding_fewer_entries_than_it_declares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An independent second opinion on the refusals above rather than a
    # substitute for them: the document states its own entry count, so a name
    # this loop never saw can be caught without knowing how it went missing.
    _install_osc(
        monkeypatch,
        _RecordingOsc(stdout=b"<directory count='2'><entry name='ethtool'/></directory>"),
    )

    with pytest.raises(DataSourceError) as caught:
        ObsProjectRepositoryImpl().list_packages(PROJECT)

    assert "2" in str(caught.value)


def test_list_packages_counts_entries_rather_than_distinct_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cross-check compares the declared count against elements seen, not
    # against the size of the set. A repeated name collapses -- which is correct,
    # membership is all the report asks -- and must not be mistaken for an entry
    # that went missing. Count distinct names instead and this document fails.
    _install_osc(
        monkeypatch,
        _RecordingOsc(
            stdout=(
                b"<directory count='2'><entry name='ethtool'/><entry name='ethtool'/></directory>"
            )
        ),
    )

    packages = ObsProjectRepositoryImpl().list_packages(PROJECT)

    assert packages == frozenset({"ethtool"})


@pytest.mark.parametrize(
    "listing",
    [
        b"<directory><entry name='ethtool'/></directory>",
        b"<directory count=''><entry name='ethtool'/></directory>",
        b"<directory count=' 2'><entry name='ethtool'/></directory>",
        b"<directory count='1_0'><entry name='ethtool'/></directory>",
        "<directory count='٢'><entry name='ethtool'/></directory>".encode(),
    ],
    ids=["absent", "empty", "padded", "underscored", "arabic-indic-digit"],
)
def test_list_packages_ignores_a_declared_count_that_is_not_a_plain_decimal(
    monkeypatch: pytest.MonkeyPatch, listing: bytes
) -> None:
    # The attribute is remote-influenced, and int() is far more generous than a
    # count from OBS would ever need: it accepts surrounding whitespace, PEP 515
    # underscores and every Unicode decimal digit, so int(" 2"), int("1_0") and
    # int("٢") all succeed. Rather than let those decide whether the run fails, a
    # count that is not plain ASCII digits is treated as absent -- the
    # cross-check is a second opinion, so declining to hold one is the safe way
    # to be unsure. Each value here would also *disagree* with the one entry
    # present if it were parsed, so relaxing the guard to a bare int() turns
    # every case red rather than leaving them passing by coincidence.
    _install_osc(monkeypatch, _RecordingOsc(stdout=listing))

    packages = ObsProjectRepositoryImpl().list_packages(PROJECT)

    assert packages == frozenset({"ethtool"})


def test_the_unexpected_root_element_message_bounds_what_it_echoes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An element name has no length limit of its own: a 2 MB tag parses fine --
    # verified, it arrives at root.tag intact -- and echoing it whole turns a
    # one-line complaint into a 2 MB one on stderr and, once -v/-d arrive, in a
    # log file.
    huge = b"<" + b"z" * 100_000 + b"/>"
    _install_osc(monkeypatch, _RecordingOsc(stdout=huge))

    with pytest.raises(DataSourceError) as caught:
        ObsProjectRepositoryImpl().list_packages(PROJECT)

    assert len(str(caught.value)) < 500


def test_the_unexpected_root_element_message_escapes_the_tag_it_echoes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A namespaced tag carries its namespace URI, and a URI is an attribute
    # value rather than an XML Name. expat refuses a raw ESC there and refuses
    # it as &#27; too, but U+202E survives into the tag -- verified -- and so
    # does a newline written as &#10;. Left unescaped, the first reverses the
    # rendering of the rest of the complaint and the second breaks it across
    # lines.
    # Written as an escape, never as the character itself: a literal U+202E in
    # this file would reorder the rendering of the test that documents it.
    # chr(), never the character itself and never a \\u escape typed into this
    # file: a literal U+202E here would reorder the rendering of the very test
    # that documents it, and an escape can be silently expanded on the way in.
    rlo = chr(0x202E)
    _install_osc(monkeypatch, _RecordingOsc(stdout=f"<status xmlns='{rlo}EVIL'/>".encode()))

    with pytest.raises(DataSourceError) as caught:
        ObsProjectRepositoryImpl().list_packages(PROJECT)

    assert rlo not in str(caught.value)
    assert "\\u202e" in str(caught.value)


def test_list_packages_returns_nothing_for_a_valid_but_empty_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty project is a legitimate answer, not a failure: the root element
    # is what proves the document was a listing, so there is nothing left to
    # suspect. Every package is then `added`, which is the truth for an empty
    # project.
    _install_osc(monkeypatch, _RecordingOsc(stdout=b"<directory count='0'/>"))

    assert ObsProjectRepositoryImpl().list_packages(PROJECT) == frozenset()


def test_list_packages_refuses_a_body_larger_than_the_listing_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The reason, not just the refusal: the body handed over here is valid XML
    # that parses into the expected names, so nothing but the cap can reject it.
    # Delete the cap and this test goes red rather than passing by luck on a
    # parser error, which is how commit 5 found guards that were never
    # exercised. The cap is monkeypatched down rather than a 32 MiB body being
    # built, and the production code reads the constant at call time.
    monkeypatch.setattr(obs_project_repository, "MAX_LISTING_BYTES", len(LISTING) - 1)
    _install_osc(monkeypatch, _RecordingOsc())

    with pytest.raises(DataSourceError) as caught:
        ObsProjectRepositoryImpl().list_packages(PROJECT)

    assert str(len(LISTING)) in str(caught.value)


def test_a_body_of_exactly_the_listing_cap_is_still_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pins which side of the comparison the boundary falls on. With `>` turned
    # into `>=`, a listing that lands exactly on the cap is refused forever and
    # the tool cannot run at all against that project.
    monkeypatch.setattr(obs_project_repository, "MAX_LISTING_BYTES", len(LISTING))
    _install_osc(monkeypatch, _RecordingOsc())

    packages = ObsProjectRepositoryImpl().list_packages(PROJECT)

    assert packages == frozenset({"ethtool", "spice", "SDL3"})


def test_the_listing_cap_leaves_room_for_the_real_listing() -> None:
    # Measured on 2026-09-16: /source/SUSE:SLE-15-SP7:GA?expand=1 is 62809
    # entries and 4,792,776 bytes. Asserted as headroom over the measurement
    # rather than as the constant restated, so the test still checks what its
    # name claims if the cap is deliberately changed.
    assert obs_project_repository.MAX_LISTING_BYTES > 4_792_776 * 2


@pytest.mark.parametrize(
    "project",
    [
        "",
        "SUSE:SLE-15-SP7:GA extra",
        "SUSE:SLE-15-SP7:GA?cmd=branch",
        "../../etc/passwd",
        "SUSE/SLE-15-SP7",
        "SUSE:SLE\n-15",
        "SUSE:SLE-15\x00",
        "SUSE:SLE-15-SP7:GA#fragment",
        "x" * 201,
        "\udcff",
    ],
    ids=[
        "empty",
        "whitespace",
        "query-injection",
        "traversal",
        "path-separator",
        "newline",
        "nul",
        "fragment",
        "too-long",
        "lone-surrogate",
    ],
)
def test_list_packages_rejects_a_project_outside_the_allowlist(
    monkeypatch: pytest.MonkeyPatch, project: str
) -> None:
    # The project is interpolated into an API path and nothing downstream
    # re-validates. A "?" or a "&" appends parameters to the request osc sends;
    # a "/" walks out of /source/; a lone surrogate -- argv is decoded with
    # surrogateescape, so `--project $'\xff'` produces one -- has no business in
    # an argv at all.
    _install_osc(monkeypatch, _RecordingOsc())

    with pytest.raises(InputError):
        ObsProjectRepositoryImpl().list_packages(project)


def test_a_rejected_project_never_reaches_a_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Order matters, not just the raise. Validation after the fetch would have
    # handed the string to osc already -- by which point refusing it is theatre.
    osc = _install_osc(monkeypatch, _RecordingOsc())

    with pytest.raises(InputError):
        ObsProjectRepositoryImpl().list_packages("SUSE:SLE-15-SP7:GA&cmd=branch")

    assert osc.calls == []


@pytest.mark.parametrize(
    "project",
    ["SUSE:SLE-15-SP7:GA", "openSUSE:Factory", "home:user-a:branches", "SUSE:ALP:Source:1.0", "x"],
)
def test_list_packages_accepts_the_shapes_a_real_project_name_takes(
    monkeypatch: pytest.MonkeyPatch, project: str
) -> None:
    # The allowlist has to admit every character a real IBS project uses --
    # colons, dots, plus, underscore, dash -- or the tool refuses to run against
    # the projects it exists for.
    _install_osc(monkeypatch, _RecordingOsc())

    assert ObsProjectRepositoryImpl().list_packages(project)


@pytest.mark.parametrize(
    "project",
    [".", "..", "...", "-", "-rf", "--help", ":SUSE", "_SUSE", "+SUSE"],
    ids=["dot", "dot-dot", "dot-dot-dot", "dash", "rf", "help", "colon", "underscore", "plus"],
)
def test_list_packages_rejects_a_project_that_does_not_start_alphanumeric(
    monkeypatch: pytest.MonkeyPatch, project: str
) -> None:
    # The character set alone is not enough, because "." and "-" are in it and a
    # name made only of those is not inert. "/source/.?expand=1" and
    # "/source/..?expand=1" are resolved client-side, before a byte leaves the
    # machine: osc hands the URL to urllib3's parse_url, which normalises the
    # dot segments away, so the first becomes the project *index* at /source/
    # and the second escapes /source/ entirely. Either answers with a
    # well-formed document that is not the listing of any project, and the
    # allowlist is the only thing positioned to refuse it -- by the time the
    # request is built the evidence is gone. Requiring the first character to be
    # alphanumeric costs nothing: every real project name starts with a letter.
    _install_osc(monkeypatch, _RecordingOsc())

    with pytest.raises(InputError):
        ObsProjectRepositoryImpl().list_packages(project)


def test_a_project_rejected_for_its_first_character_still_reaches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same ordering guarantee as the allowlist test above, asserted again for
    # the traversal names specifically: ".." must be refused before it is handed
    # to osc, because a refusal after that has happened changed nothing.
    osc = _install_osc(monkeypatch, _RecordingOsc())

    with pytest.raises(InputError):
        ObsProjectRepositoryImpl().list_packages("..")

    assert osc.calls == []


def test_the_implementation_satisfies_the_obs_project_repository_protocol() -> None:
    # mypy is scoped to src/, so nothing type-checks the conformance of this
    # Impl to the Protocol the command layer of commit 11 will inject against.
    # isinstance against a runtime_checkable Protocol compares attribute names
    # only, so it catches a renamed method and not a changed signature.
    assert isinstance(ObsProjectRepositoryImpl(), ObsProjectRepository)


def test_the_osc_failure_message_truncates_a_very_long_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A failing osc can print a Python traceback, and an osc pointed at a
    # misbehaving endpoint can print a whole HTML error page. All of it would
    # otherwise reach stderr and, once -v/-d land, a log file -- burying the one
    # line that says what went wrong.
    _install_osc(monkeypatch, _RecordingOsc(stdout=b"", returncode=1, stderr=b"E" * 20_000))

    with pytest.raises(DataSourceError) as caught:
        ObsProjectRepositoryImpl().list_packages(PROJECT)

    assert len(str(caught.value)) < 1_000
    assert "..." in str(caught.value)


def test_the_osc_failure_message_escapes_what_osc_printed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # osc's stderr is remote-influenced text on its way to a terminal. Without
    # the repr an embedded ANSI sequence or carriage return rewrites the line
    # the user is reading the failure on, which is how a failure gets made to
    # look like a success.
    _install_osc(
        monkeypatch,
        _RecordingOsc(stdout=b"", returncode=1, stderr=b"lost\r\x1b[2Kdone, 0 problems"),
    )

    with pytest.raises(DataSourceError) as caught:
        ObsProjectRepositoryImpl().list_packages(PROJECT)

    assert "\r" not in str(caught.value)
    assert "\x1b" not in str(caught.value)
    assert "\\r" in str(caught.value)
