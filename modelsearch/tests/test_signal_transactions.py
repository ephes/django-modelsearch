from datetime import date
from unittest import mock

from django.db import transaction
from django.test import TransactionTestCase, override_settings

from modelsearch import index
from modelsearch.models import IndexEntry
from modelsearch.tasks import insert_or_update_object_task
from modelsearch.test.testapp import models


class TestSignalTransactions(TransactionTestCase):
    databases = {"default", "other"}

    def setUp(self):
        self.backend = mock.Mock()
        patcher = mock.patch(
            "modelsearch.index.get_search_backends_with_name",
            return_value=[("external", self.backend)],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def create_book(self, using="default", **kwargs):
        return models.Book.objects.using(using).create(
            title=kwargs.get("title", "Synthetic book"),
            publication_date=date(2026, 1, 1),
            number_of_pages=42,
            **({"pk": kwargs["pk"]} if "pk" in kwargs else {}),
        )

    @mock.patch("modelsearch.signal_handlers.insert_or_update_object_task")
    def test_save_enqueues_only_after_commit_with_original_identity(self, task):
        enqueue = task.enqueue
        with transaction.atomic():
            obj = self.create_book()
            pk = str(obj.pk)
            obj.pk = None
            enqueue.assert_not_called()
        enqueue.assert_called_once_with(
            models.Book._meta.app_label, "book", pk, using="default"
        )

    @mock.patch("modelsearch.signal_handlers.insert_or_update_object_task")
    def test_rolled_back_save_does_not_enqueue(self, task):
        enqueue = task.enqueue
        with transaction.atomic():
            self.create_book()
            transaction.set_rollback(True)
        enqueue.assert_not_called()
        self.backend.add.assert_not_called()

    @mock.patch("modelsearch.signal_handlers.insert_or_update_object_task")
    def test_rolled_back_savepoint_does_not_enqueue(self, task):
        enqueue = task.enqueue
        with transaction.atomic():
            with transaction.atomic():
                self.create_book()
                transaction.set_rollback(True)
        enqueue.assert_not_called()

    def test_delete_runs_after_commit_with_original_uuid(self):
        obj = models.AdvertWithCustomUUIDPrimaryKey.objects.create(text="Synthetic")
        original_pk = obj.pk
        with transaction.atomic():
            obj.delete()
            self.assertIsNone(obj.pk)
            self.backend.delete.assert_not_called()
        self.backend.delete.assert_called_once()
        deleted = self.backend.delete.call_args.args[0]
        self.assertEqual(deleted.pk, original_pk)
        self.assertEqual(deleted._state.db, "default")
        self.assertIsNone(obj.pk)

    def test_rolled_back_delete_does_not_mutate_external_index(self):
        obj = self.create_book()
        original_pk = obj.pk
        self.backend.reset_mock()
        with transaction.atomic():
            obj.delete()
            transaction.set_rollback(True)
        self.backend.delete.assert_not_called()
        self.assertTrue(models.Book.objects.filter(pk=original_pk).exists())

    def test_autocommit_save_and_delete(self):
        obj = self.create_book()
        self.backend.add.assert_called_once()
        self.assertEqual(self.backend.add.call_args.args[0].pk, obj.pk)
        original_pk = obj.pk
        obj.delete()
        self.backend.delete.assert_called_once()
        self.assertEqual(self.backend.delete.call_args.args[0].pk, original_pk)

    def test_secondary_commit_uses_source_content_and_membership(self):
        default = self.create_book(title="Don't index me!")
        self.backend.reset_mock()
        # The secondary commit must not wait for default's transaction.
        with transaction.atomic(using="default"):
            with transaction.atomic(using="other"):
                other = self.create_book("other", pk=default.pk, title="Other content")
                self.backend.add.assert_not_called()
            self.backend.add.assert_called_once()
            indexed = self.backend.add.call_args.args[0]
            self.assertEqual(indexed.title, other.title)
            self.assertEqual(indexed._state.db, "other")

    def test_membership_uses_secondary_exclusion(self):
        default = self.create_book()
        with mock.patch("modelsearch.signal_handlers.insert_or_update_object_task"):
            other = self.create_book("other", pk=default.pk, title="Don't index me!")
        self.backend.reset_mock()
        index.insert_or_update_object(other)
        self.backend.add.assert_not_called()

    def test_secondary_rollback_discards_save_and_delete(self):
        obj = self.create_book("other")
        original_pk = obj.pk
        self.backend.reset_mock()
        with transaction.atomic(using="other"):
            obj.delete(using="other")
            self.create_book("other")
            transaction.set_rollback(True, using="other")
        self.backend.add.assert_not_called()
        self.backend.delete.assert_not_called()
        self.assertTrue(
            models.Book.objects.using("other").filter(pk=original_pk).exists()
        )

    def test_secondary_delete_keeps_alias_and_identity(self):
        obj = self.create_book("other")
        original_pk = obj.pk
        with transaction.atomic(using="other"):
            obj.delete(using="other")
            self.backend.delete.assert_not_called()
        deleted = self.backend.delete.call_args.args[0]
        self.assertEqual(deleted.pk, original_pk)
        self.assertEqual(deleted._state.db, "other")

    def test_old_task_arguments_still_use_default(self):
        obj = self.create_book()
        self.backend.reset_mock()
        insert_or_update_object_task.call(
            models.Book._meta.app_label, "book", str(obj.pk)
        )
        self.backend.add.assert_called_once()
        self.assertEqual(self.backend.add.call_args.args[0]._state.db, "default")

    def test_task_for_deleted_row_is_harmless(self):
        obj = self.create_book()
        original_pk = str(obj.pk)
        obj.delete()
        self.backend.reset_mock()
        insert_or_update_object_task.call(
            models.Book._meta.app_label, "book", original_pk
        )
        self.backend.add.assert_not_called()

    def test_create_then_delete_in_one_transaction(self):
        with transaction.atomic():
            obj = self.create_book()
            original_pk = obj.pk
            obj.delete()
        self.backend.add.assert_not_called()
        self.backend.delete.assert_called_once()
        self.assertEqual(self.backend.delete.call_args.args[0].pk, original_pk)


@override_settings(
    MODELSEARCH_BACKENDS={"default": {"BACKEND": "modelsearch.backends.database"}}
)
class TestDatabaseSignalTransactions(TransactionTestCase):
    def test_committed_save_and_rolled_back_delete(self):
        with transaction.atomic():
            obj = models.AdvertWithCustomUUIDPrimaryKey.objects.create(text="Synthetic")
            original_pk = obj.pk
            self.assertFalse(
                IndexEntry.objects.filter(object_id=str(original_pk)).exists()
            )
        self.assertTrue(IndexEntry.objects.filter(object_id=str(original_pk)).exists())
        with transaction.atomic():
            obj.delete()
            transaction.set_rollback(True)
        self.assertTrue(IndexEntry.objects.filter(object_id=str(original_pk)).exists())
        models.AdvertWithCustomUUIDPrimaryKey.objects.get(pk=original_pk).delete()
        self.assertFalse(IndexEntry.objects.filter(object_id=str(original_pk)).exists())

    def test_rolled_back_save_leaves_no_index_entry(self):
        with transaction.atomic():
            models.AdvertWithCustomUUIDPrimaryKey.objects.create(text="Synthetic")
            transaction.set_rollback(True)
        self.assertFalse(IndexEntry.objects.exists())
