import arrow
import pytest
from slacker import Error as SlackError

import zoo.auditing.runner as uut
from zoo import auditing
from zoo.auditing.models import Issue

pytestmark = pytest.mark.django_db


def test_check_context__init(repository, fake_path):
    context = uut.CheckContext(repository, fake_path)
    assert context.owner == repository.owner
    assert context.name == repository.name
    assert context.path == fake_path
    assert context.project_type == repository.project_type


@pytest.mark.parametrize(
    "issue_key, is_found, result",
    (
        ("missing:food", True, uut.Result("missing:food", True, None)),
        ("missing:coffee", False, uut.Result("missing:coffee", False, None)),
    ),
)
def test_check_context__result(issue_key, is_found, result, check_context):
    assert result == check_context.Result(issue_key, is_found)


def test_check_context__result_with_details(check_context):
    result = check_context.Result("games:doom", True, {"cheat": "IDDQD"})
    assert result == uut.Result("games:doom", True, {"cheat": "IDDQD"})


@pytest.mark.parametrize(
    "is_found, status",
    ((True, Issue.Status.NEW.value), (False, Issue.Status.NOT_FOUND.value)),
)
def test_save_check_result__found_new_issue(is_found, status, repository, freezer):
    uut.save_check_result(repository, "missing:coffee", is_found)

    new_issue = Issue.objects.get()
    assert new_issue.repository == repository
    assert new_issue.kind_key == "missing:coffee"
    assert new_issue.status == status
    assert new_issue.last_check == arrow.utcnow().datetime


@pytest.mark.parametrize(
    "is_found, old_status, new_status",
    (
        (True, Issue.Status.NOT_FOUND.value, Issue.Status.NEW.value),
        (True, Issue.Status.NEW.value, Issue.Status.NEW.value),
        (True, Issue.Status.FIXED.value, Issue.Status.REOPENED.value),
        (True, Issue.Status.REOPENED.value, Issue.Status.REOPENED.value),
        (True, Issue.Status.WONTFIX.value, Issue.Status.WONTFIX.value),
        (False, Issue.Status.NOT_FOUND.value, Issue.Status.NOT_FOUND.value),
        (False, Issue.Status.NEW.value, Issue.Status.FIXED.value),
        (False, Issue.Status.REOPENED.value, Issue.Status.FIXED.value),
        (False, Issue.Status.FIXED.value, Issue.Status.FIXED.value),
        (False, Issue.Status.WONTFIX.value, Issue.Status.FIXED.value),
    ),
)
def test_save_check_result__existing_issue(
    is_found, old_status, new_status, issue_factory, service_factory, freezer
):
    issue = issue_factory(
        status=old_status, last_check=arrow.utcnow().shift(years=-1).datetime
    )
    repository = issue.repository
    service = service_factory(repository=repository)

    uut.save_check_result(repository, issue.kind_key, is_found)

    updated_issue = Issue.objects.get()
    assert updated_issue.repository == repository
    assert updated_issue.kind_key == issue.kind_key
    assert updated_issue.status == new_status
    assert updated_issue.last_check == arrow.utcnow().datetime


@pytest.mark.parametrize(
    "is_found, details, expected_details",
    (
        (True, None, {}),
        (True, {"x": 1}, {"x": 1}),
        (False, None, {}),
        (False, {"x": 1}, {}),
    ),
)
def test_save_check_result__details_of_new_issue(
    is_found, details, expected_details, repository
):
    uut.save_check_result(repository, "missing:beer", is_found, details)

    new_issue = Issue.objects.get()
    assert new_issue.details == expected_details


@pytest.mark.parametrize(
    "is_found, old_details, details, expected_details",
    (
        (True, {}, None, {}),
        (True, {}, {"new": 3}, {"new": 3}),
        (True, {"old": 1}, None, {}),
        (True, {"old": 1}, {"new": 3}, {"new": 3}),
        (False, {}, None, {}),
        (False, {}, {"new": 3}, {}),
        (False, {"old": 1}, None, {}),
        (False, {"old": 1}, {"new": 3}, {}),
    ),
)
def test_save_check_result__details_of_existing_issue(
    is_found, old_details, details, expected_details, issue_factory, service_factory
):
    issue = issue_factory(details=old_details)
    service = service_factory(repository=issue.repository)

    uut.save_check_result(issue.repository, issue.kind_key, is_found, details)

    new_issue = Issue.objects.get()
    assert new_issue.details == expected_details


def check_passing(context):
    yield context.Result("check:passing", False, {"ignore": "details"})


def check_found(context):
    return [
        context.Result(
            "check:found",
            True,
            {
                "context_name": context.name,
                "context_owner": context.owner,
                "context_path": str(context.path),
            },
        )
    ]


def check_unknown(context):
    yield context.Result("check:unknown", None)


def check_failing(context):
    raise RuntimeError


def test_check_repository(repository, fake_path):
    checks = [check_passing, check_found, check_unknown]

    results = uut.check_repository(checks, repository, fake_path)

    assert list(results) == [
        uut.Result("check:passing", False, {"ignore": "details"}),
        uut.Result(
            "check:found",
            True,
            {
                "context_name": repository.name,
                "context_owner": repository.owner,
                "context_path": str(fake_path),
            },
        ),
    ]


def test_check_repository__failing_check(repository, fake_path, mocker):
    m_log = mocker.patch.object(uut, "log")

    results = uut.check_repository([check_failing], repository, fake_path)

    assert len(list(results)) == 0
    m_log.exception.assert_called_once_with(
        "auditing.check.error",
        repo_id=repository.id,
        check="check_failing",
        check_module="test.auditing.test_runner",
    )


def test_run_checks_and_save_results(repository, fake_path, mocker, service_factory):
    checks = [check_passing, check_found, check_unknown]

    uut.run_checks_and_save_results(checks, repository, fake_path)
    service = service_factory(repository=repository)

    assert Issue.objects.count() == 2

    passing_issue = Issue.objects.get(kind_key="check:passing")
    assert passing_issue.repository == repository
    assert passing_issue.status == Issue.Status.NOT_FOUND.value
    assert passing_issue.details == {}

    found_issue = Issue.objects.get(kind_key="check:found")
    assert found_issue.repository == repository
    assert found_issue.status == Issue.Status.NEW.value
    assert found_issue.details == {
        "context_name": repository.name,
        "context_owner": repository.owner,
        "context_path": str(fake_path),
    }

    checks = [check_failing, check_found, check_unknown]
    mocker.patch("zoo.auditing.runner.CHECKS", checks)

    uut.run_checks_and_save_results(checks, repository, fake_path)

    assert Issue.objects.count() == 2

    passing_issue = Issue.objects.get(kind_key="check:passing")
    found_issue = Issue.objects.get(kind_key="check:found")

    assert passing_issue.status == Issue.Status.NOT_FOUND.value
    assert passing_issue.deleted is True
    assert found_issue.status == Issue.Status.NEW.value


def test_run_checks_and_save_results__failing_check(repository, fake_path, mocker):
    m_log = mocker.patch.object(uut, "log")

    uut.run_checks_and_save_results([check_failing], repository, fake_path)

    m_log.exception.assert_called_once_with(
        "auditing.check.error",
        repo_id=repository.id,
        check="check_failing",
        check_module="test.auditing.test_runner",
    )


@pytest.mark.parametrize(
    "is_found, old_status, new_status",
    (
        (True, Issue.Status.NOT_FOUND.value, Issue.Status.NEW.value),
        (True, Issue.Status.NEW.value, Issue.Status.NEW.value),
        (True, Issue.Status.FIXED.value, Issue.Status.REOPENED.value),
        (True, Issue.Status.REOPENED.value, Issue.Status.REOPENED.value),
        (True, Issue.Status.WONTFIX.value, Issue.Status.WONTFIX.value),
        (False, Issue.Status.NOT_FOUND.value, Issue.Status.NOT_FOUND.value),
        (False, Issue.Status.NEW.value, Issue.Status.FIXED.value),
        (False, Issue.Status.REOPENED.value, Issue.Status.FIXED.value),
        (False, Issue.Status.FIXED.value, Issue.Status.FIXED.value),
        (False, Issue.Status.WONTFIX.value, Issue.Status.FIXED.value),
    ),
)
def test_determine_issue_status(is_found, old_status, new_status):
    assert uut.determine_issue_status(is_found, old_status) == new_status


def test_notify_status_change(mocker, issue_factory, service_factory):
    service = service_factory()
    issue = issue_factory(repository=service.repository)
    log = mocker.patch("zoo.auditing.runner.log", mocker.Mock())
    slack = mocker.patch("zoo.auditing.runner.slack", mocker.Mock())
    m_reverse = mocker.patch("zoo.auditing.runner.reverse", mocker.Mock())

    uut.notify_status_change(issue)
    slack.chat.post_message.assert_called_once()
    channel, text = slack.chat.post_message.call_args[0]
    assert channel == service.slack_channel
    assert issue.kind.title in text
    assert issue.repository.name in text
    m_reverse.assert_called_once_with(
        "audit_report", args=("services", service.owner_slug, service.name_slug)
    )

    # Unhappy path
    slack.chat.post_message.side_effect = SlackError("channel_not_found")
    uut.notify_status_change(issue)
    log.exception.assert_called_once_with(
        "auditing.update_issue.slack_error", error="Error('channel_not_found')"
    )


def test_notify_status_change_monorepo(mocker, issue_factory, service_factory):
    service = service_factory()
    service_2 = service_factory(repository=service.repository)
    issue = issue_factory(repository=service.repository)
    log = mocker.patch("zoo.auditing.runner.log", mocker.Mock())
    slack = mocker.patch("zoo.auditing.runner.slack", mocker.Mock())
    m_reverse = mocker.patch("zoo.auditing.runner.reverse", mocker.Mock())

    uut.notify_status_change(issue)
    assert slack.chat.post_message.call_count == 2
    channels = []
    texts = []
    for args_list in slack.chat.post_message.call_args_list:
        channels.append(args_list.args[0])
        texts.append(args_list.args[1])
    assert service.slack_channel in channels
    assert service_2.slack_channel in channels
    assert any(issue.kind.title in text for text in texts)
    assert any(issue.repository.name in text for text in texts)
    assert m_reverse.call_count == 2

    # Unhappy path
    slack.chat.post_message.side_effect = SlackError("channel_not_found")
    uut.notify_status_change(issue)
    assert log.exception.call_count == 2
