"""Discovery of issued individual lists without exposing saved personal data."""
import json
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import graphene
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core.models import User
from individual.models import Individual

from household_validation import test_individual_workbook as fixtures
from household_validation.individual_snapshots import iter_selection
from household_validation.individual_upload import IndividualValidationUploadService
from household_validation.models import HouseholdValidationBatch
from household_validation.schema import Query


class IndividualBatchHistoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.IndividualWorkbookTests.setUpTestData.__func__(cls)

    def setUp(self):
        fixtures.IndividualWorkbookTests.setUp(self)
        self.history = IndividualValidationUploadService(self.actor)

    generate = fixtures.IndividualWorkbookTests.generate

    def test_lists_only_issued_active_individual_batches_with_sql_pagination(self):
        first, _ = self.generate(target_count=1)
        second, _ = self.generate(target_count=2)
        deleted, _ = self.generate(target_count=0)
        deleted.is_deleted = True
        deleted.save(user=self.actor)
        HouseholdValidationBatch(json_ext={"legacy": True}).save(user=self.actor)
        with CaptureQueriesContext(connection) as queries:
            page = self.history.batches(first=1)
        self.assertEqual(page["count"], 2)
        self.assertTrue(page["has_next_page"])
        self.assertEqual(page["items"][0]["batch_id"], second.pk)
        self.assertEqual(page["items"][0]["selected_individuals"], 2)
        self.assertEqual(self.history.batches(first=1, offset=1)["items"][0]["batch_id"], first.pk)
        self.assertEqual(self.history.batches(offset=2)["items"], [])
        self.assertTrue(any("LIMIT 1" in query["sql"] for query in queries))
        self.assertFalse(any('"raw_row"' in query["sql"] for query in queries))
        self.assertNotIn("json_ext", page["items"][0])

    def test_scope_is_applied_before_counts_and_paging_and_creators_keep_empty_lists(self):
        visible, _ = self.generate(target_count=1)
        private, _ = self.generate(target_count=2)
        visible_id = next(iter_selection(visible))["individual_uuid"]
        reader = User.objects.create(username="batch-history-reader")
        reader.has_perms = lambda perms: list(perms) == ["958003"]
        private.user_created = reader
        private.save(user=reader)
        # No selected people are visible, but the reader may see their own batch metadata.
        with patch.object(User, "is_imis_admin", new_callable=PropertyMock, return_value=False), patch.object(
            Individual, "get_queryset", side_effect=lambda qs, user: qs.none(),
        ):
            page = IndividualValidationUploadService(reader).batches()
        self.assertEqual([row["batch_id"] for row in page["items"]], [private.pk])
        private.user_created = self.actor
        private.save(user=self.actor)
        with patch.object(User, "is_imis_admin", new_callable=PropertyMock, return_value=False), patch.object(
            Individual, "get_queryset", side_effect=lambda qs, user: qs.filter(pk=visible_id),
        ):
            page = IndividualValidationUploadService(reader).batches()
        # Both lists contain the same first selected identity, so both are accessible.
        self.assertEqual(page["count"], 2)
        Individual.objects.filter(pk=visible_id).update(is_deleted=True)
        with patch.object(User, "is_imis_admin", new_callable=PropertyMock, return_value=False), patch.object(
            Individual, "get_queryset", side_effect=lambda qs, user: qs.filter(pk=visible_id),
        ):
            self.assertEqual(IndividualValidationUploadService(reader).batches()["count"], 0)

    def test_history_right_is_required_without_an_additional_query_right(self):
        self.generate(target_count=0)
        self.actor.has_perms = lambda perms: list(perms) == ["958001"]
        with self.assertRaises(PermissionDenied):
            self.history.batches()
        self.actor.has_perms = lambda perms: list(perms) == ["958003"]
        self.assertEqual(self.history.batches()["count"], 1)
        with self.assertRaises(PermissionDenied):
            IndividualValidationUploadService(None).batches()

    def test_invalid_pagination_is_rejected(self):
        for kwargs in ({"first": 101}, {"first": True}, {"first": None}, {"offset": -1}, {"offset": False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                self.history.batches(**kwargs)

    def test_graphql_summaries_are_serializable_and_exclude_personal_snapshots(self):
        batch, _ = self.generate(target_count=1)
        result = graphene.Schema(query=Query).execute('''query {
          individualValidationBatches(first: 20, offset: 0) {
            count hasNextPage items {
              batchId benefitPlanCode generatedAt uploadedAt sourceFileName status selectedIndividuals
            }
          }
        }''', context_value=SimpleNamespace(user=self.actor))
        self.assertIsNone(result.errors)
        page = result.data["individualValidationBatches"]
        self.assertEqual(page["count"], 1)
        self.assertEqual(page["items"][0]["batchId"], str(batch.pk))
        self.assertNotIn("individual_uuid", json.dumps(result.data))
