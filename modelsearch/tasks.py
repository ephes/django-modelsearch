from inspect import signature

from django.apps import apps
from django.db import DEFAULT_DB_ALIAS
from django_tasks import task

from modelsearch import index


# Older django-tasks versions otherwise defer again on the default connection.
# The signal handler already owns the source database's commit boundary.
_task_options = (
    {"enqueue_on_commit": False}
    if "enqueue_on_commit" in signature(task).parameters
    else {}
)


@task(**_task_options)
def insert_or_update_object_task(app_label, model_name, pk, using=DEFAULT_DB_ALIAS):
    model = apps.get_model(app_label, model_name)
    try:
        obj = model.objects.using(using).get(pk=pk)
    except model.DoesNotExist:
        # The row may have been deleted before the queued task runs.
        return
    index.insert_or_update_object(obj)
