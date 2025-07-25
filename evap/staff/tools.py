from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import Group
from django.core.exceptions import SuspiciousOperation
from django.db import transaction
from django.db.models import Count, Max, Model
from django.urls import reverse
from django.utils.html import escape, format_html, format_html_join
from django.utils.safestring import SafeString
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from evap.evaluation.models import Contribution, Course, Evaluation, TextAnswer, UserProfile
from evap.evaluation.models_logging import LogEntry
from evap.evaluation.tools import StrOrPromise, clean_email, is_external_email
from evap.grades.models import GradeDocument
from evap.results.tools import STATES_WITH_RESULTS_CACHING, cache_results

if TYPE_CHECKING:
    from django.db.models.fields.related_descriptors import RelatedManager


class ImportType(Enum):
    USER = "user"
    CONTRIBUTOR = "contributor"
    PARTICIPANT = "participant"
    SEMESTER = "semester"
    USER_BULK_UPDATE = "user_bulk_update"


def generate_import_path(user_id, import_type) -> Path:
    return settings.MEDIA_ROOT / "temp_import_files" / f"{user_id}.{import_type.value}.xls"


def save_import_file(excel_file, user_id, import_type):
    path = generate_import_path(user_id, import_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as file:
        for chunk in excel_file.chunks():
            file.write(chunk)
    excel_file.seek(0)


def delete_import_file(user_id, import_type):
    path = generate_import_path(user_id, import_type)
    path.unlink(missing_ok=True)


def import_file_exists(user_id, import_type):
    path = generate_import_path(user_id, import_type)
    return path.is_file()


def get_import_file_content_or_raise(user_id, import_type):
    path = generate_import_path(user_id, import_type)
    if not path.is_file():
        raise SuspiciousOperation("No test run performed previously.")
    with open(path, "rb") as file:
        return file.read()


def create_user_list_html_string_for_message(users: Iterable[UserProfile]) -> SafeString:
    return format_html_join("", "<br />{} {} ({})", ((user.first_name, user.last_name, user.email) for user in users))


def append_user_list_if_not_empty(message: str, user_profiles: Iterable[UserProfile]) -> SafeString:
    message = conditional_escape(message)
    if not user_profiles:
        return message + escape(".")
    return message + escape(":") + create_user_list_html_string_for_message(user_profiles)


def conditional_escape(s: str) -> SafeString:
    """
    Like Django's `conditional_escape`, but only distincs `str` and `SafeString` and thus always returns SafeString. Django's version also allows third-party classes and does not always return SafeString.
    """
    if isinstance(s, SafeString):
        return s
    return escape(s)


def find_matching_internal_user_for_email(request, email):
    # for internal users only the part before the @ must be the same to match a user to an email
    matching_users = [
        user
        for user in UserProfile.objects.filter(email__startswith=email.split("@")[0] + "@").order_by("id")
        if not user.is_external
    ]

    if not matching_users:
        return None

    if len(matching_users) > 1:
        raise UserProfile.MultipleObjectsReturned(matching_users)

    return matching_users[0]


def bulk_update_users(request, user_file_content, test_run):  # noqa: PLR0912
    # pylint: disable=too-many-locals
    # user_file must have one user per line in the format "{username},{email}"
    imported_emails = {clean_email(line.decode().split(",")[1]) for line in user_file_content.splitlines()}

    emails_of_users_to_be_created = []
    users_to_be_updated = []
    skipped_external_emails_counter = 0

    for imported_email in imported_emails:
        if is_external_email(imported_email):
            skipped_external_emails_counter += 1
            continue
        try:
            matching_user = find_matching_internal_user_for_email(request, imported_email)
        except UserProfile.MultipleObjectsReturned as e:
            messages.error(
                request,
                format_html(
                    _("Multiple users match the email {}:{}"),
                    imported_email,
                    create_user_list_html_string_for_message(e.args[0]),
                ),
            )
            return False

        if not matching_user:
            emails_of_users_to_be_created.append(imported_email)
        elif matching_user.email != imported_email:
            users_to_be_updated.append((matching_user, imported_email))

    emails_of_non_obsolete_users = set(imported_emails) | {user.email for user, _ in users_to_be_updated}
    deletable_users, users_to_mark_inactive = [], []
    for user in UserProfile.objects.exclude(email__in=emails_of_non_obsolete_users):
        if user.can_be_deleted_by_manager:
            deletable_users.append(user)
        elif user.is_active and user.can_be_marked_inactive_by_manager:
            users_to_mark_inactive.append(user)

    messages.info(
        request,
        _(
            "The uploaded text file contains {} internal and {} external users. The external users will be ignored. "
            "{} users are currently in the database. Of those, {} will be updated, {} will be deleted and {} will be "
            "marked inactive. {} new users will be created."
        ).format(
            len(imported_emails) - skipped_external_emails_counter,
            skipped_external_emails_counter,
            UserProfile.objects.count(),
            len(users_to_be_updated),
            len(deletable_users),
            len(users_to_mark_inactive),
            len(emails_of_users_to_be_created),
        ),
    )
    if users_to_be_updated:
        messages.info(
            request,
            format_html(
                _("Users to be updated are:{}"),
                format_html_join(
                    "",
                    "<br />{} {} ({} &gt; {})",
                    ((user.first_name, user.last_name, user.email, email) for user, email in users_to_be_updated),
                ),
            ),
        )
    if deletable_users:
        messages.info(
            request,
            format_html(_("Users to be deleted are:{}"), create_user_list_html_string_for_message(deletable_users)),
        )
    if users_to_mark_inactive:
        messages.info(
            request,
            format_html(
                _("Users to be marked inactive are:{}"),
                create_user_list_html_string_for_message(users_to_mark_inactive),
            ),
        )
    if emails_of_users_to_be_created:
        messages.info(
            request,
            format_html(
                _("Users to be created are:{}"),
                format_html_join("", "<br />{}", ((email,) for email in emails_of_users_to_be_created)),
            ),
        )

    with transaction.atomic():
        for user in deletable_users + users_to_mark_inactive:
            for message in remove_user_from_represented_and_ccing_users(
                user, deletable_users + users_to_mark_inactive, test_run
            ):
                messages.warning(request, message)
        for user in users_to_mark_inactive:
            for message in remove_participations_if_inactive(user, test_run):
                messages.warning(request, message)
        if test_run:
            messages.info(request, _("No data was changed in this test run."))
        else:
            for user in deletable_users:
                user.delete()
            for user in users_to_mark_inactive:
                user.is_active = False
                user.save()

            for user, email in users_to_be_updated:
                user.email = email
                user.save()
            userprofiles_to_create = [UserProfile(email=email) for email in emails_of_users_to_be_created]

            UserProfile.objects.bulk_create(userprofiles_to_create)
            messages.success(request, _("Users have been successfully updated."))

    return True


@transaction.atomic
def merge_users(  # noqa: PLR0915  # This is much stuff to do. However, splitting it up into subtasks doesn't make much sense.
    main_user, other_user, preview=False
):
    """Merges other_user into main_user"""

    merged_user = {}
    merged_user["is_active"] = main_user.is_active or other_user.is_active
    merged_user["title"] = main_user.title or other_user.title or ""
    merged_user["first_name_chosen"] = main_user.first_name_chosen or other_user.first_name_chosen or ""
    merged_user["first_name_given"] = main_user.first_name_given or other_user.first_name_given or ""
    merged_user["last_name"] = main_user.last_name or other_user.last_name or ""
    merged_user["email"] = main_user.email or other_user.email or None
    merged_user["notes"] = f"{main_user.notes}\n{other_user.notes}".strip()

    merged_user["groups"] = Group.objects.filter(user__in=[main_user, other_user]).distinct()
    merged_user["is_superuser"] = main_user.is_superuser or other_user.is_superuser
    merged_user["is_proxy_user"] = main_user.is_proxy_user or other_user.is_proxy_user
    merged_user["delegates"] = UserProfile.objects.filter(represented_users__in=[main_user, other_user]).distinct()
    merged_user["represented_users"] = UserProfile.objects.filter(delegates__in=[main_user, other_user]).distinct()
    merged_user["cc_users"] = UserProfile.objects.filter(ccing_users__in=[main_user, other_user]).distinct()
    merged_user["ccing_users"] = UserProfile.objects.filter(cc_users__in=[main_user, other_user]).distinct()

    errors = []
    warnings = []
    courses_main_user_is_responsible_for = main_user.get_sorted_courses_responsible_for()
    if any(
        course in courses_main_user_is_responsible_for for course in other_user.get_sorted_courses_responsible_for()
    ):
        errors.append("courses_responsible_for")
    if any(
        contribution.evaluation in [contribution.evaluation for contribution in main_user.get_sorted_contributions()]
        for contribution in other_user.get_sorted_contributions()
    ):
        errors.append("contributions")
    if any(
        evaluation in main_user.get_sorted_evaluations_participating_in()
        for evaluation in other_user.get_sorted_evaluations_participating_in()
    ):
        errors.append("evaluations_participating_in")
    if any(
        evaluation in main_user.get_sorted_evaluations_voted_for()
        for evaluation in other_user.get_sorted_evaluations_voted_for()
    ):
        errors.append("evaluations_voted_for")

    if main_user.reward_point_grantings.all().exists() and other_user.reward_point_grantings.all().exists():
        warnings.append("rewards")

    merged_user["courses_responsible_for"] = Course.objects.filter(responsibles__in=[main_user, other_user]).order_by(
        "semester__created_at", "name_de"
    )
    merged_user["contributions"] = Contribution.objects.filter(contributor__in=[main_user, other_user]).order_by(
        "evaluation__course__semester__created_at", "evaluation__name_de"
    )
    merged_user["evaluations_participating_in"] = Evaluation.objects.filter(
        participants__in=[main_user, other_user]
    ).order_by("course__semester__created_at", "name_de")
    merged_user["evaluations_voted_for"] = Evaluation.objects.filter(voters__in=[main_user, other_user]).order_by(
        "course__semester__created_at", "name_de"
    )

    merged_user["reward_point_grantings"] = (
        main_user.reward_point_grantings.all() or other_user.reward_point_grantings.all()
    )
    merged_user["reward_point_redemptions"] = (
        main_user.reward_point_redemptions.all() or other_user.reward_point_redemptions.all()
    )

    if preview or errors:
        return merged_user, errors, warnings

    # update responsibility
    for course in Course.objects.filter(responsibles__in=[other_user]):
        responsibles = list(course.responsibles.all())
        responsibles.remove(other_user)
        responsibles.append(main_user)
        course.responsibles.set(responsibles)

    GradeDocument.objects.filter(last_modified_user=other_user).update(last_modified_user=main_user)

    # email must not exist twice. other_user can't be deleted before contributions have been changed
    other_user.email = ""
    other_user.save()

    # update values for main user
    for key, value in merged_user.items():
        attr = getattr(main_user, key)
        if hasattr(attr, "set"):
            attr.set(value)  # use the 'set' method for e.g. many-to-many relations
        else:
            setattr(main_user, key, value)  # use direct assignment for everything else
    main_user.save()

    # delete rewards
    other_user.reward_point_grantings.all().delete()
    other_user.reward_point_redemptions.all().delete()

    # update logs
    LogEntry.objects.filter(user=other_user).update(user=main_user)

    # refresh results cache
    evaluations = Evaluation.objects.filter(
        contributions__contributor=main_user, state__in=STATES_WITH_RESULTS_CACHING
    ).distinct()
    for evaluation in evaluations:
        cache_results(evaluation)

    # delete other_user
    other_user.delete()

    return merged_user, errors, warnings


def find_unreviewed_evaluations(semester, excluded):
    # as evaluations are open for an offset of hours after vote_end_datetime, the evaluations ending yesterday are also excluded during offset
    exclude_date = date.today()
    if datetime.now().hour < settings.EVALUATION_END_OFFSET_HOURS:
        exclude_date -= timedelta(days=1)

    # Evaluations where the grading process is finished should be shown first, need to be sorted in Python
    return sorted(
        (
            semester.evaluations.exclude(pk__in=excluded)
            .exclude(state=Evaluation.State.PUBLISHED)
            .exclude(vote_end_date__gte=exclude_date)
            .exclude(can_publish_text_results=False)
            .filter(
                contributions__textanswer_set__review_decision=TextAnswer.ReviewDecision.UNDECIDED,
                contributions__textanswer_set__is_flagged=False,
            )
            .annotate(num_unreviewed_textanswers=Count("contributions__textanswer_set"))
        ),
        key=lambda e: (-e.grading_process_is_finished, e.vote_end_date, -e.num_unreviewed_textanswers),
    )


def remove_user_from_represented_and_ccing_users(user, ignored_users=None, test_run=False):
    remove_messages = []
    ignored_users = ignored_users or []
    for represented_user in user.represented_users.exclude(id__in=[user.id for user in ignored_users]):
        if test_run:
            remove_messages.append(
                _("{} will be removed from the delegates of {}.").format(user.full_name, represented_user.full_name)
            )
        else:
            represented_user.delegates.remove(user)
            remove_messages.append(
                _("Removed {} from the delegates of {}.").format(user.full_name, represented_user.full_name)
            )
    for cc_user in user.ccing_users.exclude(id__in=[user.id for user in ignored_users]):
        if test_run:
            remove_messages.append(
                _("{} will be removed from the CC users of {}.").format(user.full_name, cc_user.full_name)
            )
        else:
            cc_user.cc_users.remove(user)
            remove_messages.append(_("Removed {} from the CC users of {}.").format(user.full_name, cc_user.full_name))
    return remove_messages


def remove_participations_if_inactive(user: UserProfile, test_run=False) -> list[StrOrPromise]:
    if user.is_active and not user.can_be_marked_inactive_by_manager:
        return []
    last_participation = user.evaluations_participating_in.aggregate(Max("vote_end_date"))["vote_end_date__max"]
    if (
        last_participation is None
        or (date.today() - last_participation) < settings.PARTICIPATION_DELETION_AFTER_INACTIVE_TIME
    ):
        return []

    evaluation_count = user.evaluations_participating_in.count()
    if test_run:
        return [
            ngettext(
                "{} participation of {} would be removed due to inactivity.",
                "{} participations of {} would be removed due to inactivity.",
                evaluation_count,
            ).format(evaluation_count, user.full_name)
        ]
    user.evaluations_participating_in.clear()
    return [
        ngettext(
            "{} participation of {} was removed due to inactivity.",
            "{} participations of {} were removed due to inactivity.",
            evaluation_count,
        ).format(evaluation_count, user.full_name)
    ]


def user_edit_link(user_id):
    return format_html(
        '<a href="{}" target=_blank><span class="fas fa-user-pen"></span> {}</a>',
        reverse("staff:user_edit", kwargs={"user_id": user_id}),
        _("edit user"),
    )


T = TypeVar("T", bound=Model)


def update_or_create_with_changes(
    model: type[T],
    defaults=None,
    **kwargs,
) -> tuple[T, bool, dict[str, tuple[Any, Any]]]:
    """Do update_or_create and track changed values."""

    if not defaults:
        defaults = {}

    obj, created = model._default_manager.get_or_create(**kwargs, defaults=defaults)

    if created:
        return obj, True, {}

    changes = update_with_changes(obj, defaults)

    return obj, False, changes


def update_with_changes(obj: Model, defaults: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Update a model instance and track changed values."""

    changes = {}
    for key, value in defaults.items():
        if getattr(obj, key) != value:
            changes[key] = (getattr(obj, key), value)
            setattr(obj, key, value)

    if changes:
        obj.save()

    return changes


def update_m2m_with_changes(obj: Model, field: str, new_data: Sequence) -> dict[str, tuple[Any, Any]]:
    """Update a m2m field of a model and track changed values."""
    manager: RelatedManager = getattr(obj, field)
    old_data = manager.all()
    if set(old_data) != set(new_data):
        manager.set(new_data)
        return {field: (old_data, new_data)}
    return {}
