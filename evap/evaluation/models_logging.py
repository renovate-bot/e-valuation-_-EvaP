import itertools
import threading
from collections import defaultdict, namedtuple
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, time
from enum import Enum
from json import JSONEncoder
from typing import assert_never

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models.signals import m2m_changed
from django.dispatch import receiver
from django.forms.models import model_to_dict
from django.template.defaultfilters import yesno
from django.utils.formats import localize
from django.utils.translation import gettext_lazy as _

from evap.evaluation.tools import capitalize_first

CREATE_LOGENTRIES = True


@contextmanager
def disable_logentries() -> Iterator[None]:
    global CREATE_LOGENTRIES  # noqa: PLW0603
    old_mode = CREATE_LOGENTRIES
    CREATE_LOGENTRIES = False
    try:
        yield
    finally:
        CREATE_LOGENTRIES = old_mode


class FieldActionType(str, Enum):
    M2M_ADD = "add"
    M2M_REMOVE = "remove"
    M2M_CLEAR = "clear"
    INSTANCE_CREATE = "create"
    VALUE_CHANGE = "change"
    INSTANCE_DELETE = "delete"


FieldAction = namedtuple("FieldAction", "label type items")


class InstanceActionType(str, Enum):
    CREATE = "create"
    CHANGE = "change"
    DELETE = "delete"


class LogJSONEncoder(JSONEncoder):
    """
    As JSON can't store datetime objects, we localize them to strings.
    """

    def default(self, o):
        # o is the object to serialize -- we can't rename the argument in JSONEncoder
        if isinstance(o, date | time | datetime):
            return localize(o)
        return super().default(o)


def _choice_to_display(field, choice):  # does not support nested choices
    for key, label in field.choices:
        if key == choice:
            return label
    return choice


def _field_actions_for_field(field, actions):
    label = capitalize_first(getattr(field, "verbose_name", field.name))

    for field_action_type, items in actions.items():
        if field.many_to_many or field.many_to_one or field.one_to_one:
            # convert item values from primary keys to string-representation for relation-based fields
            related_objects = field.related_model.objects.filter(pk__in=items)
            missing = len(items) - related_objects.count()
            items = [str(obj) for obj in related_objects] + [_("<deleted object>")] * missing
        elif hasattr(field, "choices") and field.choices:
            # convert values from choice-based fields to their display equivalent
            items = [_choice_to_display(field, item) for item in items]
        elif isinstance(field, models.BooleanField):
            # convert boolean to yes/no
            items = list(map(yesno, items))
        yield FieldAction(label, field_action_type, items)


class LogEntry(models.Model):
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="logs_about_me")
    content_object_id = models.PositiveIntegerField(db_index=True)
    content_object = GenericForeignKey("content_type", "content_object_id")
    attached_to_object_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="logs_for_me")
    attached_to_object_id = models.PositiveIntegerField(db_index=True)
    attached_to_object = GenericForeignKey("attached_to_object_type", "attached_to_object_id")
    datetime = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    action_type = models.CharField(max_length=255, choices=[(value, value) for value in InstanceActionType])
    request_id = models.CharField(max_length=36, blank=True)
    data = models.JSONField(default=dict, encoder=LogJSONEncoder)

    class Meta:
        ordering = ["-datetime", "-id"]

    @property
    def field_context_data(self):
        model = self.content_type.model_class()
        return {
            field_name: list(_field_actions_for_field(model._meta.get_field(field_name), actions))
            for field_name, actions in self.data.items()
        }

    @property
    def message(self):
        match self.action_type:
            case InstanceActionType.CHANGE:
                if self.content_object:
                    message = _("The {cls} {obj} was changed.")
                else:  # content_object might be deleted
                    message = _("A {cls} was changed.")
            case InstanceActionType.CREATE:
                if self.content_object:
                    message = _("The {cls} {obj} was created.")
                else:
                    message = _("A {cls} was created.")
            case InstanceActionType.DELETE:
                message = _("A {cls} was deleted.")
            case _:
                assert_never(self.action_type)

        return message.format(
            cls=capitalize_first(self.content_type.model_class()._meta.verbose_name),
            obj=f'"{str(self.content_object)}"' if self.content_object else "",
        )


class LoggedModel(models.Model):
    thread = threading.local()

    class Meta:
        abstract = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._logentry = None
        self._m2m_changes = defaultdict(lambda: defaultdict(list))

    def save(self, *args, **kw):
        # Are we creating a new instance?
        # https://docs.djangoproject.com/en/3.0/ref/models/instances/#customizing-model-loading
        if self._state.adding:
            # we need to attach a logentry to an existing object, so we save this newly created instance first
            super().save(*args, **kw)
            self.log_instance_create()
        else:
            # when saving an existing instance, we get changes by comparing to the version from the database
            # therefore we save the instance after building the logentry
            self.log_instance_change()
            super().save(*args, **kw)

    def _as_dict(self):
        """
        Return a dict mapping field names to values saved in this instance.
        Only include field names that are not to be ignored for logging and
        that don't name m2m fields.
        """
        fields = [
            field.name
            for field in type(self)._meta.get_fields()
            if field.name not in self.unlogged_fields and not field.many_to_many
        ]
        return model_to_dict(self, fields)

    def _get_change_data(self, action_type: InstanceActionType):
        """
        Return a dict mapping field names to changes that happened in this model instance,
        depending on the action that is being done to the instance.
        """
        self_dict = self._as_dict()
        if action_type == InstanceActionType.CREATE:
            changes = {
                field_name: {FieldActionType.INSTANCE_CREATE: [created_value]}
                for field_name, created_value in self_dict.items()
                if created_value is not None
            }
        elif action_type == InstanceActionType.CHANGE:
            old_dict = type(self)._default_manager.get(pk=self.pk)._as_dict()
            changes = {
                field_name: {FieldActionType.VALUE_CHANGE: [old_value, self_dict[field_name]]}
                for field_name, old_value in old_dict.items()
                if old_value != self_dict[field_name]
            }
        elif action_type == InstanceActionType.DELETE:
            old_dict = type(self)._default_manager.get(pk=self.pk)._as_dict()
            changes = {
                field_name: {FieldActionType.INSTANCE_DELETE: [deleted_value]}
                for field_name, deleted_value in old_dict.items()
                if deleted_value is not None
            }
            # as the instance is being deleted, we also need to pull out all m2m values
            m2m_field_names = [
                field.name
                for field in type(self)._meta.get_fields()
                if field.many_to_many and field.name not in self.unlogged_fields
            ]
            for field_name, related_objects in model_to_dict(self, m2m_field_names).items():
                changes[field_name] = {FieldActionType.INSTANCE_DELETE: [obj.pk for obj in related_objects]}
        else:
            raise ValueError(f"Unknown action type: '{action_type}'")

        return changes

    def log_m2m_change(self, field_name, action_type: FieldActionType, change_list, **kwargs):
        # This might be called multiple times with cumulating changes
        # But this is fine, since the old changes will be included in the latest log update
        # See https://github.com/e-valuation/EvaP/issues/1594
        self._m2m_changes[field_name][action_type] += change_list
        self._update_log(self._m2m_changes, InstanceActionType.CHANGE, **kwargs)

    def log_instance_create(self):
        changes = self._get_change_data(InstanceActionType.CREATE)
        self._update_log(changes, InstanceActionType.CREATE)

    def log_instance_change(self):
        changes = self._get_change_data(InstanceActionType.CHANGE)
        self._update_log(changes, InstanceActionType.CHANGE)

    def log_instance_delete(self):
        changes = self._get_change_data(InstanceActionType.DELETE)
        self._update_log(changes, InstanceActionType.DELETE)

    def _create_log_entry(self, action_type=InstanceActionType.CREATE):
        try:
            user = self.thread.request.user
            request_id = self.thread.request_id
        except AttributeError:
            user = None
            request_id = ""

        attach_to_model, attached_to_object_id = self.object_to_attach_logentries_to
        attached_to_object_type = ContentType.objects.get_for_model(attach_to_model)
        return LogEntry(
            content_object=self,
            attached_to_object_type=attached_to_object_type,
            attached_to_object_id=attached_to_object_id,
            user=user,
            request_id=request_id,
            action_type=action_type,
        )

    def _attach_log_entry_if_not_exists(self, *args, **kwargs):
        if not self._logentry:
            self._logentry = self._create_log_entry(*args, **kwargs)

    def _update_log(self, changes, action_type: InstanceActionType, store_in_db=True):
        if not changes or not CREATE_LOGENTRIES:
            return

        self._attach_log_entry_if_not_exists(action_type)
        # if adding more changes here, make sure you update all bulk_update calls that explicitly need to list changed
        # fields, too.
        self._logentry.data.update(changes)

        if store_in_db:
            self._logentry.save()

    def delete(self, *args, **kw):
        self.log_instance_delete()
        self.related_logentries().delete()
        super().delete(*args, **kw)

    @staticmethod
    def update_log_after_bulk_create(instances):
        for instance in instances:
            instance._logentry = instance._create_log_entry()

        log_entries = [instance._logentry for instance in instances]
        LogEntry.objects.bulk_create(log_entries)

    @staticmethod
    def update_log_after_m2m_bulk_create(
        from_instances, through_instances, from_pk_attribute: str, to_pk_attribute: str, m2m_field: str
    ):
        added_related = defaultdict(list)
        for instance in through_instances:
            from_pk = getattr(instance, from_pk_attribute)
            to_pk = getattr(instance, to_pk_attribute)
            added_related[from_pk].append(to_pk)

        for instance in from_instances:
            instance.log_m2m_change(m2m_field, FieldActionType.M2M_ADD, added_related[instance.pk], store_in_db=False)

        logentries = [instance._logentry for instance in from_instances]

        to_create = [logentry for logentry in logentries if logentry.pk is None]
        to_update = [logentry for logentry in logentries if logentry.pk is not None]

        LogEntry.objects.bulk_create(to_create)
        LogEntry.objects.bulk_update(to_update, ["data"])

    def related_logentries(self):
        """
        Return a queryset with all logentries that should be shown with this model.
        """
        return LogEntry.objects.filter(
            attached_to_object_type=ContentType.objects.get_for_model(type(self)),
            attached_to_object_id=self.pk,
        )

    def grouped_logentries(self):
        """
        Returns a list of lists of logentries for display. The order is not changed.
        Logentries are grouped if they have a matching request_id.
        """
        yield from (
            list(group)
            for key, group in itertools.groupby(
                self.related_logentries().select_related("user"),
                lambda entry: entry.request_id or entry.pk,
            )
        )

    @property
    def object_to_attach_logentries_to(self):
        """
        Return a model class and primary key for the object for which this logentry should be shown.
        By default, show it to the object described by the logentry itself.

        Returning the model instance directly might rely on fetching that object from the database,
        which can break bulk loading in some cases, so we don't do that.
        """
        return type(self), self.pk

    @property
    def unlogged_fields(self):
        """Specify a list of field names so that these fields don't get logged."""
        return ["id", "order"]


@receiver(m2m_changed)
def _m2m_changed(sender, instance, action, reverse, model, pk_set, **kwargs):  # noqa: PLR0912
    model_class = model if reverse else type(instance)
    field_name = next(
        (field.name for field in model_class._meta.many_to_many if getattr(model_class, field.name).through == sender),
        None,
    )
    if field_name is None:
        return

    if not issubclass(model_class, LoggedModel):
        return

    if reverse:
        match action:
            case "pre_remove":
                action_type = FieldActionType.M2M_REMOVE
            case "pre_add":
                action_type = FieldActionType.M2M_ADD
            case "pre_clear":
                # Since we are not clearing the LoggedModdel instance, we need to log the removal of the related instances
                action_type = FieldActionType.M2M_REMOVE
            case _:
                return

        if pk_set:
            related_instances = model.objects.filter(pk__in=pk_set)
        else:
            # When action is pre_clear, pk_set is None, so we need to get the related instances from the instance itself
            field = model._meta.get_field(field_name)
            related_name = field.remote_field.get_accessor_name()
            related_instances = getattr(instance, related_name).all()

        for related_instance in related_instances:
            if field_name in related_instance.unlogged_fields:
                continue

            related_instance.log_m2m_change(field_name, action_type, [instance.pk])

    else:
        if field_name in instance.unlogged_fields:
            return

        if action == "pre_remove":
            instance.log_m2m_change(field_name, FieldActionType.M2M_REMOVE, list(pk_set))
        elif action == "pre_add":
            instance.log_m2m_change(field_name, FieldActionType.M2M_ADD, list(pk_set))
        elif action == "pre_clear":
            instance.log_m2m_change(field_name, FieldActionType.M2M_CLEAR, [])
