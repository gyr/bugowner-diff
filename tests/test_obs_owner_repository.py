"""Tests for the OBS owner-search repository."""

import subprocess

import pytest

from bugowner_diff.exceptions import DataSourceError, MissingBinaryError, NetworkTimeoutError
from bugowner_diff.repositories import obs_owner_repository
from bugowner_diff.repositories.obs_owner_repository import (
    OSC_TIMEOUT_SECONDS,
    ObsOwnerRepository,
    ObsOwnerRepositoryImpl,
)

PACKAGE = "vim"

OWNERS = (
    b"<collection>"
    b"<owner project='SUSE:SLE-15-SP7:GA' package='vim'>"
    b"<person name='user-a' role='bugowner'/>"
    b"</owner>"
    b"</collection>"
)


class _RecordingOsc:
    """Stand in for :func:`subprocess.run`, recording calls and replaying one result."""

    def __init__(
        self,
        stdout: bytes = OWNERS,
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
        # Honoured, not merely recorded, for the reason the sibling repository's
        # fake spells out: without capture_output the real subprocess.run leaves
        # stdout None, and with check=True it raises CalledProcessError, which is
        # outside both BugownerDiffError and OSError and so escapes the taxonomy.
        stdout = self.stdout if capture_output else None
        stderr = self.stderr if capture_output else None
        if check and self.returncode != 0:
            raise subprocess.CalledProcessError(self.returncode, argv, stdout, stderr)
        return subprocess.CompletedProcess(argv, self.returncode, stdout, stderr)


def _install_osc(monkeypatch: pytest.MonkeyPatch, osc: _RecordingOsc) -> _RecordingOsc:
    """Replace the subprocess this module shells out through."""
    monkeypatch.setattr(obs_owner_repository.subprocess, "run", osc)
    return osc


@pytest.mark.parametrize(
    ("member", "expected"),
    [(b"<person name='user-a'/>", "user-a"), (b"<group name='team-a'/>", "group:team-a")],
    ids=["person", "group"],
)
def test_find_owners_tags_a_member_by_the_element_it_came_from(
    monkeypatch: pytest.MonkeyPatch, member: bytes, expected: str
) -> None:
    # The element type is the only thing that says whether a name is a user or a
    # group, and the two sides of the diff compare tagged names by exact set
    # equality -- so a group returned untagged is a difference this tool would
    # report against a project that has none.
    _install_osc(
        monkeypatch,
        _RecordingOsc(stdout=b"<collection><owner>" + member + b"</owner></collection>"),
    )

    owners = ObsOwnerRepositoryImpl().find_owners(PACKAGE)

    assert owners == frozenset({expected})
    # The type as well as the contents: a mutable set compares equal to a
    # frozenset, so every other assertion here stays green if the declared
    # frozenset[str] quietly becomes a set the callers can mutate.
    assert isinstance(owners, frozenset)


def test_find_owners_unions_every_member_of_every_owner_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # /search/owner answers with one <owner> per project that records an owner
    # for the package, each holding any number of people and groups, and all of
    # them own it. Reading only the first element -- or only the first member of
    # one -- drops owners the 15 side really has, which the report then prints as
    # a difference.
    _install_osc(
        monkeypatch,
        _RecordingOsc(
            stdout=(
                b"<collection>"
                b"<owner project='A' package='vim'>"
                b"<person name='user-a' role='bugowner'/>"
                b"<group name='team-a' role='maintainer'/>"
                b"</owner>"
                b"<owner project='B' package='vim'>"
                b"<person name='user-b' role='maintainer'/>"
                b"</owner>"
                b"</collection>"
            )
        ),
    )

    owners = ObsOwnerRepositoryImpl().find_owners(PACKAGE)

    assert owners == frozenset({"user-a", "group:team-a", "user-b"})


def test_find_owners_returns_nothing_for_a_package_nobody_owns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bare <collection/> is the real answer for 3 of the 88 packages probed,
    # not a failure: an owner-less package is exactly what the `unmaintained`
    # status is for, and raising here would stop a run over a legitimate answer.
    _install_osc(monkeypatch, _RecordingOsc(stdout=b"<collection/>"))

    assert ObsOwnerRepositoryImpl().find_owners(PACKAGE) == frozenset()


def test_find_owners_passes_the_package_to_osc_percent_encoded_inside_the_api_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two things at once, and both break silently if they regress. The package
    # sits inside an argument beginning with "/search/owner?", never in an argv
    # slot of its own, so a name starting with a dash cannot be read as an option
    # by osc's parser. And "+" means a space once it reaches a query string:
    # quote() turns "gtk+" into "gtk%2B" -- the default safe set does not hold
    # "+" -- so without it the search would ask about "gtk " and answer for
    # nothing. The query carries no project and no filter on purpose: scoping it
    # to a project returns only project-level fallback owners, and
    # filter=maintainer degrades the same way, while no filter at all is
    # equivalent to filter=bugowner,maintainer.
    osc = _install_osc(monkeypatch, _RecordingOsc())

    ObsOwnerRepositoryImpl().find_owners("gtk+")

    argv, _ = osc.calls[0]
    assert argv == ["osc", "-A", "https://api.suse.de", "api", "/search/owner?package=gtk%2B"]


def test_find_owners_bounds_the_osc_call_and_denies_it_an_stdin_to_prompt_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The deadline is what makes NetworkTimeoutError reachable at all: without it
    # a stalled TLS handshake or a black-holed route wedges the run with no
    # output and no exit code. /dev/null is the other half -- osc prompts on the
    # terminal for a password when its credentials are missing, and an inherited
    # stdin turns that into a wait nobody is watching for.
    osc = _install_osc(monkeypatch, _RecordingOsc())

    ObsOwnerRepositoryImpl().find_owners(PACKAGE)

    _, kwargs = osc.calls[0]
    assert kwargs["timeout"] == OSC_TIMEOUT_SECONDS
    assert OSC_TIMEOUT_SECONDS > 0
    assert kwargs["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize(
    ("member", "expected", "warnings"),
    [
        (b"<person name='group:team-a'/>", "group:team-a", 1),
        (b"<person name='user a'/>", "user a", 1),
        (b"<group name='group:team-a'/>", "group:group:team-a", 0),
    ],
    ids=["collides-with-a-group", "holds-whitespace", "group-tags-clear-of-the-collision"],
)
def test_find_owners_warns_about_an_ambiguous_name_and_keeps_it(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    member: bytes,
    expected: str,
    warnings: int,
) -> None:
    # Reported and kept, never dropped or rewritten: OBS is authoritative about
    # the spelling of its own owners, so a name this tool cannot render
    # unambiguously is still the owner of the package. The warning is the only
    # thing that tells a reader why two owners collapsed into one cell, or why a
    # cell looks like it holds one name more than it does.
    #
    # The silent row is what pins the `is_group` argument: a *group* named
    # "group:team-a" tags to "group:group:team-a", which no other group can
    # produce, so there is nothing ambiguous to report. Without it, hard-coding
    # is_group=False leaves every assertion here green and every group whose name
    # starts with the prefix is warned about for a collision it cannot have.
    _install_osc(
        monkeypatch,
        _RecordingOsc(stdout=b"<collection><owner>" + member + b"</owner></collection>"),
    )

    owners = ObsOwnerRepositoryImpl().find_owners(PACKAGE)

    assert owners == frozenset({expected})
    assert [record.levelname for record in caplog.records] == ["WARNING"] * warnings
    assert all(
        f"Owner name {expected!r} of package {PACKAGE!r} renders ambiguously:"
        in record.getMessage()
        for record in caplog.records
    )


def test_find_owners_reports_what_osc_said_when_it_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every non-zero exit, 404 included: a package the search does not know is
    # indistinguishable here from an expired credential or an API that is down,
    # and osc's own stderr is the whole of the diagnosis. Treating a 404 as "no
    # owners" would write `unmaintained` for a package whose ownership was never
    # read -- and it would have to be recognised by matching English text in
    # stderr, which changes with osc's locale and version.
    _install_osc(
        monkeypatch,
        _RecordingOsc(stdout=b"", returncode=2, stderr=b"Server returned an error: HTTP Error 404"),
    )

    with pytest.raises(DataSourceError) as caught:
        ObsOwnerRepositoryImpl().find_owners(PACKAGE)

    assert "2" in str(caught.value)
    assert "HTTP Error 404" in str(caught.value)


def test_find_owners_reports_a_missing_binary_when_osc_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # osc is not a dependency this package can install for the user, so "not on
    # PATH" is a setup mistake with its own exit code and its own remedy.
    # FileNotFoundError and not OSError: an osc that is present but not
    # executable raises PermissionError, which is also an OSError, and telling
    # that user to install osc sends them after the wrong problem.
    _install_osc(monkeypatch, _RecordingOsc(raises=FileNotFoundError(2, "No such file", "osc")))

    with pytest.raises(MissingBinaryError) as caught:
        ObsOwnerRepositoryImpl().find_owners(PACKAGE)

    assert caught.value.binary == "osc"


def test_find_owners_lets_an_unattributable_os_error_through_unwrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An osc that is present but not executable, a fork that hits RLIMIT_NPROC, a
    # /dev/null that cannot be opened: errno already says more than any wrapper
    # could add, and reporting one of these as a missing binary would tell the
    # user to install something that is already installed. The taxonomy's rule is
    # that OSError escapes by design, and a bare `except OSError` around the
    # subprocess call would silently break it -- FileNotFoundError and
    # PermissionError are both OSError subclasses, so only the narrow catch keeps
    # the two apart.
    _install_osc(monkeypatch, _RecordingOsc(raises=PermissionError(13, "Permission denied", "osc")))

    with pytest.raises(PermissionError):
        ObsOwnerRepositoryImpl().find_owners(PACKAGE)


def test_find_owners_reports_a_timeout_when_osc_outlives_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A separate exit code from a generic failure, because the remedy differs:
    # wait for the API and re-run. The deadline travels with the error so a slow
    # link can be told from a hung one.
    _install_osc(
        monkeypatch,
        _RecordingOsc(raises=subprocess.TimeoutExpired(cmd=["osc"], timeout=OSC_TIMEOUT_SECONDS)),
    )

    with pytest.raises(NetworkTimeoutError) as caught:
        ObsOwnerRepositoryImpl().find_owners(PACKAGE)

    assert caught.value.timeout == OSC_TIMEOUT_SECONDS


def test_find_owners_refuses_a_body_carrying_a_doctype(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # forbid_dtd is what stops a billion-laughs expansion, and defusedxml signals
    # it with DTDForbidden -- a ValueError, not a ParseError, so the arm for
    # malformed XML does not cover it and it would otherwise escape the taxonomy
    # as a traceback. A real owner search never carries a DOCTYPE.
    doctype = b"<!DOCTYPE collection [<!ENTITY a 'x'>]><collection/>"
    _install_osc(monkeypatch, _RecordingOsc(stdout=doctype))

    with pytest.raises(DataSourceError) as caught:
        ObsOwnerRepositoryImpl().find_owners(PACKAGE)

    assert "DOCTYPE" in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [b"<collection><owner>", b"<collection><person name='&boom;'/></collection>"],
    ids=["truncated", "undefined-entity"],
)
def test_find_owners_refuses_a_body_that_is_not_well_formed_xml(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    # A dropped connection mid-transfer, and -- once the DTD is forbidden -- a
    # reference to an entity that was never declared, both arrive as
    # ElementTree.ParseError. That is a SyntaxError and not a ValueError, so the
    # DOCTYPE arm above does not cover it however it is widened, and an uncaught
    # SyntaxError reads as a defect in this tool rather than a bad remote body.
    _install_osc(monkeypatch, _RecordingOsc(stdout=body))

    with pytest.raises(DataSourceError):
        ObsOwnerRepositoryImpl().find_owners(PACKAGE)


def test_find_owners_refuses_a_document_that_is_not_an_owner_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OBS answers some failures with a well-formed <status> document, and osc
    # does not reliably exit non-zero when it does. Reading members out of one
    # finds nothing, and no owners is a complete, plausible, entirely wrong
    # `unmaintained` row. The root element is what tells the two apart.
    _install_osc(monkeypatch, _RecordingOsc(stdout=b"<status code='unknown_package'/>"))

    with pytest.raises(DataSourceError) as caught:
        ObsOwnerRepositoryImpl().find_owners(PACKAGE)

    assert "status" in str(caught.value)


def test_find_owners_refuses_a_collection_holding_something_other_than_an_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fail-closed and deliberately abrupt: an element this module does not know
    # may well carry owners, and the alternative to stopping is answering from a
    # document whose shape is not the one OBS was observed to send. The message
    # names the tag, which makes that a one-read diagnosis.
    _install_osc(
        monkeypatch,
        _RecordingOsc(stdout=b"<collection><person name='user-a'/></collection>"),
    )

    with pytest.raises(DataSourceError) as caught:
        ObsOwnerRepositoryImpl().find_owners(PACKAGE)

    assert "person" in str(caught.value)


@pytest.mark.parametrize(
    "member",
    [b"<maintainer name='user-a'/>", b"<person xmlns='urn:x' name='user-a'/>"],
    ids=["unknown-tag", "namespaced"],
)
def test_find_owners_refuses_an_owner_holding_something_other_than_a_person_or_group(
    monkeypatch: pytest.MonkeyPatch, member: bytes
) -> None:
    # Why the loop walks the children instead of searching them. findall("person")
    # and iter("person") both *search*, and whatever a search fails to match is
    # simply absent from the result -- an owner silently lost, which prints as a
    # difference against a project that has none. The namespaced case is the one
    # that rules out the obvious repair: ElementTree renders the tag as
    # "{urn:x}person", which neither call matches. Comparing the tag and refusing
    # everything else covers both with one rule.
    _install_osc(
        monkeypatch,
        _RecordingOsc(stdout=b"<collection><owner>" + member + b"</owner></collection>"),
    )

    with pytest.raises(DataSourceError):
        ObsOwnerRepositoryImpl().find_owners(PACKAGE)


def test_find_owners_refuses_a_member_that_nests_another_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The second way a search loses an owner: findall on the <owner> sees only
    # the outer <person>, so the nested one is gone without a trace. The schema
    # has no nesting, so its presence proves the document is not the one it
    # claims to be -- checked before the name is read, because the outer element
    # carries a perfectly plausible name and reading it would mask the problem.
    _install_osc(
        monkeypatch,
        _RecordingOsc(
            stdout=(
                b"<collection><owner>"
                b"<person name='user-a'><person name='user-b'/></person>"
                b"</owner></collection>"
            )
        ),
    )

    with pytest.raises(DataSourceError):
        ObsOwnerRepositoryImpl().find_owners(PACKAGE)


@pytest.mark.parametrize("member", [b"<person/>", b"<person name=''/>"], ids=["missing", "empty"])
def test_find_owners_refuses_a_member_without_a_usable_name(
    monkeypatch: pytest.MonkeyPatch, member: bytes
) -> None:
    # An owner whose name was lost is not an owner, and neither variant can be
    # carried forward: dropping it understates the ownership of the package and
    # keeping the empty string adds a set member that matches nobody. `not name`
    # rather than `is None`, because name='' is present and is not a name either.
    _install_osc(
        monkeypatch,
        _RecordingOsc(stdout=b"<collection><owner>" + member + b"</owner></collection>"),
    )

    with pytest.raises(DataSourceError):
        ObsOwnerRepositoryImpl().find_owners(PACKAGE)


def test_the_implementation_satisfies_the_obs_owner_repository_protocol() -> None:
    # mypy is scoped to src/, so nothing type-checks the conformance of this Impl
    # to the Protocol the command layer will inject against. isinstance against a
    # runtime_checkable Protocol compares attribute names only, so it catches a
    # renamed method and not a changed signature.
    assert isinstance(ObsOwnerRepositoryImpl(), ObsOwnerRepository)
