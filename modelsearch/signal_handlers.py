from copy import copy
from functools import partial

from django.db import transaction
from django.db.models.signals import post_delete, post_save

from . import index
from .tasks import insert_or_update_object_task


def post_save_signal_handler(instance, using, **kwargs):
    transaction.on_commit(
        partial(
            insert_or_update_object_task.enqueue,
            instance._meta.app_label,
            instance._meta.model_name,
            str(instance.pk),
            using=using,
        ),
        using=using,
    )


def post_delete_signal_handler(instance, using, **kwargs):
    # Django clears the original instance's PK after post_delete returns.
    transaction.on_commit(partial(index.remove_object, copy(instance)), using=using)


def register_signal_handlers():
    # Loop through list and register signal handlers for each one
    for model in index.get_indexed_models():
        if not getattr(model, "search_auto_update", True):
            continue

        post_save.connect(post_save_signal_handler, sender=model)
        post_delete.connect(post_delete_signal_handler, sender=model)
